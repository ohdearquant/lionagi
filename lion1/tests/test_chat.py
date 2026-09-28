"""A conversation: a turn without a command is the reply and the loop waits for the next message; a
turn with commands runs them and reads the results before replying; every message is in one record
that the model sees whole; the person ends it, as Stopped; and a log read back is the run's history."""

import asyncio
import json

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


def test_the_cost_line_reads_what_was_billed_across_every_attempt():
    from lion_cli.chat import spent

    calls = [
        {"usage": {"cost": 0.005}, "total_cost_usd": 0.009},  # retried: the last attempt's usage only
        {"usage": None, "total_cost_usd": 0.003},  # billed and failed: no usage at all
        {"usage": {"cost": 0.002}},  # a backend that reports only usage.cost
    ]
    assert round(spent(calls), 4) == 0.014


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
