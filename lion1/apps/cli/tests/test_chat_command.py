"""`lion chat`: the console actor gets khive only in an actor's directory; the view shows prose as
markdown, calls and results as one line each, and logs every entry whole; a chat continues from its
own log; the flags resolve to a chat id or say where the logs live."""

import asyncio
import json
import re
import subprocess

import pytest
from lion_cli.chat import Interrupt, _branch, chat_main, console, signer, unsettled
from lion_cli.main import parser
from lion_cli.view import RESULT_LINES, Console
from pydantic import BaseModel, ConfigDict, Field, RootModel, computed_field, model_serializer

from hub.chat import chat, entry_row, load_log
from lion1 import Actor, Profile
from tests.lionfake import actor_dir, patch_khive, routed_config
from tests.lionfake import store as fresh_store
from tests.test_actor import scripted


def asks(*messages: str | None):
    it = iter(messages)

    async def ask() -> str | None:
        return next(it)

    return ask


def test_a_console_in_an_actors_directory_gets_khive_signing_as_that_actor(tmp_path, kkernel_stub):
    with pytest.raises(ValueError, match="no such khive config file; a chat's record is rows of khive"):
        console(tmp_path)  # no .khive/config.toml here: a chat with no actor to sign as does not start
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("lambda:x", tmp_path))
    actor, profile = console(tmp_path, model="deepseek/deepseek-v4.1-flash")
    assert "khive_ops" in profile.specs and "signing as lambda:x" in profile.system
    # every verb the boundary admits, by name: a count and a summary left a model to find them by failing
    from hub.agent.chores import ALLOWED_VERBS

    named = ", ".join(sorted(ALLOWED_VERBS))
    assert f"(these verbs and no other: {named}; memory.remember with an explicit salience" in profile.system
    assert "schedule.agenda" in named and "comm.send" not in named
    assert profile.system.startswith("You are deepseek/deepseek-v4.1-flash, talking with a person")
    assert "describe the people and agents who own this directory, not you" in profile.system
    assert "OUT{}" in profile.system  # the person ends the conversation, not the model


