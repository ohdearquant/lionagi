# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import logging
import re
import shlex
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from ._subprocess import _SHELL_CONTROL, _subprocess_sync
from .base import LionTool
from .code.ast_search import AstSearchRequest, _ast_search_sync
from .code.bash import BashRequest
from .code.check import CodeCheckRequest, _resolve_check_paths, _ruff_check_sync
from .code.nav import NavRequest, _find_definition_sync, _find_references_sync, _outline_sync
from .code.search import SearchAction as SearchAction  # re-export: preserves the public API
from .code.search import SearchRequest
from .context.context import ContextRequest, ContextTool
from .file.editor import EditorRequest, _write_text_no_follow
from .file.reader import ReaderRequest, _evict_expired, _open_sync, _read_cached
from .file.reader import _list_dir_sync as _file_list_dir_sync
from .file.reader import _read_sync as _file_read_sync

if TYPE_CHECKING:
    from lionagi.agent.nudge import NudgeEngine, NudgeRule
    from lionagi.session.branch import Branch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models (LLM-facing schemas)
# ---------------------------------------------------------------------------
# ReaderRequest and EditorRequest are imported from file/reader.py and
# file/editor.py. BashRequest, SearchAction, and SearchRequest are imported
# from code/bash.py and code/search.py — those are the canonical definitions.
# SandboxRequest and SubagentRequest have no standalone equivalent and are
# defined here.


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


def _read_file_sync(path: str, offset: int, max_lines: int, workspace_root: Path) -> dict:
    try:
        p = _resolve_workspace_path(path, workspace_root)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    if p.suffix.lower() in _IMAGE_EXTENSIONS:
        return _read_image_sync(path, workspace_root)

    resp = _file_read_sync(path, offset, max_lines, workspace_root)
    if not resp.success:
        return {"success": False, "error": resp.error}

    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0

    return {
        "success": True,
        "content": resp.content,
        "_resolved": str(p.resolve()),
        "_mtime": mtime,
    }


def _list_dir_sync(
    path: str, recursive: bool, file_types: list[str] | None, workspace_root: Path
) -> dict:
    resp = _file_list_dir_sync(path, recursive, file_types, workspace_root)
    return (
        {"success": resp.success, "content": resp.content}
        if resp.success
        else {"success": False, "error": resp.error}
    )


def _write_file_sync(file_path: str, content: str, workspace_root: Path) -> dict:
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
        hint = ""
        stripped = re.sub(r"(?m)^\s*\d+\t", "", old_string)
        if stripped != old_string and stripped in original:
            hint = (
                " — it matches once you remove the line-number prefixes. Drop the "
                "leading `<number>\\t` from each line (keep only the code)."
            )
        elif old_string.strip() and old_string.strip() in original:
            hint = " — a match exists ignoring surrounding whitespace; check indentation."
        return {
            "success": False,
            "error": (
                f"old_string not found in {file_path}{hint}. "
                "Re-read the file and copy the exact text including all whitespace."
            ),
        }
    if count > 1 and not replace_all:
        return {
            "success": False,
            "error": (
                f"old_string appears {count} times. "
                "Either set replace_all=True or expand old_string with more surrounding context."
            ),
        }

    updated = original.replace(old_string, new_string, -1 if replace_all else 1)

    try:
        _write_text_no_follow(p, updated)
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


# ---------------------------------------------------------------------------
# CodingToolkit
# ---------------------------------------------------------------------------


ALL_CODING_TOOLS: tuple[str, ...] = (
    "reader",
    "editor",
    "bash",
    "search",
    "code_check",
    "code_nav",
    "ast_search",
    "context",
    "sandbox",
    "subagent",
)

DEFAULT_CODING_TOOLS: tuple[str, ...] = (
    "reader",
    "editor",
    "bash",
    "search",
    "code_check",
    "code_nav",
    "ast_search",
    "context",
)


