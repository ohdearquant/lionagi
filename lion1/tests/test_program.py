"""The program form (ADR-0008): read whole, checked before anything runs, evaluated over data."""

import ast
import asyncio
import gc
import json
import re
import time
from typing import Any

import pytest
from pydantic import BaseModel

import lion1.actor as actor_module
from hub.chat import entry_row
from lion1 import (
    Actor,
    Entry,
    Exhausted,
    KhiveNoteStore,
    Kind,
    Message,
    Profile,
    RunRequest,
    Stopped,
    Success,
    lndl,
)
from lion1.actor import ProfileError, Result, _Reader
from lion1.checkpoint import entry_of
from lion1.lndl import (
    LIMIT,
    Command,
    EvalError,
    LNDLError,
    Script,
    Value,
    Write,
    evaluate,
    program_guidance,
    read,
)
from tests.lionfake import LionKhive
from tests.lionfake import store as fresh_store

PROGRAM = Profile("p", language="program")


def fenced(*lines: str) -> str:
    return "Working on it.\n```\n" + "\n".join(lines) + "\n```\n"


def scripted(*turns: str, seen: list | None = None):
    it = iter(turns)

    async def backend(messages: list[Message]) -> str:
        if seen is not None:
            seen.append(messages)
        return next(it)

    return backend


class SearchSimilar(BaseModel):
    input: str
    candidates: list[dict]


class Echo(BaseModel):
    text: str


class Book(BaseModel):
    title: str
    year: int


class Fetch(BaseModel):
    title: str


def _actor(name: str, calls: list) -> Actor:
    actor = Actor(name, store=fresh_store())

    @actor.handler(SearchSimilar)
    async def search_similar(req: SearchSimilar, ctx) -> list[dict]:
        calls.append(("search_similar", req.input))
        words = set(req.input.lower().split())
        return [
            {"title": c["title"], "similarity": len(words & set(c["title"].lower().split())) / 2}
            for c in req.candidates
        ]

    @actor.handler(Echo)
    async def echo(req: Echo, ctx) -> str:
        calls.append(("echo", req.text))
        return req.text

    @actor.handler(Fetch)
    async def fetch(req: Fetch, ctx) -> Book:
        calls.append(("fetch", req.title))
        return Book(title=req.title, year=1965)

    return actor


CATALOG = [{"title": "Dune Messiah"}, {"title": "Children of Dune"}, {"title": "Emma"}]
HISTORY = (Entry(seq=0, kind=Kind.RESULT, content=Result("list_books", True, CATALOG), alias="catalog"),)

# the record's example (ADR-0008/C6), its text filled in
EXAMPLE = (
    'note["book1"] = {title: "Dune", description: "Messiah of the desert", author: "Herbert"}',
    'similar = search_similar(input=*note["book1"].title + " " + *note["book1"].description, '
    "candidates=*catalog)",
    'chosen = *similar if *similar != [] else DO search_similar(input=*note["book1"].title, '
    "candidates=*catalog)",
    'note["book1"]["similar"] = *chosen',
    "OUT{similar_books: (b for b in *chosen if b.similarity > 0.8)}",
)


def _run(actor: Actor, *turns: str, profile: Profile = PROGRAM, seen: list | None = None, **request):
    request.setdefault("inputs", ("go",))
    request.setdefault("max_turns", len(turns))
    return asyncio.run(actor.run(profile, RunRequest(**request), scripted(*turns, seen=seen)))


def _notices(run) -> str:
    return "\n".join(e.content for e in run.record if e.kind is Kind.SYSTEM)


class Cut(BaseModel):
    target: str


class Hold(BaseModel):
    pass


def test_a_command_cancelled_before_it_started_leaves_no_arguments_held_for_the_run(store):
    # its arguments are computed when its handler is about to run; one that never got there kept
    # them, and the walk they close over, until the run ended
    actor = Actor("never-started", store=store)
    seen: list[dict] = []

    @actor.handler(Cut)
    async def cut(req: Cut, ctx) -> str:
        ctx.run.pending[req.target].cancel()  # launched after this one: its coroutine has not run
        seen.append(dict(ctx.run.thunks))
        return "cut"

    @actor.handler(Hold)
    async def hold(req: Hold, ctx) -> str:
        return "held"

    @actor.handler(Echo)
    async def echo(req: Echo, ctx) -> str:
        seen.append(dict(ctx.run.thunks))
        return req.text

    run = _run(actor, fenced('k = cut(target="h")', "h = hold()"), fenced('e = echo(text="after")'))
    assert run.record.get("h").content.error == "cancelled before it started"
    assert list(seen[0]) == ["h"] and seen[1] == {}
    assert run.thunks == {} and run.pointing == {}


def test_the_records_example_resolves_in_one_turn():
    calls: list = []
    actor = _actor("example", calls)
    seen: list = []
    run = _run(actor, fenced(*EXAMPLE), history=HISTORY, seen=seen)
    assert isinstance(run.outcome, Success) and run.turn == 1 and len(seen) == 1
    assert run.output == {
        "similar_books": [
            {"title": "Dune Messiah", "similarity": 1.0},
            {"title": "Children of Dune", "similarity": 1.0},
        ]
    }
    # the non-empty search made the DO in the else branch dead: one command ran
    assert calls == [("search_similar", "Dune Messiah of the desert")]
    note = actor.notes_for(PROGRAM).get("book1")
    assert note.version == 2 and note.value["author"] == "Herbert" and len(note.value["similar"]) == 3


def test_a_do_runs_only_when_its_part_of_the_expression_is_reached():
    calls: list = []
    actor = _actor("do", calls)
    empty = [{"title": "Emma"}]
    history = (Entry(seq=0, kind=Kind.RESULT, content=Result("list_books", True, empty), alias="catalog"),)
    lines = list(EXAMPLE)
    lines[1] = 'similar = search_similar(input="nothing matches", candidates=[])'
    run = _run(actor, fenced(*lines), history=history)
    assert calls == [("search_similar", "nothing matches"), ("search_similar", "Dune")]
    assert isinstance(run.outcome, Success) and run.output == {"similar_books": []}
    do = [e for e in run.record if e.kind is Kind.RESULT and e.alias.startswith("r")]
    assert len(do) == 1 and do[0].content.ok  # the DO landed as a command of its own


def test_a_syntax_error_refuses_the_whole_message_and_nothing_runs():
    calls: list = []
    actor = _actor("syntax", calls)
    turn = fenced('a = echo(text="first")', 'note["k"] = "v"', "b = echo(text=)", 'OUT{x: "done"}')
    run = _run(actor, turn, "no program this time")
    assert calls == [] and actor.notes_for(PROGRAM).get("k") is None
    assert not [e for e in run.record if e.kind is Kind.RESULT]
    # the parser's own words differ by interpreter version; the line and the refusal do not
    told = re.search(
        r"your last turn could not be read: line 3: [^;\n]+; the program was not run", _notices(run)
    )
    assert told is not None


@pytest.mark.parametrize(
    ("lines", "said"),
    [
        (('a = echo(text="x")', 'b = serch(text="y")'), "line 2: unknown command 'serch'"),
        (("a = echo(text=*b)", 'b = echo(text="x")'), "line 1: *b names nothing bound on an earlier line"),
        (('a = echo(text="x")', 'a = echo(text="y")'), "line 2: a is bound already"),
        (("if true:", '    a = echo(text="x")', "b = echo(text=*a)"), "line 3: *a names nothing bound"),
        (('a = [DO serch(text="x")]',), "line 1: unknown command 'serch'"),
        (('note.k = serch(text="x")',), "line 1: unknown command 'serch'"),
        (("OUT{x: *nobody}",), "line 1: *nobody names nothing"),
        (('note["k"] = *nobody',), "line 1: *nobody names nothing"),
    ],
)
def test_a_check_that_fails_refuses_the_whole_message_naming_the_line(lines, said):
    calls: list = []
    actor = _actor("check", calls)
    run = _run(actor, fenced(*lines), "idle")
    assert calls == [] and not [e for e in run.record if e.kind is Kind.RESULT]
    assert said in _notices(run)


def test_an_unknown_name_is_measured_against_the_nearest_known_name_in_both_forms():
    calls: list = []
    actor = _actor("misnamed", calls)
    run = _run(actor, fenced('a = serch_similar(input="x", candidates=[])'), "idle")
    tag = _run(_actor("misnamed-tag", calls), '<lact a>ecko(text="x")</lact>', "idle", profile=Profile("p"))
    assert run.misnamed == [("serch_similar", "search_similar", 1)]
    assert tag.misnamed == [("ecko", "echo", 1)]
    assert calls == []


def test_every_unknown_command_in_a_message_is_named_and_measured_not_only_the_first():
    calls: list = []
    run = _run(_actor("misnamed-two", calls), fenced('a = serch(text="x")', 'b = ecko(text="y")'), "idle")
    assert run.misnamed == [("serch", "fetch", 2), ("ecko", "echo", 1)] and calls == []
    assert (
        "line 1: unknown command 'serch'; line 2: unknown command 'ecko'; the program was not run"
        in _notices(run)
    )
    # an unknown name earlier in the message is what the model is told, whatever the check finds next,
    # and an unknown command after that is still measured and named
    for later in ("b = *zz", 'a = "again"'):
        run = _run(_actor("misnamed-first", calls), fenced('a = serch(text="x")', later), "idle")
        assert run.misnamed == [("serch", "fetch", 2)] and calls == []
        assert "line 1: unknown command 'serch'; the program was not run" in _notices(run)
        lines = ('a = serch(text="x")', later, 'c = ecko(text="y")')
        run = _run(_actor("misnamed-between", calls), fenced(*lines), "idle")
        assert run.misnamed == [("serch", "fetch", 2), ("ecko", "echo", 1)] and calls == []
        assert "line 1: unknown command 'serch'; line 3: unknown command 'ecko'" in _notices(run)
    # with every command known, the first pointer or binding refusal is the one said
    run = _run(_actor("first-refusal", calls), fenced("b = *zz", 'a = "again"', 'a = "twice"'), "idle")
    assert "*zz names nothing bound" in _notices(run) and "bound already" not in _notices(run)


def test_a_name_already_on_the_record_cannot_be_bound_again():
    actor = _actor("taken", [])
    run = _run(actor, fenced('catalog = echo(text="x")'), history=HISTORY)
    assert run.retry_error == (
        "line 1: catalog is bound already: a name is bound once for the whole run; the program was not run"
    )


def test_a_build_past_the_size_limit_settles_the_node_invalid():
    calls: list = []
    actor = _actor("limit", calls)
    half = LIMIT // 2 + 1
    run = _run(actor, fenced(f'big = echo(text="ab" * {half})', 'small = echo(text="ab" * 3)'), "idle")
    big, small = run.record.get("big").content, run.record.get("small").content
    assert not big.ok and big.reason == "invalid" and "past the size limit" in big.error
    assert small.ok and calls == [("echo", "ababab")]


def test_a_type_error_settles_the_node_invalid_naming_both_types():
    calls: list = []
    actor = _actor("types", calls)
    run = _run(actor, fenced('a = echo(text="n: " + 3)'), "idle")
    res = run.record.get("a").content
    assert res.reason == "invalid" and "cannot apply + to str and int" in res.error and calls == []


def test_if_elif_else_runs_only_the_taken_branch_and_a_return_ends_the_program():
    calls: list = []
    actor = _actor("branches", calls)
    turn = fenced(
        'a = echo(text="two")',
        'if *a == "one":',
        '    b = echo(text="branch one")',
        'elif *a == "two":',
        '    c = echo(text="branch two")',
        "    return {picked: *c}",
        "else:",
        '    d = echo(text="branch else")',
        'e = echo(text="after")',
        'OUT{picked: "never"}',
    )
    run = _run(actor, turn)
    assert isinstance(run.outcome, Success) and run.output == {"picked": "branch two"}
    assert calls == [("echo", "two"), ("echo", "branch two")]


def test_an_else_branch_runs_and_the_suffix_after_a_branch_that_continues_runs():
    calls: list = []
    actor = _actor("else", calls)
    turn = fenced(
        "if 1 > 2:", '    a = echo(text="no")', "else:", '    b = echo(text="yes")', 'c = echo(text="after")'
    )
    run = _run(actor, turn, "idle")
    assert calls == [("echo", "yes"), ("echo", "after")] and run.record.get("a") is None


def test_a_condition_that_cannot_be_computed_stops_the_program_there():
    calls: list = []
    actor = _actor("failed", calls)
    turn = fenced('a = echo(text="x")', "if *a > 3:", '    b = echo(text="no")', 'c = echo(text="no")')
    run = _run(actor, turn, "idle")
    assert calls == [("echo", "x")]
    assert "line 2: if: line 2: cannot compare str > int; nothing after it ran" in _notices(run)


def test_an_output_that_cannot_be_computed_is_a_retry_after_its_commands_settle():
    calls: list = []
    actor = _actor("out-fails", calls)
    run = _run(actor, fenced('a = echo(text="x")', "OUT{x: *a.title}"), fenced('OUT{x: "fine"}'))
    assert isinstance(run.outcome, Success) and run.output == {"x": "fine"}
    assert "your last turn could not be read: line 2: str cannot be read at 'title'" in _notices(run)
    assert run.record.get("a").content.ok


def test_a_turn_whose_output_fails_to_compute_is_no_idle_turn():
    # the retry is classed like any turn its walk acted in: it breaks a row of idle turns, so the third
    # turn here is the first of a new row, not the second, and max_idle=2 is not spent
    turns = ("thinking", fenced("OUT{x: 1 / 0}"), "still thinking", fenced("OUT{x: 1}"))
    run = _run(_actor("idle-row", []), *turns, max_idle=2)
    assert isinstance(run.outcome, Success) and run.output == {"x": 1}
    assert "line 1: division by zero" in _notices(run)


