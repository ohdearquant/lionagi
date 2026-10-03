"""A box: somewhere commands run and files live, apart from this machine. Four of them share this
shape: the local VM (`shell.Sandbox`, Apple's `container`), a docker container
(`docker.DockerSandbox`), a Daytona sandbox (`daytona.DaytonaSandbox`) and a Runta runtime
(`runta.RuntaSandbox`). Every command runs from `workdir` after `prelude` (an environment to activate,
say; `wrap` puts it in front of an argv exactly once). `exec` is one argv with stdout and stderr apart,
what `shell()` hands `bash` and `python`; `run` is a shell line under `bash -o pipefail` with its
combined output, rc 124 at the timeout, what a caller scripting the box wants. `BoxTree` puts the code
tools (read_lines, search, edit) over the box's files, and `git_diff` reads the model's patch out of a
repository in it."""

from __future__ import annotations

import base64
import contextlib
import io
import os
import posixpath
import re
import shlex
import signal
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Protocol

__all__ = ("GIT_TIMEOUT", "Box", "BoxBase", "git_diff", "inside", "pack", "BoxTree")

# seconds each of `pack`'s git calls on this machine gets: a git held by a lock or a helper that never
# answers would hold the push, and a chat that starts with one, without end
GIT_TIMEOUT = 30.0


