"""Copying a response: `/copy` and `/copy N` at the prompt, Alt-C in the editor, the console's own
commands never reaching the model, and the clipboard's routes (the platform's tool, else OSC 52)."""

import asyncio
import io
import json
import subprocess

import pytest
from lion_cli.chat import chat_main, console
from lion_cli.clipboard import to_clipboard
from lion_cli.main import parser
from lion_cli.view import Console, Editor

from hub.chat import chat, load_log
from tests.test_actor import scripted


def asks(*messages: str | None):
    it = iter(messages)

    async def ask() -> str | None:
        return next(it)

    return ask


@pytest.fixture
def copied(monkeypatch) -> list[str]:
    """What reached the clipboard; the real one is never touched."""
    got: list[str] = []

    def take(text: str) -> str:
        got.append(text)
        return "test"

    monkeypatch.setattr("lion_cli.view.to_clipboard", take)
    return got


def test_copy_takes_the_last_response_or_the_numbered_one_and_a_missing_number_copies_nothing(
    tmp_path, capsys, copied
):
    actor, profile = console(tmp_path, model="m")
    say = Console("m", None, color=False, width=200)
    asyncio.run(
        chat(
            actor, profile, scripted("First answer.", "Second **answer**."), asks("one", "two", None), say=say
        )
    )
    capsys.readouterr()
    assert say.command("/copy") and copied == ["Second **answer**."]  # the prose as written, unwrapped
    assert say.command("  /copy 1 ") and copied[1:] == ["First answer."]
    assert say.command("/copy 9") and len(copied) == 2
    assert say.command("/copy two") and len(copied) == 2
    out = capsys.readouterr().out
    assert "copied response 2 (18 chars)" in out and "copied response 1 (13 chars)" in out
    assert "no response 9: this chat has 2; nothing copied" in out
    assert "/copy takes a response number, from 1: not 'two'" in out
    # the person's own words, never the console's
    assert not say.command("/copyright") and not say.command("copy that") and len(copied) == 2


def test_a_copied_response_is_its_prose_without_the_lndl_and_a_command_only_turn_is_not_one(
    tmp_path, capsys, copied
):
    (tmp_path / "a.txt").write_text("alpha\n")
    actor, profile = console(tmp_path, model="m")
    say = Console("m", None, color=False, width=200)
    backend = scripted(
        'Let me look.\n<lact r1>read(path="a.txt")</lact>\nThen I answer.',
        '<lact l1>list_dir(path=".")</lact>',
        "It says alpha.",
    )
    asyncio.run(chat(actor, profile, backend, asks("go", None), say=say))
    assert say.responses == ["Let me look.\n\nThen I answer.", "It says alpha."]
    assert say.command("/copy 1") and copied == ["Let me look.\n\nThen I answer."]
    assert "<lact" not in copied[0] and "read(" not in copied[0]
    assert say.command("/copy 3") and len(copied) == 1


def test_a_continued_chat_counts_its_replayed_responses_first(tmp_path, capsys, copied):
    log1, log2 = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    asyncio.run(
        chat(*console(tmp_path, name="c1"), scripted("Old reply."), asks("hi", None), say=Console("m", log1))
    )
    history = load_log(log1)
    capsys.readouterr()
    say = Console("m", log2, color=False, width=200)
    say.quiet_until = len(history) - 1
    asyncio.run(
        chat(
            *console(tmp_path, name="c2"),
            scripted("New reply."),
            asks("again", None),
            say=say,
            history=history,
        )
    )
    assert "Old reply." not in capsys.readouterr().out  # replayed, not printed, and still counted
    assert say.command("/copy 1") and say.command("/copy") and copied == ["Old reply.", "New reply."]


def test_nothing_to_copy_before_the_first_response(capsys, copied):
    say = Console("m", None, color=False, width=200)
    assert say.command("/copy") and copied == []
    assert "nothing to copy: no response yet" in capsys.readouterr().out


