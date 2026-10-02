"""A conversation: a turn without a command is the reply and the loop waits for the next message; a
turn with commands runs them and reads the results before replying; every message is in one record
that the model sees whole; the person ends it, as Stopped; and a log read back is the run's history."""

import asyncio
import json
import os
import re
import shlex
import shutil
import time

import pytest

from hub import files
from hub.chat import chat, entry_row, load_log
from lionagi import Actor, Kind, Profile, RunRequest
from lionagi.actor import DEFAULT_HANDLERS, Message, Stopped, _messages
from lionagi.checkpoint import CheckpointError, load, save, take, to_history
from tests.test_actor import scripted


def asks(*messages: str | None):
    it = iter(messages)

    async def ask() -> str | None:
        return next(it)

    return ask


def reader(directory) -> tuple[Actor, Profile]:
    actor = Actor("c", notes_dir=directory / "notes")
    specs = files(actor, directory) | {h.name for h in DEFAULT_HANDLERS}
    return actor, Profile(name="c", specs=frozenset(specs), system="Reply in plain text. Never write OUT{}.")


def test_a_plain_turn_is_the_reply_and_the_next_message_is_in_the_same_record(tmp_path):
    seen: list[list[Message]] = []
    inner = scripted("Hello. What would you like?", "Two, then.", "Bye.")

    async def backend(messages):
        seen.append(messages)
        return await inner(messages)

    actor, profile = reader(tmp_path)
    said: list = []
    run = asyncio.run(chat(actor, profile, backend, asks("hi", "how many?", "/quit"), say=said.append))
    assert isinstance(run.outcome, Stopped) and run.round == 2
    texts = [e.content for e in run.record if e.kind is Kind.TEXT]
    assert texts == ["Hello. What would you like?", "Two, then."]  # the third turn was never asked for
    inputs = [(e.content, e.meta.get("origin")) for e in run.record if e.kind is Kind.INPUT]
    assert inputs == [("hi", None), ("how many?", "console")]
    # the second call's view carries the whole conversation so far, and no idleness nag
    rendered = "\n".join(m.content for m in seen[1])
    assert "hi" in rendered and "Hello. What would you like?" in rendered and "how many?" in rendered
    assert "no LNDL" not in rendered and "nothing was executed" not in rendered
    # every entry said once, in order, as the record grew
    assert [e.seq for e in said] == [e.seq for e in run.record] == list(range(len(run.record)))


def test_a_turn_with_a_command_reads_its_result_before_replying_and_the_tools_read_the_directory(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "sub").mkdir()
    actor, profile = reader(tmp_path)
    backend = scripted(
        '<lact l1>list_dir(path=".")</lact>',
        "There are two entries: a.txt and sub/.",
        '<lact r1>read(path="a.txt")</lact>',
        "It says alpha.",
    )
    run = asyncio.run(chat(actor, profile, backend, asks("what is here?", "read a.txt", None)))
    assert isinstance(run.outcome, Stopped) and run.round == 4
    results = [e.content for e in run.record if e.kind is Kind.RESULT]
    assert [r.value for r in results] == [["a.txt", "sub/"], "alpha\n"]
    texts = [e.content for e in run.record if e.kind is Kind.TEXT]
    assert texts[1] == "There are two entries: a.txt and sub/." and texts[3] == "It says alpha."


def test_a_log_read_back_is_the_history_and_the_old_aliases_still_resolve(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    log = tmp_path / "one.jsonl"
    actor, profile = reader(tmp_path)
    first = scripted("<lvar p1>a.txt</lvar><lact r1>read(path=*p1)</lact>", "It says alpha.")
    with log.open("w") as fh:
        asyncio.run(
            chat(
                actor,
                profile,
                first,
                asks("read a.txt", None),
                say=lambda e: fh.write(json.dumps(entry_row(e), default=str) + "\n"),
            )
        )
    history = load_log(log)
    assert [e.kind.name for e in history][:4] == ["INPUT", "SYSTEM", "TEXT", "RESULT"]
    assert history[3].content.ok and history[3].content.value == "alpha\n"
    seen = []

    async def second(messages):
        seen.append(messages)
        turns = ["<lact r2>read(path=*p1)</lact>", "Still alpha; *r1 was too."]
        return turns[len(seen) - 1]

    actor2, profile2 = reader(tmp_path)
    run = asyncio.run(chat(actor2, profile2, second, asks("again?", None), history=history))
    assert run.record.by_seq(0).content == "read a.txt"
    assert run.record.by_seq(len(history)).content == "again?"
    rendered = "\n".join(m.content for m in seen[0])
    assert "read a.txt" in rendered and "It says alpha." in rendered  # the old conversation is in the view
    results = [e.content for e in run.record if e.kind is Kind.RESULT and e.seq >= len(history)]
    assert results[0].ok and results[0].value == "alpha\n"  # *p1 from the old session resolved
    assert run.resolve("r1") == "alpha\n"  # the old result's alias too


def test_a_paused_run_hands_the_turn_back_at_the_next_round_even_though_it_had_not_replied(tmp_path):
    """`run.pause` is the person's interrupt: the round in flight finishes, its results land, and the
    wait hook is called as if the model had replied; the person's next message continues the run, and
    the record holds every entry when the run ends."""
    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = reader(tmp_path)
    backend = scripted(
        '<lact r1>read(path="a.txt")</lact>',
        '<lact r2>list_dir(path=".")</lact>',
        "You spoke, so I answer: alpha.",
    )

    def on_round(ev):
        if ev.payload.get("round") == 2:
            actor.runs[ev.payload["run"]].pause = True  # pressed during round 2: it still runs whole

    off = actor.bus.subscribe("round.started", on_round)
    said: list = []
    try:
        run = asyncio.run(chat(actor, profile, backend, asks("go", "still there?", None), say=said.append))
    finally:
        off()
    assert isinstance(run.outcome, Stopped) and run.round == 3
    results = [e.alias for e in run.record if e.kind is Kind.RESULT]
    assert results == ["r1", "r2"]  # round 2's command ran and settled before the turn came back
    inputs = [(e.content, e.round) for e in run.record if e.kind is Kind.INPUT]
    assert inputs == [("go", 0), ("still there?", 3)]
    assert [e.seq for e in said] == list(range(len(run.record)))


def test_an_interrupt_during_the_call_cuts_it_now_and_the_person_speaks_next_while_the_run_goes_on(
    tmp_path,
):
    """`run.interrupt()` with a backend call in flight cancels that call: the round ends with a notice
    and no turn, the wait hook asks the person, and their next message continues the same run. The
    control: an interrupt while no call is in flight only pauses (False), and a real cancellation of
    the run still propagates through the same await."""
    actor, profile = reader(tmp_path)
    started = asyncio.Event()
    calls: list[str] = []
    cancelled: list[bool] = []

    async def backend(messages):
        calls.append("\n".join(m.content for m in messages))
        if len(calls) == 1:
            started.set()
            try:
                await asyncio.sleep(30)  # the model working, cut short
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return "Back to you."

    interrupted: list[bool] = []

    async def cut_when_started():
        await started.wait()
        run = next(iter(actor.runs.values()))
        interrupted.append(run.interrupt())

    async def main():
        cutter = asyncio.ensure_future(cut_when_started())
        try:
            return await asyncio.wait_for(
                chat(actor, profile, backend, asks("go", "still there?", None)), timeout=5
            )
        finally:
            cutter.cancel()

    run = asyncio.run(main())
    assert interrupted == [True] and cancelled == [True]
    assert isinstance(run.outcome, Stopped)
    notices = [e.content for e in run.record if e.kind is Kind.SYSTEM and "cut this round short" in e.content]
    assert len(notices) == 1
    texts = [e.content for e in run.record if e.kind is Kind.TEXT]
    assert texts == ["Back to you."]  # no turn from the cut round; the next call answered the person
    inputs = [e.content for e in run.record if e.kind is Kind.INPUT]
    assert inputs == ["go", "still there?"]
    assert "still there?" in calls[1]
    # control 1: nothing in flight means a pause, not a cut
    assert run.interrupt() is False and run.pause is True
    # control 2: cancelling the run itself still ends it through the same await
    actor2, profile2 = reader(tmp_path)

    async def slow(messages):
        await asyncio.sleep(30)

    async def cancel_run():
        task = asyncio.ensure_future(chat(actor2, profile2, slow, asks("go", None)))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())


