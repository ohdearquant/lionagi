"""The box shape: what `BoxBase` derives from `exec` (run, read, write, diff), and the docker box's
argv, through a `docker` shim on PATH that runs the container command on this machine."""

import asyncio
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hub.tools import BoxTree, DaytonaSandbox, DockerSandbox, LocalTree, Sandbox, git_diff

SHIM = """#!{python}
import os, subprocess, sys
a = sys.argv[1:]
if a[0] != "exec":
    print("ok"); sys.exit(0)
i, env, cwd = 1, dict(os.environ), None
while a[i].startswith("-"):
    if a[i] == "-w":
        cwd = a[i + 1]; i += 2
    elif a[i].startswith("--env="):
        k, v = a[i][6:].split("=", 1); env[k] = v; i += 1
    else:
        i += 1
name = a[i]
assert a[i + 1 : i + 4] == ["timeout", "-s", "KILL"], a
argv = a[i + 5 :]
open(os.environ["SHIM_LOG"], "a").write(name + " " + repr(argv) + "\\n")
sys.exit(subprocess.run(argv, cwd=cwd, env=env).returncode)
"""


def shim(tmp_path: Path) -> dict[str, str]:
    d = tmp_path / "bin"
    d.mkdir()
    (d / "docker").write_text(SHIM.format(python=sys.executable))
    (d / "docker").chmod((d / "docker").stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "SHIM_LOG": str(tmp_path / "shim.log")}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_docker_box_derives_run_read_write_and_diff_from_exec(tmp_path, monkeypatch):
    for k, v in shim(tmp_path).items():
        monkeypatch.setenv(k, v)
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    (work / "a.py").write_text("old\n")
    git(work, "add", "a.py")
    git(work, "commit", "-qm", "one")
    box = DockerSandbox("img", workdir=str(work), prelude="export PRE=1")

    async def go():
        await box.start()
        r = await box.exec(["sh", "-c", "echo out; echo bad >&2; exit 3"], env={"K": "v"})
        assert (r["rc"], r["stdout"], r["stderr"]) == (3, "out\n", "bad\n")
        assert await box.run("echo $PRE $K; pwd", env={"K": "v"}) == (0, f"1 v\n{work.resolve()}\n")
        # the model's `bash` sees the prelude too, and the prelude runs once on either path
        r = await box.exec(["sh", "-c", "echo $PRE; echo ${LION_N:-0}"], env={"K": "v"})
        assert r["stdout"] == "1\n0\n" and r["timed_out"] is False
        box.prelude = "export LION_N=$((${LION_N:-0}+1))"
        assert await box.run("echo $LION_N") == (0, "1\n")
        assert (await box.exec(["sh", "-c", "echo $LION_N"]))["stdout"] == "1\n"
        box.prelude = "export PRE=1"
        assert (await box.run("false | true"))[0] == 1  # pipefail: the pipeline's rc is the failure's
        (work / "new file.py").write_text("spaced\n")
        await box.write("d/f.txt", "hi\n")  # a parent made on the way
        assert await box.read("d/f.txt") == "hi\n" and (work / "d" / "f.txt").read_text() == "hi\n"
        try:
            await box.read("missing.txt")
        except FileNotFoundError as e:
            assert "missing.txt" in str(e)
        else:
            raise AssertionError("a missing file read")
        # a Latin-1 file came back with U+FFFD and an edit rewrote its bytes
        (work / "latin.txt").write_bytes(b"caf\xe9\n")
        try:
            await box.read("latin.txt")
        except ValueError as e:
            assert "not UTF-8" in str(e)
        else:
            raise AssertionError("a non-UTF-8 file was read as text")
        assert await box.read("d/f.txt") == "hi\n"  # the base64 transport carries plain text unchanged
        await box.write("d/u.txt", "naïve 名\n")
        assert await box.read("d/u.txt") == "naïve 名\n"
        # the model's tools work the tree, not the box; the box itself reaches /tmp (the grader
        # writes /tmp/model.patch through it: a bound on the box lost four graded runs, 2026-09-22)
        tree = BoxTree(box)
        for path in ("../x", "/etc/passwd", "d/../../x"):
            for op in (tree.read(path), tree.write(path, "x"), tree.search("x", path, None)):
                try:
                    await op
                except PermissionError as e:
                    assert "outside" in str(e)
                else:
                    raise AssertionError(f"{path} was reached")
        await box.write(str(tmp_path / "beside.patch"), "outside the tree\n")
        assert await box.read(str(tmp_path / "beside.patch")) == "outside the tree\n"
        await box.write("a.py", "new\n")
        (work / "b.py").write_text("added\n")
        (work / "c.pyc").write_bytes(b"\0")
        (work / "d" / "__pycache__").mkdir()
        (work / "d" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0")
        git(work, "add", "-f", "d/__pycache__/m.cpython-312.pyc")  # staged: in the tracked diff
        git(work, "add", "d/f.txt")  # staged text stays in the patch
        (work / "-n.py").write_text("dash\n")  # a name that reads as an option without `--`
        patch = await git_diff(box)
        assert patch == await box.diff()
        assert "-old\n+new\n" in patch and "+added\n" in patch and "c.pyc" not in patch
        assert "__pycache__" not in patch and "+hi\n" in patch  # the exclusion is by path, staged or not
        assert "+spaced\n" in patch and "b/new file.py" in patch and "+dash\n" in patch
        # git's stderr is not patch text (a CRLF warning read as a change, measured)
        git(work, "config", "core.safecrlf", "warn")
        git(work, "config", "core.autocrlf", "input")
        (work / "crlf.py").write_bytes(b"x\r\n")
        patch = await git_diff(box)
        assert "warning" not in patch.lower() and "+x" in patch
        box.workdir = str(tmp_path)  # not a repository: an error, never an empty patch read as "no change"
        try:
            await git_diff(box)
        except RuntimeError as e:
            assert "git diff" in str(e) and "exited" in str(e)
        else:
            raise AssertionError("git_diff outside a repository returned")
        box.workdir = str(work)
        await box.stop()

    asyncio.run(go())
    log = (tmp_path / "shim.log").read_text().splitlines()
    assert all(ln.startswith(box.name + " ") for ln in log) and len(log) >= 8
    assert box.started is False


def test_the_box_tree_resolves_a_symlink_in_the_box_before_its_bound(tmp_path, monkeypatch):
    # the bound read the path's text, so a link inside `workdir` read and wrote through to its target
    for k, v in shim(tmp_path).items():
        monkeypatch.setenv(k, v)
    work, away = tmp_path / "work", tmp_path / "away"
    (work / "pkg" / "inner").mkdir(parents=True)
    away.mkdir()
    (away / "secret.py").write_text("secret = 1\n")
    (work / "a.py").write_text("a = 1\n")
    (work / "link.py").symlink_to(away / "secret.py")
    (work / "out").symlink_to(away)
    (work / "rel").symlink_to("../away")
    (work / "hop").symlink_to("pkg/inner")
    (work / "same.py").symlink_to("a.py")  # a link that stays in the tree is followed
    (work / "sub").symlink_to("pkg")
    tree, local = BoxTree(DockerSandbox("img", workdir=str(work))), LocalTree(work)

    async def refused(op) -> None:
        with pytest.raises(PermissionError, match="outside"):
            await op

    async def go():
        for t in (tree, local):
            for path in ("link.py", "out/secret.py", "rel/secret.py", str(work / "out" / "secret.py")):
                await refused(t.read(path))
                await refused(t.write(path, "owned\n"))
            await refused(t.write("out/new.py", "owned\n"))  # a missing file behind a link out
            await refused(t.list_dir("out"))
            await refused(t.search("secret", "out", None))
            assert await t.read("same.py") == "a = 1\n"
            await t.write("sub/b.py", "b = 1\n")
            assert await t.list_dir("sub") == ["b.py", "inner/"]
        # `ls` opens `out/..` as the kernel does, past the link; `write` opens `hop/../out/x.py` as
        # its text normalises, `out/x.py`: each form is bound
        await refused(tree.list_dir("out/.."))
        await refused(tree.write("hop/../out/x.py", "owned\n"))

    asyncio.run(go())
    assert sorted(p.name for p in away.iterdir()) == ["secret.py"]
    assert (away / "secret.py").read_text() == "secret = 1\n"
    assert (work / "pkg" / "b.py").read_text() == "b = 1\n"


@pytest.mark.parametrize("timeout", [5.0, None])
@pytest.mark.parametrize("transport", ["daytona", "docker"])
def test_box_timeout_seam(monkeypatch, transport, timeout):
    # `run` on both transports takes None as well
    calls = []

    class Proc:
        returncode = 0

        async def communicate(self, stdin):
            return b"ok", b""

    async def subprocess_stub(*argv, **kw):
        calls.append(argv)
        return Proc()

    async def sdk_stub(command, **kw):
        calls.append((command, kw))
        return types.SimpleNamespace(exit_code=0, result="ok")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_stub)
    monkeypatch.setitem(
        sys.modules, "daytona", types.SimpleNamespace(DaytonaProcessExecutionTimeoutError=TimeoutError)
    )
    if transport == "daytona":
        box = DaytonaSandbox("offline-fixture")
        box._box = types.SimpleNamespace(process=types.SimpleNamespace(exec=sdk_stub))
    else:
        box = DockerSandbox("offline-fixture")
    result = asyncio.run(box.run("echo ok", timeout=timeout))
    assert result == (0, "ok") and len(calls) == 1


