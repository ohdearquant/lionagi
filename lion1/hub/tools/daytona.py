"""A box on Daytona: a Linux sandbox from any image, native x86_64, as many in parallel as the org's
memory pool holds. The box shape of `hub.tools.box`, with the service's own file API for `read` and
`write` and `push(directory)` to copy a directory in. The `daytona` package is imported when a box
starts, never at import: the hub loads without it."""

from __future__ import annotations

import asyncio
import io
import os
import shlex
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .box import BoxBase

__all__ = ("api_key", "SandboxDead", "DEAD_MARKS", "DaytonaSandbox")


def api_key() -> str:
    """`DAYTONA_API_KEY` from the environment, else `~/.secrets/daytona.token`."""
    key = os.environ.get("DAYTONA_API_KEY", "").strip()
    if key:
        return key
    token = Path.home() / ".secrets" / "daytona.token"
    if token.exists():
        return token.read_text().strip()
    raise RuntimeError("DAYTONA_API_KEY: not in the environment and no ~/.secrets/daytona.token")


class SandboxDead(RuntimeError):
    """The box stopped answering: a container that never got an IP, a sandbox that never started."""


DEAD_MARKS = ("failed to resolve container ip", "is the sandbox started")


@dataclass
class DaytonaSandbox(BoxBase):
    """One box from `image`. The org's memory pool is shared across every live box, so `memory`
    times the fan-out is the real cap; a create refused for capacity waits and retries."""

    image: str
    workdir: str = "/work"
    prelude: str = ""
    cpu: int = 2
    memory: int = 2  # GiB
    disk: int = 10  # GiB
    label: str = "lionagi"
    create_timeout: float = 900.0  # the first pull of a large image on their side takes minutes
    capacity_retries: int = 40
    dead: str | None = None  # the error that ended the box; a caller reads it to end its run
    started: bool = False
    _client: Any = None
    _box: Any = None

    @property
    def box_id(self) -> str | None:
        """The provider's id of the live box: what a person names to reach a copy the chat kept."""
        return getattr(self._box, "id", None)

    async def start(self) -> DaytonaSandbox:
        from daytona import AsyncDaytona, CreateSandboxFromImageParams, DaytonaConfig, Resources

        self._client = AsyncDaytona(DaytonaConfig(api_key=api_key()))
        params = CreateSandboxFromImageParams(
            image=self.image,
            os_user="root",
            resources=Resources(cpu=self.cpu, memory=self.memory, disk=self.disk),
            labels={"lane": self.label},
            auto_stop_interval=60,  # minutes idle; a crashed caller cannot hold the pool forever
            auto_delete_interval=0,  # gone as soon as it stops
        )
        last: SandboxDead | None = None
        for _ in range(3):
            self._box = await self._create(params)
            try:
                # a created box is not a running one: a box can come up with no IP, and then every
                # command in it fails the same way
                await self._raw(f"mkdir -p {shlex.quote(self.workdir)}", 60, None)
                self.started = True
                return self
            except SandboxDead as e:
                last, self.dead = e, None
                try:
                    await self._box.delete()
                except Exception:
                    pass
                self._box = None
        raise SandboxDead(f"three boxes in a row never came up; the last: {last}")

    async def _create(self, params: Any) -> Any:
        from daytona import DaytonaError

        for attempt in range(self.capacity_retries + 1):
            try:
                return await self._client.create(params, timeout=self.create_timeout)
            except DaytonaError as e:
                msg = str(e).lower()
                if attempt < self.capacity_retries and (
                    "limit" in msg or "502" in msg or "bad gateway" in msg
                ):
                    await asyncio.sleep(30)
                    continue
                raise

    async def run(
        self, cmd: str, *, timeout: float | None = 120.0, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """A shell line in `workdir` after `prelude`, under `bash -o pipefail`: (rc, its combined
        output); 124 at the timeout, None no deadline. The transport takes a line, so the prelude
        rides in it and `exec` below goes through here: once either way."""
        return await self._raw(f"bash -o pipefail -c {shlex.quote(self.line(cmd))}", timeout, env)

    async def _raw(self, command: str, timeout: float | None, env: dict[str, str] | None) -> tuple[int, str]:
        from daytona import DaytonaProcessExecutionTimeoutError

        try:
            resp = await self._box.process.exec(
                command, env=env or None, timeout=None if timeout is None else int(timeout)
            )
        except DaytonaProcessExecutionTimeoutError:
            return 124, f"(timed out after {timeout:.0f}s)"
        except Exception as e:
            if any(mark in str(e).lower() for mark in DEAD_MARKS):
                self.dead = str(e)
                raise SandboxDead(str(e)) from None
            raise
        return int(resp.exit_code or 0), resp.result or ""

    async def exec(
        self,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = 120,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One command in the box, ended there at `timeout` (rc 137), None no deadline; stdout and
        stderr apart. The box hands back one stream, so stderr goes to a file and comes back after a
        marker."""
        t0 = time.monotonic()
        tag = uuid.uuid4().hex
        err, inp = f"/tmp/lion-{tag}.err", f"/tmp/lion-{tag}.in"
        if stdin is not None:
            await self._box.fs.upload_file(stdin, inp)
        cmd = shlex.join(argv) if timeout is None else f"timeout -s KILL {timeout:g} {shlex.join(argv)}"
        cmd += f" <{inp}" if stdin is not None else " </dev/null"
        cmd += f" 2>{err}; rc=$?; printf '%s' {tag}; cat {err}; rm -f {err} {inp}; exit $rc"
        rc, out = await self.run(cmd, timeout=None if timeout is None else timeout + 15, env=env)
        elapsed = round(time.monotonic() - t0, 2)
        stdout, sep, stderr = out.partition(tag)
        if not sep:  # the wrapper itself never ran (a dead box, the SDK's own timeout): all of it is stderr
            stdout, stderr = "", out
        # a command's own 124 is not the clock's
        timed_out = timeout is not None and rc in (124, 137) and elapsed >= timeout
        if timed_out and rc == 137:
            stderr = (stderr + f"\nended at the {timeout:g}s timeout").strip()
        return {"rc": rc, "stdout": stdout, "stderr": stderr, "elapsed": elapsed, "timed_out": timed_out}

    async def read(self, path: str) -> str:
        from daytona import DaytonaError

        try:
            data = await self._box.fs.download_file(self.abs(path))
        except DaytonaError as e:
            raise FileNotFoundError(str(e)) from None
        try:
            return (data or b"").decode()
        except UnicodeDecodeError as e:
            raise ValueError(
                f"{path} is not UTF-8 text ({e}); an edit would rewrite its bytes, so it is refused"
            ) from None

    async def write(self, path: str, text: str) -> None:
        target = self.abs(path)
        parent = target.rsplit("/", 1)[0]
        if parent:
            await self.run(f"mkdir -p {shlex.quote(parent)}")
        await self._box.fs.upload_file(text.encode(), target)

    async def push(self, directory: Path, dest: str | None = None) -> list[str]:
        """Copy `directory` into the box at `dest` (default `workdir`): the files git sees when it is
        a repository (tracked and untracked, ignored ones left out), else every file. Returns the names
        archived, relative to `dest`: what the box received, the manifest a baseline commits."""
        directory = Path(directory).resolve()
        dest = dest or self.workdir
        done = subprocess.run(
            ["git", "-C", str(directory), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
        )
        if done.returncode == 0:
            names = [n.decode("utf-8", "surrogateescape") for n in done.stdout.split(b"\0") if n]
        else:
            names = [str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()]
        buf = io.BytesIO()
        uploaded: list[str] = []
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in names:
                if (directory / name).is_file():
                    tar.add(directory / name, arcname=name, recursive=False)
                    uploaded.append(name)
        archive = f"/tmp/lion-push-{uuid.uuid4().hex}.tgz"
        await self._box.fs.upload_file(buf.getvalue(), archive)
        q = shlex.quote(dest)
        rc, out = await self.run(f"mkdir -p {q} && tar -xzf {archive} -C {q} && rm -f {archive}", timeout=600)
        if rc != 0:
            raise OSError(f"push into {dest} failed (rc {rc}): {out[-400:]}")
        return uploaded

    async def stop(self) -> None:
        if self._box is not None:
            try:
                await self._box.delete()
            finally:
                self._box = None
                self.started = False
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            self._client = None
