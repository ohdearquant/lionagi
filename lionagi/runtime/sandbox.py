# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Remote sandbox execution protocol and implementations.

Provides a backend-agnostic protocol for isolated command execution,
with a LocalSandboxBackend that uses subprocess and temp directories,
and a SandboxManager that orchestrates isolated runs.

Why subprocess over in-process execution:
- Process isolation: failures, signals, and resource limits stay contained
- Clean environment: env_vars can be injected without polluting host
- Timeout enforcement via asyncio.wait_for without thread gymnastics
- stdout/stderr captured with configurable size limits

The Protocol/runtime_checkable design allows isinstance checks and
enables swap-in of remote backends (E2B, Daytona, SSH) without
changing the SandboxManager interface.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "LocalSandboxBackend",
    "SandboxBackend",
    "SandboxConfig",
    "SandboxManager",
    "SandboxResult",
]

# Maximum captured output bytes before truncation (1 MiB).
_OUTPUT_SIZE_LIMIT = 1 * 1024 * 1024


class SandboxConfig(BaseModel):
    """Configuration for a sandbox execution environment."""

    sandbox_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timeout_seconds: int = Field(default=300, gt=0)
    env_vars: dict[str, str] | None = Field(default=None)
    working_dir: str | None = Field(default=None)


class SandboxResult(BaseModel, frozen=True):
    """Immutable result of a sandbox execution."""

    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    artifacts: list[str] = Field(default_factory=list)
    truncated: bool = False


