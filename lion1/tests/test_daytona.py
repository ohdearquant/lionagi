"""The Daytona box in the hub. A stub client runs the box's shell lines on this machine (the wrapper
that splits stdout from stderr and carries the rc is the part that has to be right), and the real
service is exercised once end to end with `LION_DAYTONA_E2E=1`: a box from a public image, `bash`
and `python` through `shell()`, the code tools through `BoxTree`, a directory pushed in, stop."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hub import BoxTree, DaytonaSandbox, code, shell
from hub.tools import daytona as daytona_mod
from lionagi import Actor, Kind, Profile, RunRequest
from lionagi.actor import DEFAULT_HANDLERS
from tests.test_actor import scripted


def stub_daytona(root: Path, ok: list[bool]) -> types.ModuleType:
    """A `daytona` module whose one box runs commands here: `cwd` is `root`, files live under it, and
    `timeout -s KILL <s> argv` is a shim that ends the command at `<s>` with rc 137, as the guest's
    does (coreutils is not on every machine)."""
    shim = root / "shim"
    shim.mkdir()
    (shim / "timeout").write_text(
        f"#!{sys.executable}\nimport subprocess, sys\n"
        "try:\n    sys.exit(subprocess.run(sys.argv[4:], timeout=float(sys.argv[3])).returncode)\n"
        "except subprocess.TimeoutExpired:\n    sys.exit(137)\n"
    )
    (shim / "timeout").chmod((shim / "timeout").stat().st_mode | stat.S_IEXEC)

    class DaytonaError(Exception):
        pass

    class Timeout(Exception):
        pass

    def local(path: str) -> Path:  # the box's /tmp is ours; its workdir is an absolute path here already
        return root / path.lstrip("/") if path.startswith("/tmp/lion-") else Path(path)

    class Proc:
        async def exec(self, cmd, cwd=None, env=None, timeout=None):
            if not ok[0]:
                raise DaytonaError("bad request: failed to resolve container IP after 3 attempts")
            cmd = cmd.replace("/tmp/lion-", f"{root}/tmp/lion-")  # the box's /tmp is ours
            done = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                cwd=root,
                env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", **(env or {})},
            )
            return types.SimpleNamespace(exit_code=done.returncode, result=done.stdout + done.stderr)

    class FS:
        async def upload_file(self, src, dst, timeout=None):
            p = local(dst)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(src if isinstance(src, bytes) else Path(src).read_bytes())

        async def download_file(self, path):
            p = local(path)
            if not p.exists():
                raise DaytonaError(f"no such file: {path}")
            return p.read_bytes()

    class Box:
        def __init__(self):
            self.process, self.fs, self.deleted = Proc(), FS(), False

        async def delete(self):
            self.deleted = True

    class Client:
        made: list[Box] = []

        def __init__(self, cfg):
            pass

        async def create(self, params, timeout=None):
            self.made.append(Box())
            self.made[-1].id = f"stub-box-{len(self.made)}"
            return self.made[-1]

        async def close(self):
            pass

    mod = types.ModuleType("daytona")
    mod.AsyncDaytona = Client
    mod.DaytonaConfig = lambda api_key: None
    mod.CreateSandboxFromImageParams = lambda **kw: kw
    mod.Resources = lambda **kw: kw
    mod.DaytonaError = DaytonaError
    mod.DaytonaProcessExecutionTimeoutError = Timeout
    mod.Client = Client
    return mod


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    ok = [True]
    mod = stub_daytona(tmp_path, ok)
    monkeypatch.setitem(sys.modules, "daytona", mod)
    monkeypatch.setattr(daytona_mod, "api_key", lambda: "k")
    (tmp_path / "tmp").mkdir()
    work = tmp_path / "work"
    box = asyncio.run(DaytonaSandbox("img", workdir=str(work)).start())
    return box, work, ok, mod


def test_exec_splits_stdout_from_stderr_carries_the_rc_and_feeds_stdin(stubbed):
    box, work, _, _ = stubbed
    r = asyncio.run(box.exec(["bash", "-o", "pipefail", "-c", "echo out; echo bad >&2; printf tail; exit 3"]))
    assert (r["rc"], r["stdout"], r["stderr"]) == (3, "out\ntail", "bad\n")
    r = asyncio.run(box.exec(["cat"], stdin=b"fed\n"))
    assert (r["rc"], r["stdout"], r["stderr"]) == (0, "fed\n", "")
    assert not list((work.parent / "tmp").iterdir())  # the stdin and stderr files are gone after
    r = asyncio.run(box.exec(["sh", "-c", "exit 137"], timeout=7))
    assert r["rc"] == 137 and r["stderr"] == "" and r["timed_out"] is False  # any SIGKILL is 137
    r = asyncio.run(box.exec(["sleep", "5"], timeout=1))  # the clock says it was the timeout
    assert r["rc"] == 137 and r["timed_out"] is True and r["stderr"] == "ended at the 1s timeout"
    r = asyncio.run(box.exec(["sh", "-c", "exit 124"], timeout=7))  # a command's own 124 is not the clock's
    assert r["rc"] == 124 and r["timed_out"] is False


def test_the_shell_and_code_tools_run_in_the_box_through_the_same_registrations(stubbed):
    box, work, _, _ = stubbed
    (work / "pkg").mkdir()
    (work / "pkg" / "m.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return f()\n")
    actor = Actor("d")
    specs = shell(actor, box) | code(actor, BoxTree(box)) | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="d", specs=frozenset(specs), system="x")
    backend = scripted(
        '<lact b1>bash(command="ls pkg")</lact><lact p1>python(code="print(6*7)")</lact>'
        '<lact s1>search(pattern="return f", path="pkg")</lact>'
        '<lact r1>read_lines(path="pkg/m.py", start=5, end=6)</lact>'
        '<lact e1>edit(path="pkg/m.py", old="return f()", new="return f() + 1")</lact>',
        "done",
    )
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), backend))
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["b1"].ok and r["b1"].value["stdout"] == "m.py\n" and r["b1"].value["rc"] == 0
    assert r["p1"].ok and r["p1"].value["stdout"] == "42\n"
    assert r["s1"].ok and r["s1"].value == "pkg/m.py:6:    return f()\n"
    assert r["r1"].ok and r["r1"].value == "pkg/m.py lines 5-6 of 6\n5: def g():\n6:     return f()"
    assert r["e1"].ok and (work / "pkg" / "m.py").read_text().endswith("return f() + 1\n")


def test_push_copies_what_git_sees_and_a_dead_box_is_named(stubbed, tmp_path):
    box, work, ok, mod = stubbed
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / "keep.py").write_text("x = 1\n")
    (src / ".gitignore").write_text("build/\n")
    (src / "build").mkdir()
    (src / "build" / "out.o").write_bytes(b"\0")
    assert asyncio.run(box.push(src)) == [".gitignore", "keep.py"]  # build/ is ignored, untracked or not
    assert sorted(p.name for p in work.iterdir()) == [".gitignore", "keep.py"]
    plain = tmp_path / "plain"
    (plain / "d").mkdir(parents=True)
    (plain / "d" / "f.txt").write_text("f\n")
    assert asyncio.run(box.push(plain, dest=str(work / "in"))) == ["d/f.txt"]  # no repository: every file
    assert (work / "in" / "d" / "f.txt").read_text() == "f\n"
    ok[0] = False
    with pytest.raises(daytona_mod.SandboxDead, match="resolve container IP"):
        asyncio.run(box.run("true"))
    assert "resolve container IP" in box.dead
    asyncio.run(box.stop())
    assert mod.Client.made[-1].deleted and box.started is False


def test_push_counts_what_was_actually_uploaded_not_what_git_listed(stubbed, tmp_path):
    # a dangling symlink is git-tracked but `is_file()` skips it when building
    # the tar, so `len(names)` reported one more file than the box actually received
    box, work, _, _ = stubbed
    src = tmp_path / "src2"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / "keep.py").write_text("x = 1\n")
    os.symlink("nowhere.py", src / "dangling.py")
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"], cwd=src, check=True
    )
    assert asyncio.run(box.push(src, dest=str(work / "src2"))) == ["keep.py"]
    assert sorted(p.name for p in (work / "src2").iterdir()) == ["keep.py"]


@pytest.mark.skipif(os.environ.get("LION_DAYTONA_E2E") != "1", reason="LION_DAYTONA_E2E=1 runs a real box")
def test_a_real_box_runs_the_shell_and_code_tools_over_a_pushed_directory(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (src / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    box = DaytonaSandbox("python:3.12-slim", workdir="/work", label="lionagi-test", cpu=1, memory=1, disk=3)
    actor = Actor("e")
    specs = shell(actor, box) | code(actor, BoxTree(box)) | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="e", specs=frozenset(specs), system="x")

    async def go():
        await box.start()
        try:
            assert sorted(await box.push(src)) == ["calc.py", "test_calc.py"]
            call = "python -B -c 'from calc import add; print(add(2, 2))'"
            backend = scripted(
                # -B: a bytecode cache keyed on (mtime, size) would serve the old body after an
                # edit of the same length in the same second
                f'<lact b1>bash(command="{call}; ls *.py")</lact>'
                '<lact s1>search(pattern="return a", path=".")</lact>'
                '<lact p1>python(code="import sys; print(sys.version_info[:2])")</lact>'
                '<lact t1>bash(command="sleep 30", timeout=2)</lact>',
                '<lact e1>edit(path="calc.py", old="a - b", new="a + b")</lact>',  # its own round,
                f'<lact b2>bash(command="{call}")</lact>',  # the test of it after
                "done",
            )
            return await actor.run(profile, RunRequest(inputs=("go",), max_rounds=4), backend)
        finally:
            await box.stop()

    run = asyncio.run(go())
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["b1"].value["stdout"] == "0\ncalc.py\ntest_calc.py\n" and r["b1"].value["rc"] == 0
    assert r["s1"].value == "calc.py:2:    return a - b\n"
    assert r["p1"].value["stdout"] == "(3, 12)\n" and r["p1"].value["rc"] == 0
    assert (
        r["t1"].value["rc"] == 137 and "timeout" in r["t1"].value["stderr"] and r["t1"].value["elapsed"] < 15
    )
    assert r["e1"].ok and r["b2"].value["stdout"] == "4\n"
    assert box.started is False and box._box is None


def test_push_stops_on_a_repository_git_cannot_list(stubbed, tmp_path):
    # any non-zero `git ls-files` selected every file: a repository with a broken index would have
    # sent what its .gitignore keeps out; only a directory that is no repository takes that path
    box, work, _, _ = stubbed
    src = tmp_path / "src3"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / "keep.py").write_text("x = 1\n")
    (src / ".gitignore").write_text("local.env\n")
    (src / "local.env").write_text("k=v\n")
    subprocess.run(["git", "add", "keep.py", ".gitignore"], cwd=src, check=True)
    assert asyncio.run(box.push(src, dest=str(work / "src3"))) == [".gitignore", "keep.py"]
    (src / ".git" / "index").write_bytes(b"not an index")
    with pytest.raises(OSError, match="git ls-files in .* exited 128"):
        asyncio.run(box.push(src, dest=str(work / "src3-again")))
    assert not (work / "src3-again").exists()  # nothing was sent


def test_chat_in_a_box_pushes_the_directory_lists_it_and_brings_the_patch_back(stubbed, tmp_path, capsys):
    """`lion chat --box daytona` end to end on the stub: the directory pushed at the same path, the
    file tools over the box's tree, a git baseline for the copy, the diff saved as the chat's patch
    and applied to the directory with --apply."""
    from lion_cli.chat import prepare_box, take_patch

    from hub import files

    box, work, _, _ = stubbed
    src = tmp_path / "proj"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (src / "sub").mkdir()
    (src / "sub" / "note.txt").write_text("n\n")
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"], cwd=src, check=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "one"],
        cwd=src,
        check=True,
    )
    said: list[str] = []
    box.workdir = str(work)  # the stub's box: the copy lives under the test's work dir, not at src's path
    baseline = asyncio.run(prepare_box(box, src, said.append))
    assert said == [f"box img: 2 file(s) pushed to {work}"]
    assert (work / ".git").is_dir() and (work / "calc.py").read_text().endswith("a - b\n")
    tree = BoxTree(box)
    assert asyncio.run(tree.list_dir(".")) == [".git/", "calc.py", "sub/"]
    with pytest.raises(PermissionError, match="outside"):
        asyncio.run(tree.list_dir(".."))
    actor = Actor("d")
    assert files(actor, tree) == {"read", "list_dir"}
    specs = {"read", "list_dir"} | code(actor, tree) | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="d", specs=frozenset(specs), system="x")
    backend = scripted(
        '<lact l1>list_dir(path="sub")</lact><lact r1>read(path="sub/note.txt")</lact>'
        '<lact e1>edit(path="calc.py", old="a - b", new="a + b")</lact>',
        "done",
    )
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), backend))
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["l1"].value == ["note.txt"] and r["r1"].value == "n\n" and r["e1"].ok
    assert (src / "calc.py").read_text().endswith("a - b\n")  # the person's tree is untouched so far
    # nothing changed: no patch. Then the edit: saved, printed, applied
    said.clear()
    chats = tmp_path / "chats"
    subprocess.run(["git", "-C", str(work), "stash", "-q"], check=True)
    assert (
        asyncio.run(take_patch(box, src, chats, "c1", baseline=baseline, apply=False, say=said.append))
        is None
    )
    assert said == ["box: no change in the copy"] and not chats.exists()
    subprocess.run(["git", "-C", str(work), "stash", "pop", "-q"], check=True)
    said.clear()
    path = asyncio.run(take_patch(box, src, chats, "c1", baseline=baseline, apply=True, say=said.append))
    assert path == chats / "c1.patch" and "-    return a - b\n+    return a + b\n" in path.read_text()
    assert "+    return a + b" in capsys.readouterr().out
    assert said[0].startswith("box: the copy's diff, ") and said[-1] == f"box: applied to {src} (git apply)"
    assert (src / "calc.py").read_text().endswith("a + b\n")
    # a patch that does not apply (the person's file moved on) is saved, said, and the file untouched
    (src / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    said.clear()
    path = asyncio.run(take_patch(box, src, chats, "c2", baseline=baseline, apply=True, say=said.append))
    assert path.exists() and said[-1].startswith("box: not applied, git apply exited 1")
    assert (src / "calc.py").read_text().endswith("a * b\n")


def repo_git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def box_chat(monkeypatch, make_box, directory: Path, *turns: str, **flags) -> BaseException | None:
    """`lion chat --dir <directory> --box daytona` end to end: `make_box(image, workdir)` is the box,
    the model's turns are scripted, the person says "hi" and nothing more. Returns what it raised."""
    import argparse

    from lion_cli.chat import chat_main

    monkeypatch.setattr("lion_cli.chat.DaytonaSandbox", make_box)
    monkeypatch.setattr("lion_cli.chat.backend_for", lambda model: scripted(*turns))

    async def eof(*a, **k):
        return None

    monkeypatch.setattr("lion_cli.view.stdin_ask", eof)
    a = dict(dir=str(directory), resume=None, log=None, shell=False, code=False, offline=False, model="x")
    a |= dict(rounds=4, raw=True, say="hi", verbose=False, box="daytona", image="img", apply=False)
    try:
        asyncio.run(chat_main(argparse.Namespace(**(a | flags))))
    except Exception as e:  # noqa: BLE001 - what the chat ended on is the evidence
        return e
    return None