def test_a_copy_typed_at_the_chat_prompt_is_the_consoles_and_never_reaches_the_record(
    tmp_path, monkeypatch, capsys, copied
):
    import lion_cli.chat as cli

    lines = iter(["one", "two", "/copy", "/copy 1", "/copy 9", None])

    async def typed(prompt: str = "", out=None) -> str | None:
        return next(lines)

    monkeypatch.setattr("lion_cli.view.stdin_ask", typed)
    # two replies only: a /copy sent on to the model would ask for a third and exhaust the script
    monkeypatch.setattr(cli, "backend_for", lambda *a, **k: scripted("First.", "Second."))
    log = tmp_path / "chat.jsonl"
    asyncio.run(chat_main(parser().parse_args(["chat", "--dir", str(tmp_path), "--log", str(log)])))
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [r["content"] for r in rows if r.get("kind") == "INPUT"] == ["one", "two"]
    assert [r["content"] for r in rows if r.get("kind") == "TEXT"] == ["First.", "Second."]
    assert copied == ["Second.", "First."]
    out = capsys.readouterr().out
    assert "copied response 2 (7 chars)" in out and "copied response 1 (6 chars)" in out
    assert "no response 9: this chat has 2; nothing copied" in out
    assert "/copy [N] copies a response" in out


def test_alt_c_in_the_editor_copies_and_shows_the_notice_above_the_prompt_leaving_the_line_as_typed():
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output

    calls: list[int] = []

    def copy_last() -> str | None:
        calls.append(1)
        return None if len(calls) > 1 else "copied response 3 (42 chars)"

    async def typed(keys: str) -> tuple[str | None, str]:
        with create_pipe_input() as pipe:
            screen = io.StringIO()
            out = Vt100_Output(screen, lambda: Size(rows=24, columns=100), term="xterm")
            ed = Editor(None, copy_last=copy_last, input=pipe, output=out)
            pipe.send_text(keys)
            got = await ed.ask("you > ")
            for _ in range(100):  # the notice prints on its own task, above the prompt
                if len(calls) == want:
                    break
                await asyncio.sleep(0.01)
            return got, screen.getvalue()

    want = 1
    got, drawn = asyncio.run(typed("ab\x1bccd\r"))  # Alt-C is Escape, c
    assert got == "abcd" and calls == [1]
    assert "copied response 3 (42 chars)" in drawn
    want = 2
    got, drawn = asyncio.run(typed("\x1bcx\r"))
    assert got == "x" and calls == [1, 1] and "copied response" not in drawn  # None: nothing to show

    async def without() -> str | None:
        with create_pipe_input() as pipe:
            from prompt_toolkit.output import DummyOutput

            pipe.send_text("\x1bcy\r")
            return await Editor(None, input=pipe, output=DummyOutput()).ask("you > ")

    assert asyncio.run(without()) == "y"  # no copy_last: Alt-C is unbound, the line still sends


def test_the_clipboard_takes_pbcopy_when_present_and_hands_it_the_text_on_stdin(monkeypatch, capsys):
    ran: list[tuple] = []

    def run(argv, **kw):
        ran.append((tuple(argv), kw["input"]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(
        "lion_cli.clipboard.shutil.which", lambda name: f"/usr/bin/{name}" if name == "pbcopy" else None
    )
    monkeypatch.setattr("lion_cli.clipboard.subprocess.run", run)
    assert to_clipboard("héllo\nwörld") == "pbcopy"
    assert ran == [(("pbcopy",), "héllo\nwörld".encode())]
    assert capsys.readouterr().out == ""  # no escape when a tool took it


def test_a_tool_that_fails_passes_to_the_next_and_none_left_writes_osc_52(monkeypatch, capsys):
    ran: list[str] = []

    def run(argv, **kw):
        ran.append(argv[0])
        if argv[0] == "wl-copy":  # outside Wayland
            return subprocess.CompletedProcess(argv, 1)
        raise subprocess.TimeoutExpired(argv, 5)  # xclip that never returns

    monkeypatch.setattr("lion_cli.clipboard.shutil.which", lambda name: None if name == "pbcopy" else name)
    monkeypatch.setattr("lion_cli.clipboard.subprocess.run", run)
    assert to_clipboard("hi there") == "osc52" and ran == ["wl-copy", "xclip"]
    assert capsys.readouterr().out == "\x1b]52;c;aGkgdGhlcmU=\x07"
    ran.clear()
    monkeypatch.setattr("lion_cli.clipboard.shutil.which", lambda name: None)
    assert to_clipboard("hi there") == "osc52" and ran == []  # nothing present: nothing run
    assert capsys.readouterr().out == "\x1b]52;c;aGkgdGhlcmU=\x07"
