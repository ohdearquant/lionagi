"""Links in the console: a URL opens in the browser and a path the model names opens in the editor at
its line. A relative path resolves against the chat's directory, never the console's own cwd, and only
a path that exists on disk links; fenced code and bare words stay as written."""

import asyncio
import os
from pathlib import Path
from urllib.parse import quote

import pytest
from lion_cli.view import Console, linked

from lionagi.record import Entry, Kind


def vscode(path: Path, suffix: str = "") -> str:
    return "vscode://file" + quote(os.path.normpath(path)) + suffix


def links(say: Console, pieces: list) -> dict[str, str]:
    """{shown text: link target} for everything `say` would print as a link, read off the rendered
    segments: the target an OSC 8 hyperlink carries is the segment style's `link`."""
    runs: list[list[str]] = []  # [text, target], adjacent segments of one link joined
    at = None
    for piece in pieces:
        renderable = say.out.render_str(piece) if isinstance(piece, str) else piece
        for seg in say.out.render(renderable):
            link = seg.style.link if seg.style else None
            if link and link == at:
                runs[-1][0] += seg.text
            elif link:
                runs.append([seg.text, link])
            at = link
    return {text: target for text, target in runs}


def prose(say: Console, text: str) -> dict[str, str]:
    return links(say, say.render(Entry(seq=0, kind=Kind.TEXT, content=text)))


@pytest.fixture
def chat_dir(tmp_path: Path) -> Path:
    d = tmp_path / "chat"
    (d / "hub" / "agent").mkdir(parents=True)
    (d / "hub" / "agent" / "instruments.py").write_text("x = 1\n")
    (d / "hub" / "__init__.py").write_text("")
    (d / ".khive").mkdir()
    (d / ".khive" / "config.toml").write_text("")
    (d / "a" / "b.md").parent.mkdir()
    (d / "a" / "b.md").write_text("")
    (d / "README").write_text("")
    (d / "main").write_text("")
    return d


def test_a_url_in_prose_links_to_itself_without_the_sentence_around_it(chat_dir):
    say = Console("m", color=False, directory=chat_dir)
    got = prose(say, "Docs at https://example.com/a_b?x=1 and (see https://example.org/p).")
    assert got == {
        "https://example.com/a_b?x=1": "https://example.com/a_b?x=1",
        "https://example.org/p": "https://example.org/p",
    }
    assert prose(Console("m", color=False), "at https://example.com/x.") == {
        "https://example.com/x": "https://example.com/x"
    }  # no directory: URLs still link


def test_a_relative_path_that_exists_links_to_the_editor_with_its_line(chat_dir):
    say = Console("m", color=False, directory=chat_dir)
    target = chat_dir / "hub" / "agent" / "instruments.py"
    assert prose(say, "It is in hub/agent/instruments.py.") == {"hub/agent/instruments.py": vscode(target)}
    assert prose(say, "See hub/agent/instruments.py:12 for it.") == {
        "hub/agent/instruments.py:12": vscode(target, ":12")
    }
    assert prose(say, "at hub/agent/instruments.py:12:5") == {
        "hub/agent/instruments.py:12:5": vscode(target, ":12:5")
    }


def test_the_common_spellings_of_a_path_all_link(chat_dir):
    say = Console("m", color=False, directory=chat_dir, width=1000)  # the absolute path on one line
    text = (
        "Read .khive/config.toml, then ./a/b.md and `hub/agent/instruments.py:1092`; "
        "**hub/__init__.py** too, and the same file by its absolute path "
        f"{chat_dir / 'a' / 'b.md'}."
    )
    assert prose(say, text) == {
        ".khive/config.toml": vscode(chat_dir / ".khive" / "config.toml"),
        "./a/b.md": vscode(chat_dir / "a" / "b.md"),
        "hub/agent/instruments.py:1092": vscode(chat_dir / "hub" / "agent" / "instruments.py", ":1092"),
        "hub/__init__.py": vscode(chat_dir / "hub" / "__init__.py"),  # its underscores kept, not emphasis
        str(chat_dir / "a" / "b.md"): vscode(chat_dir / "a" / "b.md"),
    }