def test_a_value_is_computed_once_when_first_read_and_bound_for_later_turns():
    calls: list = []
    actor = _actor("values", calls)
    turn = fenced(
        'a = echo(text="x")',
        "v = *a + *a",
        'unread = [DO echo(text="never")]',
        "b = echo(text=*v)",
        "c = echo(text=*v + *v)",
    )
    run = _run(actor, turn, fenced("d = echo(text=*v)"))
    assert calls == [("echo", "x"), ("echo", "xx"), ("echo", "xxxx"), ("echo", "xx")]
    assert run.values["v"][1] == "xx" and "unread" not in run.values


def test_a_value_nothing_read_stays_bound_into_later_turns_and_its_name_stays_taken():
    calls: list = []
    actor = _actor("unread", calls)
    turns = (
        fenced('later = "kept"', 'lazy = [DO echo(text="deferred")]'),
        fenced("consumer = echo(text=*later)", "first = echo(text=*lazy[0])"),
        fenced('later = echo(text="replacement")'),
        "idle",
    )
    run = _run(actor, *turns)
    assert run.record.get("consumer").content.value == "kept" and run.values["later"] == (2, "kept")
    assert run.record.get("first").content.value == "deferred"
    # the DO in the value ran once, when the second turn first read it
    assert [e.turn for e in run.record if e.kind is Kind.RESULT and e.content.value == "deferred"] == [2, 2]
    assert sorted(calls) == [("echo", "deferred"), ("echo", "deferred"), ("echo", "kept")]
    assert "later is bound already: a name is bound once for the whole run" in _notices(run)


class Wait(BaseModel):
    seconds: float


class Stubborn(BaseModel):
    seconds: float


class Drop(BaseModel):
    target: str
    after: float


def _waiting_actor(name: str, ends: list) -> Actor:
    actor = _actor(name, ends)

    @actor.handler(Wait)
    async def wait(req: Wait, ctx) -> str:
        try:
            await asyncio.sleep(req.seconds)
        except asyncio.CancelledError:
            ends.append(("wait", req.seconds, "cut"))
            raise
        ends.append(("wait", req.seconds, "ran"))
        return "rested"

    @actor.handler(Drop)
    async def drop(req: Drop, ctx) -> str:
        await asyncio.sleep(req.after)
        ctx.run.pending[req.target].cancel()
        return "dropped"

    @actor.handler(Stubborn)
    async def stubborn(req: Stubborn, ctx) -> str:
        try:
            await asyncio.sleep(req.seconds)
        except asyncio.CancelledError:
            ends.append(("stubborn", "cut and returned"))
        return "kept"

    return actor


def _results(run) -> dict:
    return {
        e.alias: (e.content.ok, e.content.error, e.meta["execution"])
        for e in run.record
        if e.kind is Kind.RESULT
    }


def test_a_command_a_value_launched_while_the_turn_settled_is_cut_at_the_deadline_with_the_rest():
    ends: list = []
    actor = _waiting_actor("late-launch", ends)
    # the walk is past its last line and settling when gate lands; only then does the last command's
    # argument read v, whose DO launches the long wait: a command the settle had not seen when it began
    # the budget is fifteen times the gate's wait, so a loop held up for a second still starts it in time
    turn = fenced("gate = wait(seconds=0.1)", "v = [DO wait(seconds=8)][0]", "out = echo(text=*gate + *v)")
    run = _run(actor, turn, time_budget=1.5)
    assert ends == [("wait", 0.1, "ran"), ("wait", 8.0, "cut")] and run.elapsed < 5
    assert _results(run) == {
        "gate": (True, None, "started"),
        "out": (False, "cancelled before it finished", "not executed"),
        "r2.2": (False, "cancelled before it finished", "completion unknown"),
    }
    assert run.pending == {} and run.tasks == set()


def test_a_late_launch_outliving_every_command_the_settle_began_with_is_cut_at_the_deadline():
    ends: list = []
    actor = _waiting_actor("late-launch-orphaned", ends)
    # out launches the long wait through v and is then cancelled by drop: every command the walk itself
    # launched is done at 0.2 s, and the wait v launched is the only one still running at the deadline
    turn = fenced(
        "gate = wait(seconds=0.1)",
        "v = [DO wait(seconds=8)][0]",
        "out = echo(text=*gate + *v)",
        'drop(target="out", after=0.2)',
    )
    run = _run(actor, turn, time_budget=1.5)
    assert ends == [("wait", 0.1, "ran"), ("wait", 8.0, "cut")] and 1.5 <= run.elapsed < 5
    assert _results(run) == {
        "gate": (True, None, "started"),
        "out": (False, "cancelled before it finished", "not executed"),
        "r2.2": (True, None, "started"),
        "r2.3": (False, "cancelled before it finished", "completion unknown"),
    }
    assert run.pending == {} and run.tasks == set()


def test_a_command_launched_after_the_deadline_cut_the_turn_never_runs_and_is_seen_through(caplog):
    ends: list = []
    actor = _waiting_actor("launch-after-cut", ends)
    # the first DO outlives its cancel and returns, so the value goes on to its second DO after the cut
    turn = fenced(
        "gate = wait(seconds=0.1)",
        'v = [DO stubborn(seconds=8), DO echo(text="late")][1]',
        "out = echo(text=*gate + *v)",
    )
    run = _run(actor, turn, time_budget=1.5)
    assert ends == [("wait", 0.1, "ran"), ("stubborn", "cut and returned")] and run.elapsed < 5
    # the late echo is refused where it starts and never reaches its handler; its refusal is on the record
    # when the run returns, and the value it failed, with no reader left to tell, is seen through
    assert _results(run) == {
        "gate": (True, None, "started"),
        "out": (False, "cancelled before it finished", "not executed"),
        "r2.2": (True, None, "started"),
        "r2.3": (False, "refused: the time_budget of 1.5 is spent", "not executed"),
    }
    assert run.pending == {} and run.tasks == set()
    del run, actor
    gc.collect()
    assert [r.getMessage() for r in caplog.records if r.name == "asyncio"] == []


def test_a_settle_sees_through_a_task_that_joined_its_list_after_the_cut():
    async def main() -> None:
        run = actor_module.Run(_actor("settle-grows", []), PROGRAM, RunRequest(time_budget=0.2))
        tasks: list[asyncio.Task] = []

        async def late() -> None:
            await asyncio.sleep(0)
            raise RuntimeError("a defect in a task the cut let start")

        async def first() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                tasks.append(asyncio.ensure_future(late()))  # as a walk's list grows while it settles
                raise

        tasks.append(asyncio.ensure_future(first()))
        # the list grew while the settle waited for what it cut: the task that joined is waited for as
        # the rest are, and its defect is raised here, not dropped
        with pytest.raises(RuntimeError, match="a defect in a task the cut let start"):
            await actor_module._settle(run, tasks)
        assert all(task.done() for task in tasks) and len(tasks) == 2

    asyncio.run(main())


def test_a_cancelled_run_cancels_the_commands_its_program_launched_and_returns():
    actor = _actor("cancelled", [])

    class Hang(BaseModel):
        pass

    @actor.handler(Hang)
    async def hang(req: Hang, ctx) -> str:
        await asyncio.Event().wait()
        return "never"

    async def main():
        before = asyncio.all_tasks()
        # the walk itself waits on the command (an output reads it): the cancel lands there
        turn = fenced("a = hang()", "OUT{x: *a}")
        task = asyncio.create_task(actor.run(PROGRAM, RunRequest(inputs=("go",)), scripted(turn)))
        await asyncio.sleep(0.1)
        (run,) = actor.runs.values()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        # the run leaves, and the command with it (before, the run waited for the command forever)
        assert task in done and task.cancelled() and asyncio.all_tasks() - before == set()
        return run

    run = asyncio.run(main())
    a = run.record.get("a").content
    assert not a.ok and a.reason == "cancelled" and run.outcome is None


@pytest.mark.parametrize(
    "lines, cancels",
    [
        # the walk is past its block and settling its command when the cancel lands
        (("a = hang()",), 1),
        # the walk cancels the command it waits on; a second cancel lands while the walk settles it
        (("a = hang()", "OUT{x: *a}"), 2),
        # each write is a task of its own, waiting on the one before: the last outlives the command
        (("note.k = hang()", *(f'note.k = "w{i}"' for i in range(8))), 1),
    ],
)
def test_a_cancelled_program_returns_only_once_every_task_it_launched_is_through(lines, cancels):
    actor = _actor("cancel-settles", [])
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    class Hang(BaseModel):
        pass

    @actor.handler(Hang)
    async def hang(req: Hang, ctx) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:  # a cleanup that sees itself through, whatever cancels it meanwhile
            cleaning.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            cleaned.set()
        return "never"

    async def main():
        before = asyncio.all_tasks()
        task = asyncio.create_task(actor.run(PROGRAM, RunRequest(inputs=("go",)), scripted(fenced(*lines))))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            (run,) = actor.runs.values()
            task.cancel()
            if cancels == 2:
                await asyncio.wait_for(cleaning.wait(), 2)
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=0.05)
            assert not done  # the run waits for the command still cleaning up
        finally:
            release.set()  # a failed assert must not leave the cleanup waiting out the loop's close
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert cleaned.is_set() and asyncio.all_tasks() - before == set()
        return run

    run = asyncio.run(main())
    (result,) = run.record.of_kind(Kind.RESULT)
    assert result.content.reason == "cancelled" and run.pending == {} and run.outcome is None
    assert run.notes.get("k") is None


def test_a_value_whose_input_failed_fails_every_reader():
    calls: list = []
    actor = _actor("value-fails", calls)
    run = _run(actor, fenced("a = echo(text=3)", "v = *a", "b = echo(text=*v)"), "idle")
    assert run.record.get("a").content.reason == "invalid"
    b = run.record.get("b").content
    assert b.reason == "invalid" and b.error.startswith("*v: *a did not settle ok") and calls == []


class CountingStore(KhiveNoteStore):
    def __init__(self) -> None:
        super().__init__(LionKhive(), "p")
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return super().get(key)


def test_a_note_is_read_once_per_node_whatever_the_expression_repeats():
    store = CountingStore()
    store.put("k", {"a": "1", "b": "2"}, run="earlier", seq=0)
    actor = _actor("note-once", [])
    profile = Profile("p", language="program", notes=store)
    run = _run(actor, fenced('x = echo(text=note["k"].a + note["k"].b + *note.k.a)'), "idle", profile=profile)
    assert run.record.get("x").content.value == "121" and store.gets == 1


def test_a_note_read_waits_for_the_assignment_before_it_and_a_missing_note_is_invalid():
    calls: list = []
    actor = _actor("note-order", calls)
    turn = fenced(
        'note["k"] = echo(text="first")',
        'note["k"] = note["k"] + " then second"',
        'a = echo(text=note["k"])',
        'b = echo(text=note["missing"])',
    )
    run = _run(actor, turn, "idle")
    assert run.record.get("a").content.value == "first then second"
    assert actor.notes_for(PROGRAM).get("k").version == 2
    assert "note['missing'] is not in the store" in run.record.get("b").content.error
    assert "notes written: note.k, note.k" in _notices(run)


def test_a_failed_producer_writes_nothing_and_the_assignment_is_told_not_written():
    calls: list = []
    actor = _actor("not-written", calls)
    store = actor.notes_for(PROGRAM)
    store.put("k", "kept", run="earlier", seq=0)
    run = _run(actor, fenced("note.k = echo(text=3)", 'note.j = note["absent"]'), "idle")
    assert store.get("k").value == "kept" and store.get("j") is None
    said = _notices(run)
    assert "note.k: not written: *r2.0 did not settle ok: invalid arguments" in said
    assert "note.j: not written: note['absent'] is not in the store" in said


def test_a_store_that_refuses_keeps_the_result_and_claims_no_write(tmp_path):
    actor = _actor("store-refuses", [])
    profile = Profile("p", language="program", notes=fresh_store().notes("p"))
    profile.notes.put("k", "a string", run="earlier", seq=0)
    run = _run(actor, fenced('note.k.part = echo(text="x")'), "idle", profile=profile)
    assert profile.notes.get("k").value == "a string"
    assert "note.k.part: not written: EvalError: the note holds str where 'part' is written" in _notices(run)
    assert [e.content.ok for e in run.record if e.kind is Kind.RESULT] == [True]


class StoreDefect(BaseException):
    """A failure of the store no write turns into a notice."""


class DefectStore(KhiveNoteStore):
    def put(self, key, value, **meta):
        raise StoreDefect(key)


@pytest.mark.parametrize(
    "lines",
    [
        ('note.k = "first"', 'OUT{answer: "done"}'),
        ('note.k = "first"', 'note.k = "second"', "x = echo(text=note.k)", "OUT{answer: *x}"),
    ],
    ids=["a lone write", "a write with a write and a reader waiting on it"],
)
def test_a_write_that_fails_on_a_defect_is_raised_by_the_settle_and_no_one_else_is_told(lines, caplog):
    """A note write's task is the run's own: the settle sees it through, so a defect in one reaches the
    caller. The write queued behind it and the command that reads the note wait on that task; the loop
    hears of the failure from neither."""
    actor = _actor("write-defect", [])
    profile = Profile("p", language="program", notes=DefectStore(LionKhive(), "p"))
    with pytest.raises(StoreDefect):
        _run(actor, fenced(*lines), profile=profile)
    gc.collect()
    assert [r.getMessage() for r in caplog.records if r.name == "asyncio"] == []


def test_a_dotted_path_writes_into_a_json_note_and_creates_what_is_missing():
    actor = _actor("path", [])
    store = actor.notes_for(PROGRAM)
    store.put("book", {"title": "Dune"}, run="earlier", seq=0)
    _run(actor, fenced('note.book.found.first = echo(text="x")', 'note.fresh.a = "y"'), "idle")
    assert store.get("book").value == {"title": "Dune", "found": {"first": "x"}}
    assert store.get("fresh").value == {"a": "y"}


