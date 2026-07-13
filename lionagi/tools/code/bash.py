# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shlex

from pydantic import BaseModel, ConfigDict, Field

from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from .._subprocess import _SHELL_CONTROL
from .._subprocess import _subprocess_sync as _subprocess_sync_inner
from ..base import LionTool


class BashRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(
        ...,
        description=(
            "A single shell command to run. Shell control operators are NOT supported "
            "and will be rejected — no `&&`, `||`, `|`, `;`, redirects (`<`/`>`), "
            "backticks, or `$(...)`. Run one command per call; to run in a directory "
            "use cwd= instead of `cd dir && cmd`. For file search/read/edit prefer the "
            "dedicated reader/editor/search tools over bash."
        ),
    )
    timeout: int | None = Field(
        None,
        description=(
            "Maximum execution time in milliseconds. "
            "Defaults to 30000 (30 s). Maximum allowed is 300000 (5 min). "
            "The process is killed if it exceeds this limit. "
            "Increase for long-running builds or tests."
        ),
    )
    cwd: str | None = Field(
        None,
        description=(
            "Working directory for the command. "
            "If omitted, inherits the current process working directory. "
            "Use this instead of a leading `cd` command."
        ),
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


def _command_for_subprocess(request: BashRequest) -> list[str]:
    """Return argv list for subprocess; rejects shell control operators (shell=False enforced)."""
    if _SHELL_CONTROL.search(request.command):
        raise PermissionError(
            f"Shell control operators are not supported: {request.command!r}. "
            "Run one command per call; use cwd= to set the working directory instead of `cd x && cmd`."
        )
    try:
        return shlex.split(request.command)
    except ValueError as e:
        raise PermissionError(
            f"Malformed command: {e}. Check quoting — use single quotes for arguments with spaces."
        ) from e


def _subprocess_sync(
    cmd: str | list[str],
    shell: bool,
    timeout_sec: float,
    timeout_ms: int,
    cwd: str | None,
) -> BashResponse:
    raw = _subprocess_sync_inner(cmd, shell, timeout_sec, cwd, timeout_ms=timeout_ms)
    return BashResponse(
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        return_code=raw["returncode"],
        timed_out=raw.get("timed_out", False),
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
            cmd = _command_for_subprocess(request)
        except PermissionError as e:
            return BashResponse(stdout="", stderr=str(e), return_code=-1)

        return await run_sync(
            _subprocess_sync,
            cmd,
            False,
            timeout_sec,
            timeout_ms,
            request.cwd or None,
        )

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def bash_tool(**kwargs):
                """Execute a single shell command (no shell interpretation) and return stdout, stderr, and return code.
                Shell operators (;, &&, ||, |, redirects, backticks) are rejected — one command per call; use cwd= instead of `cd dir && cmd`.
                Recovery: 'command not found' → check PATH; timed out → raise timeout= or split into steps.
                """
                return (await self.handle_request(BashRequest(**kwargs))).model_dump()

            if self.system_tool_name != "bash_tool":
                bash_tool.__name__ = self.system_tool_name

            self._tool = Tool(
                func_callable=bash_tool,
                request_options=BashRequest,
            )
        return self._tool