def test_a_relative_path_resolves_against_the_chats_directory_never_the_cwd(chat_dir, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "notes").mkdir(parents=True)
    (elsewhere / "notes" / "plan.md").write_text("the cwd's")
    (chat_dir / "notes").mkdir()
    (chat_dir / "notes" / "plan.md").write_text("the chat's")
    monkeypatch.chdir(elsewhere)
    say = Console("m", color=False, directory=chat_dir)
    assert prose(say, "the plan: notes/plan.md") == {"notes/plan.md": vscode(chat_dir / "notes" / "plan.md")}
    (elsewhere / "only-here.md").write_text("")
    assert prose(say, "and only-here.md") == {}  # it exists in the cwd, not in the chat's directory
    assert prose(Console("m", color=False), "the plan: notes/plan.md") == {}  # no directory: no guess


def test_a_path_that_does_not_exist_a_fenced_one_and_a_bare_word_stay_plain(chat_dir):
    for text in (
        "Create hub/agent/missing.py and docs/x.md:3.",
        "Run it:\n\n```bash\ncat hub/agent/instruments.py\nopen https://example.com\n```\n",
        "~~~\nhub/agent/instruments.py\n~~~~\n",
        "The README says to run main, version 3.14.",
        "`cat hub/agent/instruments.py` is a command, not a path",
        "Already [the file](hub/agent/instruments.py) and <https://example.com>, and \\`hub/x`.",
    ):
        assert linked(text, chat_dir) == text, text


def test_prose_after_a_closed_fence_links_again_and_an_unclosed_fence_runs_to_the_end(chat_dir):
    out = linked("```\nhub/agent/instruments.py\n````\nthen hub/agent/instruments.py\n", chat_dir)
    head, tail = out.split("````\n")
    assert head == "```\nhub/agent/instruments.py\n" and tail.startswith("then [hub/agent/instruments.py](<")
    inner = "```\nhub/agent/instruments.py\n``` not a close\nhub/agent/instruments.py\n"
    assert linked(inner, chat_dir) == inner


def test_a_code_span_holding_a_url_links_and_a_long_name_is_just_not_a_file(chat_dir):
    say = Console("m", color=False, directory=chat_dir)
    assert prose(say, "see `https://example.com/x`") == {"https://example.com/x": "https://example.com/x"}
    long = "a" * 300 + ".py"
    assert linked(f"at {long} and `{long}`", chat_dir) == f"at {long} and `{long}`"


def test_a_calls_path_argument_links_when_it_resolves_a_directory_too(chat_dir):
    say = Console("m", color=False, directory=chat_dir)
    turn = (
        '<lact r1>read(path="hub/agent/instruments.py")</lact>'
        '<lact l1>list_dir(path="hub")</lact>'
        '<lact r2>read(path="nope.py")</lact>'
        '<lact e1>edit(path="a/b.md", old="[x", new="y]")</lact>'
    )
    pieces = say.render(Entry(seq=0, kind=Kind.TEXT, content=turn))
    assert links(say, pieces) == {
        '"hub/agent/instruments.py"': vscode(chat_dir / "hub" / "agent" / "instruments.py"),
        '"hub"': vscode(chat_dir / "hub"),
        '"a/b.md"': vscode(chat_dir / "a" / "b.md"),
    }
    shown = [say.out.render_str(p).plain for p in pieces]
    assert '→ e1 edit(path="a/b.md", old="[x", new="y]")' in shown, shown  # brackets in values stay text
    assert '→ r2 read(path="nope.py")' in shown


def test_a_url_in_a_notice_is_a_terminal_hyperlink(capsys):
    say = Console("m", color=True, width=200)
    say.notice("the run is at https://example.com/runs/7. [not markup]")
    out = capsys.readouterr().out
    assert "\x1b]8;id=" in out and ";https://example.com/runs/7\x1b\\" in out  # OSC 8, to itself
    assert "the run is at " in out and ". [not markup]" in out  # the rest as written, never markup


def test_the_chat_hands_its_directory_to_the_console(tmp_path, monkeypatch):
    import lion_cli.chat as cli
    from lion_cli.main import parser

    seen = {}

    class Stop(Exception):
        pass

    def console(*args, **kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(cli, "Console", console)
    monkeypatch.setattr(cli, "backend_for", lambda *a, **kw: object())
    a = parser().parse_args(["chat", "--dir", str(tmp_path)])
    log = tmp_path / ".lion" / "chats" / "x.jsonl"
    with pytest.raises(Stop):
        asyncio.run(cli._chat_main(a, tmp_path.resolve(), log.parent, None, "x", log))
    assert seen["directory"] == tmp_path.resolve()
