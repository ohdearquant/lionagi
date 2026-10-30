# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Built-in pre/post hooks for coding agent security and observability."""

from __future__ import annotations

import logging
import re
import warnings
from collections.abc import Mapping
from functools import wraps
from pathlib import Path
from types import FunctionType
from weakref import WeakKeyDictionary

from lionagi.libs.path_safety import is_protected_name, resolve_workspace_path

__all__ = (
    "auto_format_python",
    "guard_destructive",
    "guard_email_in_headers",
    "guard_paths",
    "log_tool_call",
    "log_tool_use",
)

logger = logging.getLogger(__name__)

_DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bgit\s+push\s+--force\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fd\b",
    r"\bdrop\s+table\b",
    r"\bdrop\s+database\b",
    r"\btruncate\s+table\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd[a-z]",
]

_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE_PATTERNS), re.IGNORECASE)


async def guard_destructive(tool_name: str, action: str, args: dict) -> dict | None:
    """Pre-hook: block destructive bash commands."""
    cmd = args.get("command", "")
    if _DESTRUCTIVE_RE.search(cmd):
        raise PermissionError(
            f"Blocked destructive command: {cmd}\n"
            "If you need this, explain why and ask the user to run it manually."
        )
    return None


async def guard_email_in_headers(
    tool_name: str,
    action: str,
    args: dict,
    *,
    allow: frozenset[str] = frozenset(),
    note: str | None = None,
    _edit_preparer=None,
    _workspace_root: Path | None = None,
) -> dict | None:
    """Refuse an email in an outbound header in a command or file write."""
    from ._email_in_headers import find_leak_with_allowed

    if tool_name in ("editor", "editor_tool") and action == "edit" and _edit_preparer is None:
        raise PermissionError("Cannot check the resulting file for an unbound editor.")
    if tool_name in ("bash", "bash_tool"):
        text = args.get("command", "")
    elif tool_name in ("editor", "editor_tool") and action == "edit" and _edit_preparer:
        from pydantic import ValidationError

        from lionagi.ln.concurrency import run_sync
        from lionagi.tools.file.editor import EditorRequest

        try:
            request = EditorRequest.model_validate(args)
        except ValidationError:
            return None
        if request.old_string is None or request.new_string is None:
            return None
        try:
            prepared = await run_sync(
                _edit_preparer,
                request.file_path,
                request.old_string,
                request.new_string,
                request.replace_all,
                _workspace_root,
            )
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(prepared, tuple):
            return None
        text = prepared[1]
    elif tool_name in ("editor", "editor_tool") and action == "write":
        text = args.get("content", "")
    else:
        return None
    if not isinstance(text, str):
        return None
    hit = find_leak_with_allowed(text, allow)
    if hit is not None:
        address, token = hit
        masked = address[:3] + "…@" + address.split("@", 1)[1]
        message = f"Blocked an email address ({masked}) in an outbound HTTP header after {token!r}."
        if action == "edit" and _edit_preparer:
            message += " The resulting file contains the header."
        if note:
            message += f" {note}"
        raise PermissionError(message)
    return None


_email_header_guards = WeakKeyDictionary({guard_email_in_headers: (frozenset(), None)})


def _email_header_guard_from_settings(settings: Mapping):
    """Bind the global email-header policy to one agent's guard."""
    from ._email_in_headers import EMAIL_RE

    guards = settings.get("guards", {})
    if not isinstance(guards, Mapping):
        raise ValueError("guards must be a mapping")
    policy = guards.get("email_in_headers", {})
    if not isinstance(policy, Mapping):
        raise ValueError("guards.email_in_headers must be a mapping")
    addresses = policy.get("allow", [])
    if not isinstance(addresses, list) or any(
        not isinstance(address, str) or EMAIL_RE.fullmatch(address) is None for address in addresses
    ):
        raise ValueError("guards.email_in_headers.allow must be a list of email addresses")
    note = policy.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("guards.email_in_headers.note must be text")
    allowed = frozenset(address.lower() for address in addresses)
    if not allowed and not note:
        return guard_email_in_headers

    @wraps(guard_email_in_headers)
    async def configured_guard(tool_name: str, action: str, args: dict) -> dict | None:
        return await guard_email_in_headers(tool_name, action, args, allow=allowed, note=note)

    _email_header_guards[configured_guard] = (allowed, note)
    return configured_guard


def _bind_email_header_editor_guard(handler, preparer, workspace_root: Path):
    """Bind an installed guard to the editor instance, leaving other handlers alone."""
    # Only functions made by this module are registered. Function equality is identity;
    # arbitrary callable equality and attributes do not select a security control.
    if not isinstance(handler, FunctionType) or handler not in _email_header_guards:
        return handler
    allowed, note = _email_header_guards[handler]

    @wraps(handler)
    async def bound(tool_name: str, action: str, args: dict) -> dict | None:
        return await guard_email_in_headers(
            tool_name,
            action,
            args,
            allow=allowed,
            note=note,
            _edit_preparer=preparer,
            _workspace_root=workspace_root,
        )

    return bound