def test_a_consumed_result_never_reaches_the_backend_yet_its_pointer_and_context_show_do():
    calls: list = []
    actor = _actor("consumed", calls)
    seen: list = []
    turns = (
        fenced(
            'similar = search_similar(input="Dune", candidates=*catalog)',
            "first = echo(text=*similar[1].title)",
            'shown = echo(text="whole")',
        ),
        fenced("again = echo(text=*similar[1].title)"),
        fenced('back = context.show(refs=["similar"])'),
        "done",
    )
    run = _run(actor, *turns, history=HISTORY, seen=seen)
    body = json.dumps(run.record.get("similar").content.value, ensure_ascii=False)
    sent = [[m.content for m in messages] for messages in seen]
    # the value, whole or previewed, is in none of the three calls before the show; its placeholder is
    assert [any("similarity" in c for c in call) for call in sent] == [False, False, False, True]
    assert all(
        any(c.startswith("[hidden; *name or context.show reopens: _r4 similar") for c in call)
        for call in sent[1:3]
    )
    assert "similar (_r4, search_similar): list[3], consumed" in _notices(run)
    assert run.record.get("again").content.value == "Children of Dune"  # the pointer reaches it
    assert f"[_r4 similar search_similar] {body}" in sent[3]  # and so does context.show
    # what nothing read stays whole
    assert "[_r6 shown echo] whole" in sent[1] and "[_r5 first echo] Children of Dune" in sent[1]
    # one hide, on the notification after the program, dated when `first` settled having read it;
    # `first` was read by nothing
    hides = [(e.name, d) for e in run.record if e.kind is Kind.SYSTEM for d in e.meta.get("consumed", ())]
    assert [(name, d.refs, d.at, d.seqs) for name, d in hides] == [("_sys7", ("similar",), 5, (4,))]


def test_the_context_figure_counts_a_consumed_result_as_its_placeholder():
    actor = _actor("figure", [])
    run = _run(actor, fenced('a = echo(text="x" * 8000)', "b = echo(text=*a[0])"), "idle")
    _, figure = map(int, re.findall(r"context: ~(\d+) /", _notices(run)))  # after the program
    assert run.record.get("b").content.value == "x" and figure < 500
    # shown again by the same program: the show stands over the hide (D3) and the figure counts the
    # result whole, as the backend receives it
    seen: list = []
    lines = ('a = echo(text="x" * 8000)', "b = echo(text=*a[0])", 'shown = context.show(refs=["a"])')
    run = _run(_actor("figure-shown", []), fenced(*lines), "idle", seen=seen)
    _, figure = map(int, re.findall(r"context: ~(\d+) /", _notices(run)))
    assert figure > 1500 and any("x" * 8000 in str(m.content) for m in seen[-1])


def test_a_result_the_output_names_is_never_consumed():
    actor = _actor("named", [])
    turn = fenced('a = echo(text="kept")', "b = echo(text=*a)", "OUT{x: *a}")
    run = _run(actor, turn, fenced('OUT{x: "again"}'), accept=lambda r, o: "no" if o["x"] == "kept" else None)
    # `b` read `a` and settled ok, but OUT{} points at `a`: the refused candidate's next turn sees it whole
    notices = [e for e in run.record if e.kind is Kind.SYSTEM and not e.content.startswith("<system closure")]
    assert [e.meta.get("consumed") for e in notices] == [None, None]
    assert run.turn == 2 and run.output == {"x": "again"}


def test_the_consumed_hide_survives_the_log_as_a_directive():
    actor = _actor("logged", [])
    run = _run(actor, fenced('a = echo(text="x")', "b = echo(text=*a)"), "idle")
    notice = next(e for e in run.record if e.kind is Kind.SYSTEM and e.meta.get("consumed"))
    back = entry_of(json.loads(json.dumps(entry_row(notice), default=str)))
    assert back.meta["consumed"] == notice.meta["consumed"]
    assert [d.refs for d in back.meta["consumed"]] == [("a",)]


def test_a_show_later_in_the_same_program_stands_over_the_hide_of_what_it_consumed():
    seen: list = []
    actor = _actor("same-program-show", [])
    turn = fenced(
        'a = echo(text="kept")', "b = echo(text=*a)", 'shown = context.show(refs=["a"] if *b else ["a"])'
    )
    run = _run(actor, turn, "idle", seen=seen)
    # `b` consumed `a` when it settled, before the show; `shown` consumed `b`
    hides = [(d.refs, d.at) for e in run.record if e.kind is Kind.SYSTEM for d in e.meta.get("consumed", ())]
    assert hides == [(("a",), 4), (("b",), 5)]
    assert "[_r3 a echo] kept" in [m.content for m in seen[1]]
    assert any(m.content.startswith("[hidden; *name or context.show reopens: _r4 b") for m in seen[1])


def test_a_show_dated_with_the_branch_that_consumed_its_target_stands_over_the_hide():
    # the branch reads `a` before the show lands, so the two are dated at the same seq: the show is
    # the later of the two and stands
    seen: list = []
    actor = _actor("branch-then-show", [])
    run = _run(
        actor,
        fenced('a = echo(text="kept")', "if *a:", '    shown = context.show(refs=["a"])'),
        "idle",
        seen=seen,
    )
    hides = [(d.refs, d.at) for e in run.record if e.kind is Kind.SYSTEM for d in e.meta.get("consumed", ())]
    assert hides == [(("a",), 4)]
    assert [(e.alias, e.seq) for e in run.record if e.kind is Kind.RESULT] == [("a", 3), ("shown", 4)]
    assert "[_r3 a echo] kept" in [m.content for m in seen[1]]
    assert not any(m.content.startswith("[hidden") for m in seen[1])


def test_a_value_computed_on_the_record_resolves_after_the_log_replays_it_and_nothing_runs_again():
    calls: list = []
    turn = fenced('a = echo(text="x")', "v = *a + *a", "b = echo(text=*v)")
    first = _run(_actor("first", calls), turn, "idle")
    history = tuple(entry_of(json.loads(json.dumps(entry_row(e), default=str))) for e in first.record)
    calls.clear()
    run = _run(_actor("resumed", calls), fenced("d = echo(text=*v)"), "idle", history=history)
    assert run.record.get("d").content.value == "xx" and calls == [("echo", "xx")]
    assert run.values["v"] == first.values["v"] == (2, "xx")


def test_a_value_nothing_read_is_bound_again_after_the_log_replays_it_and_reads_as_it_would_have():
    calls: list = []
    first = _run(
        _actor("first", calls),
        fenced('a = echo(text="a")', 'v = "kept"', "", "n = *a + *v", "bad = *v / 0"),
        "idle",
    )
    assert calls == [("echo", "a")] and list(first.unread) == ["v", "n", "bad"]
    # the bindings ride the notification as their expressions, each with the line it was written on
    notice = next(e for e in first.record if e.kind is Kind.SYSTEM and e.meta.get("bound"))
    assert notice.meta["bound"] == (
        ("v", 2, 2, "'kept'"),
        ("n", 2, 4, "__ptr_a + __ptr_v"),
        ("bad", 2, 5, "__ptr_v / 0"),
    )
    history = tuple(entry_of(json.loads(json.dumps(entry_row(e), default=str))) for e in first.record)
    calls.clear()
    run = _run(_actor("resumed", calls), fenced("OUT{x: *n}"), history=history)
    assert run.output == {"x": "akept"} and calls == [] and run.values["n"] == (2, "akept")
    run = _run(_actor("resumed-bad", calls), fenced("OUT{y: *bad}"), "idle", history=history)
    assert run.output is None and "*bad: line 5: cannot apply / to str and int" in _notices(run)


@pytest.mark.parametrize(
    ("expr", "sign"),
    [(f"{'9' * 401} / 1", "/"), ("1e308 * 10", "*"), ("1e308 + 1e308", "+"), ("-1e308 - 1e308", "-")],
)
def test_an_arithmetic_result_too_large_for_a_number_is_a_language_error_not_a_crash_or_infinity(expr, sign):
    run = _run(_actor("overflow", []), fenced(f"OUT{{x: {expr}}}"), "idle")
    assert run.outcome == Exhausted(2, "turns")
    assert f"line 1: the result of {sign} is too large for a number" in _notices(run)


def test_a_number_too_large_to_write_is_refused_with_the_message():
    run = _run(_actor("inf-literal", []), fenced("OUT{x: 1e999}"), "idle")
    assert run.outcome == Exhausted(2, "turns")
    assert "line 1: inf is not a value: a number is finite" in _notices(run)


def test_a_value_bound_by_an_earlier_turn_reads_after_the_reading_programs_own_note_writes():
    actor = _actor("lazy-note", [])

    @actor.handler(Take)
    async def take(req: Take, ctx) -> str:
        await asyncio.sleep(0.2)
        return "new"

    actor.notes_for(PROGRAM).put("k", "old", run="r0", seq=0)
    turns = (
        fenced('v = note["k"]'),
        fenced("note.k = take(rows=[])", "b = echo(text=*v)", 'c = echo(text=note["k"])'),
        "idle",
    )
    run = _run(actor, *turns)
    assert run.record.get("b").content.value == run.record.get("c").content.value == "new"
    # and not after a write made later than its reader: the reader's line decides
    actor.notes_for(PROGRAM).put("k", "old", run="r0", seq=0)
    later = (
        fenced('v = note["k"]'),
        fenced("b = echo(text=*v)", "note.k = take(rows=[])", 'c = echo(text=note["k"])'),
        "idle",
    )
    run = _run(actor, *later)
    assert (run.record.get("b").content.value, run.record.get("c").content.value) == ("old", "new")


def test_a_value_bound_this_turn_reads_after_the_note_writes_before_its_first_reader_too():
    actor = _actor("lazy-note-same-turn", [])

    @actor.handler(Take)
    async def take(req: Take, ctx) -> str:
        await asyncio.sleep(0.2)
        return "new"

    actor.notes_for(PROGRAM).put("k", "old", run="r0", seq=0)
    # the binding line reads nothing: the value reads when first read, after the slow write before that
    run = _run(actor, fenced('v = note["k"]', "note.k = take(rows=[])", "OUT{x: *v}"))
    assert run.output == {"x": "new"}
    # the first reader in time decides: a branch reads as the walk reaches it, before the write after it
    actor.notes_for(PROGRAM).put("k", "old", run="r0", seq=0)
    lines = ('v = note["k"]', "if *v:", '    a = echo(text="taken")', "note.k = take(rows=[])", "OUT{x: *v}")
    run = _run(actor, fenced(*lines))
    assert run.output == {"x": "old"} and actor.notes_for(PROGRAM).get("k").value == "new"
    # a command's arguments read when it starts, after the walk: the output read the value first
    actor.notes_for(PROGRAM).put("k", "old", run="r0", seq=0)
    run = _run(actor, fenced('v = note["k"]', "b = echo(text=*v)", "note.k = take(rows=[])", "OUT{x: *v}"))
    assert run.output == {"x": "new"} and run.record.get("b").content.value == "new"


@pytest.mark.parametrize(
    ("turns", "written"),
    [
        ((fenced('v = note["k"]', 'note.k = *v + "!"', "if *v:", "    b = echo(text=*v)"),), "old!"),
        ((fenced('v = note["k"]'), fenced('note.k = *v + "!"', "if *v:", "    b = echo(text=*v)")), "old!"),
        ((fenced('v = note["k"]', "w = *v", "note.k = echo(text=*w)", "OUT{x: *v, y: *w}"),), "old"),
        # naming the value is reading it, whether or not the evaluation reaches the pointer; computing
        # the value reaches only what its own branches reach (the `else` and its command stay unrun)
        (
            (
                fenced(
                    'x = [DO echo(text="side")]',
                    'v = note["k"] if true else *x',
                    "note.k = false and *v",
                    "if *v:",
                    "    b = echo(text=*v)",
                ),
            ),
            False,
        ),
        ((fenced('v = note["k"]', "note.k = false and *v", "if *v:", "    b = echo(text=*v)"),), False),
        # a write queued behind one that reads the value waits for it: the value comes before both
        (
            (
                fenced(
                    'v = note["k"]', 'note.k = *v + "!"', 'note.k = "new"', "if *v:", "    b = echo(text=*v)"
                ),
            ),
            "new",
        ),
    ],
)
def test_a_write_that_reads_a_value_comes_after_the_value_so_a_later_reader_never_waits_on_itself(
    turns, written
):
    calls: list = []
    actor = _actor("write-reads-value", calls)
    actor.notes_for(PROGRAM).put("k", "old", run="r0", seq=0)
    request = RunRequest(inputs=("go",), max_turns=len(turns) + 1)
    # bounded: a wait cycle between the write and the value fails here, never hangs the suite
    asyncio.run(asyncio.wait_for(actor.run(PROGRAM, request, scripted(*turns, "idle")), 5))
    assert calls == [("echo", "old")] and actor.notes_for(PROGRAM).get("k").value == written


def _held(monkeypatch) -> list[int]:
    """How many note writes each reader holds, in the order the readers are made."""
    sizes: list[int] = []
    made = _Reader.__init__

    def counted(self, *args):
        made(self, *args)
        sizes.append(len(self.writes))

    monkeypatch.setattr(_Reader, "__init__", counted)
    return sizes


def test_a_reader_holds_only_the_writes_of_the_notes_it_reads_however_many_the_program_makes(monkeypatch):
    sizes = _held(monkeypatch)
    n = 400
    writes = [f'note.k{i} = "v{i}"' for i in range(n)]
    lines = (*writes, 'a = echo(text=note["k7"])', 'if note["k9"] == "v9":', "    OUT{x: *a}")
    run = _run(_actor("many-writes", []), fenced(*lines))
    assert run.output == {"x": "v7"}
    # a literal write reads no note; the command and the branch each wait for the one write they read
    assert sizes == [0] * n + [1, 1, 0]


