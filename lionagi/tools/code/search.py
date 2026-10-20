# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import is_protected_name
from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln.concurrency import run_sync
from lionagi.ln.types import Enum
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
    path: str | None = Field(
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
    max_results: int | None = Field(
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


def _validate_search_path(path: str, workspace_root: str | None) -> str:
    if workspace_root is not None:
        root = Path(workspace_root).resolve()
        resolved = _resolve_workspace_path(path, root)
    else:
        resolved = Path(path).resolve()
    return str(resolved)


def _visible_grep_lines(stdout: str) -> list[str]:
    """grep's ``path\\0line:text`` rows as ``path:line:text``, without the rows
    of a file whose path holds a protected name (``.env``, a private key).

    The reader refuses those files by name, and a recursive search reaches
    them as well, so their lines are left out here. A file name can hold a
    newline but never a NUL, so each row is read from its NUL: the name is
    everything since the previous row, the line runs to the next newline.
    Text with no NUL after it is not a row and is dropped.
    """
    rows: list[str] = []
    start = 0
    while (nul := stdout.find("\0", start)) != -1:
        end = stdout.find("\n", nul + 1)
        if end == -1:
            end = len(stdout)
        path, rest = stdout[start:nul], stdout[nul + 1 : end]
        start = end + 1
        if any(is_protected_name(part) for part in Path(path).parts):
            continue
        rows.append(f"{path}:{rest}")
    return rows


def _grep_sync(
    pattern: str,
    path: str,
    include: str | None,
    max_results: int,
    workspace_root: str | None,
) -> SearchResponse:
    resolved_path = _validate_search_path(path, workspace_root)

    # -e: a pattern that starts with "-" is a pattern, never an option.
    # --null ends each file name with a NUL, so a row's file is read exactly.
    # -I skips binary files, whose one-line notice carries no NUL to read by.
    cmd = ["grep", "-rnI", "--null", "-E", "-e", pattern, resolved_path]
    if include:
        cmd += ["--include", include]

    result = _subprocess_sync(cmd, False, 30.0, None)  # noqa: S603  # argv fixed: [grep, -rnI, --null, -E, -e, <pattern>, <validated-path>]; shell=False

    if result.get("timed_out"):
        return SearchResponse(success=False, error="grep timed out", count=0)

    rc = result["returncode"]
    # exit code 0 = matches found, 1 = no matches (not an error); anything else is a real error
    if rc not in (0, 1):
        return SearchResponse(
            success=False, error=result["stderr"].strip() or f"grep exited with code {rc}", count=0
        )

    lines = _visible_grep_lines(result["stdout"])[:max_results]
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
    resolved_path = _validate_search_path(path, workspace_root)

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
    """Filesystem grep/find tool with optional workspace-root containment."""

    is_lion_system_tool = True
    system_tool_name = "search_tool"

    def __init__(self, workspace_root: str | None = None) -> None:
        self._tool = None
        # Resolved to absolute ONCE — a stored relative root would re-resolve
        # against a later os.chdir()'d cwd and could escape the boundary.
        self._workspace_root = (
            str(Path(workspace_root).resolve()) if workspace_root is not None else None
        )

    async def handle_request(self, request: SearchRequest) -> SearchResponse:
        if isinstance(request, dict):
            request = SearchRequest(**request)

        # Callers (LLM tool calls) may send explicit null for optional fields; treat as defaults.
        path = request.path or "."
        max_results = request.max_results or 50

        # Validate path before launching subprocess (fail-closed)
        try:
            _validate_search_path(path, self._workspace_root)
        except PermissionError as exc:
            return SearchResponse(success=False, error=str(exc), count=0)

        if request.action == SearchAction.grep:
            return await run_sync(
                _grep_sync,
                request.pattern,
                path,
                request.include,
                max_results,
                self._workspace_root,
            )
        if request.action == SearchAction.find:
            return await run_sync(
                _find_sync,
                path,
                request.pattern,
                max_results,
                self._workspace_root,
            )
        return SearchResponse(success=False, error="Unknown action", count=0)

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def search_tool(**kwargs):
                """Search file contents by regex (action='grep') or find files by name glob (action='find').
                Uses portable POSIX tools (grep -E, find); results capped at max_results (default 50/100).
                """
                return (await self.handle_request(SearchRequest(**kwargs))).model_dump()

            if self.system_tool_name != "search_tool":
                search_tool.__name__ = self.system_tool_name

            self._tool = Tool(
                func_callable=search_tool,
                request_options=SearchRequest,
            )
        return self._tool