@pytest.mark.parametrize("order", ("interrupt then cancel", "cancel then interrupt"))
def test_a_cancel_of_the_run_that_lands_with_an_interrupt_is_the_cancel(tmp_path, order):
    """Both arrive before the loop wakes, so one CancelledError reaches it with `run.cut` set. The run's
    own cancellation wins: the task ends cancelled with no outcome, no second call is made and the
    wait is never asked. Were it read as the cut alone, the run would go on to its wait and the cancel
    would be lost."""
    actor, profile = reader(tmp_path)
    started = asyncio.Event()
    calls: list[int] = []
    asked: list[int] = []

    async def backend(messages):
        calls.append(len(messages))
        started.set()
        await asyncio.sleep(30)

    async def wait(run):
        asked.append(run.round)
        await asyncio.sleep(30)
        return False

    async def main():
        task = asyncio.ensure_future(actor.run(profile, RunRequest(inputs=("go",), wait=wait), backend))
        await started.wait()
        run = next(iter(actor.runs.values()))
        if order == "interrupt then cancel":
            assert run.interrupt() is True
            task.cancel()
        else:
            task.cancel()
            assert run.interrupt() is True  # the call is cancelled and has not ended yet
        done, _ = await asyncio.wait({task}, timeout=2)
        ended = bool(done)
        while not task.done():  # a run that missed the cancel is still going: end it so the asserts report it
            task.cancel()
            await asyncio.wait({task}, timeout=2)
        return run, task, ended

    run, task, ended = asyncio.run(main())
    assert ended and task.cancelled()
    assert run.outcome is None and len(calls) == 1 and asked == []
    assert run.id not in actor.runs


def test_a_console_notice_reaches_the_model_as_inbound_in_the_first_round(tmp_path):
    actor, profile = reader(tmp_path)
    seen = []

    async def backend(messages):
        seen.append(messages)
        return "Noted: r9 never settled."

    run = asyncio.run(
        chat(actor, profile, backend, asks("hi", None), notice="the previous session left r9 unsettled")
    )
    rendered = "\n".join(m.content for m in seen[0])
    assert "r9 unsettled" in rendered and "hi" in rendered
    inputs = [(e.content, e.meta.get("origin")) for e in run.record if e.kind is Kind.INPUT]
    assert inputs == [("hi", None), ("the previous session left r9 unsettled", "console")]


def test_context_directives_ride_the_log_so_a_resumed_view_folds_the_same(tmp_path):
    from lionagi.record import fold

    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    actor, profile = reader(tmp_path)
    first = scripted(
        '<lact a1>read(path="a.txt")</lact><lact a2>read(path="b.txt")</lact>',
        '<lact h1>context.hide(refs=["a1"])</lact>'
        '<lact s1>context.summarize(refs=["a2"], text="b was beta")</lact>',
        "Folded.",
    )
    log = tmp_path / "one.jsonl"
    with log.open("w") as fh:
        run1 = asyncio.run(
            chat(
                actor,
                profile,
                first,
                asks("read both", None),
                say=lambda e: fh.write(json.dumps(entry_row(e)) + "\n"),
            )
        )
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    kept = [r["meta"]["directive"] for r in rows if "directive" in r["meta"]]
    assert [d["op"] for d in kept] == ["hide", "summarize"] and kept[1]["text"] == "b was beta"
    history = load_log(log)
    actor2, profile2 = reader(tmp_path)
    run2 = asyncio.run(chat(actor2, profile2, scripted("Still folded."), asks("and?", None), history=history))

    def shape(record):
        view = fold(record)
        return (
            {v.entry.seq for v in view if v.hidden},
            [(v.entry.seq, v.summary) for v in view if v.summary],
            {v.entry.seq for v in view if not v.hidden and v.summary is None},
        )

    hidden1, summaries1, shown1 = shape(run1.record)
    hidden2, summaries2, shown2 = shape(run2.record)
    assert hidden1 == hidden2 and summaries1 == summaries2 == [(4, "b was beta")]
    assert shown1 == {s for s in shown2 if s < len(history)}  # the old view, exactly; new entries after it


def test_a_log_cut_mid_write_recovers_its_whole_lines_and_names_the_cut(tmp_path):
    import pytest

    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = reader(tmp_path)
    log = tmp_path / "cut.jsonl"
    with log.open("w") as fh:
        asyncio.run(
            chat(
                actor,
                profile,
                scripted("Hi."),
                asks("hello", None),
                say=lambda e: fh.write(json.dumps(entry_row(e)) + "\n"),
            )
        )
    whole = load_log(log)
    log.write_text(log.read_text() + '{"seq": 99, "kind": "TEXT", "con')  # a write the process died inside
    torn: list[str] = []
    assert load_log(log, torn) == whole and len(torn) == 1 and "a write that was cut" in torn[0]
    with pytest.raises(ValueError, match="the last line is not JSON"):
        load_log(log)
    lines = log.read_text().splitlines()
    log.write_text("\n".join([lines[0], "{broken", *lines[1:]]) + "\n")
    with pytest.raises(ValueError, match="line 2, with"):
        load_log(log, [])