def test_branch_resolves_via_git_in_a_linked_worktree_and_a_subdirectory(tmp_path):
    # `.git` is a file (not a dir) in a linked worktree, and there is no `.git` at all in a
    # subdirectory, so reading `.git/HEAD` off the given directory would find neither.
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("x\n")
    run("add", "a.txt")
    run("commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    run("worktree", "add", "-q", "-b", "feature", str(wt))
    sub = wt / "sub"
    sub.mkdir()
    assert _branch(repo) == "main"
    assert _branch(wt) == "feature"  # linked worktree: .git is a file
    assert _branch(sub) == "feature"  # subdirectory: no .git here at all
    assert _branch(tmp_path) == "(no git)"


def test_a_non_utf8_agents_md_does_not_crash_the_console_and_decodes_best_effort(tmp_path, console_khive):
    (tmp_path / "AGENTS.md").write_bytes(b"rules: \xe8 latin-1 byte\n")
    actor, profile = console(tmp_path, coding=True)
    assert "AGENTS.md says:" in profile.system  # started; did not raise UnicodeDecodeError
    assert "rules:" in profile.system and "latin-1 byte" in profile.system


def test_an_explicit_khive_config_signs_over_the_directorys_own_and_a_missing_one_refuses(
    tmp_path, kkernel_stub
):
    """`khive_config=` names the file the chat signs with: its actor, not the directory's, and the
    directory's own `.khive/config.toml` is not read (it may name a different actor, or none). A
    path that is not a file refuses the chat; the directory's config is never the fallback."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("lambda:x", tmp_path))
    elsewhere = tmp_path / "elsewhere" / "khive.toml"
    elsewhere.parent.mkdir()
    elsewhere.write_text(routed_config("lambda:y", tmp_path))
    assert signer(tmp_path) == "lambda:x" and signer(tmp_path, elsewhere) == "lambda:y"
    actor, profile = console(tmp_path, khive_config=elsewhere)
    assert "khive_ops" in profile.specs and "signing as lambda:y" in profile.system
    assert "lambda:x" not in profile.system
    gone = tmp_path / "gone.toml"  # not the directory's lambda:x instead
    with pytest.raises(ValueError, match=f"{re.escape(str(gone))}: no such khive config file;"):
        console(tmp_path, khive_config=gone)


def test_khive_config_missing_actor_table_or_id_refuses_the_chat_and_names_the_file(tmp_path, kkernel_stub):
    khive_dir = tmp_path / ".khive"
    khive_dir.mkdir()
    config = khive_dir / "config.toml"
    config.write_text("")  # no [actor] table at all
    with pytest.raises(ValueError, match=f"{re.escape(str(config))}: no \\[actor\\].id;"):
        console(tmp_path)

    config.write_text("[actor]\n")  # [actor] present, no id
    with pytest.raises(ValueError, match=f"{re.escape(str(config))}: no \\[actor\\].id;"):
        console(tmp_path)


def test_khive_absent_kkernel_refuses_the_chat_naming_it(tmp_path, monkeypatch):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("lambda:x", tmp_path))
    # neither PATH nor the ~/.cargo/bin fallback resolves a kkernel, on any machine this runs on
    monkeypatch.setattr("lion1.khive.shutil.which", lambda name: None)
    monkeypatch.setattr("lion1.khive.Path.home", lambda: tmp_path / "no-kkernel-here")
    with pytest.raises(
        ValueError, match="kkernel not found on PATH or under ~/.cargo/bin; a chat's record is rows of khive"
    ):
        console(tmp_path)


def test_unsettled_never_reports_an_alias_the_records_own_namespace_refused(tmp_path):
    # an alias in the record's `_` namespace is refused at declaration and never gets a RESULT, so
    # it must never be reported as unsettled either.
    log = tmp_path / "cut.jsonl"
    text = '<lact _x>read(path="a")</lact><lact r1>read(path="b")</lact>'
    rows = [
        {"seq": 0, "kind": "INPUT", "name": "_in0", "turn": 0, "alias": None, "meta": {}, "content": "go"},
        {
            "seq": 1,
            "kind": "SYSTEM",
            "name": "_sys1",
            "turn": 1,
            "alias": None,
            "meta": {},
            "content": "<s>",
        },
        {"seq": 2, "kind": "TEXT", "name": "_m2", "turn": 1, "alias": None, "meta": {}, "content": text},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert unsettled(load_log(log)) == ["r1"]


def test_the_console_shows_prose_calls_and_results_and_logs_every_entry_whole(
    tmp_path, capsys, console_khive
):
    (tmp_path / "big.txt").write_text("\n".join(f"line {i}" for i in range(40)) + "\n")
    actor, profile = console(tmp_path, model="deepseek/deepseek-v4.1-flash")
    backend = scripted(
        'Let me look.\n<lact r1>read(path="big.txt")</lact>',
        "Forty lines.",
        '<lact r2>read(path="nope.txt")</lact>',
        "It is not there.",
    )
    log = tmp_path / "chats" / "one.jsonl"
    say = Console("deepseek/deepseek-v4.1-flash", log, color=False, echo=True, verbose=True, width=200)
    run = asyncio.run(
        chat(actor, profile, backend, asks("what is in big.txt?", "and nope.txt?", None), say=say)
    )
    out = capsys.readouterr().out
    assert "you ▸ what is in big.txt?" in out and "deepseek-v4.1-flash ▸\nLet me look." in out
    assert '→ r1 read(path="big.txt")' in out and "<lact" not in out
    assert "✓ r1 read · 40 line(s), 310 chars" in out and "    line 0\n" in out
    assert f"(+{40 - RESULT_LINES} lines in the log)" in out
    assert "✗ r2 read " in out and "nope.txt" in out and "It is not there." in out
    assert "· turn 2" in out and "settled:" in out and "<system" not in out and "context:" not in out
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert len(rows) == len(run.record) == say.written and [r["seq"] for r in rows] == list(range(len(rows)))
    whole = next(
        r for r in rows if r["kind"] == "RESULT" and r["content"]["spec"] == "read" and r["content"]["ok"]
    )
    assert whole["content"]["value"].count("\n") == 40  # the log has the result whole; the console cut it
    assert next(r for r in rows if r["seq"] == 0)["content"] == "what is in big.txt?"


def test_a_resumed_chat_logs_the_whole_record_again_and_prints_only_the_new_part(
    tmp_path, capsys, console_khive
):
    (tmp_path / "a.txt").write_text("alpha\n")
    log1, log2 = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    actor, profile = console(tmp_path, name="c1")
    first = scripted("<lvar p1>a.txt</lvar><lact r1>read(path=*p1)</lact>", "It says alpha.")
    asyncio.run(chat(actor, profile, first, asks("read a.txt", None), say=Console("m", log1, color=False)))
    history = load_log(log1)
    capsys.readouterr()
    actor2, profile2 = console(tmp_path, name="c2")
    say = Console("m", log2, color=False, echo=True, width=200)
    say.quiet_until = len(history) - 1
    second = scripted("<lact r2>read(path=*p1)</lact>", "Still alpha.")
    run = asyncio.run(chat(actor2, profile2, second, asks("again?", None), say=say, history=history))
    out = capsys.readouterr().out
    assert "you ▸ again?" in out and "Still alpha." in out and "It says alpha." not in out
    rows = log2.read_text().splitlines()
    assert len(rows) == len(run.record) and json.loads(rows[0])["content"] == "read a.txt"  # log 2 is whole


def test_a_khive_config_that_is_not_a_file_or_names_no_actor_refuses_to_start_and_leaves_no_log(tmp_path):
    """The chat refuses before any model work, naming the file; the directory's own config, which
    would sign as someone else, is not the fallback. The log opened for the chat is removed."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("lambda:x", tmp_path))
    gone = tmp_path / "gone.toml"
    with pytest.raises(SystemExit, match=f"--khive-config {gone}: no such file"):
        asyncio.run(
            chat_main(parser().parse_args(["chat", "--dir", str(tmp_path), "--khive-config", str(gone)]))
        )
    empty = tmp_path / "empty.toml"
    empty.write_text("[actor]\n")
    with pytest.raises(SystemExit, match=f"--khive-config {empty}: no \\[actor\\].id in it"):
        asyncio.run(
            chat_main(parser().parse_args(["chat", "--dir", str(tmp_path), "--khive-config", str(empty)]))
        )
    assert not list((tmp_path / ".lion" / "chats").glob("*.jsonl"))


def test_resume_last_with_no_log_says_where_logs_live_instead_of_an_index_error(tmp_path):
    a = parser().parse_args(["chat", "--dir", str(tmp_path), "--resume", "last"])
    with pytest.raises(SystemExit, match=r"nothing to continue: no chat logged under .*\.lion/chats yet"):
        asyncio.run(chat_main(a))
    a = parser().parse_args(["chat", "--dir", str(tmp_path), "-r", "20260921"])
    with pytest.raises(SystemExit, match="no chat '20260921': none of 0 under .* and it is not a path"):
        asyncio.run(chat_main(a))
    assert (
        parser().parse_args(["chat", "-c"]).resume == "last"
        and parser().parse_args(["chat", "-r"]).resume == "last"
    )
    assert (
        parser().parse_args(["chat", "-r", "2026"]).resume == "2026"
        and parser().parse_args(["chat"]).resume is None
    )


def test_results_collapse_to_one_line_and_prose_renders_as_markdown_by_default(
    tmp_path, capsys, console_khive
):
    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = console(tmp_path, model="m")
    backend = scripted('<lact r1>read(path="a.txt")</lact>', "It says:\n\n- **alpha**\n- nothing else")
    say = Console("m", None, color=False, echo=True, width=200)
    asyncio.run(chat(actor, profile, backend, asks("read it", None), say=say))
    out = capsys.readouterr().out
    assert (
        "✓ r1 read · 1 line(s), 6 chars" in out and "alpha\n" not in out.split("✓ r1 read")[1].split("m ▸")[0]
    )
    assert "· turn" not in out and "settled:" not in out  # the loop's notices are verbose-only
    assert "• alpha" in out and "**" not in out  # markdown rendered: a bullet, no raw emphasis marks


def test_the_console_cuts_constructs_where_the_parser_does_and_shows_a_value_as_its_shape(
    tmp_path, capsys, console_khive
):
    actor, profile = console(tmp_path, model="m")
    turn = (
        "Writing the note.\n<lvar note.f>P4: <lvar x>inner</lvar> kept whole; `<lvar q>` quoted</lvar>\n"
        'Then the read.\n<lact r1>read(path="a.txt")</lact>\nDone for this turn.'
    )
    (tmp_path / "a.txt").write_text("alpha\n")
    backend = scripted(turn, "ok")
    say = Console("m", None, color=False, echo=True, width=200)
    asyncio.run(chat(actor, profile, backend, asks("go", None), say=say))
    out = capsys.readouterr().out
    assert (
        "= note.f · 54 chars" in out and "inner" not in out and "kept whole" not in out
    )  # the value is cut whole
    assert "Writing the note." in out and "Then the read." in out and "Done for this turn." in out
    assert "<lvar" not in out and "</lvar>" not in out


def test_a_resumed_log_names_the_commands_that_never_settled(tmp_path):
    log = tmp_path / "cut.jsonl"
    text = '<lact r1>read(path="a")</lact><lact r2>read(path="b")</lact>'
    result = {"spec": "read", "ok": True, "value": "x", "error": None, "reason": None}
    rows = [
        {"seq": 0, "kind": "INPUT", "name": "_in0", "turn": 0, "alias": None, "meta": {}, "content": "go"},
        {
            "seq": 1,
            "kind": "SYSTEM",
            "name": "_sys1",
            "turn": 1,
            "alias": None,
            "meta": {},
            "content": "<s>",
        },
        {"seq": 2, "kind": "TEXT", "name": "_m2", "turn": 1, "alias": None, "meta": {}, "content": text},
        {"seq": 3, "kind": "RESULT", "name": "_r3", "turn": 1, "alias": "r1", "meta": {}, "content": result},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert unsettled(load_log(log)) == ["r2"]


def test_ctrl_c_cuts_the_call_once_then_quits_and_quits_at_the_prompt(capsys):
    class Run:
        cut = 0

        def interrupt(self) -> bool:  # the real one cuts a call in flight (True) or pauses (False)
            self.cut += 1
            return self.cut == 1

    class Task:
        cancelled = 0

        def cancel(self):
            self.cancelled += 1

    say = Console("m", None, color=False)
    run, task = Run(), Task()
    press = Interrupt([run], task, say)
    press()
    assert run.cut == 1 and task.cancelled == 0  # the call is cut, then `you ▸`
    assert "cut: the model's call was abandoned" in capsys.readouterr().out
    press()
    assert task.cancelled == 1  # the second press ends it now
    press.presses = 0  # the person was asked: the counter is fresh
    say.asking = True
    press()
    assert task.cancelled == 2  # at the prompt, one press is a quit
    press.presses, say.asking = 0, False
    press()  # nothing in flight to cut: the turn finishes first
    assert run.cut == 2 and task.cancelled == 2
    assert "stopping after this turn" in capsys.readouterr().out


def test_the_console_installs_the_directorys_hooks(tmp_path, console_khive):
    (tmp_path / ".lion").mkdir()
    (tmp_path / ".lion" / "hooks.toml").write_text('[[before]]\nrun = "true"\n[[turn]]\nrun = "echo hi"\n')
    actor, _ = console(tmp_path)
    assert len(actor.befores) == 1 and len(actor.sections) == 1


def test_a_command_that_never_ran_and_a_broken_hook_show_without_verbose(tmp_path, capsys, console_khive):
    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = console(tmp_path, model="m")

    @actor.before
    def gate(run, la, request):
        return "closed" if la.alias == "r3" else None

    @actor.after
    def audit(run, la, request, result):
        if la.alias == "r2":
            raise RuntimeError("audit down")

    backend = scripted(
        '<lact r1>read(path="a.txt")</lact>',
        '<lact r1>read(path="a.txt")</lact><lact r2>read(path="a.txt")</lact>'
        '<lact r3>read(path="a.txt")</lact>',  # r1 again: never runs; r3: refused, which is its own ✗ line
        "done",
    )
    say = Console("m", None, color=False, echo=True, width=200)
    asyncio.run(chat(actor, profile, backend, asks("go", None), say=say))
    out = capsys.readouterr().out
    assert "✗ r1 not run · alias 'r1' already names" in out  # the → line above it would read as run
    assert "! r2 hook failed · " in out and "audit down" in out and "✓ r2 read" in out
    assert out.count("✗ r3") == 1 and "✗ r3 read refused: closed" in out  # the notice's copy is not printed
    assert "· turn" not in out and "settled:" not in out  # the rest of the notice stays verbose-only


def test_a_shell_result_is_marked_by_its_own_rc_not_by_the_handlers_success():
    from lion1.actor import Result
    from lion1.record import Entry, Kind

    say = Console("m", None, color=False, width=200)
    fine = {"rc": 0, "stdout": "a\nb\n", "stderr": "", "elapsed": 1.25}
    bad = {"rc": 7, "stdout": "collecting...\n6 failed, 63 passed\n", "stderr": "", "elapsed": 0.3}
    killed = {"rc": 137, "stdout": "", "stderr": "\nended at the 5s timeout", "elapsed": 5.1}
    lines = [
        say.render(Entry(seq=i, kind=Kind.RESULT, content=Result("bash", True, value=v), alias=f"b{i}"))[0]
        for i, v in enumerate((fine, bad, killed))
    ]
    assert lines[0].startswith("[green]✓ b0 bash[/green]") and "rc 0 · 1.25s · 2 line(s) out" in lines[0]
    assert lines[1].startswith("[red]✗ b1 bash rc 7[/red]") and "6 failed, 63 passed" in lines[1]
    assert lines[2].startswith("[red]✗ b2 bash rc 137[/red]") and "ended at the 5s timeout" in lines[2]


def test_two_consoles_in_one_second_get_two_logs_and_an_existing_log_is_refused(tmp_path):
    from lion_cli.chat import fresh_log, resolve_chat

    chats = tmp_path / ".lion" / "chats"
    a = fresh_log(chats, "20260921-175559")
    b = fresh_log(chats, "20260921-175559")
    assert a[0] == "20260921-175559" and b[0] == "20260921-175559-02" and a[1] != b[1] and b[1].exists()
    (chats / "20260921-175600.jsonl").touch()
    assert (
        resolve_chat(chats, "last").stem == "20260921-175600"
    )  # by id, so the suffix sorts before the next second
    (chats / "20260921-175600.jsonl").unlink()
    assert resolve_chat(chats, "last").stem == "20260921-175559-02"
    Console("m", a[1], color=False).close(
        "x", 0, 0.0, 0, a[1]
    )  # a console never truncates a log it is handed
    a[1].write_text("kept\n")
    Console("m", a[1], color=False)
    assert a[1].read_text() == "kept\n"
    with pytest.raises(SystemExit, match="exists"):
        asyncio.run(chat_main(parser().parse_args(["chat", "--dir", str(tmp_path), "--log", str(a[1])])))


def test_an_explicit_log_is_owned_before_the_session_starts_and_a_failed_start_leaves_none(
    tmp_path, monkeypatch, console_khive
):
    import lion_cli.chat as cli

    log = tmp_path / "logs" / "mine.jsonl"
    seen = []

    async def stub(a, directory, chats, prior, chat_id, log):
        seen.append((chat_id, log.exists()))  # the log is this session's before anything else runs
        if directory.name == "fail":
            raise RuntimeError("no image")
        log.write_text("row\n")

    monkeypatch.setattr(cli, "_chat_main", stub)
    args = ["chat", "--dir", str(tmp_path), "--log", str(log)]
    asyncio.run(chat_main(parser().parse_args(args)))
    assert seen == [("mine", True)] and log.read_text() == "row\n"
    with pytest.raises(SystemExit, match="exists"):  # a second start naming the same log is refused
        asyncio.run(chat_main(parser().parse_args(args)))
    assert seen == [("mine", True)] and log.read_text() == "row\n"
    fail = tmp_path / "fail"
    fail.mkdir()
    other = tmp_path / "logs" / "other.jsonl"
    with pytest.raises(RuntimeError, match="no image"):
        asyncio.run(chat_main(parser().parse_args(["chat", "--dir", str(fail), "--log", str(other)])))
    assert not other.exists() and log.read_text() == "row\n"  # its own empty log removed, no other


def test_a_failure_whose_message_spans_lines_is_said_on_one_line(capsys):
    """A CLI's stderr can run to several lines; the failure is one red line all the same."""
    say = Console("m", None, color=False, width=200)
    say.failed("the model's call failed: claude exited 2: provider unavailable\n  retry after 30 seconds\n")
    assert capsys.readouterr().out.splitlines() == [
        "✗ the model's call failed: claude exited 2: provider unavailable retry after 30 seconds"
    ]


def test_a_backend_that_fails_ends_the_chat_with_one_line_the_closing_line_and_exit_1(
    tmp_path, monkeypatch, capsys, console_khive
):
    """A backend's own error (a model the claude CLI does not know, a timeout) is one red line and
    the closing line, exit 1: no traceback. The record keeps the message sent, so `-r` continues."""
    import lion_cli.chat as cli

    from hub.backend import ClaudeCodeError
    from tests.test_actor import scripted

    async def refusing(messages):
        raise ClaudeCodeError("claude exited 1: unrecognized model opus-5-5")

    monkeypatch.setattr(cli, "backend_for", lambda *a, **k: refusing)
    # the closing line holds the log path: a console wider than any line, so nothing wraps
    monkeypatch.setenv("COLUMNS", str(2 * len(str(tmp_path)) + 1000))
    log = tmp_path / "chat.jsonl"
    args = ["chat", "--dir", str(tmp_path), "--log", str(log), "--say", "hi"]
    with pytest.raises(SystemExit) as died:
        asyncio.run(chat_main(parser().parse_args(args)))
    assert died.value.code == 1
    out = capsys.readouterr().out
    assert "✗ the model's call failed: claude exited 1: unrecognized model opus-5-5" in out
    assert "Traceback" not in out and "failed · 0 call(s)" in out and "continue it: lion chat -c" in out
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [r["content"] for r in rows if r.get("kind") == "INPUT"] == ["hi"]

    async def typed(prompt: str = "", out=None) -> str | None:
        return None  # the person ends it after the reply

    monkeypatch.setattr("lion_cli.view.stdin_ask", typed)
    monkeypatch.setattr(cli, "backend_for", lambda *a, **k: scripted("Hello."))
    asyncio.run(
        chat_main(parser().parse_args(["chat", "--dir", str(tmp_path), "-r", str(log), "--say", "and?"]))
    )
    resumed = max((tmp_path / ".lion" / "chats").glob("*.jsonl"))  # a continuation writes its own log
    rows = [json.loads(ln) for ln in resumed.read_text().splitlines()]
    assert [r["content"] for r in rows if r.get("kind") == "INPUT"] == ["hi", "and?"]
    assert [r["content"] for r in rows if r.get("kind") == "TEXT"] == ["Hello."]


def test_the_logs_usage_rows_read_back_to_the_sum_and_the_count_of_calls_it_misses(
    tmp_path, monkeypatch, capsys, console_khive
):
    """A router call whose known subtotal misses an attempt is partial live and on its USAGE row
    alike: the row keeps `cost_complete`, the failed last call's row too, which the close writes."""
    import lion_cli.chat as cli
    from lion_cli.chat import spent

    from hub.backend import OpenRouter, OpenRouterError
    from hub.backend import openrouter as router
    from tests.test_openrouter import _answers, _NoSleep

    answer = {
        "choices": [{"message": {"content": "Hello."}, "finish_reason": "stop"}],
        "usage": {"cost": 0.1},
    }
    blank = {"choices": [{"message": {"content": None}, "finish_reason": "stop"}], "usage": {"cost": 0.25}}
    # the second call is billed for a blank attempt, then its retry goes out and nothing readable
    # comes back: its known subtotal misses that attempt, and the call fails
    cut = OpenRouterError("request failed: ConnectionResetError")
    monkeypatch.setattr(router, "asyncio", _NoSleep)
    monkeypatch.setattr(router, "_post", _answers(answer, blank, cut))
    backend = OpenRouter(model="m", retries=1)
    monkeypatch.setattr(cli, "backend_for", lambda *a, **k: backend)
    typed = iter(["again"])

    async def ask(prompt: str = "", out=None) -> str | None:
        return next(typed, None)

    monkeypatch.setattr("lion_cli.view.stdin_ask", ask)
    log = tmp_path / "chat.jsonl"
    args = ["chat", "--dir", str(tmp_path), "--log", str(log), "--say", "hi"]
    with pytest.raises(SystemExit):
        asyncio.run(chat_main(parser().parse_args(args)))
    live = spent(backend.calls)
    assert live == (pytest.approx(0.35), 1)
    assert "$0.3500 (partial: 1 call(s) of unknown cost)" in capsys.readouterr().out
    rows: list[dict] = []
    load_log(log, usage=rows)
    assert [(r["turn"], r["total_cost_usd"], r.get("cost_complete")) for r in rows] == [
        (1, 0.1, True),
        (0, 0.25, False),  # written by the close: no entry landed after the failed call
    ]
    assert spent(rows) == live


def test_a_row_from_before_cost_complete_counts_among_the_calls_the_sum_may_miss(
    tmp_path,
):
    """A log from before the key holds rows with a known subtotal and no word on whether every
    attempt is in it. Read back, each counts as a call the sum may miss, beside the calls the newer
    rows say are incomplete or priced at nothing, and none reads as complete. The log is only read."""
    from lion_cli.chat import spent

    def row(n: int, cost, **more) -> dict:
        return {"kind": "USAGE", "turn": n, "model": "m", "total_cost_usd": cost, **more}

    rows = [
        row(1, 0.1),  # from before the key: a subtotal, no word on whether it is whole
        row(2, 0.3),
        row(3, None),  # no figure at all, whenever it was written
        row(4, 0.2, cost_complete=True),
        row(5, 0.25, cost_complete=False),
    ]
    log = tmp_path / "old.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    before = log.read_bytes()
    read: list[dict] = []
    load_log(log, usage=read)
    assert read == rows
    assert spent(read) == (pytest.approx(0.85), 4)  # two of unknown completeness, one unpriced, one cut
    assert spent(read[3:4]) == (pytest.approx(0.2), 0)  # a row that says it is whole still is
    assert log.read_bytes() == before


def test_a_row_says_cost_complete_by_its_figure_when_the_backend_does_not(
    tmp_path, monkeypatch, console_khive
):
    """A CLI's call has a figure or none and no `cost_complete`: its row says which, so the sum read
    back from the log is the live one, and a row without the key is only ever one from before it."""
    import lion_cli.chat as cli
    from lion_cli.chat import spent

    class Metered:
        def __init__(self, envelopes: list[dict]) -> None:
            self.envelopes, self.calls = iter(envelopes), []

        async def __call__(self, messages) -> str:
            self.calls.append(next(self.envelopes))
            return "Hello."

    backend = Metered(
        [
            {"model": "m", "total_cost_usd": 0.02, "usage": {"input_tokens": 5}},
            {"model": "m", "total_cost_usd": None},  # a subscription's call: no figure
            {"model": "m", "total_cost_usd": None, "usage": {"cost": 0.05}},  # only the usage's own
        ]
    )
    monkeypatch.setattr(cli, "backend_for", lambda *a, **k: backend)
    typed = iter(["two", "three"])

    async def ask(prompt: str = "", out=None) -> str | None:
        return next(typed, None)

    monkeypatch.setattr("lion_cli.view.stdin_ask", ask)
    log = tmp_path / "chat.jsonl"
    asyncio.run(
        chat_main(parser().parse_args(["chat", "--dir", str(tmp_path), "--log", str(log), "--say", "hi"]))
    )
    rows: list[dict] = []
    load_log(log, usage=rows)
    assert [r["cost_complete"] for r in rows] == [True, False, True]
    assert spent(backend.calls) == (pytest.approx(0.07), 1)
    assert spent(rows) == spent(backend.calls)


def test_a_claude_code_model_gets_no_tools_of_its_own_one_turn_per_call_and_no_gate_hook(
    tmp_path, monkeypatch, capsys, console_khive
):
    """Everything the model does is a command in its text that the console runs: the CLI it is called
    through gets no tools, one turn a call and no pre-tool hook, since nothing is left to call natively."""
    import shutil

    from hub.backend import claude_code

    calls, which = [], shutil.which

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        calls.append({"args": args, "model": model})
        usage = {"input_tokens": 1, "output_tokens": 1}
        return "Hi.", {"usage": usage, "total_cost_usd": 0.0, "duration_api_ms": 1}

    def found(name, *a, **k):  # no claude binary is needed where the call is faked
        return "/bin/claude" if name == "claude" else which(name, *a, **k)

    monkeypatch.setattr(claude_code, "_exec", fake)
    monkeypatch.setattr(shutil, "which", found)
    log = str(tmp_path / "c.jsonl")
    args = ["chat", "--dir", str(tmp_path), "--model", "claude_code/claude-x", "--log", log, "--say", "hi"]
    asyncio.run(chat_main(parser().parse_args([*args, "--turns", "1"])))
    [call] = calls
    a = call["args"]
    assert call["model"] == "claude-x"
    assert a[a.index("--tools") + 1] == "" and a[a.index("--max-turns") + 1] == "1"
    assert not {"--allowedTools", "--settings", "--mcp-config", "--add-dir"} & set(a)
    for flag in ("--tools", "--max-turns"):
        with pytest.raises(SystemExit) as refused:
            parser().parse_args(["chat", flag, "1"])
        assert refused.value.code == 2 and f"unrecognized arguments: {flag} 1" in capsys.readouterr().err


def test_every_chat_composes_the_persons_instruction_files_in_order_and_opens_by_naming_them(
    tmp_path, monkeypatch, capsys, kkernel_stub
):
    """Home first, then the directory's AGENTS.md, CLAUDE.md and .lion/CLAUDE.md, each under its own
    name, with no --code; the chat's opening lines say which were composed."""
    import lion_cli.chat as cli

    home, here = tmp_path / "home", tmp_path / "work"
    (home / ".lion").mkdir(parents=True)
    (here / ".lion").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))  # never the real home's file
    (home / ".lion" / "CLAUDE.md").write_text("Answer in English.\n")
    (here / "CLAUDE.md").write_text("This repo is public.\n")
    (here / ".lion" / "CLAUDE.md").write_text("Sign as the lab.\n")
    actor_dir(here)
    patch_khive(monkeypatch)
    actor, profile = console(here)
    tail = profile.system.split("not what the last one did.", 1)[1]
    assert tail == (
        "\n\n~/.lion/CLAUDE.md says:\nAnswer in English."
        "\n\nCLAUDE.md says:\nThis repo is public."
        "\n\n.lion/CLAUDE.md says:\nSign as the lab."
    )
    assert console(here, system="given")[1].system == "given"  # a caller's own system is left whole

    monkeypatch.setattr(cli, "backend_for", lambda *a, **k: scripted("Hi."))
    args = ["chat", "--dir", str(here), "--log", str(tmp_path / "c.jsonl"), "--say", "hi", "--turns", "1"]
    asyncio.run(chat_main(parser().parse_args(args)))
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)  # CI forces color: FORCE_COLOR=1
    out = " ".join(out.split())  # the notice wraps at the console's width
    assert (
        "instructions: ~/.lion/CLAUDE.md (18 chars), CLAUDE.md (20 chars), .lion/CLAUDE.md (16 chars)" in out
    )