@pytest.mark.parametrize("timeout", [5.0, None])
def test_no_deadline_reaches_exec_bare_never_as_a_large_number(monkeypatch, timeout):
    # None: no kill wrapper in the box, no wait on the host, no SDK timeout; a finite one keeps all three
    argvs, waits, sdk = [], [], []

    class Proc:
        returncode = 0

        async def communicate(self, stdin):
            return b"ok", b""

    async def subprocess_stub(*argv, **kw):
        argvs.append(argv)
        return Proc()

    async def sdk_stub(command, **kw):
        sdk.append((command, kw["timeout"]))
        return types.SimpleNamespace(exit_code=0, result="ok")

    wait_for = asyncio.wait_for

    def waited(aw, timeout):
        waits.append(timeout)
        return wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_stub)
    monkeypatch.setattr(asyncio, "wait_for", waited)
    monkeypatch.setitem(
        sys.modules, "daytona", types.SimpleNamespace(DaytonaProcessExecutionTimeoutError=TimeoutError)
    )
    day = DaytonaSandbox("offline-fixture")
    day._box = types.SimpleNamespace(process=types.SimpleNamespace(exec=sdk_stub))
    asyncio.run(day.exec(["echo", "ok"], timeout=timeout))
    asyncio.run(DockerSandbox("offline-fixture").exec(["echo", "ok"], timeout=timeout))
    ((line, sdk_timeout),), (argv,) = sdk, argvs
    if timeout is None:
        assert sdk_timeout is None and waits == [None]
        assert "timeout -s KILL" not in line and "timeout" not in argv
    else:
        assert sdk_timeout == 20 and waits == [20.0]
        assert "timeout -s KILL 5 " in line and "timeout -s KILL 5 echo ok" in " ".join(argv)


def test_the_vm_box_refuses_a_missing_deadline_before_any_command_runs(tmp_path, monkeypatch):
    # the docker and remote boxes take None as no deadline (above); the VM's guest `timeout` needs
    # seconds, so None fails there before a `container` process starts
    spawned = []

    async def subprocess_stub(*argv, **kw):
        spawned.append(argv)
        raise AssertionError("a command ran")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_stub)
    box = Sandbox(tmp_path, "offline-fixture", cli="container")
    with pytest.raises(TypeError):
        asyncio.run(box.exec(["echo", "ok"], timeout=None))
    with pytest.raises(TypeError):
        asyncio.run(box.run("echo ok", timeout=None))
    assert spawned == []