def test_each_entry_is_said_as_it_lands_not_at_the_next_round_boundary(tmp_path):
    # a slow tool beside an instant one: the turn and the instant result reach the log while the
    # slow one still runs; a signal in that window used to lose them and the resume notice went
    # silent about a command that had run
    from pydantic import BaseModel

    from lionagi import Actor, Context, Profile

    class Fast(BaseModel):
        pass

    class Slow(BaseModel):
        pass

    actor = Actor("c", notes_dir=tmp_path / "notes")
    said: list = []
    seen: list = []

    @actor.handler(Fast)
    async def fast(req: Fast, ctx: Context) -> str:
        return "quick"

    @actor.handler(Slow)
    async def slow(req: Slow, ctx: Context) -> str:
        await asyncio.sleep(0.2)
        seen.append([(e.kind.name, e.alias) for e in said])
        return "late"

    specs = {"fast", "slow"} | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="c", specs=frozenset(specs), system="x")
    backend = scripted("<lact f1>fast()</lact><lact s1>slow()</lact>", "done")
    run = asyncio.run(chat(actor, profile, backend, asks("go", None), say=said.append))
    assert ("TEXT", None) in seen[0] and ("RESULT", "f1") in seen[0] and ("RESULT", "s1") not in seen[0]
    assert [e.seq for e in said] == list(range(len(run.record)))  # and nothing said twice


def test_an_exact_chat_id_resolves_beside_its_same_second_sibling(tmp_path):
    # `X` and `X-02` both start with `X`; the id as given is the one meant
    from lion_cli.chat import fresh_log, resolve_chat

    chats = tmp_path / "chats"
    a, _ = fresh_log(chats, "20260921-200000")
    b, _ = fresh_log(chats, "20260921-200000")
    assert (a, b) == ("20260921-200000", "20260921-200000-02")
    assert resolve_chat(chats, a).stem == a and resolve_chat(chats, b).stem == b
    try:
        resolve_chat(chats, "20260921-2000")
    except SystemExit as e:
        assert "ambiguous" in str(e)
    else:
        raise AssertionError("a short prefix of two logs resolved")


def test_a_start_that_fails_frees_its_empty_log_so_continue_keeps_the_old_history(tmp_path, monkeypatch):
    # a bad hooks.toml failed the start after the new log was reserved; `-c` then chose the
    # empty log and the previous history was gone from the next session
    import argparse

    from lion_cli.chat import chat_main, resolve_chat

    chats = tmp_path / ".lion" / "chats"
    chats.mkdir(parents=True)
    old = chats / "20260921-100000.jsonl"
    old.write_text('{"kind": "INPUT"}\n')
    (tmp_path / ".lion" / "hooks.toml").write_text("[[before]]\nname = 'broken'\n")  # no `run`
    a = argparse.Namespace(
        dir=str(tmp_path),
        resume=None,
        log=None,
        shell=False,
        code=False,
        offline=False,
        model="x",
        rounds=1,
        raw=False,
        say=None,
        verbose=False,
    )
    try:
        asyncio.run(chat_main(a))
    except ValueError as e:
        assert "has no `run`" in str(e)
    else:
        raise AssertionError("a broken hooks.toml started")
    assert sorted(p.name for p in chats.iterdir()) == [old.name]
    assert resolve_chat(chats, "last") == old
    # --log into the chats directory and the person quits at the first prompt: the log was opened
    # empty and a `not a.log` guard left it as `-c`'s next target
    (tmp_path / ".lion" / "hooks.toml").unlink()

    async def eof(*args, **kwargs):
        return None

    monkeypatch.setattr("lion_cli.chat.backend_for", lambda model: scripted("never"))
    monkeypatch.setattr("lion_cli.view.stdin_ask", eof)
    a.log = str(chats / "debug.jsonl")
    try:
        asyncio.run(chat_main(a))
    except SystemExit:
        pass
    assert sorted(p.name for p in chats.iterdir()) == [old.name]


def test_a_continued_chat_seeds_its_sessions_first_figure_from_the_last_billed_call(tmp_path, monkeypatch):
    """A continued chat's new CLI session starts from the whole view; its first figure stands on the
    count the last call reported and the characters that call stood in, both from the log's bill."""
    import argparse
    import io

    from lion_cli.chat import chat_main
    from lion_cli.view import Console

    from hub.backend.claude_code import ClaudeCodeSession

    billed = {"usage": {"input_tokens": 9, "cache_read_input_tokens": 31000}, "prompt_chars": 60000}
    fh = io.StringIO()
    Console("m", None, color=False, width=200, calls=[billed])._bill(fh, 7)
    row = json.loads(fh.getvalue())
    assert row["prompt_chars"] == 60000  # the bill keeps what the call's prompt stood in
    chats = tmp_path / ".lion" / "chats"
    chats.mkdir(parents=True)
    first = {
        "seq": 0,
        "kind": "INPUT",
        "name": "_in0",
        "round": 0,
        "alias": None,
        "meta": {},
        "content": "hi",
    }
    failed = {
        "kind": "USAGE",
        "round": 8,
        "model": None,
        "total_cost_usd": None,
    }  # a call that printed nothing

    async def eof(*args, **kwargs):
        return None

    made: list[ClaudeCodeSession] = []
    monkeypatch.setattr("lion_cli.view.stdin_ask", eof)
    monkeypatch.setattr(
        "lion_cli.chat.backend_for",
        lambda model: made.append(ClaudeCodeSession(cli="/bin/claude")) or made[-1],
    )
    for n, rows in enumerate(
        ([first, row, failed], [first, {k: v for k, v in row.items() if k != "prompt_chars"}])
    ):
        (chats / f"20260929-10000{n}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        a = argparse.Namespace(
            dir=str(tmp_path),
            resume=f"20260929-10000{n}",
            log=None,
            shell=False,
            code=False,
            offline=False,
            model="claude_code",
            rounds=1,
            raw=False,
            say=None,
            verbose=False,
        )
        with pytest.raises(SystemExit):
            asyncio.run(chat_main(a))
    assert [b.base for b in made] == [(31009, 60000), None]  # a log without the characters seeds nothing
    assert all(not b.calls for b in made)


def test_the_cost_line_reads_what_was_billed_across_every_attempt():
    from lion_cli.chat import spent

    calls = [
        {"usage": {"cost": 0.005}, "total_cost_usd": 0.009},  # retried: the last attempt's usage only
        {"usage": None, "total_cost_usd": 0.003},  # billed and failed: no usage at all
        {"usage": {"cost": 0.002}},  # a backend that reports only usage.cost
    ]
    cost, unknown = spent(calls)
    assert round(cost, 4) == 0.014 and unknown == 0


def test_the_cost_line_marks_a_sum_that_misses_calls_of_unknown_or_incomplete_cost(capsys):
    from lion_cli.chat import spent
    from lion_cli.view import Console

    calls = [
        {"usage": {"cost": 0.002}, "total_cost_usd": 0.004},
        {"usage": {}, "total_cost_usd": None},  # no figure anywhere: a subscription CLI's call
        {"usage": {}, "total_cost_usd": 0.001, "cost_complete": False},  # a retried attempt carried none
    ]
    cost, unknown = spent(calls)
    assert round(cost, 4) == 0.005 and unknown == 2
    Console("m", None, color=False, width=200).close("done", len(calls), cost, unknown, None)
    out = capsys.readouterr().out
    assert "done · 3 call(s) · $0.0050 (partial: 2 call(s) of unknown cost) · 0 entries" in out
    Console("m", None, color=False, width=200).close("done", 1, 0.004, 0, None)
    assert "done · 1 call(s) · $0.0040 · 0 entries" in capsys.readouterr().out


