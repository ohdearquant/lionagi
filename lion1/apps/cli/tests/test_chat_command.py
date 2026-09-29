"""`lion chat`: the console actor gets khive only in an actor's directory; the view shows prose as
markdown, calls and results as one line each, and logs every entry whole; a chat continues from its
own log; the flags resolve to a chat id or say where the logs live."""

import asyncio
import json
import subprocess

import pytest
from lion_cli.chat import Interrupt, _branch, chat_main, console, unsettled
from lion_cli.main import parser
from lion_cli.view import RESULT_LINES, Console

from hub.chat import chat, load_log
from tests.test_actor import scripted


def asks(*messages: str | None):
    it = iter(messages)

    async def ask() -> str | None:
        return next(it)

    return ask


def test_a_console_in_an_actors_directory_gets_khive_signing_as_that_actor(tmp_path, kkernel_stub):
    actor, profile = console(tmp_path)
    assert "khive_ops" not in profile.specs  # no .khive/config.toml here: no khive tool
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    actor, profile = console(tmp_path, model="deepseek/deepseek-v4.1-flash")
    assert "khive_ops" in profile.specs and "signing as lambda:x" in profile.system
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


def test_a_non_utf8_agents_md_does_not_crash_the_console_and_decodes_best_effort(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"rules: \xe8 latin-1 byte\n")
    actor, profile = console(tmp_path, coding=True)
    assert "AGENTS.md at the root says" in profile.system  # started; did not raise UnicodeDecodeError
    assert "rules:" in profile.system and "latin-1 byte" in profile.system


def test_khive_config_missing_actor_table_or_id_skips_the_tool_and_notices_the_file(tmp_path):
    khive_dir = tmp_path / ".khive"
    khive_dir.mkdir()
    config = khive_dir / "config.toml"
    config.write_text("")  # no [actor] table at all
    notices: list[str] = []
    actor, profile = console(tmp_path, notify=notices.append)
    assert "khive_ops" not in profile.specs
    assert len(notices) == 1 and str(config) in notices[0] and "id" in notices[0]

    config.write_text("[actor]\n")  # [actor] present, no id
    notices2: list[str] = []
    actor, profile = console(tmp_path, notify=notices2.append)
    assert "khive_ops" not in profile.specs
    assert len(notices2) == 1 and str(config) in notices2[0] and "id" in notices2[0]


def test_khive_absent_kkernel_notices_and_keeps_the_console_alive(tmp_path, monkeypatch):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    # neither PATH nor the ~/.cargo/bin fallback resolves a kkernel, on any machine this runs on
    monkeypatch.setattr("hub.khive.shutil.which", lambda name: None)
    monkeypatch.setattr("hub.khive.Path.home", lambda: tmp_path / "no-kkernel-here")
    notices: list[str] = []
    actor, profile = console(tmp_path, notify=notices.append)
    assert "khive_ops" not in profile.specs  # degraded, not crashed
    assert len(notices) == 1 and "khive unavailable" in notices[0] and "kkernel" in notices[0]