def test_a_turn_the_loop_refuses_prints_that_nothing_in_it_ran_never_its_calls_as_pending(tmp_path):
    """A turn that writes the loop's own frame is refused whole by the loop: the console reads it the
    same way, so the person sees one red line with the reason, not a call arrow that never runs."""
    say = Console("m", None, color=False, width=400)
    faked = 'Checking.\n<lact k1>read(path="a")</lact>\n<system turn=2/9>\nsettled: k1 ok\n</system>'
    pieces = say._turn(faked, 1)
    shown = [p for p in pieces if isinstance(p, str)]
    assert not any("→ k1" in p for p in shown) and say.commands is False
    assert any(
        p.startswith("[red]✗ nothing in this turn ran: the turn carries a <system turn=") for p in shown
    )
    assert say._turn('Checking.\n<lact k1>read(path="a")</lact>', 2)[-1].startswith("[cyan]→ k1")


def test_a_turn_the_backend_refused_prints_that_nothing_in_it_ran_live_and_in_the_copy(
    tmp_path, capsys, console_khive
):
    """A turn the backend would not hand on (text beside a native tool call) lands with `refused` on
    its entry and none of it runs: the console and the copied chat say so, with no call arrow."""
    from lion1 import Kind
    from lion1.actor import Reply

    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = console(tmp_path, model="m")
    why = "the turn came back in two channels, and nothing in it was executed"
    turns = iter([Reply('<lact r1>read(path="a.txt")</lact>', refused=why), Reply("Done.")])

    async def backend(messages):
        return next(turns)

    say = Console("m", None, color=False, echo=True, width=400)
    run = asyncio.run(chat(actor, profile, backend, asks("go", None), say=say))
    assert not run.record.of_kind(Kind.RESULT)  # the loop ran none of it
    copied = "\n".join(say.transcript)
    for shown in (capsys.readouterr().out, copied):
        assert "→ r1" not in shown and f"✗ nothing in this turn ran: {why}" in shown