def test_a_value_holds_only_the_writes_of_the_notes_it_reads_whatever_its_first_reader_holds(monkeypatch):
    sizes = _held(monkeypatch)
    n = 300
    writes = [f'note.k{i} = "v{i}"' for i in range(n)]
    values = [f'v{i} = note["k{i}"]' for i in range(n)]
    out = "OUT{x: [" + ", ".join(f"*v{i}" for i in range(n)) + "]}"
    run = _run(_actor("many-values", []), fenced(*writes, *values, out))
    assert run.output == {"x": [f"v{i}" for i in range(n)]}
    # the output reads every note through the values; each value, computed for it, holds its own one
    assert sizes == [0] * n + [n] + [1] * n


@pytest.mark.parametrize(
    "lines",
    [
        ("r = grid(n=1)", "OUT{x: *r[0] + *r.a + *r[True]}"),
        ("r = grid(n=1)", "v = *r[0] + *r.a + *r[True]", "OUT{x: *v}"),
        ("r = grid(n=1)", "if *r[0]:", "    OUT{x: *r[0] + *r.a + *r[True]}"),
    ],
)
def test_a_dict_is_read_at_any_key_it_holds(lines):
    actor = _actor("intkey-index", [])

    @actor.handler(Grid)
    async def grid(req: Grid, ctx) -> dict:
        return {0: "zero", "a": "A", True: "yes"}

    run = _run(actor, fenced(*lines), "idle")
    assert isinstance(run.outcome, Success), _notices(run)
    assert run.output["x"] == "zeroAyes"


@pytest.mark.parametrize(
    "lines",
    [
        ("r = grid(n=1)", "OUT{x: *r[[1]]}"),
        ("r = grid(n=1)", "if *r[[1]]:", "    OUT{x: 1}", "else:", "    OUT{x: 2}"),
        ("r = grid(n=1)", "b = echo(text=*r[[1]])", "OUT{x: *b}"),
    ],
)
def test_a_key_a_dict_cannot_hold_is_a_language_error_not_a_crash(lines):
    actor = _actor("unhashable-key", [])

    @actor.handler(Grid)
    async def grid(req: Grid, ctx) -> dict:
        return {0: "zero"}

    run = _run(actor, fenced(*lines), "idle")
    assert not isinstance(run.outcome, Success)
    assert "list cannot name a field" in _notices(run)


def test_a_value_whose_computation_failed_stays_failed_and_what_it_ran_never_runs_again():
    calls: list = []
    actor = _actor("failed-value", calls)
    turns = (
        fenced('v = [DO echo(text="side")][0] + 1', 'w = [DO echo(text="fine")][0] + "!"'),
        fenced("b = echo(text=*v)"),
        fenced('v = "again"'),
        fenced("c = echo(text=*v)", "d = echo(text=*w)"),
        "idle",
    )
    first = _run(actor, *turns)
    told = "line 1: cannot apply + to str and int"
    assert first.record.get("b").content.error == f"*v: {told}"
    assert first.record.get("c").content.error == f"*v did not settle ok: {told}"
    assert first.record.get("d").content.value == "fine!"
    assert "v is bound already: a name is bound once for the whole run" in _notices(first)
    # the DO ran once, in the turn that first read the value; the failure is what the name holds
    assert sorted(calls) == [("echo", "fine"), ("echo", "fine!"), ("echo", "side")]
    assert not first.unread and first.values["v"][1].error == told
    # the failure rides the log like a value, so a resumed run finds it and runs nothing
    history = tuple(entry_of(json.loads(json.dumps(entry_row(e), default=str))) for e in first.record)
    calls.clear()
    run = _run(
        _actor("resumed", calls), fenced("e = echo(text=*v)", "f = echo(text=*w)"), "idle", history=history
    )
    assert run.record.get("e").content.error == f"*v did not settle ok: {told}"
    assert run.record.get("f").content.value == "fine!"
    assert calls == [("echo", "fine!")]


@pytest.mark.parametrize(
    "lines",
    [
        ("r = grid(n=1)", "OUT{x: *r.missing}"),
        ("r = grid(n=1)", "if *r.missing:", "    OUT{x: 1}", "else:", "    OUT{x: 2}"),
        ("r = grid(n=1)", "b = echo(text=*r.missing)", "OUT{x: *b}"),
    ],
)
def test_a_missing_field_of_a_dict_with_mixed_keys_is_a_language_error_not_a_crash(lines):
    actor = _actor("mixed-keys", [])

    @actor.handler(Grid)
    async def grid(req: Grid, ctx) -> dict:
        return {0: "zero", "a": "A"}

    run = _run(actor, fenced(*lines), "idle")
    assert not isinstance(run.outcome, Success)
    assert "no field 'missing' in the dict (it has [0, 'a'])" in _notices(run)


@pytest.mark.parametrize(
    ("lines", "repair", "told"),
    [
        (('a = echo(text="two', 'lines")'), "newline", "line 1: read as 'a = echo(text=\"two\\\\nlines\")'"),
        (('a = echo(text="x"',), "closers", "line 1: read as 'a = echo(text=\"x\")'"),
        (('a = echo(text="two', "lines"), ("closers", "newline"), "line 1: read as"),
        (
            ('a = echo(text="x")', 'b = search_similar(input="Dune" candidates=[])'),
            "comma",
            "line 2: read as 'b = search_similar(input=\"Dune\" , candidates=[])'",
        ),
        (
            ('a = echo(text="x")', 'b = search_similar(input="Dune" candidates=[]'),
            ("closers", "comma"),
            "line 2: read as 'b = search_similar(input=\"Dune\" , candidates=[])'",
        ),
    ],
)
def test_each_catalogue_repair_is_named_and_counted(lines, repair, told):
    calls: list = []
    actor = _actor("repairs", calls)
    run = _run(actor, fenced(*lines), "idle")
    names = (repair,) if isinstance(repair, str) else repair
    assert run.repairs == dict.fromkeys(names, 1)
    assert told in _notices(run) and "if that is not what you meant, write it again" in _notices(run)
    written = {name for name in ("echo", "search_similar") if any(f"{name}(" in line for line in lines)}
    assert {name for name, _ in calls} == written


def test_a_block_no_repair_reads_is_refused_with_the_first_error():
    actor = _actor("unrepaired", [])
    run = _run(actor, fenced("a = echo(text=))"), "idle")
    assert "your last turn could not be read: line 1: unmatched ')'" in _notices(run)


def test_the_tag_forms_repairs_are_counted_by_name():
    actor = _actor("tag-repairs", [])
    turns = (
        '<lact a>echo(text="x" </lact> <lact b>echo(text="y")</lact>',
        '<lact c>echo(text="z"</lact>',
        "idle",
    )
    run = _run(actor, *turns, profile=Profile("p"))
    assert run.repairs == {"closers": 2}
    run = _run(actor, '<lact d>search_similar(input="x" candidates=[])</lact>', "idle", profile=Profile("p"))
    assert run.repairs == {"comma": 1}
    # a tag no reading reads refuses the turn; the repairs of the tags read before it still count
    actor = _actor("tag-repairs-refused", [])
    run = _run(actor, '<lact a>echo(text="x"</lact><lact b>echo(text=)</lact>', "idle", profile=Profile("p"))
    assert run.repairs == {"closers": 1} and "could not be read" in _notices(run)
    # a call a repair read and the check then refused: the repair was applied, and is counted
    run = _run(
        _actor("tag-repairs-refused-call", []),
        "<lact a>echo(text=1, **args</lact>",
        "idle",
        profile=Profile("p"),
    )
    assert run.repairs == {"closers": 1} and "** unpacking is not allowed" in _notices(run)


@pytest.mark.parametrize(
    ("turn", "counted"),
    [
        ('<lact r>search_similar(input="x" candidates=[]</lact>', {"closers": 1, "comma": 1}),
        ('<lact r>echo(text="two\nlines")</lact>', {"newline": 1}),
    ],
)
def test_the_tag_form_counts_every_repair_one_call_needed(turn, counted):
    run = _run(_actor("tag-repairs-each", []), turn, "idle", profile=Profile("p"))
    assert run.repairs == counted and run.record.get("r").content.ok


def test_a_profile_sees_only_its_languages_guidance():
    seen: list = []
    _run(_actor("guided", []), fenced('OUT{x: "y"}'), seen=seen)
    system = seen[0][0].content
    assert "one fenced block" in system and "<lact" not in system
    seen.clear()
    _run(_actor("core", []), 'OUT{x: "y"}', profile=Profile("p"), seen=seen)
    assert "<lact" in seen[0][0].content and "one fenced block" not in seen[0][0].content
    with pytest.raises(ProfileError, match="profile language 'python'"):
        Profile("p", language="python")


def test_the_guidance_names_the_outputs_and_context_show_only_when_the_profile_has_it():
    text = program_guidance(("done",), commands=["echo", "context.show"])
    assert "return {done: *t1}" in text and "outputs: done" in text and "*name or context.show" in text
    bare = program_guidance(commands=["echo"])
    assert "outputs: listed below" in bare and "context.show" not in bare
    assert "*name or context.show" in program_guidance()


def test_a_core_profile_runs_nothing_from_a_fenced_program():
    calls: list = []
    run = _run(_actor("core-fence", calls), fenced('a = echo(text="x")'), "idle", profile=Profile("p"))
    assert calls == [] and "your last turn contained no LNDL, so nothing was executed" in _notices(run)


def test_a_turn_without_a_block_is_idle_and_under_a_conversation_is_the_reply():
    run = _run(_actor("idle", []), "thinking out loud", "still thinking")
    assert "your last turn contained no LNDL" in _notices(run)
    replies: list = []

    async def wait(run):
        replies.append(run.turn)
        return False

    run = _run(_actor("chat", []), "hello there", "unused", wait=wait)
    assert replies == [2] and run.outcome.__class__.__name__ == "Stopped"


def _asking():
    asked: list = []

    async def wait(run):
        asked.append(run.turn)
        return False

    return asked, wait


@pytest.mark.parametrize(
    "lines",
    [
        ("a = 3",),
        ('note["k"] = {x: 1}',),
        ("a = 3 if true else 4",),
        ("if true:", "    a = 3", "else:", "    b = 4"),
    ],
)
def test_a_program_that_runs_no_command_and_gives_no_out_is_a_reply_under_wait(lines):
    # a program's turn acts when its walk launches a command or reaches OUT{}, and a value or a note
    # write of data does neither, so the turn is the reply and wait is asked at the next turn's start
    asked, wait = _asking()
    run = _run(_actor("no-act", []), fenced(*lines), "unused", wait=wait)
    assert asked == [2] and run.outcome == Stopped(2)


@pytest.mark.parametrize(
    ("lines", "ran", "acted"),
    [
        ('a = echo(text="x")', [("echo", "x")], True),
        ('note["k"] = echo(text="x")', [("echo", "x")], True),
        ('if (DO echo(text="x")) == "x":\n    a = 1', [("echo", "x")], True),
        ('OUT{answer: "x"}', [], True),
        ('return {answer: "x"}', [], True),
        ("OUT{answer: 1 / 0}", [], True),
        ('if false:\n    DO echo(text="x")', [], False),
        ('if true:\n    a = 1\nelse:\n    DO echo(text="x")', [], False),
        ('v = false and DO echo(text="x")', [], False),
        ('note["k"] = false and DO echo(text="x")', [], False),
        ('if false:\n    OUT{answer: "x"}', [], False),
        ('if false:\n    return {answer: "x"}', [], False),
    ],
)
def test_a_program_is_a_reply_under_wait_unless_its_walk_launches_a_command_or_reaches_out(lines, ran, acted):
    # the turn's class is what its walk did, never the program's text: a command launched, or OUT{}
    # reached though the gate refuses it or its fields fail, is a turn that acted and the next turn
    # follows; a command or an OUT{} control flow does not reach is neither, and the turn is the reply
    calls: list = []
    asked, wait = _asking()

    def refuse(run, fields):
        return "not yet" if "answer" in fields else None

    run = _run(
        _actor("act", calls), fenced(*lines.split("\n")), fenced('OUT{ok: "yes"}'), wait=wait, accept=refuse
    )
    assert calls == ran and asked == ([] if acted else [2])
    assert isinstance(run.outcome, Success) if acted else run.outcome == Stopped(2)


def test_reading_an_earlier_turns_value_that_runs_a_command_makes_the_turn_act_under_wait():
    # the value is computed when a later turn first reads it, and computing it runs a command: that turn
    # wrote no command, yet its walk launched one, so it acted and the model sees the result next turn
    calls: list = []
    asked: list = []

    async def wait(run):  # the other side answers the binding turn's reply, and nothing after it
        asked.append(run.turn)
        return run.turn == 2

    bind, read = fenced('x = DO echo(text="x") if true else 0'), fenced('note["k"] = *x')
    seen: list = []
    run = _run(_actor("deferred", calls), bind, read, fenced('OUT{k: *note["k"]}'), wait=wait, seen=seen)
    assert calls == [("echo", "x")] and asked == [2] and run.output == {"k": "x"} and len(seen) == 3
    told = seen[2][-1].content  # the third turn's notification: the command settled, the note written
    assert "echo): str[1], consumed" in told and "notes written: note.k" in told
    # a value nothing read rides the log, so a resumed run reading it runs the command and acts the same
    first = _run(_actor("first", []), bind, "idle")
    history = tuple(entry_of(json.loads(json.dumps(entry_row(e), default=str))) for e in first.record)
    asked, wait = _asking()
    run = _run(_actor("resumed", calls), read, fenced('OUT{k: *note["k"]}'), history=history, wait=wait)
    assert asked == [] and run.output == {"k": "x"} and calls == [("echo", "x")] * 2


def test_two_fenced_blocks_are_refused():
    run = _run(_actor("two-blocks", []), fenced("a = 1") + fenced("b = 2"), "idle")
    assert "2 fenced blocks: a turn is one program in one block" in _notices(run)


