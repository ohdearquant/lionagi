# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from lionagi.ln._ssrf import is_ssrf_safe
from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from ..base import LionTool

# Finding 8/9: deny access to sensitive filenames
_DENIED_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", ".htpasswd"}
)
# Finding 9: allowed document extensions for the 'open' action
_DOC_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".pptx", ".docx", ".html", ".htm"})
# Finding 9: max document size for 'open' (50 MB)
_MAX_DOC_BYTES = 50 * 1024 * 1024
# Finding 8: max files returned by list_dir
_MAX_LIST_FILES = 1_000

_CACHE_TTL_SECONDS = 300  # 5 minutes


def _resolve_workspace_path(path: str, workspace_root: Path) -> Path:
    """Finding 8: resolve path under workspace_root; raise PermissionError if it escapes."""
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else workspace_root / raw
    # GAP B: check symlink on candidate BEFORE resolve() follows it
    if candidate.is_symlink():
        raise PermissionError(f"Refusing to access symlink: {path!r}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError as e:
        raise PermissionError(f"Path escapes workspace root: {path!r}") from e
    if resolved.name in _DENIED_NAMES:
        raise PermissionError(f"Refusing to access protected path: {resolved.name!r}")
    return resolved


class ReaderAction(str, Enum):
    read = "read"
    open = "open"
    list_dir = "list_dir"


class ReaderRequest(BaseModel):
    action: ReaderAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'read': Read a text file with line numbers (lightweight, no conversion).\n"
            "- 'open': Convert a document (PDF, PPTX, DOCX, HTML) to text via docling. "
            "Result is cached by path — chain with read in one turn: "
            "[open(path='x.pdf'), read(path='x.pdf', offset=0, limit=100)].\n"
            "- 'list_dir': List files in a directory."
        ),
    )
    path: str | None = Field(
        None,
        description=("File path, directory path, or URL. Required for all actions."),
    )
    offset: int | None = Field(
        None,
        description=(
            "Zero-indexed line number to start reading from. "
            "Used for 'read' and for reading cached 'open' results. Defaults to 0."
        ),
    )
    limit: int | None = Field(
        None,
        description=(
            "Maximum number of lines to return. Used for 'read' and cached reads. Defaults to 2000."
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
        description="The file content (for 'read') or path listing (for 'list_dir').",
    )
    error: str | None = Field(
        None,
        description="Error message when success=False.",
    )


def _read_sync(
    path: str,
    offset: int | None,
    limit: int | None,
    workspace_root: Path,
) -> ReaderResponse:
    # Finding 8: validate path before opening
    try:
        p = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return ReaderResponse(success=False, error=str(e))

    if p.is_symlink():
        return ReaderResponse(success=False, error=f"Refusing to read symlink: {path!r}")
    if not p.exists():
        return ReaderResponse(success=False, error=f"File not found: {path}")
    if not p.is_file():
        return ReaderResponse(success=False, error=f"Path is not a file: {path}")

    try:
        with open(p, "rb") as fbin:
            chunk = fbin.read(8192)
        if b"\x00" in chunk:
            return ReaderResponse(success=False, error=f"Binary file not supported: {path}")
    except OSError as e:
        return ReaderResponse(success=False, error=f"Cannot open file: {e}")

    start = max(0, offset or 0)
    max_lines = limit if (limit is not None and limit > 0) else 2000

    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return ReaderResponse(success=False, error=f"Read error: {e}")

    selected = lines[start : start + max_lines]
    numbered = "".join(f"{start + i + 1}\t{line}" for i, line in enumerate(selected))
    return ReaderResponse(success=True, content=numbered)


def _list_dir_sync(
    path: str,
    recursive: bool | None,
    file_types: list[str] | None,
    workspace_root: Path,
) -> ReaderResponse:
    # Finding 8: validate directory path before listing
    try:
        base = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return ReaderResponse(success=False, error=str(e))

    if not base.is_dir():
        return ReaderResponse(success=False, error=f"Path is not a directory: {path}")

    from lionagi.libs.file.process import dir_to_files

    try:
        # Finding 8: cap listing to prevent unbounded output
        files = dir_to_files(
            str(base),
            recursive=bool(recursive),
            file_types=file_types,
        )[:_MAX_LIST_FILES]
        content = "\n".join(str(f) for f in files)
    except Exception as e:
        return ReaderResponse(success=False, error=f"List error: {e}")

    return ReaderResponse(success=True, content=content)


def _open_sync(
    path: str,
    cache: dict[str, tuple[str, float]],
    workspace_root: Path,
    allowed_url_hosts: frozenset[str],
) -> ReaderResponse:
    """Finding 9: validate path/URL before passing to docling."""
    # Finding 9: split URL vs local file handling
    # NOTE: docling import is intentionally deferred until AFTER all URL/SSRF
    # validation so that the security checks remain testable without the
    # optional docling dependency installed.
    parsed = urlparse(path)
    if parsed.scheme in ("http", "https", "ftp"):
        if parsed.scheme != "https" or (parsed.hostname or "") not in allowed_url_hosts:
            return ReaderResponse(
                success=False,
                error=f"URL conversion not allowed: {path!r}. Only configured https hosts.",
            )
        # SSRF guard: hostname passed the allowlist but must also resolve to a public IP.
        if not is_ssrf_safe(parsed.hostname or ""):
            return ReaderResponse(
                success=False,
                error="URL blocked: hostname resolves to a private or reserved IP address.",
            )
        validated_path = path
    else:
        # local file: validate workspace containment, extension, and size
        try:
            p = _resolve_workspace_path(path, workspace_root)
        except PermissionError as e:
            return ReaderResponse(success=False, error=str(e))
        if p.suffix.lower() not in _DOC_EXTENSIONS:
            return ReaderResponse(
                success=False,
                error=f"Unsupported document type for 'open': {p.suffix!r}. "
                f"Allowed: {sorted(_DOC_EXTENSIONS)}",
            )
        try:
            if p.stat().st_size > _MAX_DOC_BYTES:
                return ReaderResponse(success=False, error="Document exceeds 50 MB size limit.")
        except OSError:
            pass
        validated_path = str(p)

    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        return ReaderResponse(
            success=False,
            error="docling not installed. Run: pip install lionagi[reader]",
        )

    try:
        converter = DocumentConverter()
        result = converter.convert(validated_path)
        text = result.document.export_to_markdown()
    except Exception as e:
        return ReaderResponse(success=False, error=f"Conversion error: {e}")

    cache[path] = (text, time.time())
    lines = text.split("\n")
    return ReaderResponse(
        success=True,
        content=f"Opened: {path} ({len(lines)} lines, {len(text)} chars). Use read with offset/limit to view.",
    )


def _read_cached(
    path: str, offset: int, limit: int, cache: dict[str, tuple[str, float]]
) -> ReaderResponse | None:
    """Read from cache if path was previously opened and not expired."""
    if path not in cache:
        return None
    text, cached_at = cache[path]
    if time.time() - cached_at > _CACHE_TTL_SECONDS:
        del cache[path]
        return None
    lines = text.split("\n")
    selected = lines[offset : offset + limit]
    numbered = "".join(f"{offset + i + 1}\t{line}\n" for i, line in enumerate(selected))
    return ReaderResponse(success=True, content=numbered)


def _evict_expired(cache: dict[str, tuple[str, float]]) -> int:
    """Remove expired entries. Returns count evicted."""
    now = time.time()
    expired = [k for k, (_, t) in cache.items() if now - t > _CACHE_TTL_SECONDS]
    for k in expired:
        del cache[k]
    return len(expired)


class ReaderTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "reader_tool"

    def __init__(
        self,
        cache_ttl: int = _CACHE_TTL_SECONDS,
        workspace_root: str | Path | None = None,
        allowed_url_hosts: frozenset[str] | set[str] | None = None,
    ):
        self._tool = None
        self._cache: dict[str, tuple[str, float]] = {}
        self._cache_ttl = cache_ttl
        # Finding 8: default to CWD when no workspace root is specified
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()
        # Finding 9: no URL hosts allowed by default
        self._allowed_url_hosts: frozenset[str] = frozenset(allowed_url_hosts or ())

    async def handle_request(self, request: ReaderRequest) -> ReaderResponse:
        if isinstance(request, dict):
            request = ReaderRequest(**request)
        if not request.path:
            return ReaderResponse(success=False, error="'path' is required")

        _evict_expired(self._cache)

        if request.action == ReaderAction.open:
            return await run_sync(
                _open_sync,
                request.path,
                self._cache,
                self.workspace_root,
                self._allowed_url_hosts,
            )

        if request.action == ReaderAction.read:
            start = max(0, request.offset or 0)
            limit = request.limit if (request.limit and request.limit > 0) else 2000
            cached = _read_cached(request.path, start, limit, self._cache)
            if cached is not None:
                return cached
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
                """Read files, convert documents (PDF/PPTX/DOCX/HTML via docling), or list directories.

                Use action='read' for text files (lightweight, line numbers).
                Use action='open' for documents needing conversion (PDF, PPTX, HTML) —
                result is cached by path, then use 'read' with offset/limit on the same path.
                Use action='list_dir' for directory listings.

                Tip: you can chain open + read in one turn as sequential actions:
                [open(path="report.pdf"), read(path="report.pdf", offset=0, limit=100)]
                The open caches the converted text, the read slices it — one round trip.

                All paths are restricted to the configured workspace root. URL conversion
                requires explicit host allowlisting.
                """
                return (await self.handle_request(ReaderRequest(**kwargs))).model_dump()

            if self.system_tool_name != "reader_tool":
                reader_tool.__name__ = self.system_tool_name

            self._tool = Tool(
                func_callable=reader_tool,
                request_options=ReaderRequest,
            )
        return self._tool