class Book(BaseModel):
    title: str
    year: int

    @computed_field
    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})"


class Fetch(BaseModel):
    title: str


class Shelve(BaseModel):
    book: Book


class Echo(BaseModel):
    text: str


class Doc(BaseModel):
    title: str = Field(alias="name")


class Pull(BaseModel):
    title: str


class Named(Doc):
    model_config = ConfigDict(populate_by_name=True)


class Index(BaseModel):
    doc: Doc


class Catalog(BaseModel):
    doc: Named


def shelf(language: str, got: list) -> tuple[Actor, Profile]:
    """A console of six commands: `fetch` returns a Book, `shelve` takes one, `echo` takes text, `pull`
    returns a Doc, whose field is read under an alias, `index` takes a Doc and `catalog` a Named one,
    which validates by the field's name too; `got` collects what `shelve`, `echo`, `index` and
    `catalog` were handed."""
    actor = Actor("shelf", store=fresh_store())

    @actor.handler(Fetch)
    async def fetch(req: Fetch, ctx) -> Book:
        return Book(title=req.title, year=1965)

    @actor.handler(Shelve)
    async def shelve(req: Shelve, ctx) -> str:
        got.append(req.book)
        return "shelved"

    @actor.handler(Echo)
    async def echo(req: Echo, ctx) -> str:
        got.append(req.text)
        return req.text

    @actor.handler(Pull)
    async def pull(req: Pull, ctx) -> Doc:
        return Doc(name=req.title)

    @actor.handler(Index)
    async def index(req: Index, ctx) -> str:
        got.append(req.doc)
        return "indexed"

    @actor.handler(Catalog)
    async def catalog(req: Catalog, ctx) -> str:
        got.append(req.doc)
        return "cataloged"

    return actor, Profile(
        "shelf", specs=frozenset({"fetch", "shelve", "echo", "pull", "index", "catalog"}), language=language
    )