def _is_hard_floor_error(exc: PermissionError) -> bool:
    """True for the symlink/protected-name floor, false for plain containment misses."""
    msg = str(exc)
    return "symlink" in msg or "protected path" in msg


def _resolve_against_any_root(raw_path: str, candidate: Path, allowed_roots: list[Path]) -> Path:
    """Accept iff resolve_workspace_path succeeds for >=1 root.

    A relative path is accepted if it resolves under *any* configured root,
    not only the first; a symlink/protected-name denial always surfaces
    over a generic one. See docs/internals/runtime.md.
    """
    hard_floor_error: PermissionError | None = None
    for root in allowed_roots:
        try:
            return resolve_workspace_path(candidate, root)
        except PermissionError as exc:
            if hard_floor_error is None and _is_hard_floor_error(exc):
                hard_floor_error = exc
    if hard_floor_error is not None:
        raise hard_floor_error
    raise PermissionError(f"Path not in allowed list: {raw_path}")


def _deny_only_floor(raw_path: str, expanded: Path) -> Path:
    """No allowed roots: keep deny-only mode, but still refuse symlinks/protected names."""
    if expanded.is_symlink():
        raise PermissionError(f"Refusing to access symlink: {raw_path!r}")
    resolved = expanded.resolve(strict=False)
    if is_protected_name(resolved.name):
        raise PermissionError(f"Refusing to access protected path: {resolved.name!r}")
    return resolved


def guard_paths(
    allowed_paths: list[str] | None = None,
    denied_paths: list[str] | None = None,
):
    """Factory: return a pre-hook that enforces allowed/denied path constraints.

    Check-time only: a TOCTOU race (swap file for symlink after the check
    passes) is out of scope. See docs/internals/runtime.md.
    """

    allowed_roots = [Path(p).expanduser().resolve(strict=False) for p in (allowed_paths or [])]

    async def _guard(tool_name: str, action: str, args: dict) -> dict | None:
        raw_path = args.get("path") or args.get("file_path") or ""
        if not raw_path:
            return None

        expanded = Path(raw_path).expanduser()

        if allowed_roots:
            # Relative paths resolve against the first allowed root, not cwd;
            # the result is accepted if contained by *any* configured root.
            candidate = expanded if expanded.is_absolute() else allowed_roots[0] / expanded
            resolved = _resolve_against_any_root(raw_path, candidate, allowed_roots)
        else:
            resolved = _deny_only_floor(raw_path, expanded)

        if denied_paths:
            for denied in denied_paths:
                denied_path = Path(denied).expanduser()
                if denied_path.is_absolute():
                    denied_resolved = denied_path.resolve(strict=False)
                    # Match exact path or any path under the denied directory.
                    if resolved == denied_resolved or denied_resolved in resolved.parents:
                        raise PermissionError(f"Path matches deny rule: {raw_path}")
                else:
                    # Glob patterns fnmatch each path component; plain-text
                    # patterns fall back to a substring check (".env" blocks ".env.local").
                    import fnmatch

                    _glob_chars = frozenset("*?[")
                    if any(c in denied for c in _glob_chars):
                        parts = resolved.parts
                        if any(fnmatch.fnmatch(part, denied) for part in parts):
                            raise PermissionError(f"Path matches deny rule: {raw_path}")
                    elif denied in raw_path or denied in resolved.name:
                        raise PermissionError(f"Path matches deny rule: {raw_path}")
        return None

    return _guard


async def log_tool_call(tool_name: str, action: str, args: dict, result: dict) -> dict | None:
    """Post-hook: log tool call for observability."""
    success = result.get("success", result.get("return_code") == 0)
    logger.info("tool=%s action=%s success=%s", tool_name, action, success)
    return None


async def log_tool_use(tool_name: str, action: str, args: dict, result: dict) -> dict | None:
    """Deprecated: use log_tool_call instead."""
    warnings.warn(
        "log_tool_use is deprecated and will be removed in a future minor release. "
        "Use log_tool_call instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return await log_tool_call(tool_name, action, args, result)


async def auto_format_python(
    tool_name: str,
    action: str,
    args: dict,
    result: dict,
    *,
    _command_env: Mapping[str, str] | None = None,
) -> dict | None:
    """Post-hook: run ruff format on edited Python files."""
    if not result.get("success"):
        return None

    file_path = args.get("file_path", "")
    if not file_path.endswith(".py"):
        return None

    from lionagi._command_env_metadata import command_environment
    from lionagi.ln.concurrency import run_sync
    from lionagi.tools.coding import _subprocess_sync

    environment = command_environment() if _command_env is None else dict(_command_env)
    await run_sync(
        _subprocess_sync, ["ruff", "format", file_path], False, 10.0, None, env=environment
    )
    return None