@runtime_checkable
class SandboxBackend(Protocol):
    """Protocol for sandbox execution backends.

    Implementations may use subprocess (local), container APIs (E2B),
    managed workspace APIs (Daytona), or SSH (self-hosted).
    """

    async def create(self, config: SandboxConfig) -> str:
        """Provision a new sandbox; return sandbox_id."""
        ...

    async def execute(self, sandbox_id: str, command: str, stdin: str = "") -> SandboxResult:
        """Run a shell command inside the sandbox; return result."""
        ...

    async def upload(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
        """Upload a local file into the sandbox at remote_path."""
        ...

    async def download(self, sandbox_id: str, remote_path: str) -> bytes:
        """Download a file from the sandbox; return raw bytes."""
        ...

    async def destroy(self, sandbox_id: str) -> None:
        """Tear down the sandbox and release all resources."""
        ...

    async def is_alive(self, sandbox_id: str) -> bool:
        """Return True if the sandbox is still provisioned."""
        ...


class _SandboxEntry:
    """Internal state for a live local sandbox."""

    __slots__ = ("config", "tmp_dir", "alive")

    def __init__(self, config: SandboxConfig, tmp_dir: str) -> None:
        self.config = config
        self.tmp_dir = tmp_dir
        self.alive = True


class LocalSandboxBackend:
    """Sandbox backend backed by subprocess and temp directories.

    Each sandbox gets an isolated temporary directory. Commands are
    executed via asyncio.create_subprocess_exec with a configurable
    timeout. stdout and stderr are captured up to _OUTPUT_SIZE_LIMIT;
    output beyond that is truncated and noted in SandboxResult.truncated.
    """

    def __init__(self, *, output_size_limit: int = _OUTPUT_SIZE_LIMIT) -> None:
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._output_size_limit = output_size_limit

    # ------------------------------------------------------------------
    # SandboxBackend protocol
    # ------------------------------------------------------------------

    async def create(self, config: SandboxConfig) -> str:
        """Create a temp directory and register the sandbox."""
        tmp_dir = tempfile.mkdtemp(prefix=f"lionagi-sandbox-{config.sandbox_id}-")
        entry = _SandboxEntry(config=config, tmp_dir=tmp_dir)
        self._sandboxes[config.sandbox_id] = entry
        return config.sandbox_id

    async def execute(self, sandbox_id: str, command: str, stdin: str = "") -> SandboxResult:
        """Execute a shell command inside the sandbox.

        The command is run via ``/bin/sh -c`` so shell constructs work.
        Environment inherits from the host process then overlays
        config.env_vars.  Working directory is the sandbox temp dir
        unless config.working_dir is set.
        """
        entry = self._get_entry(sandbox_id)
        cwd = entry.config.working_dir or entry.tmp_dir

        env = dict(os.environ)
        if entry.config.env_vars:
            env.update(entry.config.env_vars)

        stdin_bytes = stdin.encode() if stdin else None

        start = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin_bytes else None,
                cwd=cwd,
                env=env,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(input=stdin_bytes),
                    timeout=entry.config.timeout_seconds,
                )
            except (TimeoutError, asyncio.TimeoutError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                # drain any partial output already buffered
                try:
                    partial_out, partial_err = await asyncio.wait_for(
                        process.communicate(), timeout=2.0
                    )
                except Exception:  # noqa: BLE001
                    partial_out, partial_err = b"", b""
                duration_ms = (time.monotonic() - start) * 1000
                return SandboxResult(
                    sandbox_id=sandbox_id,
                    exit_code=-1,
                    stdout=partial_out.decode("utf-8", errors="replace"),
                    stderr=(
                        partial_err.decode("utf-8", errors="replace") + "\nExecution timed out"
                    ).strip(),
                    duration_ms=duration_ms,
                    truncated=False,
                )
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.monotonic() - start) * 1000
            return SandboxResult(
                sandbox_id=sandbox_id,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_ms=duration_ms,
                truncated=False,
            )

        duration_ms = (time.monotonic() - start) * 1000
        truncated = False
        limit = self._output_size_limit

        if len(stdout_bytes) > limit or len(stderr_bytes) > limit:
            truncated = True
            stdout_bytes = stdout_bytes[:limit]
            stderr_bytes = stderr_bytes[:limit]

        return SandboxResult(
            sandbox_id=sandbox_id,
            exit_code=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            truncated=truncated,
        )

    async def upload(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
        """Copy a local file into the sandbox directory."""
        entry = self._get_entry(sandbox_id)
        dest = os.path.realpath(os.path.join(entry.tmp_dir, remote_path.lstrip("/")))
        sandbox_root = os.path.realpath(entry.tmp_dir)
        if not dest.startswith(sandbox_root + os.sep) and dest != sandbox_root:
            raise ValueError(f"remote_path {remote_path!r} resolves outside sandbox boundary")
        os.makedirs(os.path.dirname(dest) or sandbox_root, exist_ok=True)
        shutil.copy2(local_path, dest)

    async def download(self, sandbox_id: str, remote_path: str) -> bytes:
        """Read a file from the sandbox directory and return its bytes."""
        entry = self._get_entry(sandbox_id)
        src = os.path.realpath(os.path.join(entry.tmp_dir, remote_path.lstrip("/")))
        sandbox_root = os.path.realpath(entry.tmp_dir)
        if not src.startswith(sandbox_root + os.sep) and src != sandbox_root:
            raise ValueError(f"remote_path {remote_path!r} resolves outside sandbox boundary")
        with open(src, "rb") as fh:
            return fh.read()

    async def destroy(self, sandbox_id: str) -> None:
        """Remove the temp directory and deregister the sandbox."""
        entry = self._sandboxes.pop(sandbox_id, None)
        if entry is None:
            return
        entry.alive = False
        if os.path.isdir(entry.tmp_dir):
            shutil.rmtree(entry.tmp_dir, ignore_errors=True)

    async def is_alive(self, sandbox_id: str) -> bool:
        """Return True if the sandbox exists and its temp dir is present."""
        entry = self._sandboxes.get(sandbox_id)
        if entry is None:
            return False
        return entry.alive and os.path.isdir(entry.tmp_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_entry(self, sandbox_id: str) -> _SandboxEntry:
        entry = self._sandboxes.get(sandbox_id)
        if entry is None:
            raise KeyError(f"Unknown sandbox {sandbox_id!r}")
        if not entry.alive:
            raise RuntimeError(f"Sandbox {sandbox_id!r} has been destroyed")
        return entry


class SandboxManager:
    """High-level facade that combines sandbox lifecycle with execution.

    Tracks active sandboxes and auto-destroys them on close.
    Provides convenience methods for one-shot isolated commands
    and script execution.
    """

    def __init__(self, backend: SandboxBackend | None = None) -> None:
        self._backend: SandboxBackend = backend if backend is not None else LocalSandboxBackend()
        self._active: dict[str, SandboxConfig] = {}

    async def run_isolated(
        self,
        command: str,
        config: SandboxConfig | None = None,
        *,
        stdin: str = "",
    ) -> SandboxResult:
        """Create a sandbox, run command, destroy sandbox, return result.

        If config is None a default SandboxConfig is used. The sandbox
        is always destroyed after execution even when an exception occurs.
        """
        if config is None:
            config = SandboxConfig()
        sandbox_id = await self._backend.create(config)
        self._active[sandbox_id] = config
        try:
            result = await self._backend.execute(sandbox_id, command, stdin)
        finally:
            await self._backend.destroy(sandbox_id)
            self._active.pop(sandbox_id, None)
        return result

    async def run_script(
        self,
        script_content: str,
        interpreter: str = "python3",
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        """Write script_content to a temp file and execute it.

        The script file is uploaded into the sandbox, then executed with
        the given interpreter. The sandbox is destroyed afterwards.
        """
        if config is None:
            config = SandboxConfig()

        sandbox_id = await self._backend.create(config)
        self._active[sandbox_id] = config
        try:
            # Write script to a physical temp file so we can upload it.
            fd, tmp_script = tempfile.mkstemp(suffix=".py", prefix="sandbox-script-")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(script_content)
                remote_script = "script.py"
                await self._backend.upload(sandbox_id, tmp_script, remote_script)
            finally:
                if os.path.exists(tmp_script):
                    os.unlink(tmp_script)

            result = await self._backend.execute(sandbox_id, f"{interpreter} {remote_script}")
        finally:
            await self._backend.destroy(sandbox_id)
            self._active.pop(sandbox_id, None)
        return result

    async def close(self) -> None:
        """Destroy all active sandboxes tracked by this manager."""
        for sandbox_id in list(self._active):
            try:
                await self._backend.destroy(sandbox_id)
            except Exception:  # noqa: BLE001, S110
                pass
        self._active.clear()

    async def __aenter__(self) -> SandboxManager:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
