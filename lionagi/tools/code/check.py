# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from ..base import LionTool

# Runtime availability of the ruff binary is checked lazily via shutil.which.
# No hard Python import is required — if the binary is absent the tool degrades
# to status='unavailable' with an actionable install message.


class CodeDiagnostic(BaseModel):
    """One structured diagnostic entry from a static-analysis tool."""

    file: str = Field(..., description="Absolute path of the file containing the diagnostic.")
    line: int = Field(..., description="1-based line number.")
    col: int = Field(..., description="1-based column number.")
    end_line: int | None = Field(None, description="End line (inclusive), if available.")
    end_col: int | None = Field(None, description="End column (inclusive), if available.")
    severity: Literal["error", "warning", "info"] = Field(
        "warning",
        description="Diagnostic severity: 'error', 'warning', or 'info'.",
    )
    code: str = Field("", description="Rule code (e.g. 'E501', 'F401').")
    message: str = Field(..., description="Human-readable diagnostic message.")
    source: str = Field("ruff", description="Name of the tool that emitted this diagnostic.")
    fixable: bool = Field(False, description="True if the tool can auto-fix this issue.")

    def as_text(self) -> str:
        """One-line file:line:col summary suitable for model consumption."""
        fix = " [fixable]" if self.fixable else ""
        return (
            f"{self.file}:{self.line}:{self.col}: "
            f"{self.severity.upper()} {self.code} {self.message}{fix}"
        )


class CodeCheckRequest(BaseModel):
    paths: list[str] = Field(
        ...,
        description=(
            "One or more file or directory paths to check. "
            "Absolute or workspace-relative paths. "
            "Pass the file you just edited to get immediate post-edit feedback: "
            "call code_check(paths=[<edited_file>]) after any editor action. "
            "Returns structured file:line:col diagnostics the agent can act on directly."
        ),
    )
    tool: Literal["ruff"] = Field(
        default="ruff",
        description=(
            "Static-analysis tool to run. Currently only 'ruff' is supported. "
            "Requires the 'ruff' binary in PATH; returns status='unavailable' if absent. "
            "Install with `uv add ruff` to enable."
        ),
    )
    max_diagnostics: int = Field(
        default=50,
        description=(
            "Maximum number of diagnostics to return per call. "
            "Increase to see all issues on large files; decrease to reduce noise. "
            "Range: 1–500."
        ),
        ge=1,
        le=500,
    )


class CodeCheckResponse(BaseModel):
    status: Literal["ok", "diagnostics", "unavailable", "error"] = Field(
        ...,
        description=(
            "'ok' — tool ran with no findings; "
            "'diagnostics' — one or more findings returned; "
            "'unavailable' — the requested tool binary is not installed; "
            "'error' — tool ran but failed unexpectedly."
        ),
    )
    diagnostics: list[CodeDiagnostic] = Field(
        default_factory=list,
        description=(
            "Structured diagnostics. "
            "Empty when status='ok' or 'unavailable'. "
            "Each entry has file, line, col, code, message, severity, and fixable flag."
        ),
    )
    summary: str = Field(
        "",
        description="One-line human-readable summary of the check result.",
    )
    tool: str = Field("ruff", description="Name of the tool that produced these results.")


def _resolve_check_paths(
    paths: list[str], workspace_root: Path
) -> tuple[list[str], CodeCheckResponse | None]:
    """Resolve paths against workspace_root; return (resolved, None) or ([], error_response)."""
    resolved: list[str] = []
    for raw in paths:
        try:
            p = _resolve_workspace_path(raw, workspace_root)
        except PermissionError as exc:
            return [], CodeCheckResponse(
                status="error",
                summary=str(exc),
                tool="ruff",
            )
        resolved.append(str(p))
    return resolved, None


