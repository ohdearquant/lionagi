# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from ..base import LionTool

__all__ = (
    "AstSearchRequest",
    "AstSearchResponse",
    "AstSearchTool",
)


class AstSearchMatch(BaseModel):
    file: str = Field(..., description="Absolute path of the matched file.")
    line: int = Field(..., description="1-based line number of the match.")
    col: int = Field(..., description="0-based column offset of the match.")
    text: str = Field(..., description="Matched source text.")


class AstSearchRequest(BaseModel):
    pattern: str = Field(
        ...,
        description=(
            "ast-grep pattern to match. Uses tree-sitter AST shape, not plain text. "
            "Examples: 'except: pass' (bare except), '$FUNC(None)' (call with None arg), "
            "'def $F($$$): ...' (any function definition)."
        ),
    )
    path: str = Field(
        default=".",
        description="File or directory to search. Defaults to '.' (workspace root).",
    )
    lang: Literal["python", "rust", "typescript", "javascript", "go", "c", "cpp"] = Field(
        default="python",
        description="Language of the target files. Passed as --lang to ast-grep.",
    )
    max_results: int = Field(
        default=50,
        description="Maximum number of matches to return. Range: 1–500.",
        ge=1,
        le=500,
    )


class AstSearchResponse(BaseModel):
    status: Literal["ok", "matches", "unavailable", "error"] = Field(
        ...,
        description=(
            "'ok' — search ran, no matches found; "
            "'matches' — one or more matches returned; "
            "'unavailable' — ast-grep (sg) binary not installed; "
            "'error' — unexpected failure."
        ),
    )
    matches: list[AstSearchMatch] = Field(default_factory=list)
    summary: str = Field("", description="One-line human-readable summary.")
    total: int = Field(0, description="Total matches found (before max_results cap).")


def _ast_search_sync(
    pattern: str,
    path: str,
    lang: str,
    max_results: int,
    cwd: str | None = None,
) -> AstSearchResponse:
    sg_bin = shutil.which("sg") or shutil.which("ast-grep")
    if sg_bin is None:
        return AstSearchResponse(
            status="unavailable",
            summary=(
                "ast-grep (sg) is not installed or not in PATH. "
                "Install from https://ast-grep.github.io or via `cargo install ast-grep`."
            ),
        )

    cmd = [sg_bin, "run", "--pattern", pattern, "--lang", lang, "--json", path]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return AstSearchResponse(
            status="error",
            summary="ast-grep timed out after 30 s. Try narrowing the search path.",
        )
    except OSError as e:
        return AstSearchResponse(status="error", summary=f"ast-grep failed to start: {e}")

    # sg exits 0 (matches) or 1 (no matches); 2+ is an error.
    if result.returncode >= 2:
        return AstSearchResponse(
            status="error",
            summary=f"ast-grep exited {result.returncode}: {result.stderr.strip()[:300]}",
        )

    raw = result.stdout.strip()
    if not raw:
        return AstSearchResponse(status="ok", summary="No matches found.")

    import json

    try:
        raw_matches = json.loads(raw)
    except json.JSONDecodeError:
        # sg may emit NDJSON (one JSON object per line) or a JSON array.
        try:
            raw_matches = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError as e:
            return AstSearchResponse(
                status="error", summary=f"Failed to parse ast-grep output: {e}"
            )

    if not raw_matches:
        return AstSearchResponse(status="ok", summary="No matches found.")

    total = len(raw_matches)
    matches: list[AstSearchMatch] = []
    for entry in raw_matches[:max_results]:
        # ast-grep JSON output schema: {file, range: {start: {line, column}}, text}
        rng = entry.get("range") or {}
        start = rng.get("start") or {}
        matches.append(
            AstSearchMatch(
                file=entry.get("file", ""),
                line=start.get("line", 0) + 1,  # ast-grep uses 0-based lines
                col=start.get("column", 0),
                text=(entry.get("text") or "").strip(),
            )
        )

    shown = len(matches)
    truncated = f" (showing {shown}/{total})" if total > shown else ""
    return AstSearchResponse(
        status="matches",
        matches=matches,
        summary=f"{total} match(es) found{truncated}.",
        total=total,
    )


class AstSearchTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "ast_search"

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._tool: Tool | None = None
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()

    async def handle_request(self, request: AstSearchRequest) -> AstSearchResponse:
        if isinstance(request, dict):
            request = AstSearchRequest(**request)
        path = request.path or "."
        try:
            resolved = str(_resolve_workspace_path(path, self.workspace_root))
        except PermissionError as exc:
            return AstSearchResponse(status="error", summary=str(exc))

        return await run_sync(
            _ast_search_sync,
            request.pattern,
            resolved,
            request.lang,
            request.max_results,
            str(self.workspace_root),
        )

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def ast_search(**kwargs):
                """Search source code by AST shape using ast-grep (structural patterns, not text — e.g. 'except: pass' regardless of whitespace).
                Requires the 'sg'/'ast-grep' binary in PATH; returns status='unavailable' if absent, not an error.
                """
                return (await self.handle_request(AstSearchRequest(**kwargs))).model_dump()

            self._tool = Tool(func_callable=ast_search, request_options=AstSearchRequest)
        return self._tool
