# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from enum import Enum

from pydantic import BaseModel, Field

from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from ..base import LionTool


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


def _grep_sync(
    pattern: str,
    path: str,
    include: str | None,
    max_results: int,
) -> SearchResponse:
    cmd = ["grep", "-rn", "-E", pattern, path]
    if include:
        cmd += ["--include", include]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return SearchResponse(success=False, error="grep timed out", count=0)
    except FileNotFoundError:
        return SearchResponse(success=False, error="grep not found on this system", count=0)
    except Exception as e:
        return SearchResponse(success=False, error=f"grep error: {e}", count=0)

    # exit code 1 = no matches (not an error), 2 = real error
    if result.returncode == 2:
        return SearchResponse(success=False, error=result.stderr.strip(), count=0)

    lines = [line for line in result.stdout.splitlines() if line][:max_results]
    return SearchResponse(
        success=True,
        content="\n".join(lines),
        count=len(lines),
    )


def _find_sync(path: str, pattern: str, max_results: int) -> SearchResponse:
    cmd = ["find", path, "-name", pattern]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return SearchResponse(success=False, error="find timed out", count=0)
    except FileNotFoundError:
        return SearchResponse(success=False, error="find not found on this system", count=0)
    except Exception as e:
        return SearchResponse(success=False, error=f"find error: {e}", count=0)

    if result.returncode != 0 and result.stderr.strip():
        return SearchResponse(success=False, error=result.stderr.strip(), count=0)

    lines = [line for line in result.stdout.splitlines() if line][:max_results]
    return SearchResponse(
        success=True,
        content="\n".join(lines),
        count=len(lines),
    )


class SearchTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "search_tool"

    def __init__(self):
        self._tool = None

    async def handle_request(self, request: SearchRequest) -> SearchResponse:
        if isinstance(request, dict):
            request = SearchRequest(**request)
        if request.action == SearchAction.grep:
            return await run_sync(
                _grep_sync,
                request.pattern,
                request.path,
                request.include,
                request.max_results,
            )
        if request.action == SearchAction.find:
            return await run_sync(_find_sync, request.path, request.pattern, request.max_results)
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