def test_the_cost_line_takes_a_total_of_zero_over_the_usage_cost_beside_it():
    from lion_cli.chat import spent

    assert spent([{"usage": {"cost": 0.005}, "total_cost_usd": 0}]) == (0, 0)
    assert spent([{"usage": {"cost": 0.005}, "total_cost_usd": 0.0}, {"usage": {"cost": 0.002}}]) == (
        0.002,
        0,
    )
    assert spent([{"usage": {"cost": 0.005}, "total_cost_usd": None}]) == (0.005, 0)


def test_a_turn_with_out_is_refused_and_the_conversation_goes_on(tmp_path):
    actor, profile = reader(tmp_path)
    said: list = []
    backend = scripted('Here it is.\nOUT{done: "yes"}', "Still here.")
    run = asyncio.run(chat(actor, profile, backend, asks("hi", "/quit"), say=said.append))
    assert isinstance(run.outcome, Stopped) and run.round == 2
    sys_entries = [e.content for e in run.record if e.kind is Kind.SYSTEM]
    assert any("this conversation has no OUT{}" in c for c in sys_entries)
    assert [e.content for e in run.record if e.kind is Kind.TEXT][-1] == "Still here."


def test_the_console_reads_and_codes_over_a_given_tree_with_the_box_guidance(tmp_path):
    """`console(tree=..., about_box=...)`: the file and code tools work the tree handed in, not the
    directory, and the shell guidance is the box's, not the VM's."""
    from lion_cli.chat import console

    class FakeTree:
        def __init__(self):
            self.files = {"a.py": "x = 1\n"}

        async def read(self, path):
            return self.files[path]

        async def write(self, path, text):
            self.files[path] = text

        async def search(self, pattern, path, include):
            return "a.py:1:x = 1\n" if pattern in "x = 1" else ""

        async def list_dir(self, path):
            return sorted(self.files)

    class FakeBox:
        workdir, prelude, dead = "/w", "", None

        async def exec(self, argv, *, stdin=None, timeout=120, env=None):
            return {"rc": 0, "stdout": "ran " + " ".join(argv[-1:]), "stderr": ""}

    tree = FakeTree()
    actor, profile = console(
        tmp_path, model="m", sandbox=FakeBox(), coding=True, tree=tree, about_box="THE BOX RULES"
    )
    assert "THE BOX RULES" in profile.system and "a VM" not in profile.system
    backend = scripted(
        '<lact l1>list_dir(path=".")</lact><lact s1>search(pattern="x = 1", path=".")</lact>'
        '<lact e1>edit(path="a.py", old="x = 1", new="x = 2")</lact><lact b1>bash(command="pytest")</lact>',
        "done",
    )
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), backend))
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["l1"].value == ["a.py"] and r["s1"].value == "a.py:1:x = 1\n" and r["e1"].ok
    assert tree.files["a.py"] == "x = 2\n" and not (tmp_path / "a.py").exists()
    assert r["b1"].value["stdout"] == "ran pytest"


def test_capture_failure_retains_box_and_reports_identity(tmp_path, monkeypatch, capsys):
    # the copy is the only one, so the box is kept, not stopped, and named; the error the chat ended
    # on is the one taking the patch raised
    from tests.test_daytona import ChatBox, box_chat

    async def unreadable(self, base="HEAD"):
        raise RuntimeError("git diff in /w exited 128: fatal: bad object")

    monkeypatch.setattr(ChatBox, "diff", unreadable)
    ChatBox.made.clear()
    err = box_chat(monkeypatch, ChatBox, tmp_path, "done")
    box = ChatBox.made[-1]
    assert isinstance(err, RuntimeError) and "fatal: bad object" in str(err)
    assert box.started and not box.stopped  # retained
    out = " ".join(capsys.readouterr().out.split())
    assert f"box {box.box_id}: kept, not stopped" in out  # and named, so it can be reached


def test_a_save_failure_or_a_dead_box_keeps_the_error_and_says_whether_the_box_can_be_reached(
    tmp_path, monkeypatch, capsys
):
    from tests.test_daytona import ChatBox, box_chat

    monkeypatch.setattr(
        ChatBox, "patch", "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
    )
    chats = tmp_path / ".lion" / "chats"
    (chats / "c1.patch").mkdir(parents=True)  # the patch cannot be written where it goes
    ChatBox.made.clear()
    err = box_chat(monkeypatch, ChatBox, tmp_path, "done", log=str(chats / "c1.jsonl"))
    box = ChatBox.made[-1]
    assert isinstance(err, IsADirectoryError) and not box.stopped
    assert f"box {box.box_id}: kept, not stopped" in " ".join(capsys.readouterr().out.split())

    async def died(self, base="HEAD"):
        self.dead = "failed to resolve container IP"
        raise RuntimeError("git diff: failed to resolve container IP")

    monkeypatch.setattr(ChatBox, "diff", died)
    err = box_chat(monkeypatch, ChatBox, tmp_path, "done")
    out = " ".join(capsys.readouterr().out.split())
    assert isinstance(err, RuntimeError) and "recovery unavailable: failed to resolve container IP" in out


def test_preparation_failure_stops_the_box(tmp_path, monkeypatch):
    from tests.test_daytona import ChatBox, box_chat

    async def refused(self, *a, **k):
        return {
            "rc": 1,
            "stdout": "",
            "stderr": "fatal: not a git repository",
            "elapsed": 0,
            "timed_out": False,
        }

    async def refused_line(self, *a, **k):
        return 1, "fatal: not a git repository"

    monkeypatch.setattr(ChatBox, "exec", refused)
    monkeypatch.setattr(ChatBox, "run", refused_line)
    ChatBox.made.clear()
    err = box_chat(monkeypatch, ChatBox, tmp_path, "done")
    assert isinstance(err, RuntimeError) and "baseline failed" in str(err)
    assert ChatBox.made[-1].started and ChatBox.made[-1].stopped


def test_a_push_that_fails_still_stops_the_box(tmp_path, monkeypatch):
    from tests.test_daytona import ChatBox, box_chat

    async def bad_push(self, directory):
        raise OSError("push into /w failed (rc 2): tar: short read")

    monkeypatch.setattr(ChatBox, "push", bad_push)
    ChatBox.made.clear()
    err = box_chat(monkeypatch, ChatBox, tmp_path, "done")
    box = ChatBox.made[-1]
    assert box.started and isinstance(err, OSError)
    assert box.stopped, f"the start failed on {err!r} and the box was never stopped"


