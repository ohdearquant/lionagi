# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import contextlib
import os
import re
import shlex
import signal
import subprocess
import threading
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool
from lionagi.service.token_calculator import TokenCalculator

from .base import LionTool

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


# ---------------------------------------------------------------------------
# Workspace path validation (Finding 14)
# ---------------------------------------------------------------------------

_DENIED_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", ".htpasswd"}
)


def _resolve_workspace_path(path: str, workspace_root: Path) -> Path:
    """Finding 14: resolve path under workspace_root; raise PermissionError if it escapes."""
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


# ---------------------------------------------------------------------------
# Request models (LLM-facing schemas)
# ---------------------------------------------------------------------------


class ReaderAction(str, Enum):
    read = "read"
    list_dir = "list_dir"


class ReaderRequest(BaseModel):
    action: ReaderAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'read': Read a file and return its contents with line numbers.\n"
            "- 'list_dir': List files in a directory."
        ),
    )
    path: str = Field(
        ...,
        description="Absolute path to a file (for 'read') or directory (for 'list_dir').",
    )
    offset: int | None = Field(
        None,
        description="Zero-indexed line number to start reading from. Defaults to 0.",
    )
    limit: int | None = Field(
        None,
        description="Maximum number of lines to return. Defaults to 2000.",
    )
    recursive: bool | None = Field(
        None,
        description="Whether to list subdirectories recursively. Only for 'list_dir'.",
    )
    file_types: list[str] | None = Field(
        None,
        description="Filter by extensions (e.g. ['.py', '.rs']). Only for 'list_dir'.",
    )


class EditorAction(str, Enum):
    write = "write"
    edit = "edit"


class EditorRequest(BaseModel):
    action: EditorAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'write': Create or overwrite a file. Creates parent dirs automatically.\n"
            "- 'edit': Exact string replacement. Fails if old_string not found or ambiguous."
        ),
    )
    file_path: str = Field(
        ...,
        description="Absolute path to the target file.",
    )
    content: str | None = Field(
        None,
        description="Full file content. Required for 'write'.",
    )
    old_string: str | None = Field(
        None,
        description="Exact text to find. Required for 'edit'. Must match byte-for-byte.",
    )
    new_string: str | None = Field(
        None,
        description="Replacement text. Required for 'edit'. Empty string = deletion.",
    )
    replace_all: bool = Field(
        default=False,
        description="Replace all occurrences. If False and multiple matches, edit fails.",
    )


class BashRequest(BaseModel):
    command: str = Field(
        ...,
        description="Shell command to execute.",
    )
    timeout: int | None = Field(
        None,
        description="Timeout in milliseconds. Default 30000, max 300000.",
    )
    cwd: str | None = Field(
        None,
        description="Working directory. Defaults to current directory.",
    )


class SearchAction(str, Enum):
    grep = "grep"
    find = "find"


class SearchRequest(BaseModel):
    action: SearchAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'grep': Search file contents with regex pattern.\n"
            "- 'find': Find files by name pattern."
        ),
    )
    pattern: str = Field(
        ...,
        description="Regex pattern (for 'grep') or glob pattern (for 'find').",
    )
    path: str | None = Field(
        None,
        description="File or directory to search in. Defaults to current directory.",
    )
    include: str | None = Field(
        None,
        description="Glob filter for grep, e.g. '*.py'. Only for 'grep'.",
    )
    max_results: int | None = Field(
        None,
        description="Max results to return. Default 50 for grep, 100 for find.",
    )


class ContextAction(str, Enum):
    status = "status"
    get_messages = "get_messages"
    evict = "evict"
    evict_action_results = "evict_action_results"


class ContextRequest(BaseModel):
    action: ContextAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'status': Context usage — message count, types, token estimate.\n"
            "- 'get_messages': List messages with index, role, preview.\n"
            "- 'evict': Remove messages by index range (protects system message).\n"
            "- 'evict_action_results': Remove old tool outputs, keep last N."
        ),
    )
    start: int | None = Field(None, description="Start index (inclusive, 0-based).")
    end: int | None = Field(None, description="End index (exclusive, 0-based).")
    keep_last: int | None = Field(
        None,
        description="For 'evict_action_results': keep N most recent. Default 5.",
    )


