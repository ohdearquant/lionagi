# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln._paths import _resolve_workspace_root
from lionagi.ln.concurrency import run_sync
from lionagi.ln.types import Enum
from lionagi.protocols.action.tool import Tool

from ..base import LionTool

_MAX_LIST_FILES = 1_000


class ReaderAction(str, Enum):
    read = "read"
    list_dir = "list_dir"


class ReaderRequest(BaseModel):
    action: ReaderAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'read': Read a text file. Each line is returned as `<number>\\t<code>`; "
            "the line-number prefix is for navigation only — strip it (and the tab) "
            "before using any line as editor old_string. For large files use "
            "offset+limit to read in windows rather than loading everything at once.\n"
            "Text reads include total_lines and reached_eof. An offset at or past the "
            "end of a nonempty file returns an end-of-file notice.\n"
            "- 'list_dir': List files in a directory."
        ),
    )
    path: str = Field(
        ...,
        description=(
            "Absolute or workspace-relative file or directory path. "
            "Required for all actions. Paths must stay within the configured workspace root."
        ),
    )
    offset: int | None = Field(
        None,
        description=(
            "Zero-indexed line number to start reading from. "
            "Only used for 'read'. Defaults to 0. "
            "To read lines 200-400 of a large file: offset=200, limit=200."
        ),
    )
    limit: int | None = Field(
        None,
        description=(
            "Maximum number of lines to return. Only used for 'read'. "
            "Defaults to 2000. Increase for larger files or decrease for quick scans."
        ),
    )
    recursive: bool | None = Field(
        None,
        description=(
            "Whether to list files recursively in subdirectories. "
            "Only used for 'list_dir'. Defaults to False."
        ),
    )
    file_types: list[str] | None = Field(
        None,
        description=(
            "Filter by file extensions (e.g. ['.py', '.txt']). "
            "Only used for 'list_dir'. If omitted, all files are listed."
        ),
    )


class ReaderResponse(BaseModel):
    success: bool = Field(
        ...,
        description="True if the action completed without error.",
    )
    content: str | None = Field(
        None,
        description=(
            "The file content or end-of-file notice (for 'read'), or path listing (for 'list_dir')."
        ),
    )
    error: str | None = Field(
        None,
        description="Error message when success=False.",
    )
    total_lines: int | None = Field(
        None,
        description="Total number of lines in the text file; None when not known.",
    )
    reached_eof: bool | None = Field(
        None,
        description=(
            "Whether the read window reaches or starts beyond the end of the text file; "
            "None when not known."
        ),
    )


def _read_sync(
    path: str,
    offset: int | None,
    limit: int | None,
    workspace_root: Path,
) -> ReaderResponse:
    try:
        p = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return ReaderResponse(success=False, error=str(e))

    if p.is_symlink():
        return ReaderResponse(
            success=False,
            error=f"Refusing to read symlink: {path!r}. Use the real file path within the workspace.",
        )
    if not p.exists():
        return ReaderResponse(
            success=False,
            error=f"File not found: {path}. Check the path spelling and that it is within the workspace root.",
        )
    if not p.is_file():
        return ReaderResponse(
            success=False,
            error=f"Path is not a file: {path}. Use action='list_dir' to list directory contents.",
        )

    try:
        with open(p, "rb") as fbin:
            chunk = fbin.read(8192)
        if b"\x00" in chunk:
            return ReaderResponse(
                success=False,
                error=f"Binary file not supported: {path}. Use bash to inspect binary files (e.g. `file {path}`).",
            )
    except OSError as e:
        return ReaderResponse(
            success=False, error=f"Cannot open file: {e}. Check file permissions."
        )

    start = max(0, offset or 0)
    max_lines = limit if (limit is not None and limit > 0) else 2000

    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return ReaderResponse(
            success=False, error=f"Read error: {e}. Check file permissions and encoding."
        )

    total_lines = len(lines)
    selected = lines[start : start + max_lines]
    if total_lines > 0 and start >= total_lines:
        content = (
            f"Offset {start} is at or beyond the end of the file ({total_lines} lines). "
            f"Use an offset below {total_lines} to read file content."
        )
    else:
        content = "".join(f"{start + i + 1}\t{line}" for i, line in enumerate(selected))
    return ReaderResponse(
        success=True,
        content=content,
        total_lines=total_lines,
        reached_eof=start + len(selected) >= total_lines,
    )


def _list_dir_sync(
    path: str,
    recursive: bool | None,
    file_types: list[str] | None,
    workspace_root: Path,
) -> ReaderResponse:
    try:
        base = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return ReaderResponse(success=False, error=str(e))

    if not base.is_dir():
        return ReaderResponse(
            success=False,
            error=f"Path is not a directory: {path}. Use action='read' to read a file's contents.",
        )

    from lionagi.libs.file.process import dir_to_files

    try:
        files = dir_to_files(
            str(base),
            recursive=bool(recursive),
            file_types=file_types,
        )[:_MAX_LIST_FILES]
        content = "\n".join(str(f) for f in files)
    except Exception as e:
        return ReaderResponse(
            success=False,
            error=f"List error: {e}. Check the directory path and permissions.",
        )

    return ReaderResponse(success=True, content=content)


class ReaderTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "reader_tool"

    def __init__(self, *, workspace_root: str | Path | None = None):
        self._tool = None
        self.workspace_root = _resolve_workspace_root(workspace_root)

    async def handle_request(self, request: ReaderRequest) -> ReaderResponse:
        if isinstance(request, dict):
            request = ReaderRequest(**request)
        if not request.path:
            return ReaderResponse(
                success=False,
                error="'path' is required. Provide a file path for 'read' or a directory path for 'list_dir'.",
            )

        if request.action == ReaderAction.read:
            return await run_sync(
                _read_sync,
                request.path,
                request.offset,
                request.limit,
                self.workspace_root,
            )

        if request.action == ReaderAction.list_dir:
            return await run_sync(
                _list_dir_sync,
                request.path,
                request.recursive,
                request.file_types,
                self.workspace_root,
            )

        return ReaderResponse(success=False, error="Unknown action")

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def reader_tool(**kwargs):
                """Read text files or list directories.

                Use action='read' for text files. Output format is `<number>\\t<code>` per line;
                the number prefix is for reference only — strip the leading `<digits>\\t` before
                using any line as an editor old_string. Use offset+limit to read large files in
                windows (e.g. offset=200, limit=200 to get lines 200-400). Text reads
                include total_lines and reached_eof; an offset at or past a nonempty
                file's end returns an end-of-file notice instead of numbered content.

                Use action='list_dir' for directory listings.

                All paths are restricted to the configured workspace root.
                """
                return (await self.handle_request(ReaderRequest(**kwargs))).model_dump()

            if self.system_tool_name != "reader_tool":
                reader_tool.__name__ = self.system_tool_name

            self._tool = Tool(
                func_callable=reader_tool,
                request_options=ReaderRequest,
            )
        return self._tool