def test_saved_patch_then_apply_failure_cleans_up(tmp_path, monkeypatch, capsys):
    from tests.test_daytona import ChatBox, box_chat

    (tmp_path / "f.txt").write_text("moved on\n")
    patch = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setattr(ChatBox, "patch", patch)
    ChatBox.made.clear()
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done", apply=True) is None
    (saved,) = (tmp_path / ".lion" / "chats").glob("*.patch")
    assert saved.read_text() == patch and (tmp_path / "f.txt").read_text() == "moved on\n"
    assert ChatBox.made[-1].stopped  # the patch is on disk: the box is not needed
    monkeypatch.setattr(ChatBox, "patch", "")
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done") is None
    assert ChatBox.made[-1].stopped and "box: no change in the copy" in capsys.readouterr().out


def test_a_continued_boxed_chat_runs_in_its_box_again_and_is_told_its_unapplied_patch(
    tmp_path, monkeypatch, capsys
):
    # the log recorded no box: `-c` without `--box daytona` went on over the person's own tree, and an
    # unapplied patch stood in the record and not in the new copy, with nothing said
    from lion_cli.chat import resolve_chat

    from tests.test_daytona import ChatBox, box_chat, repo_git

    (tmp_path / "f.txt").write_text("a\n")
    patch = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setattr(ChatBox, "patch", patch)
    ChatBox.made.clear()
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done", image="img-1") is None
    chats = tmp_path / ".lion" / "chats"
    first = resolve_chat(chats, "last")
    ran = {"kind": "BOX", "box": "daytona", "image": "img-1"}
    assert json.loads(first.read_text().splitlines()[0]) == ran
    saved = chats / f"{first.stem}.patch"
    assert saved.read_text() == patch and (tmp_path / "f.txt").read_text() == "a\n"
    monkeypatch.setattr(ChatBox, "patch", "")
    capsys.readouterr()
    # `lion chat -c` with no box flag: the box and image its log names, and the model is told
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done", resume="last", box=None, image=None) is None
    assert len(ChatBox.made) == 2 and ChatBox.made[-1].image == "img-1" and ChatBox.made[-1].stopped
    rows = [json.loads(r) for r in resolve_chat(chats, "last").read_text().splitlines()]
    assert rows[0] == ran and [r["kind"] for r in rows].count("BOX") == 1
    told = [r["content"] for r in rows if r["kind"] == "INPUT" and r["meta"].get("origin") == "console"]
    assert len(told) == 1 and f"saved as {saved}, are not applied to {tmp_path}" in told[0]
    assert "are not applied to" in " ".join(capsys.readouterr().out.split())  # the person sees it too
    # the patch applied, and `--box vm` given: the flag wins, and nothing is said of a patch that landed
    repo_git(tmp_path, "apply", str(saved))
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done", resume=first.stem, box="vm", image=None) is None
    assert len(ChatBox.made) == 2
    rows = [json.loads(r) for r in resolve_chat(chats, "last").read_text().splitlines()]
    assert rows[0] == {"kind": "BOX", "box": "vm"}
    assert not [r for r in rows if r["kind"] == "INPUT" and r["meta"].get("origin") == "console"]
    # no git on the machine: the check cannot run, the resume goes on, and the model is told that much
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done", resume=first.stem, box="vm", image=None) is None
    rows = [json.loads(r) for r in resolve_chat(chats, "last").read_text().splitlines()]
    told = [r["content"] for r in rows if r["kind"] == "INPUT" and r["meta"].get("origin") == "console"]
    assert len(told) == 1 and f"saved as {saved}, could not be checked against {tmp_path}" in told[0]


def resumed(monkeypatch, directory, chat_id: str) -> tuple[BaseException | None, list[str]]:
    """`lion chat -r <chat_id> --box vm` over `directory`: what it raised, and what the resume told
    the model."""
    from lion_cli.chat import resolve_chat

    from tests.test_daytona import ChatBox, box_chat

    err = box_chat(monkeypatch, ChatBox, directory, "done", resume=chat_id, box="vm", image=None)
    rows = [
        json.loads(r) for r in resolve_chat(directory / ".lion" / "chats", "last").read_text().splitlines()
    ]
    return err, [r["content"] for r in rows if r["kind"] == "INPUT" and r["meta"].get("origin") == "console"]


def test_a_resumed_chat_tells_a_patch_git_could_not_check_as_unknown_with_gits_reason(tmp_path, monkeypatch):
    # any failure of the reverse check was told as "not applied": a patch git cannot read, and a file
    # edited after the patch landed, read as a patch never applied, and git's reason was dropped
    from lion_cli.chat import resolve_chat

    from tests.test_daytona import ChatBox, box_chat, repo_git

    repo = tmp_path / "repo"
    src = repo / "sub"  # a subdirectory: both checks run from the top, with its prefix
    src.mkdir(parents=True)
    (src / "f.txt").write_text("a\n")
    repo_git(repo, "init", "-q")
    repo_git(repo, "add", "-A")
    repo_git(repo, "commit", "-qm", "one")
    patch = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setattr(ChatBox, "patch", patch)
    assert box_chat(monkeypatch, ChatBox, src, "done") is None
    first = resolve_chat(src / ".lion" / "chats", "last")
    saved = src / ".lion" / "chats" / f"{first.stem}.patch"
    monkeypatch.setattr(ChatBox, "patch", "")
    # the controls: a patch that goes on whole is not applied; one that comes off is, and nothing is said
    err, (told,) = resumed(monkeypatch, src, first.stem)
    assert err is None and f"saved as {saved}, are not applied to {src} as it stands, so edits" in told
    (src / "f.txt").write_text("b\n")
    assert resumed(monkeypatch, src, first.stem) == (None, [])
    # the person edited the line the patch changed: it neither comes off nor goes on
    (src / "f.txt").write_text("c\n")
    err, (told,) = resumed(monkeypatch, src, first.stem)
    assert f"saved as {saved}, could not be checked against {src} as it stands (git apply exited 1: " in told
    assert "patch failed: sub/f.txt:1)" in told and "not applied" not in told and err is None
    # a patch git cannot read
    saved.write_text(patch.removesuffix("+b\n"))
    err, (told,) = resumed(monkeypatch, src, first.stem)
    assert f"could not be checked against {src} as it stands (git apply exited 128: " in told
    assert "corrupt patch at" in told and "not applied" not in told and err is None


REAL_GIT = shutil.which("git")  # resolved before any test puts a stub first on PATH


def hanging_git(tmp_path, monkeypatch, discovery: float | None = None):
    """`git` on PATH that hangs, each call's pid appended to the file returned; `rev-parse` hangs too
    unless `discovery` is how long it takes before the real git answers it."""
    real = REAL_GIT
    pids = tmp_path / "git-pids"
    stub = tmp_path / "hanging-bin"
    stub.mkdir(exist_ok=True)
    find = (
        "exec /bin/sleep 15"
        if discovery is None
        else f'/bin/sleep {discovery}; exec {shlex.quote(real)} "$@"'
    )
    (stub / "git").write_text(
        f"#!/bin/sh\necho $$ >> {shlex.quote(str(pids))}\n"
        f'case " $* " in *" rev-parse "*) {find} ;; esac\nexec /bin/sleep 15\n'
    )
    (stub / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(stub))
    return pids