class SandboxAction(str, Enum):
    create = "create"
    diff = "diff"
    commit = "commit"
    merge = "merge"
    discard = "discard"


class SandboxRequest(BaseModel):
    action: SandboxAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'create': Create an isolated git worktree for safe experimentation.\n"
            "- 'diff': See what changed in the sandbox vs the base branch.\n"
            "- 'commit': Commit current changes in the sandbox.\n"
            "- 'merge': Apply sandbox changes back to the main branch and clean up.\n"
            "- 'discard': Throw away the sandbox and all changes."
        ),
    )
    message: str | None = Field(
        None,
        description="Commit message. Required for 'commit'.",
    )


class SubagentRequest(BaseModel):
    instruction: str = Field(
        ...,
        description=(
            "Task description for the sub-agent. Be specific about what to do, "
            "what files to look at, and what output you expect."
        ),
    )
    permissions: str = Field(
        default="read_only",
        description=(
            "Permission level for the sub-agent. One of:\n"
            "- 'read_only': Can only read files and search (safest).\n"
            "- 'safe': Can read/write/search, bash restricted (no rm/sudo).\n"
            "- 'inherit': Same permissions as parent agent.\n"
            "- 'allow_all': No restrictions (use with caution)."
        ),
    )
    max_turns: int = Field(
        default=20,
        description="Maximum ReAct iterations. Default 20, max 50.",
    )
    cwd: str | None = Field(
        None,
        description="Working directory for the sub-agent. Defaults to parent's cwd.",
    )


# ---------------------------------------------------------------------------
# Blocking helpers (run via run_sync in async tools)
# ---------------------------------------------------------------------------


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def _read_image_sync(path: str, workspace_root: Path) -> dict:
    # Finding 14: validate path before reading image bytes
    try:
        p = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return {"success": False, "error": str(e)}
    ext = p.suffix.lower()
    media_type = _IMAGE_MEDIA_TYPES.get(ext, "image/png")
    try:
        raw = p.read_bytes()
    except OSError as e:
        return {"success": False, "error": str(e)}
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "success": True,
        "type": "image",
        "media_type": media_type,
        "content": f"data:{media_type};base64,{encoded}",
        "size_bytes": len(raw),
    }