def lion_chat(monkeypatch, tmp_path, made, turns: tuple[str, ...], log, *flags: str) -> list[dict]:
    """One `lion chat` in `tmp_path` writing `log`, over the console `made` builds, the model saying
    `turns` and the person ending it after the reply; the rows the log holds."""
    import lion_cli.chat as cli

    async def typed(prompt: str = "", out=None) -> str | None:
        return None

    monkeypatch.setattr(cli, "console", lambda directory, **kw: made())
    monkeypatch.setattr(cli, "backend_for", lambda *a, **k: scripted(*turns))
    monkeypatch.setattr("lion_cli.view.stdin_ask", typed)
    asyncio.run(chat_main(parser().parse_args(["chat", "--dir", str(tmp_path), "--log", str(log), *flags])))
    return [json.loads(ln) for ln in log.read_text().splitlines()]


def by_alias(rows: list[dict], alias: str) -> dict:
    return next(r for r in rows if r.get("kind") == "RESULT" and r["alias"] == alias)["content"]


def test_a_logged_model_value_resumes_as_its_fields_and_a_command_typed_as_the_model_gets_the_model(
    tmp_path, monkeypatch
):
    """The log holds a model value as its fields, a computed one included; `lion chat -r` replays it
    as that dict, and a command whose argument is typed as the model is handed the model again,
    validated from the dict, the computed field's key ignored."""
    got: list = []
    first = tmp_path / "first.jsonl"
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: shelf("core", got),
        ('<lact b>fetch(title="Dune")</lact>', "Fetched."),
        first,
        "--say",
        "fetch Dune",
    )
    fields = {"title": "Dune", "year": 1965, "label": "Dune (1965)"}
    assert by_alias(rows, "b")["value"] == fields
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: shelf("core", got),
        ("<lact s>shelve(book=*b)</lact>", "Shelved."),
        tmp_path / "second.jsonl",
        "-r",
        str(first),
        "--say",
        "shelve it",
    )
    assert [type(g) for g in got] == [Book] and got == [Book(title="Dune", year=1965)]
    assert (by_alias(rows, "s")["ok"], by_alias(rows, "s")["value"]) == (True, "shelved")
    assert by_alias(rows, "b")["value"] == fields  # the replayed row is logged again as it was read