def reaped(pids) -> int:
    """How many git calls the stub took, each a process of its own, all killed and waited for:
    nothing left running, nor a zombie."""
    started = [int(p) for p in pids.read_text().split()]
    assert len(set(started)) == len(started)
    for pid in started:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    pids.unlink()
    return len(started)


def test_a_resumed_chat_whose_git_hangs_is_told_its_patch_could_not_be_checked_within_one_bound(
    tmp_path, monkeypatch
):
    # the resume's git ran with no time limit: a git that hangs held the chat before its first call
    from lion_cli.chat import resolve_chat

    from tests.test_daytona import ChatBox, box_chat

    (tmp_path / "f.txt").write_text("a\n")
    patch = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setattr(ChatBox, "patch", patch)
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done") is None
    first = resolve_chat(tmp_path / ".lion" / "chats", "last")
    monkeypatch.setattr(ChatBox, "patch", "")
    # git hangs finding the repository; then it answers late and the check hangs: the check gets what
    # is left of the one bound, never a fresh one (1.5 s + 2 s would be past the 3 s asserted)
    for bound, discovery in ((1.0, None), (2.0, 1.5)):
        monkeypatch.setattr("lion_cli.chat.GIT_TIMEOUT", bound, raising=False)
        pids = hanging_git(tmp_path, monkeypatch, discovery)
        start = time.monotonic()
        err, told = resumed(monkeypatch, tmp_path, first.stem)
        assert time.monotonic() - start < bound + 1, err
        assert err is None and len(told) == 1
        unchecked = (
            f"could not be checked against {tmp_path} as it stands (git did not answer within {bound:g}s)"
        )
        assert unchecked in told[0]
        assert reaped(pids) == (1 if discovery is None else 2)  # the lookup; or it and the check


def test_a_resumed_coding_chat_whose_git_hangs_starts_with_the_branch_said_unknown_within_its_bound(
    tmp_path, monkeypatch
):
    # with --code the guidance names the branch, read from git with no time limit: past the bounded
    # patch check, a git that hangs still held the resumed chat before its first call
    from lion_cli.chat import console, resolve_chat

    from tests.test_daytona import ChatBox, box_chat

    (tmp_path / "f.txt").write_text("a\n")
    patch = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setattr(ChatBox, "patch", patch)
    assert box_chat(monkeypatch, ChatBox, tmp_path, "done") is None
    first = resolve_chat(tmp_path / ".lion" / "chats", "last")
    monkeypatch.setattr(ChatBox, "patch", "")
    systems: list[str] = []

    def seen(*args, **kwargs):
        actor, profile = console(*args, **kwargs)
        systems.append(profile.system)
        return actor, profile

    monkeypatch.setattr("lion_cli.chat.console", seen)
    monkeypatch.setattr("lion_cli.chat.GIT_TIMEOUT", 1.0, raising=False)
    pids = hanging_git(tmp_path, monkeypatch)
    start = time.monotonic()
    err = box_chat(monkeypatch, ChatBox, tmp_path, "done", resume=first.stem, code=True)
    assert time.monotonic() - start < 3, err  # the patch check's bound, then the branch's
    assert err is None and len(systems) == 1
    assert f"the tree is {tmp_path} on branch (git did not answer within 1s)," in systems[0]
    assert reaped(pids) == 2  # the patch check's lookup and the branch


def test_a_patch_apply_that_hangs_is_stopped_and_what_it_changed_is_said_to_be_unknown(
    tmp_path, monkeypatch, capsys
):
    # take_patch's git ran with no time limit; and an apply stopped mid-way may have changed the
    # directory, so it is never told as "not applied", nor the directory as it was
    from tests.test_daytona import ChatBox, box_chat, repo_git

    src = tmp_path / "proj"
    src.mkdir()
    (src / "f.txt").write_text("a\n")
    repo_git(src, "init", "-q")
    repo_git(src, "add", "-A")
    repo_git(src, "commit", "-qm", "one")
    patch = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setattr(ChatBox, "patch", patch)
    monkeypatch.setattr("lion_cli.chat.GIT_TIMEOUT", 1.0, raising=False)
    ChatBox.made.clear()
    for apply, discovery, said in (
        (True, 0, "box: git apply did not finish within 1s and was stopped; whether it changed"),
        (False, None, "box: not applied; git rev-parse did not answer within 1s"),  # nothing ran
    ):
        pids = hanging_git(tmp_path, monkeypatch, discovery)
        start = time.monotonic()
        err = box_chat(monkeypatch, ChatBox, src, "done", apply=apply)
        assert time.monotonic() - start < 3, err
        assert err is None
        out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
        assert said in out and "box: applied" not in out
        assert ("is unknown: read the files before applying" in out) is apply
        assert ("not applied" in out) is not apply
        assert ChatBox.made[-1].stopped  # the patch is saved: the box is not its only copy
        assert reaped(pids) == (2 if apply else 1)
    assert [p.read_text() for p in sorted((src / ".lion" / "chats").glob("*.patch"))] == [patch, patch]


def test_a_checkpoint_of_the_record_resumes_the_view_the_run_last_showed(tmp_path):
    from lionagi.record import fold

    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    actor, profile = reader(tmp_path)
    first = scripted(
        '<lact a1>read(path="a.txt")</lact><lact a2>read(path="b.txt")</lact>',
        '<lact h1>context.hide(refs=["a1"])</lact>'
        '<lact s1>context.summarize(refs=["a2"], text="b was beta")</lact>',
        "Folded.",
    )
    log = tmp_path / "one.jsonl"
    with log.open("w") as fh:
        run1 = asyncio.run(
            chat(
                actor,
                profile,
                first,
                asks("read both", None),
                say=lambda e: fh.write(json.dumps(entry_row(e)) + "\n"),
            )
        )
    source = {"record": str(log), "run": run1.id}
    ckpt = take(source, [], actor="c", why="manual", budget=60_000, record=run1.record)
    save(ckpt, tmp_path)
    history = to_history(load(tmp_path))
    rows = lambda es: [(e.seq, e.kind, e.content, e.alias, e.meta) for e in es]  # noqa: E731
    assert rows(history) == rows(load_log(log)) == rows(run1.record)
    seen: list[list[Message]] = []
    inner = scripted("Still folded.")

    async def backend(messages):
        seen.append(messages)
        return await inner(messages)

    actor2, profile2 = reader(tmp_path)
    asyncio.run(chat(actor2, profile2, backend, asks("and?", None), history=history))
    # the resumed first view opens with exactly the messages the ended run's view rendered
    ended = [(m.role, m.content, m.name) for m in _messages(fold(run1.record))]
    assert [(m.role, m.content, m.name) for m in seen[0][1 : 1 + len(ended)]] == ended
    assert any(m.content.startswith("[hidden;") for m in seen[0]) and "b was beta" in seen[0][1 + 4].content
    # a record that no longer folds to the checkpoint's view is refused, never replayed
    lines = log.read_text().splitlines()
    cut = [json.loads(ln) for ln in lines]
    for r in cut:
        r["meta"].pop("directive", None)
    log.write_text("".join(json.dumps(r) + "\n" for r in cut))
    try:
        to_history(load(tmp_path))
    except CheckpointError as e:
        assert "no longer folds to the view" in str(e)
    else:
        raise AssertionError("a record without its directives replayed")