def test_a_fence_inside_a_string_argument_is_the_strings_text():
    # a fence stands at the start of its line: one in a string neither ends the block (the call
    # then ran with its string cut and closed) nor, with a second, makes the turn two blocks
    for line, args in (
        ('a = echo(text="```")', {"text": "```"}),
        ('a = edit(old="```", new="```")', {"old": "```", "new": "```"}),
    ):
        script = read(fenced(line))
        (command,) = script.body
        assert {k: ast.literal_eval(v) for k, v in command.args} == args and script.problems == ()


@pytest.mark.parametrize(
    "text",
    [
        'Here:\n\n  ```python\nx = echo(text="hi")\n```\n',
        '```python\nx = echo(text="hi")\n  ```\n',
        '\t```\nx = echo(text="hi")\n\t```',
        '- ```python\nx = echo(text="hi")\n```',
        '1. ```python\nx = echo(text="hi")\n```',
        'Here is the code: ```python\nx = echo(text="hi")\n```',
        '```python\nx = echo(text="hi")```',
    ],
)
def test_a_fence_opens_wherever_it_stands_and_closes_at_a_line_start_or_else_where_it_is(text):
    script = read(text)
    (command,) = script.body
    assert command.name == "echo" and script.problems == ()


@pytest.mark.parametrize("text", ['```python\nx = echo(text="hi")\n', 'Here: ```python\nx = echo(text="hi")'])
def test_a_fence_that_nothing_after_it_closes_opens_no_block(text):
    assert read(text) == Script()


@pytest.mark.usefixtures("no_collection")
@pytest.mark.parametrize("depth", [1500, 3000, 100_000])
def test_a_program_nested_too_deeply_is_refused_at_reading(depth):
    # past the checker's recursion, or the parser's own stack: an LNDLError the loop retries. 1500
    # parses on every supported Python and overflows the checker; 3000 overflows 3.11's parser

    with pytest.raises(LNDLError, match="the program is nested too deeply to read; the program was not run"):
        read(fenced("x = " + "-" * depth + "1"))


@pytest.mark.usefixtures("no_collection")
def test_a_long_dotted_command_name_is_read_or_refused_never_a_crash():
    # from 3.12 the parser builds this without recursing, and the name is read without recursing too
    try:
        (command,) = read(fenced("x = " + ".".join(["a"] * 3000) + "(y=1)")).body
    except LNDLError as e:
        assert "nested too deeply" in str(e)
    else:
        assert command.name == ".".join(["a"] * 3000)


def test_a_resumed_program_turn_admits_nothing_from_its_prose():
    history = (Entry(seq=0, kind=Kind.TEXT, content="<lvar scratch>kept?</lvar>\n" + fenced('OUT{x: "y"}')),)
    run = _run(_actor("resume", []), "idle", history=history)
    assert "scratch" not in run.values
    core = _run(_actor("resume-core", []), "idle", history=history, profile=Profile("p"))
    assert core.values["scratch"][1] == "kept?"


# the evaluator over data, without a run


class Env:
    def __init__(self, **values):
        self.values = values
        self.notes = {"k": {"a": [1, 2]}}

    async def point(self, name):
        return self.values[name]

    async def note(self, key):
        return self.notes[key]

    async def do(self, name, args, line):
        return {"ran": name, **args}


def value(expr: str, **values):
    (node,) = read(fenced(f"x = {expr}")).body
    assert isinstance(node, Value)
    return asyncio.run(evaluate(node.expr, Env(**values)))


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ("1 + 2 * 3 - 4 / 2", 5.0),
        ("-3 + 1.5", -1.5),
        ('"a" + "b"', "ab"),
        ("[1] + [2]", [1, 2]),
        ('3 * "ab"', "ababab"),
        ("not true", False),
        ("null", None),
        ('false or 0 or "last"', "last"),
        ("1 and 0 and 2", 0),
        ("1 or 2", 1),
        ("1 and 2", 2),
        ("false or 0", 0),
        ("1 < 2 <= 2 < 3", True),
        ("1 < 2 > 3", False),
        ("1 == 1 != 2 >= 2", True),
        ('{a: 1, "b": [true, null]}', {"a": 1, "b": [True, None]}),
        ("*r[0].title", "t"),
        ('*r[-1]["title"]', "t"),
        ("*s[1]", "b"),
        ('note["k"].a[1]', 2),
        ("*note.k.a", [1, 2]),
        ('(x.title for x in *r if x.title == "t" if true)', ["t"]),
        ('(c for c in "ab")', ["a", "b"]),
        ("(x for x in *r if false)", []),
        ("[DO fetch(title=*s)]", [{"ran": "fetch", "title": "abc"}]),
        ("1 if *r else 2", 1),
        ("*m.title", "Dune"),
    ],
)
def test_the_evaluator_computes_over_data(expr, want):
    assert value(expr, r=[{"title": "t"}], s="abc", m=Book(title="Dune", year=1965)) == want