@pytest.fixture
def stub_chat_box(tmp_path, monkeypatch):
    """A stubbed Daytona box for `chat_main`, not yet started: its copy lives in `<tmp>/work`. Its git
    runs on this machine, so this machine's global config (hooks included) is kept out, as a box's is."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setitem(sys.modules, "daytona", stub_daytona(tmp_path, [True]))
    monkeypatch.setattr(daytona_mod, "api_key", lambda: "k")
    (tmp_path / "tmp").mkdir()
    work = tmp_path / "work"
    made: list[DaytonaSandbox] = []

    def make(image, workdir):
        made.append(DaytonaSandbox(image, workdir=str(work)))
        return made[-1]

    return work, make, made


def saved_patches(directory: Path) -> list[str]:
    return [p.read_text() for p in sorted((directory / ".lion" / "chats").glob("*.patch"))]


def test_subdir_apply_changes_only_selected_file(stub_chat_box, tmp_path, monkeypatch):
    # from a subdirectory `git apply` skipped the box's patch, exited 0, and "applied"
    _, make, _ = stub_chat_box
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "f.txt").write_text("old\n")
    (repo / "sub" / "f.txt").write_text("old\n")
    repo_git(repo, "init", "-q")
    repo_git(repo, "add", "-A")
    repo_git(repo, "commit", "-qm", "one")
    turn = '<lact b1>bash(command="echo new > f.txt")</lact>'
    assert box_chat(monkeypatch, make, repo / "sub", turn, "done", apply=True) is None
    assert (repo / "sub" / "f.txt").read_text() == "new\n"
    assert (repo / "f.txt").read_text() == "old\n"  # the root's file of the same name is not the target


def test_baseline_diff_keeps_committed_and_uncommitted_edits(stub_chat_box, tmp_path, monkeypatch):
    # a commit in the box moved HEAD past the baseline and its edit came back as no change
    _, make, _ = stub_chat_box
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    (src / "b.txt").write_text("b\n")
    repo_git(src, "init", "-q")
    repo_git(src, "add", "-A")
    repo_git(src, "commit", "-qm", "one")
    work = "echo A > a.txt && git -c user.email=m@x -c user.name=m commit -qam fix && echo B > b.txt"
    work += " && echo C > c.txt && git add c.txt && echo D > d.txt"  # unstaged, staged, untracked
    assert (
        box_chat(monkeypatch, make, src, f'<lact b1>bash(command="{work}")</lact>', "done", apply=True)
        is None
    )
    (patch,) = saved_patches(src)
    assert "+A\n" in patch  # the committed edit
    assert "+B\n" in patch and "+C\n" in patch and "+D\n" in patch
    assert [(src / n).read_text() for n in ("a.txt", "b.txt", "c.txt", "d.txt")] == [
        "A\n",
        "B\n",
        "C\n",
        "D\n",
    ]


def test_ignored_pushed_source_in_baseline(stub_chat_box, tmp_path, monkeypatch):
    # a tracked file .gitignore also matches is pushed; the baseline's `add -A` skipped it
    _, make, _ = stub_chat_box
    src = tmp_path / "proj"
    src.mkdir()
    (src / ".gitignore").write_text("settings.ini\nbuild/\n")
    (src / "settings.ini").write_text("debug=false\n")
    repo_git(src, "init", "-q")
    repo_git(src, "add", ".gitignore")
    repo_git(src, "add", "-f", "settings.ini")
    repo_git(src, "commit", "-qm", "one")
    turn = (
        '<lact b1>bash(command="echo debug=true > settings.ini && mkdir build && echo o > build/x.o")</lact>'
    )
    assert box_chat(monkeypatch, make, src, turn, "done") is None
    patches = saved_patches(src)
    assert patches and "+debug=true" in patches[0]
    assert "build/x.o" not in patches[0]  # an ignored build output is not source: never force-added


def test_root_and_standalone_directories_apply_and_an_unchanged_copy_saves_nothing(
    stub_chat_box, tmp_path, monkeypatch
):
    _, make, made = stub_chat_box
    root = tmp_path / "repo"
    root.mkdir()
    (root / "f.txt").write_text("old\n")
    repo_git(root, "init", "-q")
    repo_git(root, "add", "-A")
    repo_git(root, "commit", "-qm", "one")
    edit = '<lact b1>bash(command="echo new > f.txt")</lact>'
    assert box_chat(monkeypatch, make, root, edit, "done", apply=True) is None
    assert (root / "f.txt").read_text() == "new\n"
    plain = tmp_path / "plain"  # no repository here or above: git apply works it as plain files
    (plain / "d").mkdir(parents=True)
    (plain / "d" / "f.txt").write_text("old\n")
    outside = subprocess.run(["git", "-C", str(plain), "rev-parse", "--git-dir"], capture_output=True)
    assert outside.returncode != 0, "the standalone fixture sits inside a repository"
    shutil.rmtree(made[-1].workdir)  # a fresh box: the copy starts empty
    edit = '<lact b1>bash(command="echo new > d/f.txt")</lact>'
    assert box_chat(monkeypatch, make, plain, edit, "done", apply=True) is None
    assert (plain / "d" / "f.txt").read_text() == "new\n"
    shutil.rmtree(made[-1].workdir)
    assert box_chat(monkeypatch, make, root, '<lact l1>bash(command="ls")</lact>', "done") is None
    assert len(saved_patches(root)) == 1  # the first chat's; the unchanged one saved none
    assert made[-1]._box is None and not made[-1].started  # and its box was stopped


class ChatBox:
    """A box for `chat_main` whose diff is whatever `patch` holds: the copy's git can be the model's."""

    made: list[ChatBox] = []
    patch = ""

    def __init__(self, image, workdir):
        self.image, self.workdir, self.prelude, self.dead = image, workdir, "", None
        self.box_id = f"sbx-{len(ChatBox.made) + 1:04d}"
        self.started = self.stopped = False
        ChatBox.made.append(self)

    async def start(self):
        self.started = True
        return self

    async def push(self, directory):
        return ["f.txt"]

    async def run(self, cmd, *, timeout=120, env=None):
        return 0, ""

    async def exec(self, argv, *, stdin=None, timeout=120, env=None):
        return {"rc": 0, "stdout": "ab" * 20 + "\n", "stderr": "", "elapsed": 0.0, "timed_out": False}

    async def diff(self, base="HEAD"):
        return type(self).patch

    async def stop(self):
        self.stopped = True


def test_patch_rejects_traversal_and_symlink_escape(tmp_path, monkeypatch):
    # plain `git apply` refuses both; this pins that no repair reached for `--unsafe-paths`
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "f.txt").write_text("old\n")
    repo_git(repo, "init", "-q")
    repo_git(repo, "add", "-A")
    repo_git(repo, "commit", "-qm", "one")
    (tmp_path / "outside").mkdir()
    escape = (
        "diff --git a/../victim.txt b/../victim.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/../victim.txt\n@@ -0,0 +1 @@\n+owned\n"
    )
    link = (
        "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n"
        f"+{tmp_path / 'outside'}\n\\ No newline at end of file\n"
        "diff --git a/link/owned.txt b/link/owned.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/link/owned.txt\n@@ -0,0 +1 @@\n+owned\n"
    )
    for directory in (repo, repo / "sub"):
        for patch in (escape, link):
            monkeypatch.setattr(ChatBox, "patch", patch)
            assert box_chat(monkeypatch, ChatBox, directory, "done", apply=True) is None
            assert not (tmp_path / "victim.txt").exists() and not (repo / "victim.txt").exists()
            assert not (tmp_path / "outside" / "owned.txt").exists() and not (repo / "link").exists()
            assert not (repo / "sub" / "link").exists()


def test_a_patch_that_will_not_apply_is_saved_whole_and_lands_nothing(tmp_path, monkeypatch, capsys):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    (src / "b.txt").write_text("b moved on\n")  # the person's file changed during the chat
    repo_git(src, "init", "-q")
    repo_git(src, "add", "-A")
    repo_git(src, "commit", "-qm", "one")
    long = "x" * 5000
    patch = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n a\n"
        f"+{long}\n"
        "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-b\n+B\n"
    )
    monkeypatch.setattr(ChatBox, "patch", patch)
    ChatBox.made.clear()
    assert box_chat(monkeypatch, ChatBox, src, "done", apply=True) is None
    (saved,) = (src / ".lion" / "chats").glob("*.patch")
    assert saved.read_bytes() == patch.encode()  # every byte, the long line whole
    assert (src / "a.txt").read_text() == "a\n" and (src / "b.txt").read_text() == "b moved on\n"
    out = capsys.readouterr().out
    assert long in out and "not applied, git apply exited 1" in " ".join(out.split())
    assert ChatBox.made[-1].stopped


def test_apply_from_a_subdirectory_lands_or_is_said_not_to(stubbed, tmp_path):
    # the baseline is passed explicitly
    from lion_cli.chat import prepare_box, take_patch

    box, work, _, _ = stubbed
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    repo_git(repo, "init", "-q")
    (sub / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    repo_git(repo, "add", "-A")
    repo_git(repo, "commit", "-qm", "one")
    said: list[str] = []
    baseline = asyncio.run(prepare_box(box, sub, said.append))
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    said.clear()
    path = asyncio.run(
        take_patch(box, sub, tmp_path / "chats", "c1", baseline=baseline, apply=True, say=said.append)
    )
    assert path is not None and "+    return a + b" in path.read_text()
    landed = (sub / "calc.py").read_text().endswith("a + b\n")
    claims_applied = said[-1].startswith("box: applied")
    assert landed or not claims_applied, (said[-1], (sub / "calc.py").read_text())


def test_a_commit_in_the_box_still_comes_back_as_the_patch(stubbed, tmp_path):
    # a baseline the box does not have, or a copy whose repository is gone, is an error and never
    # "no change"
    from lion_cli.chat import prepare_box, take_patch

    box, work, _, _ = stubbed
    src = tmp_path / "proj"
    src.mkdir()
    repo_git(src, "init", "-q")
    (src / "calc.py").write_text("x = 1\n")
    repo_git(src, "add", "-A")
    repo_git(src, "commit", "-qm", "one")
    said: list[str] = []
    baseline = asyncio.run(prepare_box(box, src, said.append))
    (work / "calc.py").write_text("x = 2\n")
    repo_git(work, "commit", "-qam", "fix")
    said.clear()
    chats = tmp_path / "chats"
    path = asyncio.run(take_patch(box, src, chats, "c1", baseline=baseline, apply=False, say=said.append))
    assert path is not None, said
    assert "+x = 2" in path.read_text()
    for base in ("0" * 40, baseline):
        if base == baseline:
            shutil.rmtree(work / ".git")
        with pytest.raises(RuntimeError, match="git diff"):
            asyncio.run(take_patch(box, src, chats, "c2", baseline=base, apply=False, say=said.append))
    assert not (chats / "c2.patch").exists()


def test_an_edit_to_a_tracked_but_ignored_file_comes_back(stubbed, tmp_path):
    # the baseline is passed explicitly
    from lion_cli.chat import prepare_box, take_patch

    box, work, _, _ = stubbed
    src = tmp_path / "proj"
    src.mkdir()
    repo_git(src, "init", "-q")
    (src / ".gitignore").write_text("settings.ini\n")
    (src / "settings.ini").write_text("debug = false\n")
    repo_git(src, "add", ".gitignore")
    repo_git(src, "add", "-f", "settings.ini")
    repo_git(src, "commit", "-qm", "one")
    baseline = asyncio.run(prepare_box(box, src, lambda s: None))
    assert (work / "settings.ini").read_text() == "debug = false\n"
    (work / "settings.ini").write_text("debug = true\n")
    said: list[str] = []
    path = asyncio.run(
        take_patch(box, src, tmp_path / "chats", "c1", baseline=baseline, apply=False, say=said.append)
    )
    assert path is not None, said
    assert "+debug = true" in path.read_text()


def test_a_copy_whose_repository_is_gone_keeps_the_real_box_and_names_its_provider_id(
    stub_chat_box, tmp_path, monkeypatch, capsys
):
    _, make, made = stub_chat_box
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    err = box_chat(monkeypatch, make, src, '<lact b1>bash(command="mv .git .git-gone")</lact>', "done")
    box = made[-1]
    assert isinstance(err, RuntimeError) and "git diff" in str(err)
    assert box._box is not None and not box._box.deleted  # retained
    assert "box stub-box-1: kept, not stopped" in " ".join(capsys.readouterr().out.split())
    assert box.box_id == "stub-box-1"
