"""A box in a docker container: any image, `sleep infinity` as its process, one `docker exec` per
command under the guest's `timeout -s KILL`. An amd64 image runs under emulation, so run one at a time;
`docker(...)` is the CLI call the runner uses for images too."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .box import BoxBase

__all__ = ("docker", "DockerSandbox")


async def docker(*args: str, check: bool = True) -> str:
    """One docker CLI call, its combined output; a failure raises with the output when `check`."""
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    if check and proc.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {out.decode(errors='replace')[:500]}")
    return out.decode(errors="replace")


@dataclass
class DockerSandbox(BoxBase):
    image: str
    workdir: str = "/work"
    prelude: str = ""
    name: str = field(default_factory=lambda: f"lion-{uuid.uuid4().hex[:8]}")
    platform: str = "linux/amd64"
    started: bool = False

    async def start(self) -> DockerSandbox:
        argv = ["run", "-d", "--name", self.name, "--platform", self.platform, "-w", self.workdir]
        await docker(*argv, self.image, "sleep", "infinity")  # `-w` makes the directory when missing
        self.started = True
        return self

    async def exec(
        self,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = 120,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One command in the container after `prelude`, ended there at `timeout` (rc 137 and
        `timed_out`), None no deadline; stdout and stderr apart."""
        t0 = time.monotonic()
        head = ["exec", "-i", "-w", self.workdir, *(f"--env={k}={v}" for k, v in (env or {}).items())]
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *head,
            self.name,
            *(["timeout", "-s", "KILL", f"{timeout:g}"] if timeout is not None else []),
            *self.wrap(argv),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin), None if timeout is None else timeout + 15
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "rc": -1,
                "stdout": "",
                "stderr": f"docker exec did not return in {timeout + 15:g}s",
                "elapsed": 0,
                "timed_out": True,
            }
        rc = proc.returncode or 0
        stderr = err.decode(errors="replace")
        elapsed = round(time.monotonic() - t0, 2)
        # 137 alone is any SIGKILL; the clock says which
        timed_out = timeout is not None and rc == 137 and elapsed >= timeout
        if timed_out:
            stderr = (stderr + f"\nended at the {timeout:g}s timeout").strip()
        return {
            "rc": rc,
            "stdout": out.decode(errors="replace"),
            "stderr": stderr,
            "elapsed": elapsed,
            "timed_out": timed_out,
        }

    async def stop(self) -> None:
        if self.started:
            await docker("container", "stop", "-t", "1", self.name, check=False)
            await docker("container", "remove", "--force", self.name, check=False)
            self.started = False