class Pair(BaseModel):
    pass


def test_a_value_json_cannot_hold_whole_is_logged_with_that_part_as_its_str_and_the_chat_goes_on(
    tmp_path, monkeypatch
):
    """A command whose value has a key JSON cannot hold, or holds itself, is logged with that part as its
    str (ADR-0018 D16), as the store writes its row; the chat goes on to the model's next reply."""
    ring: list = [1]
    ring.append(ring)

    def made():
        actor = Actor("pairs", store=fresh_store())

        @actor.handler(Pair)
        async def pair(req: Pair, ctx) -> dict:
            return {(1, 2): "x", "ring": ring}

        return actor, Profile("pairs", specs=frozenset({"pair"}))

    turns = ("<lact p>pair()</lact>", "Paired.")
    rows = lion_chat(monkeypatch, tmp_path, made, turns, tmp_path / "log.jsonl", "--say", "pair them")
    assert by_alias(rows, "p")["value"] == {"(1, 2)": "x", "ring": [1, "[1, [...]]"]}
    assert [r["content"] for r in rows if r.get("kind") == "TEXT"][-1] == "Paired."


def test_a_resumed_program_reads_a_field_of_a_logged_model_value(tmp_path, monkeypatch):
    """`*v.title` on a model value from the previous session, a command's result and a value the
    program computed from it: its str had no field to read."""
    got: list = []
    first = tmp_path / "first.jsonl"
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: shelf("program", got),
        ('```\nb = fetch(title="Dune")\nv = *b\nf = echo(text=*v.title)\n```', "Fetched."),
        first,
        "--say",
        "fetch Dune",
    )
    computed = next(r for r in rows if r.get("meta", {}).get("values"))["meta"]["values"]
    assert computed == [["v", 2, {"title": "Dune", "year": 1965, "label": "Dune (1965)"}]]
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: shelf("program", got),
        ('```\nt = echo(text=*v.title + " " + *b.title)\ns = shelve(book=*v)\n```', "Done."),
        tmp_path / "second.jsonl",
        "-r",
        str(first),
        "--say",
        "its title?",
    )
    assert by_alias(rows, "t")["value"] == "Dune Dune" and by_alias(rows, "s")["ok"]
    assert got == ["Dune", "Dune Dune", Book(title="Dune", year=1965)] and type(got[2]) is Book


