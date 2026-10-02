"""The coding tools over a local tree: numbered spans, a search that honours ignores and reports no
match, an edit that lands only where `old` is unique and serializes with another edit of the same file."""

import asyncio
import itertools
from pathlib import Path

import pytest

from hub import LocalTree, code
from hub.tools.code import Edit, numbered, replace_once
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
    # rg drops the filename column when `path` names one file directly, so a
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
    # numbered's own (1, 0, 0) span read back as "lines 1-0 of 0", a range that
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
    # read_text() folded CRLF to LF and the write rewrote every line of a three-line file
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
    # a file with one LF line among CRLF ones was neither "a CRLF file" nor an LF
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


def test_an_edit_leaves_a_bare_carriage_return_where_it_was(tmp_path):
    # a line's ending was stripped as any run of CR and LF while the edit folded only CRLF, so a CR
    # not before an LF read as part of the ending and was written back once more on every edit
    actor, profile = tree(tmp_path)
    (tmp_path / "one.txt").write_bytes(b"a\r\nb\r")
    (tmp_path / "two.txt").write_bytes(b"x\r\r\ny\r\n")
    run = asyncio.run(
        actor.run(
            profile,
            RunRequest(inputs=("go",), max_rounds=2),
            scripted(
                '<lact e1>edit(path="one.txt", old="a", new="A")</lact>'
                '<lact e2>edit(path="two.txt", old="y", new="Y")</lact>',
                "done",
            ),
        )
    )
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["e1"].ok and r["e2"].ok, (r["e1"].error, r["e2"].error)
    written = ((tmp_path / "one.txt").read_bytes(), (tmp_path / "two.txt").read_bytes())
    assert written == (b"A\r\nb\r", b"x\r\r\nY\r\n")


def test_read_lines_shows_a_bare_carriage_return_as_text(tmp_path):
    # the read dropped it with the ending, so an `old` copied from the read missed the line
    actor, profile = tree(tmp_path)
    (tmp_path / "one.txt").write_bytes(b"a\r\nb\r")
    (tmp_path / "two.txt").write_bytes(b"x\r\r\ny\r\n")
    run = asyncio.run(
        actor.run(
            profile,
            RunRequest(inputs=("go",), max_rounds=2),
            scripted(
                '<lact r1>read_lines(path="one.txt")</lact><lact r2>read_lines(path="two.txt")</lact>',
                "done",
            ),
        )
    )
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["r1"].value == "one.txt lines 1-2 of 2\n1: a\n2: b\r"
    assert r["r2"].value == "two.txt lines 1-2 of 2\n1: x\r\n2: y"


def test_an_edit_of_a_small_corpus_matches_the_read_and_changes_no_other_line(tmp_path):
    # every string of length 0..6 over a, CR and LF holding a CRLF: the file as read_lines shows it is
    # what an edit matches, an edit of it to itself writes the same bytes, and in a file whose LFs all
    # follow a CR an edit of one line leaves every other line byte-identical (with mixed endings the
    # diff may pair an untouched line with an equal line ending otherwise, so those take only the first)
    actor = Actor("c")
    code(actor, LocalTree(tmp_path))
    edit, f = actor.handlers["edit"].fn, tmp_path / "f.txt"

    async def corpus() -> int:
        edits = 0
        for chars in itertools.chain.from_iterable(itertools.product("a\r\n", repeat=n) for n in range(7)):
            text = "".join(chars)
            if "\r\n" not in text:
                continue
            shown = [row.partition(": ")[2] for row in numbered(text)[0].split("\n")]
            tail = "\n" if text.endswith("\n") else ""
            read = "\n".join(shown) + tail
            f.write_bytes(text.encode())
            await edit(Edit(path="f.txt", old=read, new=read), None)
            assert f.read_bytes().decode() == text, repr(text)
            edits += 1
            if text.count("\n") != text.count("\r\n"):
                continue
            lines = text.split("\n")
            for i in range(len(shown)):
                new = "\n".join([*shown[:i], "Z", *shown[i + 1 :]]) + tail
                f.write_bytes(text.encode())
                await edit(Edit(path="f.txt", old=read, new=new), None)
                want = [*lines[:i], "Z\r" if i < len(lines) - 1 else "Z", *lines[i + 1 :]]
                assert f.read_bytes().decode().split("\n") == want, (repr(text), i)
                edits += 1
        return edits

    assert asyncio.run(corpus()) == 484 + 306  # every string once, then each line of the 161 CRLF-only ones


def test_search_reaches_hidden_files_but_never_the_git_directory(tmp_path):
    # rg skips dotfiles by default, so `.github/ci.yml` was invisible and `.git` would not be
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
    # `..`, an absolute path elsewhere and a symlink out of the tree all reached the host
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


def test_an_edit_keeps_the_ending_of_a_line_holding_a_unicode_separator():
    # the numbering split lines at U+2028 (str.splitlines) while the edit matched at LF: the line
    # before the edited one read as two, and the ending given back to it was the LF of the edit side
    from hub.tools.code import numbered, reend, replace_once

    before = "left\u2028right\r\ntarget = 1\r\n"
    assert numbered(before)[3] == 2 and numbered(before)[0] == "1: left\u2028right\n2: target = 1"
    after = replace_once(before.replace("\r\n", "\n"), "target = 1", "target = 2")
    assert reend(before, after) == "left\u2028right\r\ntarget = 2\r\n"
    assert reend("a\x85b\r\nc\n", "a\x85b\nC\n") == "a\x85b\r\nC\n"  # NEL likewise