@pytest.mark.parametrize(
    ("expr", "said"),
    [
        ('"a" - 1', "cannot apply - to str and int"),
        ("1 / 0", "division by zero"),
        ('-"a"', "- needs a number, not str"),
        ("1 < null", "cannot compare int < null"),
        ("*r.title", "list cannot be read at 'title'"),
        ("*r[5]", "index 5 is past the end of list[1]"),
        ("*r[true]", "list cannot be read at True"),
        ('*r[0]["nope"]', "no field 'nope' in the dict"),
        ("*m.nope", "Book cannot be read at 'nope'"),
        ("(x for x in 3)", "a comprehension runs over a list, not int"),
        (f'"ab" * {LIMIT}', "past the size limit"),
        (f'{LIMIT} * "ab"', "past the size limit"),
        (f'"{"a" * (LIMIT // 2 + 1)}" + "{"a" * (LIMIT // 2 + 1)}"', "past the size limit"),
        (f'["{"a" * (LIMIT // 2 + 1)}", "{"a" * (LIMIT // 2 + 1)}"]', "past the size limit"),
        (f'{{a: "{"a" * (LIMIT + 1)}"}}', "past the size limit"),
        (f"(*s * {LIMIT // 6} for i in [1, 2, 3])", "past the size limit"),
        ("[*big, *big]", "past the size limit"),
        (f"*t * {LIMIT + 1}", "past the size limit"),
        (f"{LIMIT + 1} * *t", "past the size limit"),
    ],
)
def test_the_evaluator_names_what_it_could_not_compute(expr, said, monkeypatch):
    # a product past the limit is refused before it is built: the multiplication itself never runs
    sign, mul = lndl._ARITH[ast.Mult]
    monkeypatch.setitem(
        lndl._ARITH,
        ast.Mult,
        (sign, lambda a, b: pytest.fail("built") if tuple in (type(a), type(b)) else mul(a, b)),
    )
    with pytest.raises(EvalError, match=said.replace("[", r"\[").replace("*", r"\*")):
        value(
            expr,
            r=[{"title": "t"}],
            s="abc",
            m=Book(title="Dune", year=1965),
            big=list(range(LIMIT // 2 + 1)),
            t=(0,),
        )


def test_a_container_already_past_the_size_limit_is_counted_and_not_walked():
    # the count stops there either way; its children were pushed first, every one of them
    class Dict(dict):
        def values(self):
            raise AssertionError("walked")

    class List(list):
        def __iter__(self):
            raise AssertionError("walked")

        def __getitem__(self, item):  # a slice of it is a walk too
            raise AssertionError("walked")

    assert lndl._size(Dict({"k" * (LIMIT + 1): 1})) == LIMIT + 2
    assert lndl._size(List([0] * (LIMIT + 1))) == LIMIT + 1


def test_a_long_whole_number_counts_as_its_digits_and_a_product_past_the_limit_is_never_built(monkeypatch):
    # a number was one item whatever its length: twenty lines squaring one held the loop 2.7 s under
    # a 0.5 s time budget, each further line three times longer, and no deadline lands in arithmetic
    n = 10 ** (LIMIT * 2 // 3)
    assert value("*n + 1", n=n) == n + 1 and value("*n * 2", n=n) == 2 * n and value("7 * 6") == 42
    edge = 2 ** (3 * LIMIT + 1)  # as many digits as the limit holds, by the count: its double is one past
    assert value("*edge - 1", edge=edge) == edge - 1
    for expr in ("*edge + *edge", "[*n, *n]"):
        with pytest.raises(EvalError, match="past the size limit"):
            value(expr, n=n, edge=edge)
    sign, _ = lndl._ARITH[ast.Mult]
    monkeypatch.setitem(lndl._ARITH, ast.Mult, (sign, lambda a, b: pytest.fail("built")))
    with pytest.raises(EvalError, match="past the size limit"):
        value("*n * *n", n=n)


@pytest.mark.parametrize(
    ("line", "said"),
    [
        ('x = b"bytes"', "b'bytes' is not a value"),
        ("x = note", 'a note is read as note["key"]'),
        ("x = *note", 'a note is read as note["key"]'),
        ("x = DO y", "DO names a command call"),
        ("x = y", "y: a name is read by pointing at it, *y"),
        ("x = 1 % 2", "BinOp is not part of the program form"),
        ("x = ~1", "UnaryOp is not part of the program form"),
        ("x = 1 in [1]", "Compare is not part of the program form"),
        ("x = [y for y in [1]]", "ListComp is not part of the program form"),
        ("x = (a for a, b in [1])", "GeneratorExp is not part of the program form"),
        ("x = (note for note in [1])", "'note' cannot be bound"),
        ("x = echo(text=1) + 1", "inside an expression a command is written DO name"),
        ('x = (DO echo(text=missing) for w in ["a"])', "missing: a name is read by pointing at it, *missing"),
        ("x = [DO a[0](y=1)]", "the command must be a name"),
        ("x = f()(y=1)", "the command must be a name"),
        ("x = a[0].f(y=1)", "the command must be a name"),
        ("x = echo(1)", "arguments are keyword only"),
        ("x = echo(**y)", "** unpacking is not allowed"),
        ("x = {*y: 1}", "a key is a bare word or a string"),
        ("x = {1: 1}", "a key is a bare word or a string"),
        ("x = {**y}", "a key is a bare word or a string"),
        ("x = *a[*b]", ""),
        ("_x = 1", "'_x' cannot be bound"),
        ("note = 1", "'note' cannot be bound"),
        ("x, y = 1, 2", "a statement binds a name"),
        ("x += 1", "a statement binds a name"),
        ("return 3", "a statement binds a name"),
        ("for x in [1]:\n    pass", "a statement binds a name"),
        ("3", "a statement binds a name"),
        ("*r[0] = 1", "a statement binds a name"),
        ("note[0] = 1", "a statement binds a name"),
        ("note[*k] = 1", "a statement binds a name"),
        ("x = OUT{a: 1}", "a statement binds a name"),
        ("__y = 1", "__y: names starting with __ are the runtime's"),
        ('x = (1\ny = """a', "line 2: unterminated triple-quoted string literal"),
        ("x = 'nul\x00'", "the program was not run"),
    ],
)
def test_what_is_outside_the_program_form_is_refused_at_reading(line, said):
    if said == "":  # an index that is itself a pointer is read, not refused
        assert isinstance(read(fenced(line)).body[0], Value)
        return
    with pytest.raises(LNDLError, match=said.replace("*", r"\*").replace("[", r"\[").replace("(", r"\(")):
        read(fenced(line))


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ('(DO echo(text=word) for word in ["a", "b"])', ["a", "b"]),
        ('(w for w in ["a", "b"] if DO echo(text=w))', ["a", "b"]),
        ('([DO echo(text=w)] for w in ["a", "b"])', [["a"], ["b"]]),
    ],
)
def test_a_command_inside_a_comprehension_reads_the_comprehensions_name(expr, want):
    calls: list = []
    run = _run(_actor("comprehension-do", calls), fenced(f"OUT{{x: {expr}}}"))
    assert isinstance(run.outcome, Success) and run.output == {"x": want}
    assert calls == [("echo", "a"), ("echo", "b")]


def test_the_reader_lowers_statements_to_nodes_and_leaves_products_alone():
    lines = (
        "a = 2 * *b",
        "# a comment",
        "c = echo(text=*a)",
        'note.k.p = echo(text="x")',
        "DO echo(text=*a)",
    )
    script = read(fenced(*lines))
    value_node, command, write, bare = script.body
    assert isinstance(value_node, Value) and isinstance(command, Command) and command.alias == "c"
    assert isinstance(write, Write) and (write.key, write.path, write.value.alias) == ("k", ("p",), None)
    assert isinstance(bare, Command) and bare.name == "echo" and bare.alias is None
    assert read("no block here").body == ()


def test_a_command_can_be_called_by_a_dotted_name_and_returns_a_specs_fields():
    calls: list = []
    actor = _actor("dotted", calls)
    turn = fenced(
        'n = note.list(prefix="")', 'b = fetch(title="Dune")', 'y = echo(text=*b.title + " " + *b.title)'
    )
    run = _run(actor, turn, "idle")
    assert run.record.get("n").content.ok and run.record.get("y").content.value == "Dune Dune"


class Many(BaseModel):
    n: int


def _counting_actor(name: str) -> Actor:
    actor = _actor(name, [])

    @actor.handler(Many)
    async def many(req: Many, ctx) -> list[str]:
        return [""] * req.n

    return actor


def test_a_comprehension_counts_each_item_it_builds_toward_the_size_limit():
    actor = _counting_actor("items")
    turn = fenced(
        f"xs = many(n={LIMIT + 1})",
        "big = (x for x in *xs)",
        "a = echo(text=*big[0])",
        f"ys = many(n={LIMIT})",
        "fits = (y for y in *ys)",
        "b = echo(text=*fits[0])",
    )
    run = _run(actor, turn, "idle")
    a, b = run.record.get("a").content, run.record.get("b").content
    assert (
        not a.ok
        and a.reason == "invalid"
        and "line 2: the value built here is past the size limit" in a.error
    )
    assert b.ok and len(run.values["fits"][1]) == LIMIT
    assert not run.values["big"][1].ok  # the name holds its failure, so the value is never built again


def test_a_dicts_keys_count_toward_the_size_limit():
    actor = _counting_actor("keys")
    key = "k" * 200
    turn = fenced(
        "xs = many(n=1000)",
        f'ds = ({{"{key}": x}} for x in *xs)',
        f"a = echo(text=*ds[0].{key})",
        f'one = {{"{key}": "v"}}',
        f"b = echo(text=*one.{key})",
    )
    run = _run(actor, turn, "idle")
    a, b = run.record.get("a").content, run.record.get("b").content
    assert not a.ok and "line 2: the value built here is past the size limit" in a.error
    assert b.ok and b.value == "v"


@pytest.mark.parametrize(
    "build",
    [
        '[DO echo(text=*s), DO echo(text=*s), DO echo(text="after")]',
        '{a: DO echo(text=*s), b: DO echo(text=*s), c: DO echo(text="after")}',
    ],
)
def test_a_list_or_dict_past_the_size_limit_runs_no_part_after_the_one_that_took_it_there(build):
    calls: list = []
    turn = fenced(f's = "a" * {LIMIT // 2 + 1}', f"OUT{{x: {build}}}")
    run = _run(_actor("prefix", calls), turn, "idle")
    # the second part took the build past the limit and has run; the part after it never runs
    assert [text if text == "after" else len(text) for _, text in calls] == [LIMIT // 2 + 1] * 2
    assert "line 2: the value built here is past the size limit" in _notices(run)


def test_a_list_at_exactly_the_size_limit_is_built_and_one_character_more_is_not():
    n = (LIMIT - 2) // 2
    s, t = "a" * n, "a" * (LIMIT - 2 - n)  # two items and their characters: LIMIT exactly
    assert value("[*s, *t]", s=s, t=t) == [s, t]
    with pytest.raises(EvalError, match="past the size limit"):
        value("[*s, *t]", s=s, t=t + "a")


def test_a_key_a_dict_writes_twice_counts_once_toward_the_size_limit():
    s = "a" * (LIMIT // 2 + 1)  # either value fits alone, the two together would not
    assert value('{k: *s, k: *s + "b"}', s=s) == {"k": s + "b"}
    # counted by its value at the time: one that passes the limit on the way is refused, though the value
    # the key ends with would fit
    with pytest.raises(EvalError, match="past the size limit"):
        value('{k: *s, k: "b"}', s="a" * LIMIT)


@pytest.mark.parametrize(
    ("size", "fits"),
    [(40_000, True), (60_000, False)],
    ids=["two 40,000-character fields", "two 60,000-character fields"],
)
def test_the_fields_of_out_are_held_together_to_the_size_limit_as_a_dict_literal_is(size, fits):
    run = _run(_actor("out-size", []), fenced(f'OUT{{a: "x" * {size}, b: "y" * {size}}}'))
    # each field is under the limit alone: the output is the dict the model wrote, and it is counted as
    # one, so the pair that passes the limit is the turn's failure, a retry that names the limit
    if fits:
        assert isinstance(run.outcome, Success) and run.output == {"a": "x" * size, "b": "y" * size}
    else:
        assert run.output is None and not isinstance(run.outcome, Success)
        assert run.retry_error == f"line 1: the OUT{{}} fields together are past the size limit of {LIMIT}"


class Rows(BaseModel):
    n: int


class Grow(BaseModel):
    rows: Any
    n: int
    after: float = 0  # seconds the handler waits before it grows the list


class Shrink(BaseModel):
    rows: Any


class Blob(BaseModel):
    text: str


class Poke(BaseModel):
    blob: Any
    after: float = 0


def _growing_actor(name: str) -> Actor:
    actor = Actor(name, store=fresh_store())

    @actor.handler(Rows)
    async def rows(req: Rows, ctx) -> list[str]:
        return ["r"] * req.n

    @actor.handler(Grow)
    async def grow(req: Grow, ctx) -> str:
        if req.after:
            await asyncio.sleep(req.after)
        req.rows.append("x" * req.n)  # the list the earlier field holds, reached through the pointer
        return "grown"

    @actor.handler(Shrink)
    async def shrink(req: Shrink, ctx) -> str:
        req.rows.clear()
        return "shrunk"

    @actor.handler(Blob)
    async def blob(req: Blob, ctx) -> bytearray:
        return bytearray(req.text.encode())

    @actor.handler(Poke)
    async def poke(req: Poke, ctx) -> str:
        if req.after:
            await asyncio.sleep(req.after)
        req.blob.extend(b"!")
        return "poked"

    return actor


def test_a_part_a_command_shrank_after_it_was_counted_still_counts_at_the_size_it_landed_with():
    # a is counted as it lands, near the limit; b empties the list a holds; c would fit beside the emptied
    # list, but the running size counts a as it landed and the pair with c passes the limit: refused, as
    # a dict literal built of those parts is (S6)
    n = (LIMIT - 50) // 2
    run = _run(
        _growing_actor("out-shrunk"),
        fenced(f"rows = rows(n={n})", 'OUT{a: *rows, b: DO shrink(rows=*rows), c: "y" * 100}'),
    )
    assert run.output is None and not isinstance(run.outcome, Success)
    assert run.retry_error == f"line 2: the OUT{{}} fields together are past the size limit of {LIMIT}"


def test_a_value_of_a_kind_a_capture_shares_is_shared_in_the_output():
    # the output is a copy as a capture is (ADR-0007/C1): a list, dict, set, tuple, model or result is
    # copied, a value of another kind is shared, and the size limit counts it as one whatever it holds
    run = _run(
        _growing_actor("out-blob"),
        fenced('blob = blob(text="ab")', "late = poke(blob=*blob, after=0.05)", "OUT{a: *blob}"),
    )
    assert isinstance(run.outcome, Success)
    assert run.output["a"] is run.record.get("blob").content.value and run.output["a"] == bytearray(b"ab!")


def test_a_field_of_out_a_command_grew_after_it_was_counted_is_caught_as_the_output_is_emitted():
    run = _run(
        _growing_actor("out-grown"),
        fenced("rows = rows(n=3)", f"OUT{{a: *rows, b: DO grow(rows=*rows, n={LIMIT})}}"),
    )
    # field a was counted at three items; the command b runs grew that list past the limit, so the running
    # size reads fit while the dict does not: the whole is measured once more as it is emitted
    assert run.output is None and not isinstance(run.outcome, Success)
    assert run.retry_error == f"line 2: the OUT{{}} fields together are past the size limit of {LIMIT}"


@pytest.mark.parametrize("site", ["literal", "out"])
def test_a_key_written_again_gives_back_what_it_landed_as_whatever_a_command_made_of_it_since(site):
    # k lands holding the big list; the command between clears that list through the pointer; k is written
    # again with a string that fits only if what leaves the count is the size k landed with, not the size
    # of the emptied list it holds by then
    big, text = LIMIT // 4, "b" * (LIMIT - 20)  # the list counts two per one-character row
    inner = f'{{k: *rows, s: DO shrink(rows=*rows), k: "{text}"}}'
    turn = fenced(f"rows = rows(n={big})", f"OUT{{c: {inner}}}" if site == "literal" else f"OUT{inner}")
    run = _run(_growing_actor(f"landed-{site}"), turn)
    assert isinstance(run.outcome, Success), run.retry_error
    out = run.output["c"] if site == "literal" else run.output
    assert out == {"k": text, "s": "shrunk"}


@pytest.mark.parametrize("site", ["literal", "out"])
def test_a_key_written_again_after_its_list_grew_lets_no_part_past_the_limit_run(site):
    # k landed small; the command grew its list past the limit through the pointer; k is written again, and
    # what leaves the count is the small size it landed with, so the big field after it is refused as it
    # lands and the command after that never runs
    inner = (
        f'{{k: *rows, g: DO grow(rows=*rows, n={LIMIT}), k: "b", big: "c" * {LIMIT}, '
        "late: DO grow(rows=*rows, n=1)}"
    )
    turn = fenced("rows = rows(n=3)", f"OUT{{c: {inner}}}" if site == "literal" else f"OUT{inner}")
    run = _run(_growing_actor(f"grown-{site}"), turn)
    assert run.output is None and not isinstance(run.outcome, Success)
    assert "past the size limit" in run.retry_error
    assert len(run.record.get("rows").content.value) == 4  # grown once; the late command never ran


def test_an_output_that_cannot_be_copied_refuses_the_turn_and_the_run_goes_on(monkeypatch):
    real = actor_module._captured

    def captured(value, memo=None):
        if isinstance(value, dict) and "deep" in value:  # a nesting past the interpreter's depth
            raise RecursionError("maximum recursion depth exceeded")
        return real(value, memo)

    monkeypatch.setattr(actor_module, "_captured", captured)
    run = _run(_actor("out-uncopyable", []), fenced('OUT{deep: "x"}'))
    assert run.output is None and not isinstance(run.outcome, Success)
    assert run.retry_error == (
        "line 1: the OUT{} fields cannot be copied, RecursionError: maximum recursion depth exceeded"
    )


def test_an_output_field_that_runs_past_the_deadline_between_two_yields_commits_nothing(monkeypatch):
    real = actor_module._size

    def size(value):
        if value == "slow":
            time.sleep(0.3)  # sized synchronously: the deadline passes with no yield after it
        return real(value)

    monkeypatch.setattr(actor_module, "_size", size)
    request = RunRequest(inputs=("go",), time_budget=0.1, max_turns=1)
    actor = _actor("out-past-deadline-unyielded", [])
    run = asyncio.run(actor.run(PROGRAM, request, scripted(fenced('note.k = "w"', 'OUT{a: "slow"}'))))
    # the field's evaluation runs on to its end with no yield to cut it, and what it built commits nothing:
    # the write queued before it finds the budget spent and the output is refused at its handler
    assert run.elapsed >= 0.3 and run.output is None and not isinstance(run.outcome, Success)
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [("turn", "the time_budget of 0.1 is spent; nothing after it ran")]


def test_a_command_settling_after_the_output_was_emitted_grows_the_stored_value_not_the_output():
    # the command is launched before OUT{} and settles after the walk: the output emitted is a copy of the
    # list, and the settling command grows the stored one
    run = _run(
        _growing_actor("out-settled"),
        fenced("rows = rows(n=3)", f"late = grow(rows=*rows, n={LIMIT}, after=0.05)", "OUT{a: *rows}"),
    )
    assert isinstance(run.outcome, Success) and run.output == {"a": ["r", "r", "r"]}
    assert len(run.record.get("rows").content.value) == 4  # the stored value did grow


@pytest.mark.parametrize(
    "literal",
    [
        f"[*rows, DO grow(rows=*rows, n={LIMIT})]",
        f"{{a: *rows, b: DO grow(rows=*rows, n={LIMIT})}}",
        # the second item grows the list the first holds past the limit and is itself left out
        f"(*rows for x in [1, 2] if DO grow(rows=*rows, n={LIMIT // 2}) and x == 1)",
    ],
    ids=["list", "dict", "comprehension"],
)
def test_a_part_of_a_literal_a_command_grew_after_it_was_counted_is_caught_as_it_is_built(literal):
    # a binding is read where it is used: the literal is built under OUT{} and refused as itself, before
    # the fields are measured together
    run = _run(_growing_actor("literal-grown"), fenced("rows = rows(n=3)", f"OUT{{c: {literal}}}"))
    assert run.output is None and not isinstance(run.outcome, Success)
    assert run.retry_error == f"line 2: the value built here is past the size limit of {LIMIT}"


def test_the_fields_of_out_are_each_sized_once_as_they_land_and_never_all_again_after_each(monkeypatch):
    sized: list[int] = []
    real = lndl._size

    def counted(value):
        sized.append(real(value))
        return sized[-1]

    monkeypatch.setattr(lndl, "_size", counted)
    monkeypatch.setattr(actor_module, "_size", counted)
    n = 2000
    run = _run(_actor("out-many", []), fenced("OUT{" + ", ".join(f'k{i}: "a"' for i in range(n)) + "}"))
    # what the sizing visits, summed: a field once as it lands and the whole once more as it is emitted,
    # so a few per field twice over. Sizing the whole dict after every field visits every field before it
    # again, some 12,000,000 visits for these
    assert isinstance(run.outcome, Success) and len(run.output) == n
    assert sum(sized) < 8 * n


@pytest.mark.parametrize(
    ("first", "fits"),
    [('"x" * 60000', True), (f'"x" * {LIMIT - 1}', False)],
    ids=["a first value that fits alone", "a first value past the limit alone"],
)
def test_a_field_of_out_written_twice_counts_by_its_value_at_the_time_as_a_dict_literals_key_does(
    first, fits
):
    run = _run(_actor("out-twice", []), fenced(f'OUT{{k: {first}, k: "y" * 60000}}'))
    # the second value replaces the first: the pair held at once would pass the limit, the dict the output
    # is does not. One that passed the limit on the way is refused, as it is in a dict literal
    if fits:
        assert isinstance(run.outcome, Success) and run.output == {"k": "y" * 60000}
    else:
        assert run.output is None and not isinstance(run.outcome, Success)
        assert run.retry_error == f"line 1: the OUT{{}} fields together are past the size limit of {LIMIT}"


def test_a_field_of_out_past_the_size_limit_runs_no_field_after_the_one_that_took_it_there():
    calls: list = []
    turn = fenced(
        f's = "a" * {LIMIT // 2 + 1}',
        'OUT{a: DO echo(text=*s), b: DO echo(text=*s), c: DO echo(text="after")}',
    )
    run = _run(_actor("out-prefix", calls), turn)
    # the second field took the fields past the limit and has run; the field after it never runs
    assert [len(text) for _, text in calls] == [LIMIT // 2 + 1] * 2
    assert run.retry_error == f"line 2: the OUT{{}} fields together are past the size limit of {LIMIT}"


class Reads(Env):
    """Counts pointer reads: a filter reading *keep reads it once for each item it examines."""

    def __init__(self, **values):
        super().__init__(**values)
        self.reads = 0

    async def point(self, name):
        self.reads += 1
        return self.values[name]


@pytest.mark.parametrize(
    ("expr", "values"),
    [
        ("(x for x in *xs if *keep)", {"xs": [0] * (2 * LIMIT + 1)}),
        # neither comprehension reaches LIMIT items alone; together they examine three times that
        ("(x for x in *xs if (y for y in *ys if *keep))", {"xs": [0] * 300, "ys": [0] * (LIMIT // 100)}),
    ],
)
def test_a_long_comprehension_lets_other_tasks_run_every_limit_items_it_examines_rejected_ones_too(
    expr, values
):
    (node,) = read(fenced(f"x = {expr}")).body
    env = Reads(keep=False, **values)
    turns: list[int] = []  # the reads done by each time another task had a turn

    async def other():
        while True:
            turns.append(env.reads)
            await asyncio.sleep(0)

    async def main():
        task = asyncio.create_task(other())
        await asyncio.sleep(0)
        try:
            return await evaluate(node.expr, env)
        finally:
            task.cancel()

    assert asyncio.run(main()) == []
    gaps = [b - a for a, b in zip(turns, [*turns[1:], env.reads], strict=True)]
    assert env.reads > 2 * LIMIT and max(gaps) <= LIMIT + 1


class Count(BaseModel):
    items: list[int]


# a nested comprehension that examines 300 * `inner` items of the listings below and keeps none
LONG = "(x for x in *outer if (y for y in *inner if false))"


def _spent(turn: str, inner: int = LIMIT // 100):
    """A one-turn request over two listings and a backend whose turn arrives once its 0.2 s time
    budget is spent."""
    history = (
        Entry(seq=0, kind=Kind.RESULT, content=Result("listing", True, [0] * 300), alias="outer"),
        Entry(seq=1, kind=Kind.RESULT, content=Result("listing", True, [0] * inner), alias="inner"),
    )

    async def late(messages: list[Message]) -> str:
        await asyncio.sleep(0.3)
        return turn

    return RunRequest(inputs=("go",), history=history, time_budget=0.2, max_turns=1), late


def test_the_runs_deadline_cancels_a_command_whose_argument_is_still_a_long_comprehension():
    calls: list = []
    actor = _actor("deadline", calls)

    @actor.handler(Count)
    async def count(req: Count, ctx) -> int:
        calls.append(("count", len(req.items)))
        return len(req.items)

    run = asyncio.run(actor.run(PROGRAM, *_spent(fenced(f"n = count(items={LONG})"))))
    # three LIMITs of items to examine: the deadline beside the turn's wait cancels the command
    # inside its comprehension, which before ran to its end so that only the handler was refused
    n = run.record.get("n").content
    assert n.reason == "cancelled" and calls == []


@pytest.mark.parametrize(
    "turn",
    [(f"if not {LONG}:", '    a = echo(text="after")'), (f'OUT{{n: {LONG}, a: DO echo(text="after")}}',)],
)
def test_the_runs_deadline_stops_a_branch_test_or_an_output_field_still_in_a_long_comprehension(turn):
    calls: list = []
    run = asyncio.run(_actor("walk-deadline", calls).run(PROGRAM, *_spent(fenced(*turn))))
    # the program computes these itself, in no command: the comprehension ran to its end past the
    # deadline and the command after it was launched; the deadline stops it there now
    assert ("turn", "the time_budget of 0.2 is spent; nothing after it ran") in run.refused
    assert [e.alias for e in run.record if e.kind is Kind.RESULT] == ["outer", "inner"] and calls == []
    # the refusal is on the run's list only: the spent budget ends the run before a notification tells it,
    # and the closure names the outcome and the cancelled commands, never a refusal
    assert "nothing after it ran" not in _notices(run)


def test_a_value_whose_reader_the_deadline_cancelled_stops_computing_before_the_run_returns():
    async def main():
        run = await _actor("value-deadline", []).run(
            PROGRAM, *_spent(fenced(f"v = {LONG}", "n = echo(text=*v)"), inner=LIMIT // 10)
        )
        return run, [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    run, alive = asyncio.run(main())
    # thirty LIMITs of items: the deadline cancels the command reading *v, and the value, computed in
    # a task of its own, went on computing for nobody after the run returned
    assert run.record.get("n").content.reason == "cancelled" and alive == []


SPENT = ("turn", "the time_budget of 0.2 is spent; nothing after it ran")


@pytest.mark.parametrize("keys", [("k",), ("a", "b")], ids=["one write", "two writes"])
def test_a_write_reached_past_the_deadline_with_nothing_yielding_commits_nothing(keys):
    actor = _actor("late-write", [])
    turn = fenced(*(f'note.{key} = "v"' for key in keys))
    run = asyncio.run(actor.run(PROGRAM, *_spent(turn)))
    # the backend answered after the budget and nothing in the program yields: the deadline lands at
    # the walk's next yield, which these writes never wait for, so they were committed past it
    notes = actor.notes_for(PROGRAM)
    assert [notes.get(key) for key in keys] == [None] * len(keys) and run.notes_written == []
    assert run.refused == [SPENT]


def test_a_write_on_a_spent_budget_and_the_walks_cut_in_a_comprehension_refuse_the_turn_once():
    actor = _actor("cut-write", [])
    run = asyncio.run(actor.run(PROGRAM, *_spent(fenced('note.k = "v"', f"OUT{{n: {LONG}}}"))))
    # the budget is spent before the walk begins: the write is reached on it and refused as spent, and the
    # walk, cut inside its comprehension, finds it spent again and says nothing more
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [SPENT]


def test_a_write_refused_for_the_spent_budget_consumes_none_of_the_results_it_read():
    actor = _actor("late-read", [])

    @actor.handler(Nap)
    async def nap(req: Nap, ctx) -> float:
        time.sleep(req.seconds)  # holds the loop past the 0.2 s budget, then settles ok
        return req.seconds

    turn = fenced("n = nap(seconds=0.3)", "note.k = *n")
    run = asyncio.run(
        actor.run(PROGRAM, RunRequest(inputs=("go",), time_budget=0.2, max_turns=1), scripted(turn))
    )
    # the result is ok and the write that read it is reached after the budget: nothing is written, and the
    # result stays in view for the next notification, which only a note that took it would have hidden
    assert run.record.get("n").content.ok and actor.notes_for(PROGRAM).get("k") is None
    assert run.notes_written == [] and run.intermediates == {} and run.refused == [SPENT]


def test_a_write_reached_past_the_deadline_is_refused_as_spent_whatever_its_evaluation_would_raise():
    actor = _actor("late-failing-write", [])
    run = asyncio.run(actor.run(PROGRAM, *_spent(fenced('note.k = note["missing"]'))))
    # reading a note the store does not hold would have refused the write as "not written", and the turn
    # would have carried no word of the spent budget: the write is refused as spent before it reads
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [SPENT]


@pytest.mark.parametrize(
    "write",
    ['note.k = echo(text=note["missing"])', 'note.k = echo(text="v")'],
    ids=["an argument that would raise", "a valid command"],
)
def test_a_write_reached_past_the_deadline_launches_no_command_and_runs_no_hook(write):
    calls: list = []
    hooks: list = []
    actor = _actor("late-command-write", calls)
    actor.before(lambda run, la, req: hooks.append(la.alias))
    run = asyncio.run(actor.run(PROGRAM, *_spent(fenced(write))))
    # the command on the right side is the write's own: with the budget spent before the walk it is not
    # launched, so no argument is evaluated, no hook is called and no result lands, and the turn carries
    # the one refusal for the spent budget, where the command ran its gates before it was refused
    assert hooks == [] and calls == []
    assert [e.alias for e in run.record if e.kind is Kind.RESULT] == ["outer", "inner"]
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [SPENT]


def test_a_write_queued_with_time_left_is_refused_as_spent_whatever_its_evaluation_raises():
    actor = _actor("blocked-write", [])

    @actor.handler(Nap)
    async def nap(req: Nap, ctx) -> float:
        time.sleep(req.seconds)  # holds the loop past the 0.2 s budget, after the write was queued
        return req.seconds

    turn = fenced("n = nap(seconds=0.3)", 'note.k = note["missing"]')
    request = RunRequest(inputs=("go",), time_budget=0.2, max_turns=1)
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the budget was ahead when the write was reached and gone when its task began: reading the note the
    # store does not hold would have refused it as "not written", and the turn would not have said so
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [SPENT]


class Faulty(KhiveNoteStore):
    """A store whose reads raise `error`."""

    def __init__(self, error: Exception) -> None:
        super().__init__(LionKhive(), "p")
        self.error = error

    def get(self, key):
        raise self.error


class Nap(BaseModel):
    seconds: float


def test_a_walk_whose_settle_raises_still_stops_the_value_it_was_computing():
    profile = Profile("p", language="program", notes=Faulty(RuntimeError("the store is down")))
    turn = fenced(f"v = {LONG}", "n = echo(text=*v)", 'note.k = note["j"]')
    listings, _ = _spent(turn, inner=LIMIT)  # v takes some 20 s to compute here; the backend is prompt
    # 2 s ahead: a loaded machine still starts the write before the deadline, and v still computes at it
    request = RunRequest(inputs=("go",), history=listings.history, time_budget=2, max_turns=1)

    async def main():
        actor = _actor("settle-raises", [])
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="the store is down"):
            await actor.run(profile, request, scripted(turn))
        return time.monotonic() - started, [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    # the write reads the store, with the budget still ahead, and fails in its own task, which the settle
    # raises after the deadline cancelled the command reading *v; the value, computed in a task of its own,
    # was left computing for nobody once the walk raised. A comprehension is the one computation that
    # yields on its own account, and so the one that outlives the readers a spent budget cancels: a value
    # of commands ends at its next step, which the spent budget refuses
    elapsed, left = asyncio.run(main())
    assert left == [] and 2 <= elapsed < 15  # raised at the deadline, far short of v's own end


def test_a_walk_whose_settle_raises_stops_a_value_waiting_on_something_no_budget_ends():
    profile = Profile("p", language="program", notes=Faulty(RuntimeError("the store is down")))

    async def main():
        actor = _actor("settle-raises-waiting", [])
        run = actor_module.Run(actor, profile, RunRequest(inputs=("go",), time_budget=100))
        walk = actor_module._Walk(actor, run, actor.operable, 1)
        waiting = walk.computed["v"] = asyncio.ensure_future(asyncio.Event().wait())
        try:
            with pytest.raises(RuntimeError, match="the store is down"):
                await walk.execute(read(fenced('note.k = note["j"]')))
            await asyncio.sleep(0)
            return waiting.cancelled()
        finally:
            waiting.cancel()

    # the value was computing when the walk began, and awaits an event that never comes: what outlives
    # its readers does not depend on what a comprehension yields, and the walk stops it as it raises
    assert asyncio.run(main())


@pytest.mark.parametrize("budget", [60.0, None])
def test_a_timeout_error_from_a_store_read_is_that_error_not_the_time_budget_spent(budget):
    profile = Profile("p", language="program", notes=Faulty(TimeoutError("the store did not answer")))
    request = RunRequest(inputs=("go",), time_budget=budget, max_turns=1)
    # the deadline is far off or there is none: nothing here expired, so the walk's error is the error
    # raised, never a refusal telling the budget is spent and never a format of a budget that is None
    with pytest.raises(TimeoutError, match="the store did not answer"):
        asyncio.run(_actor("store-timeout", []).run(profile, request, scripted(fenced('OUT{n: note["k"]}'))))


def test_a_time_extension_granted_inside_the_walk_moves_the_deadline_the_walk_runs_under():
    actor = _actor("extended", [])

    @actor.handler(Nap)
    async def nap(req: Nap, ctx) -> float:
        await asyncio.sleep(req.seconds)
        return req.seconds

    grant = 'DO run.more(bound="time_budget", amount=5, reason="one more step")'
    turn = fenced(f"OUT{{granted: {grant}, after: DO nap(seconds=0.8)}}")
    request = RunRequest(inputs=("go",), time_budget=0.4, max_turns=1)
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the nap after the grant ends 0.8 s in, past the 0.4 s the run began with: the walk's deadline moved
    # with the bound, so the output is built where the old deadline would have cut it
    assert run.elapsed > 0.4
    assert isinstance(run.outcome, Success)
    assert run.output == {"granted": "time_budget raised from 0.4 to 5.4", "after": 0.8}


def test_the_same_walk_without_the_grant_is_cut_at_the_deadline_it_began_with():
    request = RunRequest(inputs=("go",), time_budget=0.4, max_turns=1)
    run = asyncio.run(
        _nap_actor("unextended").run(PROGRAM, request, scripted(fenced("OUT{after: DO nap(seconds=0.8)}")))
    )
    # the walk waits on the same nap and nothing moved its deadline: it is cut at 0.4 s with the budget
    # refusal, and no output is built, where a walk with no deadline would have built it at 0.8 s
    assert run.refused == [("turn", "the time_budget of 0.4 is spent; nothing after it ran")]
    assert run.output is None and not isinstance(run.outcome, Success) and run.elapsed < 0.8


def test_a_write_the_settlement_cut_while_it_waited_for_its_command_is_refused_as_spent_once():
    request = RunRequest(inputs=("go",), time_budget=0.1, max_turns=1)
    actor = _nap_actor("settle-cut-write")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(fenced("note.k = nap(seconds=1)"))))
    # the walk ends at once and the write, a task of its own, waits for the nap: the settlement's deadline
    # cancels both, and the write is a write the budget cut, told as the walk's own cut is
    (nap,) = [e.content for e in run.record if e.kind is Kind.RESULT]
    assert nap.reason == "cancelled" and run.elapsed < 1
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [("turn", "the time_budget of 0.1 is spent; nothing after it ran")]
    assert run.outcome == Exhausted(1, "time")


def test_a_write_the_settlement_cut_after_a_grant_names_the_budget_the_settlement_ran_under():
    turn = fenced(
        'a = echo(text=DO run.more(bound="time_budget", amount=0.2, reason="a little more"))',
        "note.k = nap(seconds=2)",
    )
    request = RunRequest(inputs=("go",), time_budget=0.3, max_turns=1)
    actor = _nap_actor("settle-cut-granted")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the grant landed while the settlement waited and moved its deadline: the cut is at 0.5 s, and the
    # refusal names that budget, with no grant "after the deadline" beside it, for none came after
    assert run.bounds["time_budget"] == 0.5 and 0.5 <= run.elapsed < 2
    assert actor.notes_for(PROGRAM).get("k") is None
    assert run.refused == [("turn", "the time_budget of 0.5 is spent; nothing after it ran")]


def test_a_write_the_settlement_cut_names_the_budget_it_ran_under_when_a_grant_lands_after_the_cut():
    async def extend(run, bound, amount, reason) -> bool:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass  # the settlement's deadline cancels the request, and it is granted anyway
        return True

    turn = fenced('g = run.more(bound="time_budget", amount=5, reason="too late")', "note.k = nap(seconds=2)")
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=extend)
    actor = _nap_actor("settle-cut-late-grant")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the cut at 0.5 s cancels the write's command and the request; the request lands the grant after it, so
    # the bound reads 5.5 when the cut is told, and the refusal names the 0.5 s the settlement ran under
    late = "the time_budget of 0.5 is spent, and a grant of 5 landed after the deadline"
    assert run.bounds["time_budget"] == 5.5 and run.elapsed < 2
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [("turn", f"{late}; nothing after it ran")]


def test_an_output_built_before_a_late_grant_commits_under_the_raised_bound_when_no_write_was_cut():
    async def extend(run, bound, amount, reason) -> bool:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass  # the settlement's deadline cancels the request, and it is granted anyway
        return True

    turn = fenced('g = run.more(bound="time_budget", amount=5, reason="too late")', 'OUT{k: "v"}')
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=extend)
    actor = _nap_actor("settle-late-grant-output")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the cut cancels the request and no write waited on it: nothing is refused, and the output the walk had
    # built is checked against the bound the late grant raised, and commits
    assert run.bounds["time_budget"] == 5.5 and run.elapsed < 2
    assert isinstance(run.outcome, Success) and run.output == {"k": "v"} and run.refused == []


def test_an_output_built_before_a_late_grant_commits_nothing_when_the_settlement_cut_a_write():
    async def extend(run, bound, amount, reason) -> bool:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass  # the settlement's deadline cancels the request, and it is granted anyway
        return True

    turn = fenced(
        'g = run.more(bound="time_budget", amount=5, reason="too late")',
        "note.k = nap(seconds=2)",
        'OUT{k: "v"}',
    )
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=extend)
    actor = _nap_actor("settle-cut-late-grant-output")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the cut cancels the write's command and the request, and the grant lands after it and raises the
    # bound: the output the walk built before the cut goes with the write, never checked against the
    # raised bound, so nothing the walk held commits
    late = "the time_budget of 0.5 is spent, and a grant of 5 landed after the deadline"
    assert run.bounds["time_budget"] == 5.5 and run.elapsed < 2
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.output is None and not isinstance(run.outcome, Success)
    assert run.refused == [("turn", f"{late}; nothing after it ran")]


def test_a_write_queued_behind_a_grant_that_held_the_settlement_past_its_deadline_commits_nothing():
    def extend(run, bound, amount, reason) -> bool:
        time.sleep(0.7)  # a synchronous grant: the settlement's deadline passes under it, unfired
        return True

    turn = fenced('g = run.more(bound="time_budget", amount=5, reason="held")', 'note.k = "v"', 'OUT{k: "v"}')
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=extend)
    actor = _nap_actor("settle-held-grant-write")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the walk ends in time and the settlement runs the grant first: it holds the loop past the settlement's
    # deadline, raises the bound and fires the limit, and the write queued behind it runs before the cut
    # lands. The fired deadline spends the walk whatever the grant raised: the write commits nothing, the
    # output goes with it, and the turn names the budget the settlement ran under and the late grant
    late = "the time_budget of 0.5 is spent, and a grant of 5 landed after the deadline"
    assert run.bounds["time_budget"] == 5.5
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.output is None and not isinstance(run.outcome, Success)
    assert run.refused == [("turn", f"{late}; nothing after it ran")]


def test_a_command_a_write_queued_behind_a_held_grant_names_never_enters_its_handler():
    def extend(run, bound, amount, reason) -> bool:
        time.sleep(0.7)  # a synchronous grant: the settlement's deadline passes under it, unfired
        return True

    ran: list[float] = []
    actor = _actor("settle-held-grant-command", [])

    @actor.handler(Nap)
    async def nap(req: Nap, ctx) -> float:
        ran.append(req.seconds)  # the effect a command on a write's right side has on the world
        return req.seconds

    turn = fenced('g = run.more(bound="time_budget", amount=5, reason="held")', "note.k = nap(seconds=0)")
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=extend)
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the write launched its command before the grant held the loop past the settlement's deadline: the
    # command reaches its handler after the grant raised the bound, and the walk's fired deadline, not the
    # raised bound, decides it, so the handler never runs and the note is never written
    late = "the time_budget of 0.5 is spent, and a grant of 5 landed after the deadline"
    assert run.bounds["time_budget"] == 5.5
    assert ran == [] and actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [("r2.1", HALTED), ("turn", f"{late}; nothing after it ran")]


HALTED = "refused: the time budget its turn ran under is spent; a later grant does not reach it"


def test_a_command_queued_behind_a_held_grant_is_refused_alone_and_the_output_commits():
    def extend(run, bound, amount, reason) -> bool:
        time.sleep(0.7)  # a synchronous grant: the settlement's deadline passes under it, unfired
        return True

    ran: list[float] = []
    actor = _actor("settle-held-grant-bare-command", [])

    @actor.handler(Nap)
    async def nap(req: Nap, ctx) -> float:
        ran.append(req.seconds)
        return req.seconds

    turn = fenced(
        'g = run.more(bound="time_budget", amount=5, reason="held")', "n = nap(seconds=0)", 'OUT{k: "v"}'
    )
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=extend)
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # no write waits on the command: its refusal is its own result, as a cut command's cancellation is,
    # and the output the walk built commits under the raised bound
    assert ran == [] and run.record.get("n").content.reason == "refused"
    assert run.output == {"k": "v"} and isinstance(run.outcome, Success)
    assert run.refused == [("n", HALTED)]


def test_a_grant_landing_as_the_settlements_deadline_fires_is_named_beside_the_budget_it_ran_under():
    async def extend(run, bound, amount, reason) -> bool:
        while not any(limit.expired() for limit in run.timeouts):
            await asyncio.sleep(0)  # the settlement's deadline has not fired
        return True  # it has, and the settlement has not yet left its limit: the grant lands here

    turn = fenced(
        'g = run.more(bound="time_budget", amount=5, reason="as it fires")', "note.k = nap(seconds=2)"
    )
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=extend)
    actor = _nap_actor("settle-cut-fired-grant")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(turn)))
    # the grant raised the bound to 5.5 between the deadline firing and the settlement noticing: the budget
    # the settlement ran under is the one it began with, whatever the bound reads by then
    late = "the time_budget of 0.5 is spent, and a grant of 5 landed after the deadline"
    assert run.bounds["time_budget"] == 5.5 and run.elapsed < 2
    assert actor.notes_for(PROGRAM).get("k") is None and run.notes_written == []
    assert run.refused == [("turn", f"{late}; nothing after it ran")]