def test_a_log_written_before_model_values_were_logged_as_fields_resumes_the_value_as_its_string(
    tmp_path, monkeypatch
):
    """An older log holds a model value as its str; it resumes as that string, as it always did."""
    from lion1 import Kind, Record
    from lion1.actor import Result

    rec = Record("old")
    rec.append(Kind.INPUT, "fetch Dune")
    rec.append(Kind.TEXT, '<lact b>fetch(title="Dune")</lact>', turn=1)
    rec.append(Kind.RESULT, Result("fetch", True, Book(title="Dune", year=1965)), alias="b", turn=1)
    rec.append(Kind.TEXT, "Fetched.", turn=2)
    old = tmp_path / "old.jsonl"
    old.write_text("".join(json.dumps(entry_row(e), default=str) + "\n" for e in rec))  # the old writer
    was = "title='Dune' year=1965 label='Dune (1965)'"
    assert by_alias([json.loads(ln) for ln in old.read_text().splitlines()], "b")["value"] == was
    got: list = []
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: shelf("core", got),
        ("<lact e>echo(text=*b)</lact>", "Echoed."),
        tmp_path / "new.jsonl",
        "-r",
        str(old),
        "--say",
        "echo it",
    )
    assert got == [was] and by_alias(rows, "e")["value"] == was and by_alias(rows, "b")["value"] == was


