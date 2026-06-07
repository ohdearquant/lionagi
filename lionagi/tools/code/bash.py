# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import subprocess
import threading

from pydantic import BaseModel, Field

from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from ..base import LionTool

_SHELL_CONTROL = re.compile(r"(;|&&|\|\||\||`|\$\(|[<>]|\n)")
_MAX_OUTPUT_BYTES = 100_000


class BashRequest(BaseModel):
    command: str = Field(
        ...,
        description=(
            "Shell command to execute. Simple commands without shell operators "
            "(;, &&, ||, |, etc.) run safely via argv. Use absolute paths when "
            "the working directory matters."
        ),
    )
    timeout: int | None = Field(
        None,
        description=(
            "Maximum execution time in milliseconds. "
            "Defaults to 30000 (30 s). Maximum allowed is 300000 (5 min). "
            "The process is killed if it exceeds this limit."
        ),
    )
    cwd: str | None = Field(
        None,
        description=(
            "Working directory for the command. "
            "If omitted, inherits the current process working directory."
        ),
    )
    allow_shell: bool = Field(
        False,
        description="Allow shell control operators. Only set via trusted code.",
        exclude=True,
    )


class BashResponse(BaseModel):
    stdout: str = Field(
        default="",
        description="Standard output from the command.",
    )
    stderr: str = Field(
        default="",
        description="Standard error from the command.",
    )
    return_code: int = Field(
        ...,
        description="Exit code returned by the process. 0 typically means success.",
    )
    timed_out: bool = Field(
        default=False,
        description="True if the command was killed due to the timeout limit.",
    )


def _command_for_subprocess(request: BashRequest) -> tuple[str | list[str], bool]:
    """Rejects shell operators unless trusted."""
    if request.allow_shell:
        return request.command, True
    if _SHELL_CONTROL.search(request.command):
        raise PermissionError(
            f"Shell control operators require trusted shell mode: {request.command!r}"
        )
    try:
        return shlex.split(request.command), False
    except ValueError as e:
        raise PermissionError(f"Malformed command: {e}") from e


def _drain(stream, buf: bytearray) -> bool:
    """Continues reading after the cap is reached to prevent pipe-buffer deadlock."""
    truncated = False
    while True:
        try:
            chunk = stream.read(8192)
        except Exception:
            break
        if not chunk:
            break
        remaining = _MAX_OUTPUT_BYTES - len(buf)
        if remaining > 0:
            buf.extend(chunk[:remaining])
            if len(buf) >= _MAX_OUTPUT_BYTES:
                truncated = True
        # keep draining even after cap to avoid deadlocking the child process
    return truncated


def _decode_output(buf: bytearray, truncated: bool) -> str:
    text = bytes(buf).decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n[... output truncated at {_MAX_OUTPUT_BYTES} bytes ...]\n"
    return text


def _subprocess_sync(
    cmd: str | list[str],
    shell: bool,
    timeout_sec: float,
    timeout_ms: int,
    cwd: str | None,
) -> BashResponse:
    try:
        proc = subprocess.Popen(  # noqa: S603  # cmd validated by _command_for_subprocess: shlex-split argv or trusted shell mode
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
    except Exception as e:
        return BashResponse(stdout="", stderr=f"Execution error: {e}", return_code=-1)

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]

    def _drain_stdout():
        stdout_truncated[0] = _drain(proc.stdout, stdout_buf)

    def _drain_stderr():
        stderr_truncated[0] = _drain(proc.stderr, stderr_buf)

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        # os.killpg is POSIX-only; on Windows fall through to proc.kill().
        if hasattr(os, "killpg") and isinstance(proc.pid, int) and proc.pid > 1:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        return BashResponse(
            stdout=_decode_output(stdout_buf, True),
            stderr=f"Command timed out after {timeout_ms} ms",
            return_code=-1,
            timed_out=True,
        )

    t_out.join()
    t_err.join()

    return BashResponse(
        stdout=_decode_output(stdout_buf, stdout_truncated[0]),
        stderr=_decode_output(stderr_buf, stderr_truncated[0]),
        return_code=proc.returncode,
        timed_out=False,
    )


class BashTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "bash_tool"

    def __init__(self):
        self._tool = None

    async def handle_request(self, request: BashRequest) -> BashResponse:
        if isinstance(request, dict):
            request = BashRequest(**request)

        timeout_ms = request.timeout if request.timeout is not None else 30_000
        timeout_ms = min(max(timeout_ms, 1), 300_000)
        timeout_sec = timeout_ms / 1000.0

        try:
            cmd, shell = _command_for_subprocess(request)
        except PermissionError as e:
            return BashResponse(stdout="", stderr=str(e), return_code=-1)

        return await run_sync(
            _subprocess_sync,
            cmd,
            shell,
            timeout_sec,
            timeout_ms,
            request.cwd or None,
        )

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def bash_tool(**kwargs):
                """
                Execute a shell command and return its output.

                Runs the command safely without shell interpretation by default.
                Shell operators (;, &&, ||, |, etc.) are rejected unless the caller
                sets allow_shell=True via trusted code. Enforces a configurable
                timeout (default 30 s, max 5 min). Output exceeding 100 KB per
                stream is truncated. Prefer absolute paths; set cwd when the
                command depends on a specific working directory.
                """
                return (await self.handle_request(BashRequest(**kwargs))).model_dump()

            if self.system_tool_name != "bash_tool":
                bash_tool.__name__ = self.system_tool_name

            self._tool = Tool(
                func_callable=bash_tool,
                request_options=BashRequest,
            )
        return self._tool