class Box(Protocol):
    workdir: str
    prelude: str
    dead: str | None  # the error that ended the box, when one did; a caller reads it to end its run

    def abs(self, path: str) -> str: ...
    async def start(self) -> Any: ...
    async def exec(
        self,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = 120,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...
    async def run(
        self, cmd: str, *, timeout: float | None = 120, env: dict[str, str] | None = None
    ) -> tuple[int, str]: ...
    async def read(self, path: str) -> str: ...
    async def write(self, path: str, text: str) -> None: ...
    async def diff(self, base: str = "HEAD") -> str: ...
    async def stop(self) -> None: ...


class BoxBase:
    """What a box gets for free once it has `exec`: `run`, `read` and `write` through the shell,
    `diff` through git. A box with a file API of its own (Daytona) overrides read and write."""

    workdir: str
    prelude: str
    dead: str | None = None

    def line(self, cmd: str) -> str:
        """`cmd` as one shell line from `workdir`, after `prelude`: for a box whose transport takes a
        line, not an argv (Daytona); a box whose `exec` wraps the prelude itself must not use it."""
        head = f"{self.prelude}; " if self.prelude else ""
        return f"{head}cd {shlex.quote(self.workdir)} && {cmd}"

    def wrap(self, argv: list[str]) -> list[str]:
        """`argv` after `prelude`, once: the model's `bash` and the runner's `run` see one environment."""
        if not self.prelude:
            return argv
        return ["bash", "-c", f'{self.prelude}\nexec "$@"', "lion-prelude", *argv]

    def abs(self, path: str) -> str:
        """`path` as an absolute path, relative ones under `workdir`. The box itself reads and writes
        anywhere: the bench grader writes `/tmp/model.patch` through it (run-2, 2026-09-22, lost four
        graded runs to a bound here); the model's tools go through `inside`."""
        return posixpath.normpath(path if path.startswith("/") else posixpath.join(self.workdir, path))

    async def run(
        self, cmd: str, *, timeout: float | None = 120, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """`cmd` from `workdir` under `bash -o pipefail` (`pytest | tail` fails when pytest does), its
        combined output; rc 124 when the box ended it at `timeout`, and None is no deadline."""
        argv = ["bash", "-o", "pipefail", "-c", f"cd {shlex.quote(self.workdir)} && {cmd}"]
        r = await self.exec(argv, timeout=timeout, env=env)  # type: ignore[attr-defined]
        rc = 124 if r.get("timed_out") else r["rc"]
        return rc, r["stdout"] + r["stderr"]

    async def read(self, path: str) -> str:
        """The file's text, decoded strictly: a file that is not UTF-8 is refused, because an edit
        through a lossy decode would rewrite every bad byte (measured through the docker box)."""
        argv = ["sh", "-c", 'exec base64 < "$1"', "lion-read", self.abs(path)]  # GNU and BSD base64 alike
        r = await self.exec(argv)  # type: ignore[attr-defined]
        if r["rc"] != 0:
            raise FileNotFoundError(r["stderr"].strip() or r["stdout"].strip())
        raw = base64.b64decode("".join(r["stdout"].split()))
        try:
            return raw.decode()
        except UnicodeDecodeError as e:
            raise ValueError(
                f"{path} is not UTF-8 text ({e}); an edit would rewrite its bytes, so it is refused"
            ) from None

    async def write(self, path: str, text: str) -> None:
        q = shlex.quote(self.abs(path))
        r = await self.exec(  # type: ignore[attr-defined]
            ["sh", "-c", f'mkdir -p "$(dirname {q})" && cat > {q}'], stdin=text.encode()
        )
        if r["rc"] != 0:
            raise OSError(r["stderr"].strip() or r["stdout"].strip())

    async def diff(self, base: str = "HEAD") -> str:
        return await git_diff(self, base)  # type: ignore[arg-type]


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """`git -C directory <args>`, its output captured, within `GIT_TIMEOUT`. Past it, or on a ^C, the
    process group git leads is killed, a helper it started with it, and git reaped; past it,
    `TimeoutError` says git did not answer, which a git that exited with an rc never does."""
    with subprocess.Popen(
        ["git", "-C", str(directory), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # a group of its own: the kill reaches what git started, holding its pipes
    ) as proc:
        try:
            out, err = proc.communicate(timeout=GIT_TIMEOUT)
        except BaseException as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:  # the group ended on its own
                pass
            except PermissionError:
                # macOS answers EPERM for a group whose only member is its leader, exited and not yet
                # reaped: `poll` reaps it without blocking. EPERM again is a member this process may not
                # signal, and raises
                proc.poll()
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            if isinstance(e, subprocess.TimeoutExpired):
                raise TimeoutError(
                    f"git {args[0]} in {directory} did not answer within {GIT_TIMEOUT:g}s and was stopped"
                ) from None
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _in_repository(directory: Path) -> bool:
    """Whether `directory` is inside a git work tree (read without the index: a broken one still says yes)."""
    done = _git(directory, "rev-parse", "--is-inside-work-tree")
    return done.returncode == 0 and done.stdout.strip() == b"true"


def pack(directory: Path) -> tuple[bytes, list[str]]:
    """`directory` as a gzipped tar for a remote box's `push`, and the names in it: the files and symlinks
    git sees when it is a repository (tracked and untracked, ignored ones left out), else every one. A
    symlink goes in as a link, not followed, whether it points at a file, a directory or nothing. A git
    call that does not answer within `GIT_TIMEOUT` is stopped, and `TimeoutError` raised."""
    directory = Path(directory).resolve()
    done = _git(directory, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if done.returncode == 0:
        names = [n.decode("utf-8", "surrogateescape") for n in done.stdout.split(b"\0") if n]
    elif _in_repository(directory):
        # a repository git could not list (a broken index): every-file would send what its
        # .gitignore keeps out, so the push stops here instead
        said = done.stderr.decode(errors="replace").strip()[-300:]
        raise OSError(f"git ls-files in {directory} exited {done.returncode}: {said}")
    else:
        names = [str(p.relative_to(directory)) for p in directory.rglob("*")]
    buf = io.BytesIO()
    packed: list[str] = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in names:
            path = directory / name
            if path.is_symlink() or path.is_file():  # a directory is no entry: its files are
                tar.add(path, arcname=name, recursive=False)
                packed.append(name)
    return buf.getvalue(), packed


def bytecode(path: str) -> bool:
    return path.endswith((".pyc", ".pyo")) or "__pycache__" in path


def without_bytecode(patch: str) -> str:
    """`patch` without its bytecode files: a `.pyc` the model staged or committed is in the tracked
    diff, and the exclusion is by path, whatever git's index says of the file."""
    chunks = re.split(r"(?m)^(?=diff --git )", patch)
    return "".join(c for c in chunks if c and not bytecode(c.split("\n", 1)[0].rstrip('"')))


async def git_diff(box: Box, base: str = "HEAD") -> str:
    """The changes in the repository at `workdir`: the work tree against `base`, staged or not, plus
    new source files, never bytecode (a model that ran `git add` would otherwise lose its edit). A
    `base` below HEAD keeps what the model committed since it."""

    async def git(*args: str, ok: tuple[int, ...] = (0,)) -> str:
        # stdout alone is the patch: a warning on stderr (CRLF, an unreadable directory) read as
        # patch text once, and an unchanged repository as a change. A wrong rc raises: an empty
        # patch here would read as "no change".
        r = await box.exec(["git", "-C", box.workdir, *args])
        if r["rc"] not in ok:
            raise RuntimeError(
                f"git {args[0]} in {box.workdir} exited {r['rc']}: {r['stderr'].strip()[-300:]}"
            )
        return r["stdout"]

    tracked = without_bytecode(await git("diff", "--binary", base))
    parts = [tracked] if tracked.strip() else []
    for path in (await git("ls-files", "--others", "--exclude-standard", "-z")).split("\0"):
        if not path or bytecode(path):
            continue
        d = await git("diff", "--no-index", "--binary", "--", "/dev/null", path, ok=(0, 1))  # 1: differs
        if d.strip():
            parts.append(d)
    return "".join(parts)


def inside(workdir: str, path: str) -> None:
    """Refuse a `path` that resolves outside `workdir` (`..`, another absolute path): the model's
    tools work the tree, not the box."""
    root = posixpath.normpath(workdir)
    target = posixpath.normpath(path if path.startswith("/") else posixpath.join(root, path))
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        raise PermissionError(f"{path} is outside {workdir}")


# each argument's physical path, NUL-ended (a name may hold a newline), symlinks followed as the
# kernel does and a missing tail kept as written: `realpath -m` is GNU only, and `readlink -n` reads
# alike on both. No name goes through `$(...)` bare: that drops a newline the name ends with, so the
# parent is cut by expansion and the link target carries a sentinel past its last byte
_RESOLVE = r"""
for arg do
  r=/ todo=$arg n=0
  while [ -n "$todo" ]; do
    c=${todo%%/*}
    case $todo in */*) todo=${todo#*/} ;; *) todo= ;; esac
    case $c in
      '' | .) ;;
      ..) r=${r%/*}; r=${r:-/} ;;
      *)
        p=${r%/}/$c
        if [ -L "$p" ]; then
          n=$((n + 1))
          [ "$n" -le 40 ] || exit 1
          t=$(readlink -n -- "$p" && printf x) || exit 1
          t=${t%x}
          case $t in /*) r=/ ;; esac
          todo=$t${todo:+/$todo}
        else
          r=$p
        fi
        ;;
    esac
  done
  printf '%s\0' "$r"
done
"""


class BoxTree:
    """The code tools' `Tree` over a box's `workdir`: `rg` when the image has it, else `grep -rE`;
    every path is bound to the tree before the box sees it, by its text (`inside`) and then as the
    box resolves it, so a symlink out of `workdir` is refused as `LocalTree` refuses it."""

    def __init__(self, box: Box):
        self.box = box
        self._rg: bool | None = None

    async def _bound(self, path: str) -> None:
        root = getattr(self.box, "workdir", None)  # a box without one (test doubles) has no bound
        if not root:
            return
        inside(root, path)
        # the shell tools open the path as written, `read` and `write` its normalised form (`abs`):
        # a symlink before a `..` takes the two apart, so both must stay in the tree
        joined = path if path.startswith("/") else posixpath.join(root, path)
        argv = ["sh", "-c", _RESOLVE, "lion-resolve", root, joined, posixpath.normpath(joined)]
        r = await self.box.exec(argv)
        real = r["stdout"].split("\0")[:-1]  # three paths, each NUL-ended, nothing after the last
        if r["rc"] != 0 or not r["stdout"].endswith("\0") or len(real) != 3:
            raise PermissionError(f"{path} did not resolve in the box: {r['stderr'].strip()[-300:]}")
        top = real[0].rstrip("/") + "/"
        for target in real[1:]:
            if target != real[0] and not target.startswith(top):
                raise PermissionError(f"{path} resolves to {target}, outside {root}")

    async def read(self, path: str) -> str:
        await self._bound(path)
        return await self.box.read(path)

    async def write(self, path: str, text: str) -> None:
        await self._bound(path)
        await self.box.write(path, text)

    async def list_dir(self, path: str) -> list[str]:
        await self._bound(path)
        rc, out = await self.box.run(f"ls -1Ap -- {shlex.quote(path)} 2>&1")  # -p: `/` after a directory
        if rc != 0:
            raise FileNotFoundError(out.strip() or f"ls exited {rc}")
        return sorted(out.splitlines())

    async def search(self, pattern: str, path: str, include: str | None) -> str:
        if self._rg is None:
            rc, _ = await self.box.run("command -v rg >/dev/null")
            self._rg = rc == 0
        await self._bound(path)  # inside the tree, or refused
        if self._rg:
            # -H: a single-file path drops the filename column without it
            argv = [
                "rg",
                "-n",
                "--no-heading",
                "--color",
                "never",
                "--hidden",
                "-H",
                "-g",
                "!.git",
                "-e",
                pattern,
            ]
            argv += ["-g", include] if include else []
        else:
            argv = ["grep", "-rnIEH", "--exclude-dir=.git", "-e", pattern]
            argv += [f"--include={include}"] if include else []
        argv += ["--", path]
        rc, out = await self.box.run(shlex.join(argv) + " 2>&1")
        if rc > 1:  # 1 is "no matches" for both tools; 2 is a bad pattern or path
            raise ValueError(out.strip() or f"search exited {rc}")
        return "".join(ln.removeprefix("./") + "\n" for ln in out.splitlines())
