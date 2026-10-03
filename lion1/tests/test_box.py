"""The box shape: what `BoxBase` derives from `exec` (run, read, write, diff), and the docker box's
argv, through a `docker` shim on PATH that runs the container command on this machine."""

import asyncio
import io
import os
import stat
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

from hub.tools import BoxTree, DaytonaSandbox, DockerSandbox, LocalTree, Sandbox, git_diff
from hub.tools.box import pack

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


def test_a_git_read_that_fails_is_an_error_not_an_empty_patch():
    class Scripted:
        """git as the patch read sees it: a tracked change and a new file, the read `fails` names failing."""

        workdir, fails = "/w", None

        async def exec(self, argv, *, stdin=None, timeout=120, env=None):
            args = argv[3:]  # after `git -C /w`
            if args[0] == "ls-files":
                return {"rc": 0, "stdout": "new.py\0", "stderr": ""}
            if "--no-index" in args:  # 1 is the difference it was asked for
                if self.fails == "new":
                    return {"rc": 128, "stdout": "", "stderr": "fatal: bad object"}
                return {"rc": 1, "stdout": "diff --git a/new.py b/new.py\n+x = 2\n", "stderr": ""}
            if self.fails == "tracked":  # partial output, then the error
                return {"rc": 128, "stdout": "diff --git a/a.py", "stderr": "fatal: bad revision"}
            return {"rc": 0, "stdout": "diff --git a/a.py b/a.py\n-x = 1\n+x = 2\n", "stderr": ""}

    box = Scripted()
    patch = asyncio.run(git_diff(box, "b" * 40))  # the control: both reads healthy
    assert "a/a.py" in patch and "b/new.py" in patch
    for fails, said in (("tracked", "fatal: bad revision"), ("new", "fatal: bad object")):
        box.fails = fails
        with pytest.raises(RuntimeError, match=f"exited 128: {said}"):
            asyncio.run(git_diff(box, "b" * 40))


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
    (work / "line\nbreak.py").write_text("lb = 1\n")  # a name holding a newline is still one path
    twin = tmp_path / "work\n"  # a sibling named as the tree plus a newline: a link into it leads out
    (twin / "sub").mkdir(parents=True)
    (twin / "secret.py").write_text("twin = 1\n")
    (work / "twin").symlink_to("../work\n")
    (work / "sub\n").mkdir()  # a link whose target ends in a newline still lands on that target
    (work / "sub\n" / "c.py").write_text("c = 1\n")
    (work / "nl").symlink_to("sub\n")
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
            assert await t.read("line\nbreak.py") == "lb = 1\n"
            assert await t.read("nl/c.py") == "c = 1\n"
            await refused(t.read("twin/sub/../secret.py"))
            await refused(t.write("twin/sub/../new.py", "owned\n"))
            await t.write("sub/b.py", "b = 1\n")
            assert await t.list_dir("sub") == ["b.py", "inner/"]
        # `ls` opens `out/..` as the kernel does, past the link; `write` opens `hop/../out/x.py` as
        # its text normalises, `out/x.py`: each form is bound
        await refused(tree.list_dir("out/.."))
        await refused(tree.write("hop/../out/x.py", "owned\n"))

    asyncio.run(go())
    assert sorted(p.name for p in away.iterdir()) == ["secret.py"]
    assert sorted(p.name for p in twin.iterdir()) == ["secret.py", "sub"]
    assert (away / "secret.py").read_text() == "secret = 1\n"
    assert (work / "pkg" / "b.py").read_text() == "b = 1\n"


@pytest.mark.parametrize(
    "stdout",
    ["/w\0/w/a\0/w/a\0tail", "/w\0/w/a\0/w/a", "/w\0/w/a\0", "", "/w\0/w/a\0/w/a\0/w/b\0"],
)
def test_a_resolver_answer_that_is_not_three_nul_ended_paths_is_refused(stdout):
    # a fourth record, a missing last NUL, two records, nothing, or bytes past the last NUL: not a frame
    class Box:
        workdir = "/w"

        async def exec(self, argv, timeout=None):
            return {"rc": 0, "stdout": stdout, "stderr": ""}

    async def go():
        with pytest.raises(PermissionError, match="did not resolve"):
            await BoxTree(Box()).read("a")

    asyncio.run(go())


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
    given, exec_ = [], box.exec

    async def spy(argv, **kw):  # `run` hands the transport the deadline as given, None included
        given.append(kw.get("timeout", "absent"))
        return await exec_(argv, **kw)

    monkeypatch.setattr(box, "exec", spy)
    result = asyncio.run(box.run("echo ok", timeout=timeout))
    assert result == (0, "ok") and len(calls) == 1
    assert given == [timeout] if transport == "docker" else calls[0][1]["timeout"] == timeout


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


@pytest.mark.parametrize(
    "target", ["target.txt", "adir", "missing"], ids=["to a file", "to a directory", "to nothing"]
)
@pytest.mark.parametrize("repository", [True, False], ids=["in a repository", "in no repository"])
def test_pack_adds_a_symlink_as_a_link_not_followed_whatever_it_points_at(tmp_path, repository, target):
    (tmp_path / "target.txt").write_text("t\n")
    (tmp_path / "adir").mkdir()
    (tmp_path / "adir" / "inner.txt").write_text("i\n")
    (tmp_path / "link").symlink_to(target)
    if repository:
        git(tmp_path, "init", "-q")
        git(tmp_path, "add", "-A")  # the link is tracked, as a tracked one is in a checkout
    data, names = pack(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert sorted(names) == sorted(members) == ["adir/inner.txt", "link", "target.txt"]
    assert members["link"].issym() and members["link"].linkname == target  # a link, whatever is behind it
    assert members["target.txt"].isreg() and members["adir/inner.txt"].isreg()