def _ruff_check_sync(
    paths: list[str], max_diagnostics: int, cwd: str | None = None
) -> CodeCheckResponse:
    """Run ``ruff check --output-format=json`` on pre-resolved paths; return structured results."""
    ruff_bin = shutil.which("ruff")
    if ruff_bin is None:
        return CodeCheckResponse(
            status="unavailable",
            summary=(
                "ruff is not installed or not in PATH. "
                "Install with `uv add ruff` (or `uv add --dev ruff`) to enable static analysis."
            ),
            tool="ruff",
        )

    cmd = [ruff_bin, "check", "--output-format=json", "--"] + paths
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return CodeCheckResponse(
            status="error",
            summary=(
                "ruff check timed out after 30 s. "
                "Try passing individual files instead of large directories."
            ),
            tool="ruff",
        )
    except OSError as e:
        return CodeCheckResponse(
            status="error",
            summary=f"ruff check failed to start: {e}",
            tool="ruff",
        )

    # ruff exits 0 (no violations) or 1 (violations found); 2+ is an internal error.
    raw_output = result.stdout.strip()
    if result.returncode >= 2 and not raw_output:
        return CodeCheckResponse(
            status="error",
            summary=f"ruff exited {result.returncode}: {result.stderr.strip()[:300]}",
            tool="ruff",
        )

    if not raw_output:
        return CodeCheckResponse(
            status="ok",
            summary="No issues found.",
            tool="ruff",
        )

    try:
        raw_diags = json.loads(raw_output)
    except json.JSONDecodeError as e:
        return CodeCheckResponse(
            status="error",
            summary=f"Failed to parse ruff JSON output: {e}",
            tool="ruff",
        )

    if not raw_diags:
        return CodeCheckResponse(
            status="ok",
            summary="No issues found.",
            tool="ruff",
        )

    diagnostics: list[CodeDiagnostic] = []
    for entry in raw_diags[:max_diagnostics]:
        loc = entry.get("location") or {}
        end_loc = entry.get("end_location") or {}
        fix = entry.get("fix")
        raw_sev = entry.get("severity", "warning")
        if raw_sev == "error":
            sev: Literal["error", "warning", "info"] = "error"
        elif raw_sev == "warning":
            sev = "warning"
        else:
            sev = "info"
        diagnostics.append(
            CodeDiagnostic(
                file=entry.get("filename", ""),
                line=loc.get("row", 0),
                col=loc.get("column", 0),
                end_line=end_loc.get("row"),
                end_col=end_loc.get("column"),
                severity=sev,
                code=entry.get("code", ""),
                message=entry.get("message", ""),
                source="ruff",
                fixable=bool(fix),
            )
        )

    total = len(raw_diags)
    shown = len(diagnostics)
    truncated = f" (showing {shown}/{total})" if total > shown else ""
    summary = f"{total} issue(s) found{truncated}."

    return CodeCheckResponse(
        status="diagnostics",
        diagnostics=diagnostics,
        summary=summary,
        tool="ruff",
    )


class CodeCheckTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "code_check"

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._tool: Tool | None = None
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()

    async def handle_request(self, request: CodeCheckRequest) -> CodeCheckResponse:
        if isinstance(request, dict):
            request = CodeCheckRequest(**request)
        # Enforce workspace containment before any subprocess call.  An empty
        # paths list would let ruff fall back to scanning the process working
        # directory, which may lie outside the workspace — default it to the
        # workspace root instead.
        paths = request.paths or [str(self.workspace_root)]
        resolved_paths, err = _resolve_check_paths(paths, self.workspace_root)
        if err is not None:
            return err
        if request.tool == "ruff":
            return await run_sync(
                _ruff_check_sync,
                resolved_paths,
                request.max_diagnostics,
                str(self.workspace_root),
            )
        return CodeCheckResponse(
            status="unavailable",
            summary=(f"Tool '{request.tool}' is not yet supported. Currently supported: 'ruff'."),
            tool=request.tool,
        )

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def code_check(**kwargs):
                """
                Run static analysis on Python files and return structured diagnostics.

                Call this after editing a file to get immediate IDE-grade feedback.
                Each diagnostic is returned as file:line:col with code and message so
                the agent can locate and fix the issue without re-reading the file.

                Composability (edit -> check workflow):
                  1. editor(action='edit', file_path=..., old_string=..., new_string=...)
                  2. code_check(paths=[<same file_path>])
                  3. Diagnostics list gives actionable file:line:col entries to fix next.

                Supported tools:
                - 'ruff': fast Python linter (default). Requires ruff in PATH.
                  Returns status='unavailable' if the binary is absent — not an error.

                Result status values:
                - 'ok': no issues found
                - 'diagnostics': one or more issues found (see diagnostics list)
                - 'unavailable': tool binary not installed
                - 'error': tool failed unexpectedly
                """
                return (await self.handle_request(CodeCheckRequest(**kwargs))).model_dump()

            self._tool = Tool(
                func_callable=code_check,
                request_options=CodeCheckRequest,
            )
        return self._tool