def test_a_command_the_settlement_cut_is_a_cancelled_result_and_not_a_refusal_of_the_turn():
    request = RunRequest(inputs=("go",), time_budget=0.1, max_turns=1)
    actor = _nap_actor("settle-cut-command")
    run = asyncio.run(actor.run(PROGRAM, request, scripted(fenced("n = nap(seconds=1)"))))
    # only a write the budget cut is the turn's spent budget: a command the settlement cancels settles as
    # a cancelled RESULT, which is what it says already
    assert run.record.get("n").content.reason == "cancelled" and run.refused == []


GRANT_IN_AN_ARGUMENT = (
    'a = echo(text=DO run.more(bound="time_budget", amount=5, reason="one more nap"))',
    "b = nap(seconds=1)",
)


def _nap_actor(name: str) -> Actor:
    actor = _actor(name, [])

    @actor.handler(Nap)
    async def nap(req: Nap, ctx) -> float:
        await asyncio.sleep(req.seconds)
        return req.seconds

    return actor


def test_a_grant_of_time_by_a_commands_argument_moves_the_deadline_its_turns_settle_waits_under():
    turn = fenced(*GRANT_IN_AN_ARGUMENT, 'OUT{done: "yes"}')
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1)
    run = asyncio.run(_nap_actor("settle-grant").run(PROGRAM, request, scripted(turn)))
    # the output is a literal, so the walk waits for no command: its own limit is gone when the turn's
    # commands settle, and the argument of `a` grants during that wait: the grant extended the settle, so
    # the nap finished that the 0.5 s the run began with would have cancelled
    assert run.bounds["time_budget"] == 5.5
    assert run.resolve("b") == 1 and run.elapsed > 0.5
    assert isinstance(run.outcome, Success) and run.output == {"done": "yes"}
    assert run.timeouts == set()