def _row(seq: int, content: str) -> dict:
    return {"seq": seq, "kind": "TEXT", "content": content, "round": 1, "meta": {}}


def test_a_log_row_holding_a_unicode_line_separator_reads_back_whole(tmp_path):
    # a row written with ensure_ascii=False carries U+2028, U+2029 or NEL as itself; the reader split
    # rows with str.splitlines, broke such a row in two and refused a valid log
    log = tmp_path / "log.jsonl"
    rows = [_row(i, f"left{sep}right") for i, sep in enumerate(("\u2028", "\u2029", "\x85"))]
    log.write_bytes("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode())
    assert [e.content for e in load_log(log)] == ["left\u2028right", "left\u2029right", "left\x85right"]


def test_a_write_cut_inside_a_character_is_a_torn_last_row(tmp_path):
    # the whole file was decoded first, so a write cut inside a multi-byte character raised
    # UnicodeDecodeError before the torn last row could be dropped
    import pytest

    log = tmp_path / "log.jsonl"
    whole = (json.dumps(_row(0, "a")) + "\n").encode()
    log.write_bytes(whole + b'{"seq": 1, "kind": "TEXT", "content": "' + "☃".encode()[:2])
    torn: list[str] = []
    assert [e.content for e in load_log(log, torn=torn)] == ["a"]
    assert len(torn) == 1 and torn[0].startswith("line 2 of") and "a write that was cut" in torn[0]
    with pytest.raises(ValueError, match="the last line is not JSON"):
        load_log(log)  # without `torn` the cut row is an error, as before
    log.write_bytes(b'{"seq": 0, "kind": "TEXT", "content": "\xff"}\n' + whole)
    with pytest.raises(ValueError, match="line 1, with 1 after it"):
        load_log(log, torn=[])  # an unreadable row with rows after it is never a cut write


def test_the_closure_rides_the_log_and_a_resumed_chat_folds_it_into_its_first_view(tmp_path):
    actor, profile = reader(tmp_path)
    log = tmp_path / "one.jsonl"
    with log.open("w") as fh:
        run1 = asyncio.run(
            chat(
                actor,
                profile,
                scripted("Hello."),
                asks("hi", None),
                say=lambda e: fh.write(json.dumps(entry_row(e)) + "\n"),
            )
        )
    closure = run1.record.entries[-1]
    assert closure.kind is Kind.SYSTEM and "outcome: Stopped(round=2)" in closure.content
    history = load_log(log)
    last = history[-1]
    assert (last.seq, last.kind, last.content) == (closure.seq, Kind.SYSTEM, closure.content)
    seen: list[list[Message]] = []
    inner = scripted("Again.")

    async def backend(messages):
        seen.append(messages)
        return await inner(messages)

    actor2, profile2 = reader(tmp_path)
    asyncio.run(chat(actor2, profile2, backend, asks("more", None), history=history))
    # the first view carries the old run's closure where it landed, ahead of the new message
    first = [(m.role, m.name, m.content) for m in seen[0]]
    at = first.index(("system", closure.name, closure.content))
    assert first[at + 1][1:] == (f"_in{closure.seq + 1}", f"[_in{closure.seq + 1}] more")


def test_the_editor_sends_a_paste_whole_breaks_a_line_on_alt_enter_and_ends_on_ctrl_c_only_when_empty(
    tmp_path,
):
    """The terminal editor: a bracketed paste of several lines is one message; Enter sends; Alt-Enter
    breaks a line inside the message; Ctrl-C on a line with text clears it, on an empty line it ends
    the chat (None); Up recalls the previous message from the history file."""
    from lion_cli.view import Editor
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def typed(keys: list[str], history) -> list[str | None]:
        got: list[str | None] = []
        with create_pipe_input() as pipe:
            ed = Editor(history, input=pipe, output=DummyOutput())
            for k in keys:
                pipe.send_text(k)
            for _ in keys:
                got.append(await ed.ask("you > "))
                if got[-1] is None:
                    break
        return got

    history = tmp_path / "history"
    paste = "\x1b[200~one\ntwo\nthree\x1b[201~\r"
    assert asyncio.run(typed([paste], history)) == ["one\ntwo\nthree"]
    assert asyncio.run(typed(["ab\x1b\rcd\r"], history)) == ["ab\ncd"]  # Alt-Enter = Escape, Enter
    assert asyncio.run(typed(["ab\x03cd\r", "\x03"], history)) == ["cd", None]
    assert asyncio.run(typed(["\x1b[A\r"], history)) == ["cd"]  # Up: the last message sent


def test_the_status_line_reads_inbox_context_notes_and_cost_off_the_live_run(tmp_path):
    """The console's status line is computed from the actor, its run and the backend each time it is
    read: before the run only what exists (inbox, notes, cost); at the prompt after a round that wrote
    a note and ran a command, the round notice's own context figure; with a backend that reports its
    prompt and cost, the last call's prompt and the partial mark for a call of unknown cost."""
    import re

    from lion_cli.chat import status_line

    from lionagi import Reply
    from lionagi.actor import Handle, Inbound

    (tmp_path / "a.txt").write_text("alpha\n")

    def drive(backend) -> tuple:
        actor, profile = reader(tmp_path)
        runs: list = []
        off = actor.bus.subscribe("run.started", lambda ev: runs.append(actor.runs[ev.payload["run"]]))
        before = status_line(actor, profile, runs, backend)
        shown: list[str] = []
        replies = iter(["go", None])

        async def ask() -> str | None:
            if runs:  # the prompt after the reply: what the toolbar shows there
                shown.append(status_line(actor, profile, runs, backend))
            return next(replies)

        try:
            run = asyncio.run(chat(actor, profile, backend, ask))
        finally:
            off()
        notices = [
            e.content for e in run.record if e.kind is Kind.SYSTEM and e.content.startswith("<system round=")
        ]
        figure = int(re.search(r"context: ~(\d+) / 200000 tokens", notices[-1]).group(1))
        assert run.context_figure == figure  # the number the round notice told the model
        return actor, profile, runs, before, shown, figure

    turns = ('<lvar note.plan>ship it</lvar><lact r1>read(path="a.txt")</lact>', "Done.")
    backend = scripted(*turns)
    actor, profile, runs, before, shown, figure = drive(backend)
    assert before == "inbox 0 · notes 0 (0 chars) · $0.0000"
    assert 0 < figure < 1000 and runs[0].anchor is None  # a plain str reply reports no prompt
    assert [e.alias for e in runs[0].record if e.kind is Kind.RESULT] == ["r1"]
    assert shown == [f"inbox 0 · ctx ~{figure}/200k · notes 1 (7 chars) · $0.0000"]
    # live, never a copy: a message queued now is counted on the next read
    actor.inbox.append(Inbound(Handle("h1"), "later", "someone", "-", None))
    assert status_line(actor, profile, runs, backend) == shown[0].replace("inbox 0", "inbox 1")

    class Billed:  # the same turns, as a backend that reports its prompt and what each call cost
        def __init__(self):
            self.turns, self.calls = scripted(*turns), []

        async def __call__(self, messages):
            text = await self.turns(messages)
            self.calls.append({"total_cost_usd": None if self.calls else 0.0125})
            return Reply(text, prompt_tokens=14_100)

    actor, profile, runs, before, shown, figure = drive(Billed())
    assert before == "inbox 0 · notes 1 (7 chars) · $0.0000"  # the notes file outlives the actor
    assert runs[0].anchor[0] == 14_100 and 14_100 < figure < 15_000
    assert shown == [
        f"inbox 0 · ctx ~{figure / 1000:.1f}k/200k (last call 14.1k) · notes 1 (7 chars) · "
        "$0.0125 (partial: 1 call(s) of unknown cost)"
    ]


def test_the_console_prints_the_status_line_at_each_round_notice_only_when_verbose(tmp_path, capsys):
    from lion_cli.view import Console

    actor, profile = reader(tmp_path)
    reads = []

    def status() -> str:
        reads.append(1)
        return f"status read {len(reads)}"

    backend = scripted('<lact l1>list_dir(path=".")</lact>', "Done.")
    say = Console("m", None, color=False, width=200, verbose=True, status=status)
    run = asyncio.run(chat(actor, profile, backend, asks("go", None), say=say))
    out = capsys.readouterr().out
    rounds = [e for e in run.record if e.kind is Kind.SYSTEM and e.content.startswith("<system round=")]
    assert len(rounds) == 2 and len(reads) == 2  # the closure and every other entry print none
    assert "status read 1" in out and "status read 2" in out
    asyncio.run(
        chat(*reader(tmp_path), scripted("Hi."), asks("go", None), say=Console("m", None, status=status))
    )
    assert len(reads) == 2  # not verbose: the line is the prompt's, never printed


def test_the_editor_draws_the_status_line_under_the_prompt_as_it_stands_at_each_draw():
    import io

    from lion_cli.view import Editor
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output

    class Terminal(Vt100_Output):
        # prompt_toolkit draws the toolbar once it knows the rows below the cursor; a terminal says so
        # in its answer to a cursor-position request, which a pipe never sends
        def get_rows_below_cursor_position(self) -> int:
            return 20

    waiting = [0]

    async def typed() -> list[tuple[str | None, str]]:
        got = []
        with create_pipe_input() as pipe:
            screen = io.StringIO()
            out = Terminal(screen, lambda: Size(rows=24, columns=100), term="xterm")
            ed = Editor(None, status=lambda: f"inbox {waiting[0]} · $0.0000", input=pipe, output=out)
            for text, n in (("hi\r", 0), ("again\r", 3)):
                waiting[0] = n
                screen.seek(0)
                screen.truncate()
                pipe.send_text(text)
                got.append((await ed.ask("you > "), screen.getvalue()))
        return got

    (first, drawn1), (second, drawn2) = asyncio.run(typed())
    assert (first, second) == ("hi", "again")
    assert "inbox 0 · $0.0000" in drawn1 and "inbox 3" not in drawn1
    assert "inbox 3 · $0.0000" in drawn2 and "inbox 0" not in drawn2


def test_the_editor_leaves_the_loops_sigint_handler_in_place_after_a_prompt(tmp_path):
    """A mid-round Ctrl-C is a SIGINT the chat's own loop handler turns into a cut; a prompt must not
    swap that handler out (prompt_toolkit's own SIGINT handling would, and remove it on exit)."""
    import signal

    from lion_cli.view import Editor
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    fired: list[int] = []

    async def main():
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: fired.append(1))
        with create_pipe_input() as pipe:
            ed = Editor(tmp_path / "history", input=pipe, output=DummyOutput())
            pipe.send_text("hi\r")
            assert await ed.ask("you > ") == "hi"
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.05)
        loop.remove_signal_handler(signal.SIGINT)

    asyncio.run(main())
    assert fired == [1]


