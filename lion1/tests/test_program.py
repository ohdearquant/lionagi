"""The program form (ADR-0008): read whole, checked before anything runs, evaluated over data."""

import asyncio
import json
import re

import pytest
from pydantic import BaseModel

from hub.chat import entry_row
from lionagi import Actor, Entry, Exhausted, Kind, Message, Profile, RunRequest, Success
from lionagi.actor import ProfileError, Result
from lionagi.checkpoint import entry_of
from lionagi.lndl import LNDLError
from lionagi.notes import FileNoteStore, MemoryNoteStore
from lionagi.program import LIMIT, Command, EvalError, Value, Write, evaluate, guidance, read

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
    actor = Actor(name)

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
    request.setdefault("max_rounds", len(turns))
    return asyncio.run(actor.run(profile, RunRequest(**request), scripted(*turns, seen=seen)))


def _notices(run) -> str:
    return "\n".join(e.content for e in run.record if e.kind is Kind.SYSTEM)


def test_the_records_example_resolves_in_one_turn():
    calls: list = []
    actor = _actor("example", calls)
    seen: list = []
    run = _run(actor, fenced(*EXAMPLE), history=HISTORY, seen=seen)
    assert isinstance(run.outcome, Success) and run.round == 1 and len(seen) == 1
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
    assert [e.round for e in run.record if e.kind is Kind.RESULT and e.content.value == "deferred"] == [2, 2]
    assert sorted(calls) == [("echo", "deferred"), ("echo", "deferred"), ("echo", "kept")]
    assert "later is bound already: a name is bound once for the whole run" in _notices(run)


def test_a_value_whose_input_failed_fails_every_reader():
    calls: list = []
    actor = _actor("value-fails", calls)
    run = _run(actor, fenced("a = echo(text=3)", "v = *a", "b = echo(text=*v)"), "idle")
    assert run.record.get("a").content.reason == "invalid"
    b = run.record.get("b").content
    assert b.reason == "invalid" and b.error.startswith("*v: *a did not settle ok") and calls == []


class CountingStore(MemoryNoteStore):
    def __init__(self) -> None:
        super().__init__()
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
    profile = Profile("p", language="program", notes=FileNoteStore(tmp_path / "p.json"))
    profile.notes.put("k", "a string", run="earlier", seq=0)
    run = _run(actor, fenced('note.k.part = echo(text="x")'), "idle", profile=profile)
    assert profile.notes.get("k").value == "a string"
    assert "note.k.part: not written: EvalError: the note holds str where 'part' is written" in _notices(run)
    assert [e.content.ok for e in run.record if e.kind is Kind.RESULT] == [True]


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


def test_a_result_the_output_names_is_never_consumed():
    actor = _actor("named", [])
    turn = fenced('a = echo(text="kept")', "b = echo(text=*a)", "OUT{x: *a}")
    run = _run(actor, turn, fenced('OUT{x: "again"}'), accept=lambda r, o: "no" if o["x"] == "kept" else None)
    # `b` read `a` and settled ok, but OUT{} points at `a`: the refused candidate's next turn sees it whole
    assert [e.meta.get("consumed") for e in run.record if e.kind is Kind.SYSTEM] == [None, None]
    assert run.round == 2 and run.output == {"x": "again"}


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


def test_an_arithmetic_result_too_large_for_a_number_is_a_language_error_not_a_crash():
    run = _run(_actor("overflow", []), fenced(f"OUT{{x: {'9' * 401} / 1}}"), "idle")
    assert run.outcome == Exhausted(2, "rounds")
    assert "line 1: the result of / is too large for a number" in _notices(run)


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
    text = guidance(("done",), commands=["echo", "context.show"])
    assert "return {done: *t1}" in text and "outputs: done" in text and "*name or context.show" in text
    bare = guidance(commands=["echo"])
    assert "outputs: listed below" in bare and "context.show" not in bare
    assert "*name or context.show" in guidance()


def test_a_core_profile_runs_nothing_from_a_fenced_program():
    calls: list = []
    run = _run(_actor("core-fence", calls), fenced('a = echo(text="x")'), "idle", profile=Profile("p"))
    assert calls == [] and "your last turn contained no LNDL, so nothing was executed" in _notices(run)


def test_a_turn_without_a_block_is_idle_and_under_a_conversation_is_the_reply():
    run = _run(_actor("idle", []), "thinking out loud", "still thinking")
    assert "your last turn contained no LNDL" in _notices(run)
    replies: list = []

    async def wait(run):
        replies.append(run.round)
        return False

    run = _run(_actor("chat", []), "hello there", "unused", wait=wait)
    assert replies == [2] and run.outcome.__class__.__name__ == "Stopped"


def test_two_fenced_blocks_are_refused():
    run = _run(_actor("two-blocks", []), fenced("a = 1") + fenced("b = 2"), "idle")
    assert "2 fenced blocks: a turn is one program in one block" in _notices(run)


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
    ],
)
def test_the_evaluator_names_what_it_could_not_compute(expr, said):
    with pytest.raises(EvalError, match=said.replace("[", r"\[").replace("*", r"\*")):
        value(
            expr,
            r=[{"title": "t"}],
            s="abc",
            m=Book(title="Dune", year=1965),
            big=list(range(LIMIT // 2 + 1)),
        )


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


class Grid(BaseModel):
    n: int


class Take(BaseModel):
    rows: list[dict]


@pytest.mark.parametrize(
    "lines",
    [
        ("g = grid(n=2)", "OUT{x: (x for x in *g)}"),
        ("g = grid(n=2)", "if [*g[0]]:", '    OUT{x: "taken"}'),
        ("g = grid(n=2)", 'note["k"] = [*g[0]]', 'OUT{x: *note["k"]}'),
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
