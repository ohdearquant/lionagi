# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import os
from pathlib import Path

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln._paths import _resolve_workspace_root
from lionagi.ln.concurrency import run_sync
from lionagi.ln.types import Enum
from lionagi.protocols.action.tool import Tool

from ..base import LionTool
from ._bounded_io import _NotRegularFileError, _open_regular_file

# A read keeps only its line window but still scans from the top of the file to
# find it and to count lines. The scan stops here so a worker's time stays bounded
# on any file; 64 MiB reads in well under a second from local disk.
_MAX_TEXT_SCAN_BYTES = 64 * 1024 * 1024
# Characters kept from one line; the rest of the line is scanned and dropped, so a
# file without newlines cannot turn a one-line window into the whole file.
_MAX_LINE_CHARS = 1_000_000
_LINE_CHUNK_CHARS = 65_536
_MAX_LIST_FILES = 1_000
_MAX_DIRECTORY_ENTRIES = 10_000


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
            "before using any line as editor old_string. Use offset+limit to read "
            "a line window.\n"
            "Text reads include total_lines and reached_eof. An offset at or past the "
            "end of a nonempty file returns an end-of-file notice. A read scans at most "
            f"the first {_MAX_TEXT_SCAN_BYTES:,} bytes: past that, total_lines and "
            "reached_eof are None, and an offset beyond the scanned part is an error. "
            f"A line longer than {_MAX_LINE_CHARS:,} characters is cut.\n"
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


def _scan_window(binary, start: int, max_lines: int) -> tuple[list[str], int | None]:
    """Lines ``start`` to ``start + max_lines`` and the line count, within the scan budget.

    The count is None when the budget ran out before the end of the file.
    """
    raw = binary.raw
    selected: list[str] = []
    index = 0
    text = io.TextIOWrapper(binary, encoding="utf-8", errors="replace")
    try:
        while True:
            if raw.tell() > _MAX_TEXT_SCAN_BYTES:
                return selected, None
            piece = text.readline(_LINE_CHUNK_CHARS)
            if not piece:
                return selected, index
            keep = start <= index < start + max_lines
            line = piece if keep else ""
            out_of_budget = False
            while not piece.endswith("\n"):
                if raw.tell() > _MAX_TEXT_SCAN_BYTES:
                    out_of_budget = True
                    break
                piece = text.readline(_LINE_CHUNK_CHARS)
                if not piece:
                    break
                if keep and len(line) <= _MAX_LINE_CHARS:
                    line += piece
            if keep:
                if len(line) > _MAX_LINE_CHARS:
                    ending = "\n" if piece.endswith("\n") else ""
                    line = (
                        line[:_MAX_LINE_CHARS]
                        + f" [... line truncated at {_MAX_LINE_CHARS:,} characters]{ending}"
                    )
                selected.append(line)
            if out_of_budget:
                return selected, None
            index += 1
    finally:
        text.detach()


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
    start = max(0, offset or 0)
    max_lines = limit if (limit is not None and limit > 0) else 2000
    try:
        with _open_regular_file(p, os.O_RDONLY) as (fd, _info):
            with os.fdopen(fd, "rb", closefd=False) as binary:
                if b"\x00" in binary.read(8192):
                    return ReaderResponse(
                        success=False,
                        error=f"Binary file not supported: {path}. Use bash to inspect binary files (e.g. `file {path}`).",
                    )
                binary.seek(0)
                selected, total_lines = _scan_window(binary, start, max_lines)
    except FileNotFoundError:
        return ReaderResponse(
            success=False,
            error=f"File not found: {path}. Check the path spelling and that it is within the workspace root.",
        )
    except _NotRegularFileError as e:
        return ReaderResponse(
            success=False,
            error=f"{e} Use action='list_dir' to list directory contents.",
        )
    except OSError as e:
        return ReaderResponse(
            success=False, error=f"Cannot open file: {e}. Check file permissions."
        )

    if total_lines is None:
        if start > 0 and not selected:
            return ReaderResponse(
                success=False,
                error=(
                    f"Offset {start} is beyond the first {_MAX_TEXT_SCAN_BYTES:,} bytes of "
                    f"{path}, which is as far as a read scans. Use bash (e.g. `tail` or `sed -n`) "
                    "to reach later lines."
                ),
            )
        content = "".join(f"{start + i + 1}\t{line}" for i, line in enumerate(selected))
        return ReaderResponse(success=True, content=content, total_lines=None, reached_eof=None)

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


def _scan_files(base: Path, recursive: bool, file_types: list[str] | None) -> list[Path]:
    files: list[Path] = []
    pending = [base]
    scanned = 0
    suffixes = None if file_types is None else frozenset(file_types)
    while pending:
        directory = pending.pop()
        try:
            listing = os.scandir(directory)
        except OSError:
            # The base must be readable; an unreadable subdirectory is skipped, as a
            # recursive glob skips it.
            if directory is base:
                raise
            continue
        with listing as entries:
            for entry in entries:
                scanned += 1
                if scanned > _MAX_DIRECTORY_ENTRIES:
                    raise ValueError(
                        f"Directory {base} exceeds the {_MAX_DIRECTORY_ENTRIES:,}-entry scan limit"
                    )
                if recursive and entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif len(files) < _MAX_LIST_FILES and entry.is_file():
                    path = Path(entry.path)
                    if suffixes is None or path.suffix in suffixes:
                        files.append(path)
    return files


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

    try:
        files = _scan_files(base, bool(recursive), file_types)
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
                using any line as an editor old_string. Use offset+limit to read text files in
                windows (e.g. offset=200, limit=200 to get lines 200-400). Text reads
                include total_lines and reached_eof; an offset at or past a nonempty
                file's end returns an end-of-file notice instead of numbered content.
                A read scans at most the first 64 MiB of a file; past that, total_lines
                and reached_eof are None and later lines need bash.

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
