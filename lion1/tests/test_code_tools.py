"""The coding tools over a local tree: numbered spans, a search that honours ignores and reports no
match, an edit that lands only where `old` is unique and serializes with another edit of the same file."""

import asyncio
from pathlib import Path

import pytest

from hub import LocalTree, code
from hub.tools.code import numbered, replace_once
from lionagi import Actor, Kind, Profile, RunRequest
from lionagi.actor import DEFAULT_HANDLERS
from tests.test_actor import scripted


def tree(tmp_path: Path) -> tuple[Actor, Profile]:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return f() + 1\n")
    (tmp_path / "notes.txt").write_text("f is one\n")
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "m.py").write_text("def f():\n    return 99\n")
    actor = Actor("c")
    specs = code(actor, LocalTree(tmp_path)) | {h.name for h in DEFAULT_HANDLERS}
    return actor, Profile(name="c", specs=frozenset(specs), system="x")


def test_numbered_and_replace_once_are_exact():
    body, start, end, total = numbered("a\nb\nc\nd\n", 2, 9)
    assert (body, start, end, total) == ("2: b\n3: c\n4: d", 2, 4, 4)
    assert replace_once("x = 1\ny = 1\n", "y = 1", "y = 2") == "x = 1\ny = 2\n"
    with pytest.raises(ValueError, match="occurs 2 times"):
        replace_once("x = 1\ny = 1\n", "= 1", "= 2")
    with pytest.raises(ValueError, match="occurs 0 times"):
        replace_once("x = 1\n", "z", "")


def test_read_lines_search_and_edit_over_a_tree(tmp_path):
    actor, profile = tree(tmp_path)
    backend = scripted(
        '<lact r1>read_lines(path="pkg/m.py", start=5, end=99)</lact>'
        '<lact s1>search(pattern="return f", path=".")</lact>'
        '<lact s2>search(pattern="def f", include="*.txt")</lact>'
        '<lact s3>search(pattern="[unclosed", path=".")</lact>',
        '<lact e1>edit(path="pkg/m.py", old="return 1\\n", new="return 2\\n")</lact>'
        '<lact e2>edit(path="pkg/m.py", old="return f() + 1", new="return f() + 2")</lact>'
        '<lact e3>edit(path="pkg/m.py", old="return", new="yield")</lact>',
        "done",
    )
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=3), backend))
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["r1"].value == "pkg/m.py lines 5-6 of 6\n5: def g():\n6:     return f() + 1"
    assert r["s1"].value.splitlines() == ["pkg/m.py:6:    return f() + 1"]  # build/ is ignored
    assert r["s2"].value == "(no matches)"
    # rg says "unclosed character class", GNU grep "Unmatched [", BSD grep "brackets ([ ]) not balanced"
    assert r["s3"].ok is False and any(
        w in r["s3"].error.lower() for w in ("unclosed", "unmatched", "balanced")
    )
    assert r["e1"].ok and r["e2"].ok  # two edits of one file in one round: both land
    assert r["e3"].ok is False and "occurs 2 times" in r["e3"].error
    assert (
        tmp_path / "pkg" / "m.py"
    ).read_text() == "def f():\n    return 2\n\n\ndef g():\n    return f() + 2\n"


def test_search_over_a_single_file_still_prints_the_filename(tmp_path):
    # P3-2 (hub review): rg drops the filename column when `path` names one file directly, so a
    # single-file search read back as `line:text` instead of the promised `file:line:text`
    actor, profile = tree(tmp_path)
    run = asyncio.run(
        actor.run(
            profile,
            RunRequest(inputs=("go",), max_rounds=2),
            scripted('<lact s>search(pattern="return f", path="pkg/m.py")</lact>', "done"),
        )
    )
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["s"].value.splitlines() == ["pkg/m.py:6:    return f() + 1"]


def test_read_lines_on_an_empty_file_says_so(tmp_path):
    # P3-4 (hub review): numbered's own (1, 0, 0) span read back as "lines 1-0 of 0", a range that
    # names no line
    actor, profile = tree(tmp_path)
    (tmp_path / "empty.py").write_text("")
    run = asyncio.run(
        actor.run(
            profile,
            RunRequest(inputs=("go",), max_rounds=2),
            scripted('<lact r>read_lines(path="empty.py")</lact>', "done"),
        )
    )
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["r"].ok and r["r"].value == "empty.py is empty"


