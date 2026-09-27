"""The Daytona box in the hub. A stub client runs the box's shell lines on this machine (the wrapper
that splits stdout from stderr and carries the rc is the part that has to be right), and the real
service is exercised once end to end with `LION_DAYTONA_E2E=1`: a box from a public image, `bash`
and `python` through `shell()`, the code tools through `BoxTree`, a directory pushed in, stop."""

from __future__ import annotations

import asyncio
import os
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
    r = asyncio.run(box.exec(["sleep", "5"], timeout=1))  # F12: the clock says it was the timeout
    assert r["rc"] == 137 and r["timed_out"] is True and r["stderr"] == "ended at the 1s timeout"
    r = asyncio.run(
        box.exec(["sh", "-c", "exit 124"], timeout=7)
    )  # P3-6: a command's own 124 is not the clock's
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
    # P3-3 (hub review): a dangling symlink is git-tracked but `is_file()` skips it when building
    # the tar, so `len(names)` reported one more file than the box actually received
    box, work, _, _ = stubbed
    src = tmp_path / "src2"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / "keep.py").write_text("x = 1\n")
    os.symlink("nowhere.py", src / "dangling.py")
    subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "add", "-A"], cwd=src, check=True)
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