def test_a_logged_aliased_model_replays_under_its_field_names_which_only_a_by_name_command_accepts(
    tmp_path, monkeypatch
):
    """The log holds the model by its field names, not by the alias a handler typed as it validates by;
    `lion chat -r` replays that dict, a command typed as the model is refused as invalid arguments
    naming the field, and one typed as a model that validates by name too is handed the model."""
    got: list = []
    first = tmp_path / "first.jsonl"
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: shelf("core", got),
        ('<lact d>pull(title="Dune")</lact>', "Pulled."),
        first,
        "--say",
        "pull Dune",
    )
    assert by_alias(rows, "d")["value"] == {"title": "Dune"}  # the field's name; its alias is `name`
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: shelf("core", got),
        ("<lact i>index(doc=*d)</lact><lact c>catalog(doc=*d)</lact>", "Filed."),
        tmp_path / "second.jsonl",
        "-r",
        str(first),
        "--say",
        "file it",
    )
    refused = by_alias(rows, "i")
    assert (refused["ok"], refused["reason"]) == (False, "invalid")
    assert refused["error"] == "invalid arguments: doc.name: Field required"
    assert (by_alias(rows, "c")["ok"], by_alias(rows, "c")["value"]) == (True, "cataloged")
    assert [type(g) for g in got] == [Named] and got[0].title == "Dune"
    assert by_alias(rows, "d")["value"] == {"title": "Dune"}


class Tags(RootModel[list[str]]):
    pass


class Mark(BaseModel):
    title: str

    @model_serializer
    def as_title(self) -> str:
        return self.title


class Sealed(BaseModel):
    title: str
    key: str = Field(exclude=True)


class Make(BaseModel):
    kind: str


class Filing(BaseModel):
    tags: Tags


class Cite(BaseModel):
    mark: Mark


class Unseal(BaseModel):
    sealed: Sealed


def vault(got: list) -> tuple[Actor, Profile]:
    """A console of four commands: `make` returns a model by kind, a `Tags` root model, a `Mark` whose
    serializer returns its title, or a `Sealed` whose key its dump leaves out; `filing`, `cite` and
    `unseal` take one each and put what they were handed in `got`."""
    actor = Actor("vault", store=fresh_store())

    @actor.handler(Make)
    async def make(req: Make, ctx) -> BaseModel:
        return {
            "tags": Tags(["Dune", "sf"]),
            "mark": Mark(title="Dune"),
            "sealed": Sealed(title="Dune", key="k"),
        }[req.kind]

    @actor.handler(Filing)
    async def filing(req: Filing, ctx) -> str:
        got.append(req.tags)
        return "filed"

    @actor.handler(Cite)
    async def cite(req: Cite, ctx) -> str:
        got.append(req.mark)
        return "cited"

    @actor.handler(Unseal)
    async def unseal(req: Unseal, ctx) -> str:
        got.append(req.sealed)
        return "unsealed"

    return actor, Profile("vault", specs=frozenset({"make", "filing", "cite", "unseal"}), language="core")


def test_a_logged_root_model_resumes_as_its_root_and_a_command_typed_as_it_gets_the_model(
    tmp_path, monkeypatch
):
    """A root model's dump is its root, here a list; `lion chat -r` replays that list and a command
    whose argument is typed as the model is handed the model again, validated from it."""
    got: list = []
    first = tmp_path / "first.jsonl"
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: vault(got),
        ('<lact t>make(kind="tags")</lact>', "Made."),
        first,
        "--say",
        "make tags",
    )
    assert by_alias(rows, "t")["value"] == ["Dune", "sf"]
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: vault(got),
        ("<lact f>filing(tags=*t)</lact>", "Filed."),
        tmp_path / "second.jsonl",
        "-r",
        str(first),
        "--say",
        "file it",
    )
    assert [type(g) for g in got] == [Tags] and got == [Tags(["Dune", "sf"])]
    assert (by_alias(rows, "f")["ok"], by_alias(rows, "f")["value"]) == (True, "filed")
    assert by_alias(rows, "t")["value"] == ["Dune", "sf"]  # the replayed row is logged again as it was read


def test_a_logged_model_its_own_validation_refuses_is_refused_at_replay_as_invalid_arguments(
    tmp_path, monkeypatch
):
    """A serializer that returns a scalar the model does not validate, and a required field the dump
    leaves out, are logged as that dump; the command typed as the model is handed the model live, and
    refused after `lion chat -r` by its own validation, which names what it did not accept."""
    got: list = []
    first = tmp_path / "first.jsonl"
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: vault(got),
        (
            '<lact m>make(kind="mark")</lact><lact s>make(kind="sealed")</lact><lact l>cite(mark=*m)</lact>',
            "Made.",
        ),
        first,
        "--say",
        "make them",
    )
    assert (by_alias(rows, "m")["value"], by_alias(rows, "s")["value"]) == ("Dune", {"title": "Dune"})
    assert (by_alias(rows, "l")["ok"], got) == (True, [Mark(title="Dune")])  # live, the pointer is the model
    rows = lion_chat(
        monkeypatch,
        tmp_path,
        lambda: vault(got),
        ("<lact c>cite(mark=*m)</lact><lact u>unseal(sealed=*s)</lact>", "Tried."),
        tmp_path / "second.jsonl",
        "-r",
        str(first),
        "--say",
        "use them",
    )
    for alias, error in (
        ("c", "invalid arguments: mark: Input should be a valid dictionary or instance of Mark"),
        ("u", "invalid arguments: sealed.key: Field required"),
    ):
        refused = by_alias(rows, alias)
        assert (refused["ok"], refused["reason"], refused["error"]) == (False, "invalid", error)
    assert got == [Mark(title="Dune")]  # neither refused command ran
