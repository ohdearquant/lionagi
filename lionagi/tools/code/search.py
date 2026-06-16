# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from .._subprocess import _subprocess_sync
from ..base import LionTool

__all__ = (
    "SearchAction",
    "SearchRequest",
    "SearchResponse",
    "SearchTool",
)


class SearchAction(str, Enum):
    grep = "grep"
    find = "find"


class SearchRequest(BaseModel):
    action: SearchAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'grep': Search file contents for a regex pattern. "
            "Returns matching lines with file:line prefix.\n"
            "- 'find': Find files by name glob pattern. "
            "Returns matching file paths."
        ),
    )
    pattern: str = Field(
        ...,
        description=(
            "For 'grep': an extended regex pattern to search for in file contents.\n"
            "For 'find': a shell glob pattern to match filenames (e.g. '*.py', 'test_*')."
        ),
    )
    path: str = Field(
        default=".",
        description=(
            "File or directory to search. Defaults to '.' (current directory). "
            "For 'grep', may be a single file or a directory (searched recursively). "
            "For 'find', must be the root directory to search under."
        ),
    )
    include: str | None = Field(
        None,
        description=(
            "For 'grep' only: glob pattern to restrict which files are searched "
            "(e.g. '*.py'). Passed as --include to grep."
        ),
    )
    max_results: int = Field(
        default=50,
        description=(
            "Maximum number of results to return. "
            "Defaults to 50 for 'grep', 100 for 'find'. "
            "Results beyond this limit are silently dropped."
        ),
    )


class SearchResponse(BaseModel):
    success: bool = Field(
        ...,
        description="True if the search completed without error.",
    )
    content: str | None = Field(
        None,
        description="Newline-separated search results.",
    )
    count: int = Field(
        default=0,
        description="Number of results returned.",
    )
    error: str | None = Field(
        None,
        description="Error message when success=False.",
    )


def _validate_search_path(path: str, workspace_root: str | None) -> tuple[str, str | None]:
    if workspace_root is not None:
        root = Path(workspace_root).resolve()
        resolved = _resolve_workspace_path(path, root)
    else:
        resolved = Path(path).resolve()
    return str(resolved), None


def _grep_sync(
    pattern: str,
    path: str,
    include: str | None,
    max_results: int,
    workspace_root: str | None,
) -> SearchResponse:
    resolved_path, err = _validate_search_path(path, workspace_root)
    if err:
        return SearchResponse(success=False, error=err, count=0)

    cmd = ["grep", "-rn", "-E", pattern, resolved_path]
    if include:
        cmd += ["--include", include]

    result = _subprocess_sync(cmd, False, 30.0, None)  # noqa: S603  # argv fixed: [grep, -rn, -E, <pattern>, <validated-path>]; shell=False

    if result.get("timed_out"):
        return SearchResponse(success=False, error="grep timed out", count=0)

    rc = result["returncode"]
    # exit code 0 = matches found, 1 = no matches (not an error); anything else is a real error
    if rc not in (0, 1):
        return SearchResponse(
            success=False, error=result["stderr"].strip() or f"grep exited with code {rc}", count=0
        )

    lines = [line for line in result["stdout"].splitlines() if line][:max_results]
    return SearchResponse(
        success=True,
        content="\n".join(lines),
        count=len(lines),
    )


def _find_sync(
    path: str,
    pattern: str,
    max_results: int,
    workspace_root: str | None,
) -> SearchResponse:
    resolved_path, err = _validate_search_path(path, workspace_root)
    if err:
        return SearchResponse(success=False, error=err, count=0)

    cmd = ["find", resolved_path, "-name", pattern]

    result = _subprocess_sync(cmd, False, 30.0, None)  # noqa: S603  # argv fixed: [find, <validated-path>, -name, <glob>]; shell=False

    if result.get("timed_out"):
        return SearchResponse(success=False, error="find timed out", count=0)

    if result["returncode"] != 0 and result["stderr"].strip():
        return SearchResponse(success=False, error=result["stderr"].strip(), count=0)

    lines = [line for line in result["stdout"].splitlines() if line][:max_results]
    return SearchResponse(
        success=True,
        content="\n".join(lines),
        count=len(lines),
    )


class SearchTool(LionTool):
    """Filesystem search tool (grep/find) with optional workspace containment.

    When *workspace_root* is supplied at construction time, every search path
    is resolved and checked to remain within that root before the subprocess
    is launched.  Paths that escape the root are rejected with a PermissionError
    (returned as a SearchResponse with success=False).

    If *workspace_root* is None (the default), no containment is applied —
    callers should pair this with a :class:`lionagi.agent.permissions.PermissionPolicy`
    allowlist, or set the root via ``create_agent()`` / ``AgentSpec``.
    """

    is_lion_system_tool = True
    system_tool_name = "search_tool"

    def __init__(self, workspace_root: str | None = None) -> None:
        self._tool = None
        # Resolve the containment root ONCE, at construction, against the cwd
        # in effect now. Storing the raw (possibly relative) value would let a
        # later os.chdir() move the boundary — e.g. a relative "ws" would be
        # re-resolved against whatever cwd is current when a search runs, so a
        # search could escape the originally intended root. Resolving here
        # freezes the boundary to an absolute path; re-resolving it downstream
        # is then idempotent.
        self._workspace_root = (
            str(Path(workspace_root).resolve()) if workspace_root is not None else None
        )

    async def handle_request(self, request: SearchRequest) -> SearchResponse:
        if isinstance(request, dict):
            request = SearchRequest(**request)

        # Validate path before launching subprocess (fail-closed)
        try:
            _validate_search_path(request.path, self._workspace_root)
        except PermissionError as exc:
            return SearchResponse(success=False, error=str(exc), count=0)

        if request.action == SearchAction.grep:
            return await run_sync(
                _grep_sync,
                request.pattern,
                request.path,
                request.include,
                request.max_results,
                self._workspace_root,
            )
        if request.action == SearchAction.find:
            return await run_sync(
                _find_sync,
                request.path,
                request.pattern,
                request.max_results,
                self._workspace_root,
            )
        return SearchResponse(success=False, error="Unknown action", count=0)

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def search_tool(**kwargs):
                """
                Search file contents or find files by name.

                Use action='grep' to find lines matching a regex across files — supports
                include glob to narrow the file set. Use action='find' to locate files
                whose names match a glob pattern. Both actions use portable POSIX tools
                (grep -E, find) with no external dependencies. Results are capped at
                max_results (default 50/100) to avoid flooding context.
                """
                return (await self.handle_request(SearchRequest(**kwargs))).model_dump()

            if self.system_tool_name != "search_tool":
                search_tool.__name__ = self.system_tool_name

            self._tool = Tool(
                func_callable=search_tool,
                request_options=SearchRequest,
            )
        return self._tool