def test_unsettled_never_reports_an_alias_the_records_own_namespace_refused(tmp_path):
    # an alias in the record's `_` namespace is refused at declaration and never gets a RESULT, so
    # it must never be reported as unsettled either.
    log = tmp_path / "cut.jsonl"
    text = '<lact _x>read(path="a")</lact><lact r1>read(path="b")</lact>'
    rows = [
        {"seq": 0, "kind": "INPUT", "name": "_in0", "round": 0, "alias": None, "meta": {}, "content": "go"},
        {
            "seq": 1,
            "kind": "SYSTEM",
            "name": "_sys1",
            "round": 1,
            "alias": None,
            "meta": {},
            "content": "<s>",
        },
        {"seq": 2, "kind": "TEXT", "name": "_m2", "round": 1, "alias": None, "meta": {}, "content": text},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert unsettled(load_log(log)) == ["r1"]


def test_the_console_shows_prose_calls_and_results_and_logs_every_entry_whole(tmp_path, capsys):
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
    assert "· round 2" in out and "settled:" in out and "<system" not in out and "context:" not in out
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert len(rows) == len(run.record) == say.written and [r["seq"] for r in rows] == list(range(len(rows)))
    whole = next(
        r for r in rows if r["kind"] == "RESULT" and r["content"]["spec"] == "read" and r["content"]["ok"]
    )
    assert whole["content"]["value"].count("\n") == 40  # the log has the result whole; the console cut it
    assert next(r for r in rows if r["seq"] == 0)["content"] == "what is in big.txt?"


def test_a_resumed_chat_logs_the_whole_record_again_and_prints_only_the_new_part(tmp_path, capsys):
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


def test_results_collapse_to_one_line_and_prose_renders_as_markdown_by_default(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = console(tmp_path, model="m")
    backend = scripted('<lact r1>read(path="a.txt")</lact>', "It says:\n\n- **alpha**\n- nothing else")
    say = Console("m", None, color=False, echo=True, width=200)
    asyncio.run(chat(actor, profile, backend, asks("read it", None), say=say))
    out = capsys.readouterr().out
    assert (
        "✓ r1 read · 1 line(s), 6 chars" in out and "alpha\n" not in out.split("✓ r1 read")[1].split("m ▸")[0]
    )
    assert "· round" not in out and "settled:" not in out  # the loop's notices are verbose-only
    assert "• alpha" in out and "**" not in out  # markdown rendered: a bullet, no raw emphasis marks


def test_the_console_cuts_constructs_where_the_parser_does_and_shows_a_value_as_its_shape(tmp_path, capsys):
    actor, profile = console(tmp_path, model="m")
    turn = (
        "Writing the note.\n<lvar note.f>P4: <lvar x>inner</lvar> kept whole; `<lvar q>` quoted</lvar>\n"
        'Then the read.\n<lact r1>read(path="a.txt")</lact>\nDone for this round.'
    )
    (tmp_path / "a.txt").write_text("alpha\n")
    backend = scripted(turn, "ok")
    say = Console("m", None, color=False, echo=True, width=200)
    asyncio.run(chat(actor, profile, backend, asks("go", None), say=say))
    out = capsys.readouterr().out
    assert (
        "= note.f · 54 chars" in out and "inner" not in out and "kept whole" not in out
    )  # the value is cut whole
    assert "Writing the note." in out and "Then the read." in out and "Done for this round." in out
    assert "<lvar" not in out and "</lvar>" not in out


def test_a_resumed_log_names_the_commands_that_never_settled(tmp_path):
    log = tmp_path / "cut.jsonl"
    text = '<lact r1>read(path="a")</lact><lact r2>read(path="b")</lact>'
    result = {"spec": "read", "ok": True, "value": "x", "error": None, "reason": None}
    rows = [
        {"seq": 0, "kind": "INPUT", "name": "_in0", "round": 0, "alias": None, "meta": {}, "content": "go"},
        {
            "seq": 1,
            "kind": "SYSTEM",
            "name": "_sys1",
            "round": 1,
            "alias": None,
            "meta": {},
            "content": "<s>",
        },
        {"seq": 2, "kind": "TEXT", "name": "_m2", "round": 1, "alias": None, "meta": {}, "content": text},
        {"seq": 3, "kind": "RESULT", "name": "_r3", "round": 1, "alias": "r1", "meta": {}, "content": result},
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
    press()  # nothing in flight to cut: the round finishes first
    assert run.cut == 2 and task.cancelled == 2
    assert "stopping after this round" in capsys.readouterr().out


def test_the_console_installs_the_directorys_hooks(tmp_path):
    (tmp_path / ".lion").mkdir()
    (tmp_path / ".lion" / "hooks.toml").write_text('[[before]]\nrun = "true"\n[[round]]\nrun = "echo hi"\n')
    actor, _ = console(tmp_path)
    assert len(actor.befores) == 1 and len(actor.sections) == 1


def test_a_command_that_never_ran_and_a_broken_hook_show_without_verbose(tmp_path, capsys):
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
    assert "· round" not in out and "settled:" not in out  # the rest of the notice stays verbose-only


def test_a_shell_result_is_marked_by_its_own_rc_not_by_the_handlers_success():
    from lionagi.actor import Result
    from lionagi.record import Entry, Kind

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
    tmp_path, monkeypatch
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