def test_a_refused_grant_during_settlement_leaves_the_deadline_and_the_late_command_cancelled():
    request = RunRequest(inputs=("go",), time_budget=0.5, max_turns=1, extend=lambda *ask: False)
    run = asyncio.run(
        _nap_actor("settle-refused").run(PROGRAM, request, scripted(fenced(*GRANT_IN_AN_ARGUMENT)))
    )
    # nothing was granted, so the settle's deadline is the one it began with: the nap is cancelled there
    assert run.bounds["time_budget"] == 0.5
    assert run.resolve("b").reason == "cancelled" and 0.5 <= run.elapsed < 1
    assert run.outcome == Exhausted(1, "time") and run.timeouts == set()


@pytest.mark.parametrize(
    "first", [('a = echo(text="x")',), ("if 1 / 0:", '    a = echo(text="x")')], ids=["settled", "refused"]
)
def test_a_walk_leaves_no_time_limit_behind_however_it_ends_so_a_later_grant_of_time_lands(first):
    grant = 'DO run.more(bound="time_budget", amount=5, reason="one more step")'
    request = RunRequest(inputs=("go",), time_budget=60.0, max_turns=2)
    turns = scripted(fenced(*first), fenced(f"OUT{{granted: {grant}}}"))
    run = asyncio.run(_actor("limits", []).run(PROGRAM, request, turns))
    # a limit left over from the first turn cannot be moved, and the grant would fail on it
    assert isinstance(run.outcome, Success) and run.output == {
        "granted": "time_budget raised from 60.0 to 65.0"
    }
    assert run.timeouts == set()


class Grid(BaseModel):
    n: int


class Take(BaseModel):
    rows: list[dict]


@pytest.mark.parametrize(
    "lines",
    [
        ("g = grid(n=2)", "OUT{x: (x for x in *g)}"),
        ("g = grid(n=2)", "if [*g[0]]:", '    OUT{x: "taken"}'),
        ("g = grid(n=2)", "a = take(rows=*g)", "OUT{x: *a}"),
    ],
)
def test_a_dict_whose_keys_are_not_strings_counts_each_key_as_one_item(lines):
    actor = _actor("intkeys", [])

    @actor.handler(Grid)
    async def grid(req: Grid, ctx) -> list[dict]:
        return [{i: f"row{i}"} for i in range(req.n)]

    @actor.handler(Take)
    async def take(req: Take, ctx) -> int:
        return len(req.rows)

    run = _run(actor, fenced(*lines), "idle")
    assert isinstance(run.outcome, Success), _notices(run)
    assert run.output["x"] in ([{0: "row0"}, {1: "row1"}], "taken", [{0: "row0"}], 2)


def test_a_repair_is_counted_when_the_check_then_refuses_the_program():
    actor = _actor("repaired-then-refused", [])
    run = _run(actor, fenced('a = search_similar(input="Dune" candidates=[])', 'b = serch(text="y")'), "idle")
    assert run.repairs == {"comma": 1} and "line 2: unknown command 'serch'" in _notices(run)
    assert not [e for e in run.record if e.kind is Kind.RESULT]


def test_a_result_read_through_a_value_is_consumed_only_when_the_values_reader_settles_ok():
    actor = _actor("through-a-value", [])
    turn = fenced(
        'a = echo(text="x")',
        "v = *a",
        "b = echo(text=*v + 3)",
        'c = echo(text="y")',
        "w = *c",
        "d = echo(text=*w)",
    )
    run = _run(actor, turn, "idle")
    assert run.record.get("b").content.reason == "invalid" and run.record.get("d").content.value == "y"
    hides = [d.refs for e in run.record if e.kind is Kind.SYSTEM for d in e.meta.get("consumed", ())]
    assert hides == [("c",)]


def test_an_unnamed_command_skips_an_auto_name_the_record_already_holds():
    calls: list = []
    actor = _actor("taken", calls)
    held = (Entry(seq=0, kind=Kind.RESULT, content=Result("earlier", True, "kept"), alias="r3.0"),)
    run = _run(actor, fenced('note.k = echo(text="a")'), "idle", history=held)
    assert run.record.get("r3.0").content.value == "kept" and run.record.get("r3.1").content.value == "a"
    assert calls == [("echo", "a")]