def test_an_edit_keeps_a_crlf_files_other_bytes(tmp_path):
    # F08: read_text() folded CRLF to LF and the write rewrote every line of a three-line file
    actor, profile = tree(tmp_path)
    (tmp_path / "win.txt").write_bytes(b"a = 1\r\nb = 1\r\nc = 1\r\n")
    run = asyncio.run(
        actor.run(
            profile,
            RunRequest(inputs=("go",), max_rounds=2),
            scripted('<lact e>edit(path="win.txt", old="b = 1", new="b = 2")</lact>', "done"),
        )
    )
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["e"].ok, r["e"].error
    assert (tmp_path / "win.txt").read_bytes() == b"a = 1\r\nb = 1\r\nc = 1\r\n".replace(b"b = 1", b"b = 2")


def test_an_edit_keeps_each_line_of_a_mixed_endings_file(tmp_path):
    # P2-9 (hub review): a file with one LF line among CRLF ones was neither "a CRLF file" nor an LF
    # one, so an edit rewrote every ending as LF; each unchanged line keeps its own now
    from hub.tools.code import reend

    actor, profile = tree(tmp_path)
    (tmp_path / "mixed.txt").write_bytes(b"a = 1\r\nb = 1\nc = 1\r\nd = 1")
    run = asyncio.run(
        actor.run(
            profile,
            RunRequest(inputs=("go",), max_rounds=2),
            scripted('<lact e>edit(path="mixed.txt", old="c = 1", new="c = 2\nc2 = 3")</lact>', "done"),
        )
    )
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["e"].ok, r["e"].error
    assert (tmp_path / "mixed.txt").read_bytes() == b"a = 1\r\nb = 1\nc = 2\r\nc2 = 3\r\nd = 1"
    assert reend("x\r\ny\n", "x\ny\nz\n") == "x\r\ny\nz\n" and reend("x\r\n", "w\nx\n") == "w\r\nx\r\n"


def test_search_reaches_hidden_files_but_never_the_git_directory(tmp_path):
    # P2-1: rg skips dotfiles by default, so `.github/ci.yml` was invisible and `.git` would not be
    actor, profile = tree(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("needle: 1\n")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("needle\n")
    run = asyncio.run(
        actor.run(
            profile,
            RunRequest(inputs=("go",), max_rounds=2),
            scripted('<lact s>search(pattern="needle", path=".")</lact>', "done"),
        )
    )
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["s"].ok, r["s"].error
    assert ".github/ci.yml" in str(r["s"].value) and ".git/HEAD" not in str(r["s"].value)


def test_the_local_tree_refuses_paths_that_leave_it(tmp_path):
    # F11: `..`, an absolute path elsewhere and a symlink out of the tree all reached the host
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "in.py").write_text("x = 1\n")
    (root / "link.py").symlink_to(outside)
    t = LocalTree(root)
    assert asyncio.run(t.read("in.py")) == "x = 1\n"
    assert asyncio.run(t.read(str(root / "in.py"))) == "x = 1\n"  # absolute inside the tree is fine
    for path in ("../outside.py", str(outside), "link.py"):
        with pytest.raises(PermissionError, match="outside"):
            asyncio.run(t.read(path))
        with pytest.raises(PermissionError, match="outside"):
            asyncio.run(t.write(path, "changed\n"))
    with pytest.raises(PermissionError, match="outside"):
        asyncio.run(t.search("secret", "..", None))
    assert outside.read_text() == "secret = 1\n"


def test_the_local_tree_lists_a_directory_with_a_slash_after_each_directory(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a.py").write_text("x\n")
    (tmp_path / ".hidden").write_text("h\n")
    t = LocalTree(tmp_path)
    assert asyncio.run(t.list_dir(".")) == [".hidden", "a.py", "b/"]
    assert asyncio.run(t.list_dir("b")) == []
    with pytest.raises(PermissionError, match="outside"):
        asyncio.run(t.list_dir(".."))
