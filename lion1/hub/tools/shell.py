"""A shell for the model: `bash` and `python` run inside a Linux VM (Apple's `container`), the
console's directory mounted at its own path, uv's environment and cache on a named volume under
/opt so the host's `.venv` is never touched. One VM per conversation, started when the chat opens
and removed when it ends; a command is one `container exec`, ~60 ms after boot. Every command runs
under the guest's `timeout -s KILL`, so a hung one ends there, not on the host."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from lionagi import Actor, Context

from .box import Box, BoxBase

__all__ = ("IMAGE", "OFFLINE_NETWORK", "SANDBOX_DIR", "Bash", "Python", "Sandbox", "shell", "host_zone")

IMAGE = "lion-sandbox"
OFFLINE_NETWORK = "lion-offline"  # `container network create --internal`: no DNS, no egress, no host
SANDBOX_DIR = Path(__file__).resolve().parent / "sandbox"  # the Dockerfile the image is built from


class Bash(BaseModel):
    """Run a shell line in the sandbox: a Linux VM with this directory mounted at the same path,
    `uv`, `git`, `rg`, `jq`; nothing else of the host. Returns rc, stdout, stderr, elapsed, whole."""

    command: str = Field(
        description="one shell line, run by `bash -o pipefail -c` from the console's directory: a "
        "pipeline's rc is its first failing command's, so `pytest | tail` still fails when pytest does"
    )
    timeout: float = Field(default=120, description="seconds; the command is ended in the VM at this")


class Python(BaseModel):
    """Run Python source in the sandbox with the project's environment (uv's, under /opt/venv).
    Returns rc, stdout, stderr, elapsed, whole."""

    code: str = Field(description="the source, run as a script from the console's directory")
    timeout: float = Field(default=120, description="seconds; ended in the VM at this")


@dataclass
class Sandbox(BoxBase):
    directory: Path
    name: str  # the container's name; one per conversation
    prelude: str = ""
    image: str = IMAGE
    offline: bool = False  # the internal network: no DNS, no egress; else the default network
    cpus: int = 2
    memory: str = "4G"
    volume: str = "lion-sandbox-opt"  # /opt across conversations: uv's environment and cache
    cli: str = field(default_factory=lambda: shutil.which("container") or "container")
    started: bool = False

    @property
    def workdir(self) -> str:  # the directory is mounted at its own path: the same string inside
        return str(Path(self.directory).resolve())

    async def _cli(
        self, *argv: str, stdin: bytes | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.cli,
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except TimeoutError:
            proc.terminate()
            await proc.wait()
            return -1, "", f"container {argv[0]} did not return in {timeout:g}s"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def ensure_image(self) -> str | None:
        """Build the image from `SANDBOX_DIR` when it is missing; returns the build's tail on failure."""
        rc, out, _ = await self._cli("image", "list", "--format", "json")
        if rc == 0 and self.image in out:
            return None
        rc, out, err = await self._cli("build", "-t", self.image, str(SANDBOX_DIR), timeout=600)
        return None if rc == 0 else (err or out)[-800:]

    async def start(self) -> None:
        directory = str(Path(self.directory).resolve())
        if self.offline:
            _, out, _ = await self._cli("network", "list", "--format", "json")
            if OFFLINE_NETWORK not in out:
                await self._cli("network", "create", "--internal", OFFLINE_NETWORK)
        _, out, _ = await self._cli("volume", "list", "--format", "json")
        if self.volume not in out:
            await self._cli("volume", "create", self.volume)
        argv = ["run", "-d", "--name", self.name, "-v", f"{directory}:{directory}", "-w", directory]
        argv += ["-v", f"{self.volume}:/opt", "--cpus", str(self.cpus), "--memory", self.memory]
        if (zone := host_zone()) is not None:
            argv += ["-e", f"TZ={zone}"]  # the sandbox's clock reads like the notice's
        if self.offline:
            argv += ["--network", OFFLINE_NETWORK]
        rc, out, err = await self._cli(*argv, self.image, "sleep", "infinity", timeout=120)
        if rc != 0:
            raise RuntimeError(f"sandbox {self.name} did not start: {(err or out).strip()[-400:]}")
        self.started = True

    async def exec(
        self,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float = 120,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One command in the VM after `prelude`, ended there at `timeout` (rc 137 and `timed_out`);
        the whole of stdout and stderr."""
        t0 = time.monotonic()
        guard = ["timeout", "-s", "KILL", f"{timeout:g}"]
        vars_ = [f"--env={k}={v}" for k, v in (env or {}).items()]
        rc, out, err = await self._cli(
            "exec", "-i", *vars_, self.name, *guard, *self.wrap(argv), stdin=stdin, timeout=timeout + 15
        )
        elapsed = round(time.monotonic() - t0, 2)
        timed_out = rc == -1 or (rc == 137 and elapsed >= timeout)
        if timed_out and rc == 137:
            err = (err + f"\nended at the {timeout:g}s timeout").strip()
        return {"rc": rc, "stdout": out, "stderr": err, "elapsed": elapsed, "timed_out": timed_out}

    async def stop(self) -> None:
        if self.started:
            await self._cli("stop", self.name, timeout=60)
            await self._cli("delete", self.name, timeout=60)
            self.started = False


def shell(actor: Actor, sandbox: Box) -> set[str]:
    """Register `bash` and `python` on the actor, both running in `sandbox` (any box)."""

    @actor.handler(Bash)
    async def bash(req: Bash, ctx: Context) -> dict[str, Any]:
        return await sandbox.exec(["bash", "-o", "pipefail", "-c", req.command], timeout=req.timeout)

    @actor.handler(Python)
    async def python(req: Python, ctx: Context) -> dict[str, Any]:
        run = "P=/opt/venv/bin/python; [ -x $P ] || P=python; exec $P -"
        return await sandbox.exec(["sh", "-c", run], stdin=req.code.encode(), timeout=req.timeout)

    return {"bash", "python"}


def host_zone() -> str | None:
    """The host's IANA time zone from the `/etc/localtime` link (`.../zoneinfo/America/New_York`), or
    `TZ` when set; None when neither says."""
    zone = os.environ.get("TZ")
    if zone:
        return zone
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    _, sep, rest = target.partition("zoneinfo/")
    return rest or None if sep else None