class CodingToolkit(LionTool):
    """Coding tools (reader, editor, bash, search, etc.) bound to a Branch."""

    is_lion_system_tool = True
    system_tool_name = "coding_toolkit"

    def security_pre(self, tool_name: str, handler: Callable) -> CodingToolkit:
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
        from lionagi.agent.factory import _chain_pre_hooks

        security_hooks = [
            *self._security_pre_hooks.get("*", []),
            *self._security_pre_hooks.get(tool_name, []),
        ]
        user_hooks = [
            *self._pre_hooks.get(tool_name, []),
            *self._pre_hooks.get("*", []),
        ]
        return _chain_pre_hooks(tool_name, security_hooks, user_hooks)

    def _build_postprocessor(self, tool_name: str) -> Callable | None:
        from lionagi.agent.factory import _chain_post_hooks

        hooks = [
            *self._post_hooks.get(tool_name, []),
            *self._post_hooks.get("*", []),
        ]
        return _chain_post_hooks(tool_name, hooks)

    def __init__(
        self,
        notify: bool = True,
        notify_threshold: float = 0.7,
        notify_max_tokens: int = 200_000,
        workspace_root: str | Path | None = None,
        tools: Sequence[str] | None = None,
        nudge_engine: NudgeEngine | None = None,
        nudge_rules: Sequence[NudgeRule] | None = None,
    ):
        self._security_pre_hooks: dict[str, list[Callable]] = {}
        self._pre_hooks: dict[str, list[Callable]] = {}
        self._post_hooks: dict[str, list[Callable]] = {}
        self._error_hooks: dict[str, list[Callable]] = {}
        self.notify = notify
        self.notify_threshold = notify_threshold
        self.notify_max_tokens = notify_max_tokens
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()
        selected = tuple(tools) if tools is not None else DEFAULT_CODING_TOOLS
        unknown = [t for t in selected if t not in ALL_CODING_TOOLS]
        if unknown:
            raise ValueError(f"unknown coding tool(s): {unknown}. Valid: {list(ALL_CODING_TOOLS)}")
        self.enabled_tools = selected
        self.nudge_engine = nudge_engine
        self.nudge_rules = tuple(nudge_rules) if nudge_rules is not None else ()
        self._bound_nudge_engine: NudgeEngine | None = None

    def bind(self, branch: Branch) -> list[Tool]:
        from lionagi.protocols.messages import ActionResponse

        file_state: dict[str, float] = {}
        read_tracked: set[str] = set()
        msgs = branch.msgs
        notify = self.notify
        workspace_root = self.workspace_root

        engine: NudgeEngine | None = None
        if notify:
            from lionagi.agent.nudge import NudgeEngine, default_nudge_rules

            engine = self.nudge_engine
            if engine is None:
                rules = default_nudge_rules() + list(self.nudge_rules)
                engine = NudgeEngine(branch, rules=rules)
        self._bound_nudge_engine = engine

        def _invalidate_stale_reads() -> None:
            """Drop file_state entries whose backing reader-read result was evicted/compacted.

            Prevents a silent unsoundness: without this, evicting a verbose read
            result still leaves the read-before-edit guard satisfied, letting the
            model edit a file it no longer has in view.
            """
            if not read_tracked:
                return
            active = branch.progression
            pile = msgs.messages
            active_paths: set[str] = set()
            for uid in active:
                if uid not in pile:
                    continue
                m = pile[uid]
                if not isinstance(m, ActionResponse):
                    continue
                c = m.content
                if getattr(c, "function", None) != "reader":
                    continue
                args = getattr(c, "arguments", None) or {}
                if args.get("action") != "read":
                    continue
                p = args.get("path")
                if not p:
                    continue
                try:
                    active_paths.add(str(_resolve_workspace_path(p, workspace_root).resolve()))
                except PermissionError:
                    continue
            for p in [p for p in read_tracked if p not in active_paths]:
                read_tracked.discard(p)
                file_state.pop(p, None)

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

        def _track(result: dict, *, is_read: bool = False):
            resolved = result.pop("_resolved", None)
            mtime = result.pop("_mtime", None)
            if resolved and mtime is not None:
                file_state[resolved] = mtime
                if is_read:
                    read_tracked.add(resolved)
                else:
                    read_tracked.discard(resolved)

        # Cache for documents opened via action='open' (docling conversion).
        # Keyed by path; values are (text, cached_at) tuples — same layout as
        # the standalone ReaderTool cache so _read_cached/_evict_expired work
        # without modification.
        _open_cache: dict[str, tuple[str, float]] = {}

        async def reader(
            action: str,
            path: str,
            offset: int = None,
            limit: int = None,
            recursive: bool = None,
            file_types: list[str] = None,
        ) -> dict:
            """Read files, convert documents, or list directory contents.

            Use action='read' to get file contents with line numbers.
            Use action='open' to convert a document (PDF, PPTX, DOCX, HTML) to
            text via docling — the result is cached by path so you can then use
            action='read' with offset/limit on the same path to paginate it.
            Use action='list_dir' to list files. Always read a file before editing it.
            """
            # Canonical empty-path contract: every reader action requires path,
            # matching ReaderTool.handle_request's pre-dispatch guard.
            if not path:
                return {
                    "success": False,
                    "content": None,
                    "error": "'path' is required. Provide a file path for 'read'/'open' or a directory path for 'list_dir'.",
                }
            if action == "open":
                _evict_expired(_open_cache)
                resp = await run_sync(_open_sync, path, _open_cache, workspace_root, frozenset())
                return {"success": resp.success, "content": resp.content, "error": resp.error}
            if action == "read":
                start = max(0, offset or 0)
                max_lines = limit if (limit and limit > 0) else 2000
                # Serve from docling cache if the path was previously opened.
                cached = _read_cached(path, start, max_lines, _open_cache)
                if cached is not None:
                    return {
                        "success": cached.success,
                        "content": cached.content,
                        "error": cached.error,
                    }
                result = await run_sync(_read_file_sync, path, start, max_lines, workspace_root)
                _track(result, is_read=True)
                return result
            elif action == "list_dir":
                return await run_sync(
                    _list_dir_sync, path, bool(recursive), file_types, workspace_root
                )
            return {"success": False, "error": f"Unknown action: {action}"}

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
            exact string replacement — safer than full rewrites. When building
            old_string from reader output, strip the `<number>\\t` line-number
            prefix and keep only the code, with its exact indentation.
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
                result = await run_sync(_write_file_sync, file_path, content, workspace_root)
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

        async def bash(
            command: str,
            timeout: int = None,
            cwd: str = None,
        ) -> dict:
            """Execute a single shell command and return stdout, stderr, and return code.

            Use for running builds, tests, git commands, and any system operations.
            One command per call — shell operators (&&, ||, |, ;, redirects, backticks,
            $(...)) are rejected; pass cwd= to run in a directory. Output is truncated
            if it exceeds 100 KB per stream.
            """
            timeout_ms = max(1, min(timeout or 30000, 300000))
            timeout_s = timeout_ms / 1000.0

            if _SHELL_CONTROL.search(command):
                return {
                    "stdout": "",
                    "stderr": (
                        f"Shell control operators are not supported: {command!r}. "
                        "Run one command per call; use cwd= instead of `cd x && cmd`."
                    ),
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
                    search_path = str(_resolve_workspace_path(path or ".", workspace_root))
                except PermissionError as e:
                    return {"success": False, "error": str(e)}
                limit = max_results or 50
                cmd = ["grep", "-rn", "-E", pattern, search_path]
                if include:
                    cmd.insert(3, f"--include={include}")
                raw = await run_sync(_subprocess_sync, cmd, False, 30.0, None)
                if raw.get("returncode") == 2:
                    return {"success": False, "error": raw["stderr"].strip()}
                lines = raw["stdout"].strip().split("\n") if raw["stdout"].strip() else []
                total = len(lines)
                return {
                    "success": True,
                    "content": "\n".join(lines[:limit]),
                    "total_matches": total,
                    "shown": min(total, limit),
                }
            elif action == "find":
                try:
                    search_path = str(_resolve_workspace_path(path or ".", workspace_root))
                except PermissionError as e:
                    return {"success": False, "error": str(e)}
                limit = max_results or 100
                cmd = ["find", search_path, "-name", pattern]
                raw = await run_sync(_subprocess_sync, cmd, False, 30.0, None)
                if raw.get("returncode", 0) != 0 and raw.get("stderr", "").strip():
                    return {"success": False, "error": raw["stderr"].strip()}
                lines = raw["stdout"].strip().split("\n") if raw["stdout"].strip() else []
                total = len(lines)
                return {
                    "success": True,
                    "content": "\n".join(lines[:limit]),
                    "total_found": total,
                    "shown": min(total, limit),
                }
            return {"success": False, "error": f"Unknown action: {action}"}

        _ctx_tool = ContextTool()
        _ctx_func = _ctx_tool.bind(branch).func_callable

        async def context(
            action: str,
            start: int = None,
            end: int = None,
            keep_last: int = None,
            summary: str = None,
            mode: str = None,
            scope: str = None,
            auto: bool = False,
        ) -> dict:
            """Manage your conversation context — check usage, list messages, evict old ones.

            Use this to stay within context limits during long tasks. Evict verbose
            tool outputs you no longer need to free space for new work.
            Evicted messages are hidden from the LLM but preserved in conversation record.
            """
            result = await _ctx_func(
                action=action,
                start=start,
                end=end,
                keep_last=keep_last,
                summary=summary,
                mode=mode,
                scope=scope,
                auto=auto,
            )
            if (
                action in ("evict", "evict_action_results", "compact")
                and isinstance(result, dict)
                and result.get("success")
            ):
                _invalidate_stale_reads()
            if action == "status" and isinstance(result, dict) and result.get("success"):
                result["files_tracked"] = len(file_state)
            return result

        async def _notify_post(
            tool_name: str, action: str, args: dict, result: dict
        ) -> dict | None:
            if engine is None:
                return result
            try:
                ok = (
                    result.get("success", result.get("return_code") == 0)
                    if isinstance(result, dict)
                    else True
                )
                engine.record_call(tool_name, action, ok)
                status = engine.evaluate(files_tracked=len(file_state))
            except Exception:
                logger.warning(
                    "NudgeEngine evaluation failed; returning tool result unmodified",
                    exc_info=True,
                )
                return result
            if status and isinstance(result, dict):
                result["system"] = status
            return result

        if notify:
            self.post("*", _notify_post)

        _sandbox_session = [None]

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
            from lionagi.agent.spec import AgentSpec

            max_turns = min(max(1, max_turns), 50)
            sub_cwd = cwd or (str(workspace_root) if workspace_root else None)

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

                sub_spec = AgentSpec.compose(
                    "implementer",
                    model=model_spec,
                    tools=["coding"],
                    permissions=permissions,
                    system_prompt=(
                        "You are a sub-agent. Complete the assigned task concisely. "
                        "Report your findings and any changes made. Be thorough but brief."
                    ),
                    cwd=sub_cwd,
                    yolo=False,
                )
                sub_spec.lion_system = False

                from lionagi.agent.factory import create_agent as _create

                sub_branch = await _create(sub_spec, load_settings=False)

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

        async def code_check(
            paths: list,
            tool: str = "ruff",
            max_diagnostics: int = 50,
        ) -> dict:
            """Run static analysis on Python files and return structured diagnostics.

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
            """
            resolved_paths, err = _resolve_check_paths(paths, workspace_root)
            if err is not None:
                return err.model_dump()
            resp = await run_sync(_ruff_check_sync, resolved_paths, max_diagnostics)
            return resp.model_dump()

        async def code_nav(action: str, path: str, symbol: str | None = None) -> dict:
            """Navigate Python source without reading the full file.

            Actions:
            - 'outline': list all class/function signatures in a file (cheap context).
            - 'find_definition': locate where a named symbol is defined.
            - 'find_references': find all uses of a symbol in the file.
            """
            try:
                resolved = str(_resolve_workspace_path(path, workspace_root))
            except PermissionError as exc:
                return {"success": False, "items": [], "error": str(exc)}
            if action == "outline":
                resp = await run_sync(_outline_sync, resolved)
            elif action == "find_definition":
                if not symbol:
                    return {
                        "success": False,
                        "items": [],
                        "error": "'symbol' required for find_definition.",
                    }
                resp = await run_sync(_find_definition_sync, resolved, symbol)
            elif action == "find_references":
                if not symbol:
                    return {
                        "success": False,
                        "items": [],
                        "error": "'symbol' required for find_references.",
                    }
                resp = await run_sync(_find_references_sync, resolved, symbol)
            else:
                return {"success": False, "items": [], "error": f"Unknown action: {action!r}."}
            return resp.model_dump()

        async def ast_search(
            pattern: str,
            path: str = ".",
            lang: str = "python",
            max_results: int = 50,
        ) -> dict:
            """Search source code by AST shape using ast-grep (sg).

            Finds structural patterns rather than text — catches syntax constructs
            regardless of whitespace. Returns status='unavailable' if the sg binary
            is absent (not an error).
            """
            try:
                resolved = str(_resolve_workspace_path(path, workspace_root))
            except PermissionError as exc:
                from .code.ast_search import AstSearchResponse

                return AstSearchResponse(status="error", summary=str(exc)).model_dump()
            resp = await run_sync(
                _ast_search_sync, pattern, resolved, lang, max_results, str(workspace_root)
            )
            return resp.model_dump()

        tool_defs = [
            ("reader", reader, ReaderRequest),
            ("editor", editor, EditorRequest),
            ("bash", bash, BashRequest),
            ("search", search, SearchRequest),
            ("code_check", code_check, CodeCheckRequest),
            ("code_nav", code_nav, NavRequest),
            ("ast_search", ast_search, AstSearchRequest),
            ("context", context, ContextRequest),
            ("sandbox", sandbox, SandboxRequest),
            ("subagent", subagent, SubagentRequest),
        ]

        tools = []
        for name, func, request_cls in tool_defs:
            if name not in self.enabled_tools:
                continue
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