def _read_file_sync(
    path: str, offset: int, max_lines: int, workspace_root: Path
) -> dict:
    # Finding 14: validate path under workspace root
    try:
        p = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    if not p.exists():
        return {"success": False, "error": f"File not found: {path}"}
    if not p.is_file():
        return {"success": False, "error": f"Not a file: {path}"}

    if p.suffix.lower() in _IMAGE_EXTENSIONS:
        return _read_image_sync(path, workspace_root)

    try:
        with open(p, "rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return {"success": False, "error": f"Binary file: {path}"}
    except OSError as e:
        return {"success": False, "error": str(e)}

    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return {"success": False, "error": str(e)}

    selected = lines[offset : offset + max_lines]
    numbered = "".join(f"{offset + i + 1}\t{line}" for i, line in enumerate(selected))

    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0

    return {
        "success": True,
        "content": numbered,
        "_resolved": str(p.resolve()),
        "_mtime": mtime,
    }


def _list_dir_sync(
    path: str, recursive: bool, file_types: list[str] | None, workspace_root: Path
) -> dict:
    # Finding 14: validate directory path
    try:
        base = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    from lionagi.libs.file.process import dir_to_files

    try:
        files = dir_to_files(str(base), recursive=recursive, file_types=file_types)
        return {"success": True, "content": "\n".join(str(f) for f in files)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _write_file_sync(file_path: str, content: str, workspace_root: Path) -> dict:
    # Finding 14: validate path before writing
    try:
        p = _resolve_workspace_path(file_path, workspace_root)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": str(e)}

    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0

    return {
        "success": True,
        "content": f"Written: {p} ({len(content)} chars)",
        "_resolved": str(p.resolve()),
        "_mtime": mtime,
    }


def _edit_file_sync(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    workspace_root: Path,
) -> dict:
    # Finding 14: validate path before reading or writing
    try:
        p = _resolve_workspace_path(file_path, workspace_root)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    try:
        original = p.read_text(encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": str(e)}

    count = original.count(old_string)
    if count == 0:
        return {"success": False, "error": f"old_string not found in {file_path}"}
    if count > 1 and not replace_all:
        return {
            "success": False,
            "error": f"old_string appears {count} times. Set replace_all=True.",
        }

    updated = original.replace(old_string, new_string, -1 if replace_all else 1)

    try:
        p.write_text(updated, encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": str(e)}

    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0

    idx = updated.find(new_string)
    s = max(0, idx - 40)
    e = min(len(updated), idx + len(new_string) + 40)
    snippet = updated[s:e]

    return {
        "success": True,
        "content": f"Replaced {count if replace_all else 1}x. ...{snippet}...",
        "_resolved": str(p.resolve()),
        "_mtime": mtime,
    }


# GAP A: shell control operators — same pattern as bash.py
_SHELL_CONTROL = re.compile(r"(;|&&|\|\||\||`|\$\(|[<>]|\n)")

_MAX_OUTPUT_BYTES = 100_000


def _drain_stream(stream, buf: bytearray) -> bool:
    """Finding 5: read stream into buf up to _MAX_OUTPUT_BYTES; return True if truncated.

    Continues reading even after cap to prevent pipe-buffer deadlock.
    """
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
    return truncated


def _subprocess_sync(cmd, shell: bool, timeout_s: float, cwd: str | None) -> dict:
    # Finding 5: use Popen + threads for bounded memory capture and child-group kill
    try:
        proc = subprocess.Popen(
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or None,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]

    def _drain_out():
        stdout_truncated[0] = _drain_stream(proc.stdout, stdout_buf)

    def _drain_err():
        stderr_truncated[0] = _drain_stream(proc.stderr, stderr_buf)

    t_out = threading.Thread(target=_drain_out, daemon=True)
    t_err = threading.Thread(target=_drain_err, daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if isinstance(proc.pid, int) and proc.pid > 1:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        timed_out = True

    if not timed_out:
        t_out.join()
        t_err.join()

    def _decode(buf: bytearray, truncated: bool) -> str:
        text = bytes(buf).decode("utf-8", errors="replace")
        if truncated:
            text += f"\n\n[... truncated at {_MAX_OUTPUT_BYTES} bytes ...]\n"
        return text

    if timed_out:
        return {
            "stdout": _decode(stdout_buf, True),
            "stderr": f"Timed out after {timeout_s}s",
            "returncode": -1,
            "timed_out": True,
        }

    return {
        "stdout": _decode(stdout_buf, stdout_truncated[0]),
        "stderr": _decode(stderr_buf, stderr_truncated[0]),
        "returncode": proc.returncode,
    }


# ---------------------------------------------------------------------------
# CodingToolkit
# ---------------------------------------------------------------------------


class CodingToolkit(LionTool):
    """Coding tools bound to a Branch with shared file state and hooks.

    Usage::

        toolkit = CodingToolkit()

        # Register hooks before binding
        async def guard_destructive(tool_name, action, args):
            cmd = args.get("command", "")
            if "rm -rf" in cmd:
                raise PermissionError(f"Blocked: {cmd}")

        async def auto_format(tool_name, action, args, result):
            if result.get("success") and args.get("file_path", "").endswith(".py"):
                # run formatter...
                pass
            return result

        toolkit.pre("bash", guard_destructive)
        toolkit.post("editor", auto_format)

        tools = toolkit.bind(branch)
        branch.register_tools(tools)

    Hook signatures:
        pre:  async def handler(tool_name: str, action: str, args: dict) -> dict | None
              - Return modified args dict to override, or None to pass through.
              - Raise to abort the tool call (exception propagates as error result).
        post: async def handler(tool_name: str, action: str, args: dict, result: dict) -> dict | None
              - Return modified result dict to override, or None to pass through.
        on_error: async def handler(tool_name: str, action: str, args: dict, error: Exception) -> dict | None
              - Return a result dict to suppress the error, or None to propagate.
    """

    is_lion_system_tool = True
    system_tool_name = "coding_toolkit"

    def security_pre(self, tool_name: str, handler: Callable) -> CodingToolkit:
        """Finding 13: register a security hook that runs before user pre-hooks."""
        self._security_pre_hooks.setdefault(tool_name, []).append(handler)
        return self

    def pre(self, tool_name: str, handler: Callable) -> CodingToolkit:
        self._pre_hooks.setdefault(tool_name, []).append(handler)
        return self

    def post(self, tool_name: str, handler: Callable) -> CodingToolkit:
        self._post_hooks.setdefault(tool_name, []).append(handler)
        return self

    def on_error(self, tool_name: str, handler: Callable) -> CodingToolkit:
        self._error_hooks.setdefault(tool_name, []).append(handler)
        return self

    def _build_preprocessor(self, tool_name: str) -> Callable | None:
        """Build a chained preprocessor from registered hooks for this tool.

        Finding 13: security_pre hooks run before user pre hooks.
        """
        security_hooks = [
            # Finding 13: security hooks first, then user hooks
            *self._security_pre_hooks.get("*", []),
            *self._security_pre_hooks.get(tool_name, []),
        ]
        user_hooks = [
            *self._pre_hooks.get(tool_name, []),
            *self._pre_hooks.get("*", []),
        ]
        hooks = [*security_hooks, *user_hooks]
        if user_hooks:
            # User pre-hooks may rewrite args; validate the final args too.
            hooks.extend(security_hooks)
        if not hooks:
            return None

        async def chained_pre(args: dict, **_kw) -> dict:
            for handler in hooks:
                result = await handler(tool_name, args.get("action", ""), args)
                if isinstance(result, dict):
                    args = result
            return args

        return chained_pre

    def _build_postprocessor(self, tool_name: str) -> Callable | None:
        """Build a chained postprocessor from registered post-hooks for this tool."""
        hooks = [
            *self._post_hooks.get(tool_name, []),
            *self._post_hooks.get("*", []),
        ]
        if not hooks:
            return None

        async def chained_post(result: Any, **_kw) -> Any:
            if not isinstance(result, dict):
                return result
            for handler in hooks:
                modified = await handler(tool_name, "", {}, result)
                if isinstance(modified, dict):
                    result = modified
            return result

        return chained_post

    def __init__(
        self,
        notify: bool = True,
        notify_threshold: float = 0.7,
        notify_max_tokens: int = 200_000,
        workspace_root: str | Path | None = None,
    ):
        self._security_pre_hooks: dict[str, list[Callable]] = {}  # Finding 13
        self._pre_hooks: dict[str, list[Callable]] = {}
        self._post_hooks: dict[str, list[Callable]] = {}
        self._error_hooks: dict[str, list[Callable]] = {}
        self.notify = notify
        self.notify_threshold = notify_threshold
        self.notify_max_tokens = notify_max_tokens
        # Finding 14: workspace root for path containment checks
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()

    def bind(self, branch: Branch) -> list[Tool]:
        from lionagi.protocols.messages import ActionResponse

        file_state: dict[str, float] = {}
        call_count = [0]
        msgs = branch.msgs
        notify = self.notify
        threshold = self.notify_threshold
        max_tokens = self.notify_max_tokens
        # Finding 14: capture workspace root for use in all sync file helpers
        workspace_root = self.workspace_root

        def _system_status() -> str | None:
            if not notify:
                return None
            call_count[0] += 1

            from lionagi.service.token_budget import get_token_budget

            budget = get_token_budget(branch)
            n_active = len(branch.progression)
            n_total = len(msgs.progression)
            n_files = len(file_state)

            n_action_results = 0
            pile = msgs.messages
            for uid in branch.progression:
                if uid in pile and isinstance(pile[uid], ActionResponse):
                    n_action_results += 1

            parts = [
                f"context {budget.used // 1000}k/{budget.limit // 1000}k tokens ({budget.usage_pct:.0%})"
            ]
            parts.append(f"{n_active} messages")
            if n_action_results > 0:
                parts.append(f"{n_action_results} action results")
            if n_files > 0:
                parts.append(f"{n_files} files tracked")
            if n_total > n_active:
                parts.append(f"{n_total - n_active} evicted")

            status = f"[System: {', '.join(parts)}]"

            if budget.is_critical:
                status += " ⚠️ Context nearly full — evict old action results now."
            elif budget.is_warning:
                status += " Consider evicting earlier action results to free space."

            return status

        def _check_read_guard(path: str) -> str | None:
            try:
                resolved_path = _resolve_workspace_path(path, workspace_root)
            except PermissionError as e:
                return str(e)
            resolved = str(resolved_path.resolve())
            if resolved not in file_state:
                return f"Must read file before editing: {path}"
            try:
                current_mtime = resolved_path.stat().st_mtime
            except OSError:
                return None
            if current_mtime != file_state[resolved]:
                return f"File changed since last read: {path}. Read it again."
            return None

        def _track(result: dict):
            resolved = result.pop("_resolved", None)
            mtime = result.pop("_mtime", None)
            if resolved and mtime is not None:
                file_state[resolved] = mtime

        # -- Reader ----------------------------------------------------------

        async def reader(
            action: str,
            path: str,
            offset: int = None,
            limit: int = None,
            recursive: bool = None,
            file_types: list[str] = None,
        ) -> dict:
            """Read files or list directory contents.

            Use action='read' to get file contents with line numbers.
            Use action='list_dir' to list files. Always read a file before editing it.
            """
            if action == "read":
                start = max(0, offset or 0)
                max_lines = limit if (limit and limit > 0) else 2000
                # Finding 14: pass workspace_root to enforce path containment
                result = await run_sync(
                    _read_file_sync, path, start, max_lines, workspace_root
                )
                _track(result)
                return result
            elif action == "list_dir":
                return await run_sync(
                    _list_dir_sync, path, bool(recursive), file_types, workspace_root
                )
            return {"success": False, "error": f"Unknown action: {action}"}

        # -- Editor ----------------------------------------------------------

        async def editor(
            action: str,
            file_path: str,
            content: str = None,
            old_string: str = None,
            new_string: str = None,
            replace_all: bool = False,
        ) -> dict:
            """Write or edit files. You must read a file before editing it.

            Use action='write' to create or overwrite. Use action='edit' for
            exact string replacement — safer than full rewrites.
            """
            if action == "write":
                if content is None:
                    return {"success": False, "error": "'content' required for write"}
                try:
                    target_path = _resolve_workspace_path(file_path, workspace_root)
                except PermissionError as e:
                    return {"success": False, "error": str(e)}
                if target_path.exists():
                    guard = _check_read_guard(file_path)
                    if guard:
                        return {"success": False, "error": guard}
                # Finding 14: pass workspace_root to enforce path containment
                result = await run_sync(
                    _write_file_sync, file_path, content, workspace_root
                )
                _track(result)
                return result
            elif action == "edit":
                if old_string is None:
                    return {"success": False, "error": "'old_string' required for edit"}
                if new_string is None:
                    return {"success": False, "error": "'new_string' required for edit"}
                guard = _check_read_guard(file_path)
                if guard:
                    return {"success": False, "error": guard}
                # Finding 14: pass workspace_root to enforce path containment
                result = await run_sync(
                    _edit_file_sync,
                    file_path,
                    old_string,
                    new_string,
                    replace_all,
                    workspace_root,
                )
                _track(result)
                return result
            return {"success": False, "error": f"Unknown action: {action}"}

        # -- Bash ------------------------------------------------------------

        async def bash(
            command: str,
            timeout: int = None,
            cwd: str = None,
        ) -> dict:
            """Execute a shell command and return stdout, stderr, and return code.

            Use for running builds, tests, git commands, and any system operations.
            Output is truncated if it exceeds 100 KB per stream.
            """
            timeout_ms = max(1, min(timeout or 30000, 300000))
            timeout_s = timeout_ms / 1000.0

            # GAP A: reject shell control operators (same filter as standalone BashTool)
            if _SHELL_CONTROL.search(command):
                return {
                    "stdout": "",
                    "stderr": f"Shell control operators rejected: {command!r}",
                    "return_code": -1,
                    "timed_out": False,
                }
            try:
                cmd = shlex.split(command)
            except ValueError as exc:
                return {
                    "stdout": "",
                    "stderr": f"Malformed command: {exc}",
                    "return_code": -1,
                    "timed_out": False,
                }

            result = await run_sync(_subprocess_sync, cmd, False, timeout_s, cwd)

            result.setdefault("timed_out", False)
            result["return_code"] = result.pop("returncode", -1)
            return result

        # -- Search ----------------------------------------------------------

        async def search(
            action: str,
            pattern: str,
            path: str = None,
            include: str = None,
            max_results: int = None,
        ) -> dict:
            """Search file contents (grep) or find files by name.

            Use action='grep' to search with regex. Use action='find' for file names.
            Results are capped at max_results to prevent context overflow.
            """
            if action == "grep":
                try:
                    search_path = str(
                        _resolve_workspace_path(path or ".", workspace_root)
                    )
                except PermissionError as e:
                    return {"success": False, "error": str(e)}
                limit = max_results or 50
                cmd = ["grep", "-rn", "-E", pattern, search_path]
                if include:
                    cmd.insert(3, f"--include={include}")
                raw = await run_sync(_subprocess_sync, cmd, False, 30.0, None)
                if raw.get("returncode") == 2:
                    return {"success": False, "error": raw["stderr"].strip()}
                lines = (
                    raw["stdout"].strip().split("\n") if raw["stdout"].strip() else []
                )
                total = len(lines)
                return {
                    "success": True,
                    "content": "\n".join(lines[:limit]),
                    "total_matches": total,
                    "shown": min(total, limit),
                }
            elif action == "find":
                try:
                    search_path = str(
                        _resolve_workspace_path(path or ".", workspace_root)
                    )
                except PermissionError as e:
                    return {"success": False, "error": str(e)}
                limit = max_results or 100
                cmd = ["find", search_path, "-name", pattern]
                raw = await run_sync(_subprocess_sync, cmd, False, 30.0, None)
                if raw.get("returncode", 0) != 0 and raw.get("stderr", "").strip():
                    return {"success": False, "error": raw["stderr"].strip()}
                lines = (
                    raw["stdout"].strip().split("\n") if raw["stdout"].strip() else []
                )
                total = len(lines)
                return {
                    "success": True,
                    "content": "\n".join(lines[:limit]),
                    "total_found": total,
                    "shown": min(total, limit),
                }
            return {"success": False, "error": f"Unknown action: {action}"}

        # -- Context ---------------------------------------------------------

        def _ensure_current_progression():
            """Lazily copy full progression into metadata on first evict."""
            if "current_progression" not in branch.metadata:
                from lionagi.protocols.generic.progression import Progression

                cp = Progression()
                for uid in msgs.progression:
                    cp.append(uid)
                branch.metadata["current_progression"] = cp
            return branch.metadata["current_progression"]

        async def context(
            action: str,
            start: int = None,
            end: int = None,
            keep_last: int = None,
        ) -> dict:
            """Manage your conversation context — check usage, list messages, evict old ones.

            Use this to stay within context limits during long tasks. Evict verbose
            tool outputs you no longer need to free space for new work.
            Evicted messages are hidden from the LLM but preserved in conversation record.
            """
            progression = branch.progression
            pile = msgs.messages

            if action == "status":
                full_len = len(msgs.progression)
                active_len = len(progression)
                by_type: dict[str, int] = {}
                total_tokens = 0
                for uid in progression:
                    if uid in pile:
                        msg = pile[uid]
                        role = msg.role if hasattr(msg, "role") else type(msg).__name__
                        by_type[role] = by_type.get(role, 0) + 1
                        c = msg.content if hasattr(msg, "content") else ""
                        if c:
                            total_tokens += TokenCalculator.tokenize(
                                str(c) if not isinstance(c, str) else c
                            )
                return {
                    "success": True,
                    "active_messages": active_len,
                    "total_messages": full_len,
                    "evicted": full_len - active_len,
                    "by_type": by_type,
                    "estimated_tokens": total_tokens,
                    "files_tracked": len(file_state),
                }

            elif action == "get_messages":
                s = max(0, start or 0)
                e = min(len(progression), end or len(progression))
                summaries = []
                for i in range(s, e):
                    uid = progression[i]
                    if uid in pile:
                        msg = pile[uid]
                        role = msg.role if hasattr(msg, "role") else type(msg).__name__
                        c = ""
                        if hasattr(msg, "content") and msg.content:
                            raw = (
                                str(msg.content)
                                if not isinstance(msg.content, str)
                                else msg.content
                            )
                            c = raw[:120].replace("\n", " ")
                            if len(raw) > 120:
                                c += "..."
                        summaries.append(f"[{i}] {role}: {c}")
                return {
                    "success": True,
                    "range": f"[{s}:{e}] of {len(progression)}",
                    "messages": summaries,
                }

            elif action == "evict":
                cp = _ensure_current_progression()
                s = max(1, start or 1)
                e = end if end is not None else s + 1
                e = min(len(cp), e)
                if s >= e:
                    return {"success": False, "error": f"Invalid range [{s}:{e})"}
                uids = [cp[i] for i in range(s, e) if i < len(cp)]
                cp.exclude(uids)
                return {
                    "success": True,
                    "removed": len(uids),
                    "active": len(cp),
                    "total": len(msgs.progression),
                }

            elif action == "evict_action_results":
                cp = _ensure_current_progression()
                keep = keep_last if keep_last is not None else 5
                ar_uids = [
                    uid
                    for uid in cp
                    if uid in pile and isinstance(pile[uid], ActionResponse)
                ]
                if len(ar_uids) <= keep:
                    return {
                        "success": True,
                        "removed": 0,
                        "message": f"Only {len(ar_uids)} action results, keeping all.",
                    }
                to_evict = ar_uids[:-keep] if keep > 0 else ar_uids
                cp.exclude(to_evict)
                return {
                    "success": True,
                    "removed": len(to_evict),
                    "active": len(cp),
                    "total": len(msgs.progression),
                }

            return {"success": False, "error": f"Unknown action: {action}"}

        # -- System notification as built-in post-hook -----------------------

        async def _notify_post(
            tool_name: str, action: str, args: dict, result: dict
        ) -> dict | None:
            status = _system_status()
            if status and isinstance(result, dict):
                result["system"] = status
            return result

        if notify:
            self.post("*", _notify_post)

        # -- Sandbox ---------------------------------------------------------

        _sandbox_session = [None]  # mutable ref for closure

        async def sandbox(
            action: str,
            message: str = None,
        ) -> dict:
            """Work in an isolated git worktree — safe experimentation with easy merge/discard.

            Workflow: create → make changes (edit/bash in sandbox dir) → diff → commit → merge or discard.
            The sandbox is a real git branch. Merge applies your changes; discard throws them away.
            """
            from .sandbox import (
                create_sandbox,
                sandbox_commit,
                sandbox_diff,
                sandbox_discard,
                sandbox_merge,
            )

            if action == "create":
                if _sandbox_session[0] is not None:
                    return {
                        "success": False,
                        "error": "Sandbox already active. Discard or merge first.",
                    }
                repo = str(workspace_root) if workspace_root else None
                if not repo:
                    return {
                        "success": False,
                        "error": "No workspace root — cannot create sandbox.",
                    }
                try:
                    session = await create_sandbox(repo)
                    _sandbox_session[0] = session
                    return {
                        "success": True,
                        "worktree": session.worktree_path,
                        "branch": session.branch_name,
                        "base": session.base_branch,
                        "message": f"Sandbox ready at {session.worktree_path}. Edit files there, then use diff/commit/merge.",
                    }
                except Exception as e:
                    return {"success": False, "error": str(e)}

            session = _sandbox_session[0]
            if session is None:
                return {
                    "success": False,
                    "error": "No active sandbox. Create one first.",
                }

            if action == "diff":
                return {"success": True, **(await sandbox_diff(session))}
            elif action == "commit":
                if not message:
                    return {"success": False, "error": "'message' required for commit."}
                return await sandbox_commit(session, message)
            elif action == "merge":
                result = await sandbox_merge(session)
                if result.get("success"):
                    _sandbox_session[0] = None
                return result
            elif action == "discard":
                result = await sandbox_discard(session)
                _sandbox_session[0] = None
                return {"success": True, **result}

            return {"success": False, "error": f"Unknown action: {action}"}

        # -- Subagent --------------------------------------------------------

        async def subagent(
            instruction: str,
            permissions: str = "read_only",
            max_turns: int = 20,
            cwd: str = None,
        ) -> dict:
            """Spawn a sub-agent to handle a task independently.

            The sub-agent gets its own Branch with coding tools and runs a
            ReAct loop. Use for delegating research, exploration, or scoped
            edits without polluting your own context. Results are returned
            as a summary — the sub-agent's full conversation stays separate.

            Permission levels control what the sub-agent can do:
            - read_only: search + read files only (safest for research)
            - safe: read + write + search, bash restricted (no rm/sudo)
            - allow_all: full access (use for trusted implementation tasks)
            """
            from lionagi.agent.config import AgentConfig
            from lionagi.agent.permissions import PermissionPolicy

            max_turns = min(max(1, max_turns), 50)
            sub_cwd = cwd or (str(workspace_root) if workspace_root else None)

            perm_map = {
                "read_only": PermissionPolicy.read_only(),
                "safe": PermissionPolicy.safe(),
                "allow_all": PermissionPolicy.allow_all(),
            }
            sub_permissions = perm_map.get(permissions, PermissionPolicy.read_only())

            try:
                model_spec = None
                try:
                    ep = branch.chat_model.endpoint
                    provider = getattr(ep.config, "provider", "")
                    model_name = ""
                    if hasattr(ep.config, "kwargs"):
                        model_name = ep.config.kwargs.get("model", "")
                    if provider and model_name:
                        model_spec = f"{provider}/{model_name}"
                except AttributeError:
                    pass

                sub_config = AgentConfig(
                    name="subagent",
                    model=model_spec,
                    tools=["coding"],
                    permissions=sub_permissions,
                    cwd=sub_cwd,
                    system_prompt=(
                        "You are a sub-agent. Complete the assigned task concisely. "
                        "Report your findings and any changes made. Be thorough but brief."
                    ),
                    lion_system=False,
                )

                from lionagi.agent.factory import create_agent as _create

                sub_branch = await _create(sub_config, load_settings=False)

                result = await sub_branch.ReAct(
                    instruction=instruction,
                    tools=True,
                    max_extensions=max_turns,
                )

                response = result if isinstance(result, str) else str(result)
                return {
                    "success": True,
                    "response": response[:5000],
                    "tools_used": len(sub_branch.msgs.messages) - 2,
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

        # -- Assemble (hooks wired via Tool's native pre/postprocessor) ------

        tool_defs = [
            ("reader", reader, ReaderRequest),
            ("editor", editor, EditorRequest),
            ("bash", bash, BashRequest),
            ("search", search, SearchRequest),
            ("context", context, ContextRequest),
            ("sandbox", sandbox, SandboxRequest),
            ("subagent", subagent, SubagentRequest),
        ]

        tools = []
        for name, func, request_cls in tool_defs:
            tools.append(
                Tool(
                    func_callable=func,
                    request_options=request_cls,
                    preprocessor=self._build_preprocessor(name),
                    postprocessor=self._build_postprocessor(name),
                )
            )
        return tools

    def to_tool(self) -> Tool:
        raise NotImplementedError(
            "CodingToolkit requires branch context. Use toolkit.bind(branch) instead."
        )