def test_the_status_line_leads_with_the_model_and_the_khive_actor_when_asked(tmp_path):
    from lion_cli.chat import signer, status_line

    actor, profile = reader(tmp_path)
    assert signer(tmp_path) is None
    assert status_line(actor, profile, [], None, "claude_code/claude-sonnet-5-5", None).startswith(
        "claude-sonnet-5-5 · no actor · inbox 0"
    )
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    assert signer(tmp_path) == "lambda:x"
    line = status_line(actor, profile, [], None, "m", signer(tmp_path))
    assert line.startswith("m · lambda:x · inbox 0")
    (tmp_path / ".khive" / "config.toml").write_text("[actor]\n")
    assert signer(tmp_path) is None


def test_the_working_indicator_lives_on_a_terminal_only_and_holds_the_last_detail_under_it():
    from lion_cli.view import Console

    from lionagi.record import Entry

    """On a terminal the round in flight shows a spinner at the bottom; a verbose result prints its
    head to the scrollback and parks its detail under the spinner until the next one lands; the
    model's turn switches the label to `running`; asking or closing clears it. Off a terminal nothing
    is drawn live and the result prints whole, as before."""
    import io

    from rich.console import Console as RichConsole

    from lionagi.actor import Result

    def result(seq: int, alias: str, value, spec="read") -> Entry:
        return Entry(seq=seq, kind=Kind.RESULT, round=1, alias=alias, content=Result(spec, True, value))

    say = Console("m", None, color=False, verbose=True, width=80)
    say.working("thinking")
    assert say.live is None and say.doing is not None and say.doing.what == "thinking"
    say(result(3, "a1", "alpha\nbeta"))
    assert say.detail is None  # off a terminal the detail printed with the head
    say.idle()
    assert say.doing is None

    buf = io.StringIO()
    say = Console("m", None, color=False, verbose=True, width=80)
    say.out = RichConsole(file=buf, force_terminal=True, width=80, no_color=True)
    say.working("thinking")
    assert say.live is not None and "thinking" in str(say.doing.__rich__())
    say(result(3, "a1", "alpha\nbeta"))
    assert say.detail is not None and "alpha" in say.detail  # parked under the spinner
    say(result(4, "a2", "gamma"))
    assert "gamma" in say.detail and "alpha" not in say.detail  # the next one replaces it
    say(Entry(seq=5, kind=Kind.TEXT, round=1, content="Done."))
    assert say.doing.what == "thinking"  # a plain reply asks for nothing: the person is next
    say(Entry(seq=6, kind=Kind.TEXT, round=1, content='<lact b1>read(path="a")</lact>'))
    assert say.doing.what == "running"
    say.idle()
    assert say.live is None and say.detail is None
    out = buf.getvalue()
    assert "✓ a1 read" in out and "✓ a2 read" in out
    say.working("thinking")
    say.close("ended", 0, 0.0, 0, None)  # closing clears the live region too
    assert say.live is None
