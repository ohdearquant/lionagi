# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Built-in pre/post hooks for coding agent security and observability."""

from __future__ import annotations

import logging
import re
import warnings
from pathlib import Path

from lionagi.libs.path_safety import DENIED_NAMES, resolve_workspace_path

__all__ = (
    "auto_format_python",
    "guard_destructive",
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


def _is_hard_floor_error(exc: PermissionError) -> bool:
    """True for the symlink/protected-name floor, false for plain containment misses."""
    msg = str(exc)
    return "symlink" in msg or "protected path" in msg


def _resolve_against_any_root(raw_path: str, expanded: Path, allowed_roots: list[Path]) -> Path:
    """Absolute-path case: accept iff resolve_workspace_path succeeds for >=1 root.

    A symlink or protected basename fails the same way against every root (the
    check runs before containment), so it can never be masked by trying another
    root — surface that reason instead of a generic denial when it occurs.
    """
    hard_floor_error: PermissionError | None = None
    for root in allowed_roots:
        try:
            return resolve_workspace_path(expanded, root)
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
    if resolved.name in DENIED_NAMES:
        raise PermissionError(f"Refusing to access protected path: {resolved.name!r}")
    return resolved


def guard_paths(
    allowed_paths: list[str] | None = None,
    denied_paths: list[str] | None = None,
):
    """Factory: return a pre-hook that enforces allowed/denied path constraints."""

    allowed_roots = [Path(p).expanduser().resolve(strict=False) for p in (allowed_paths or [])]

    async def _guard(tool_name: str, action: str, args: dict) -> dict | None:
        raw_path = args.get("path") or args.get("file_path") or ""
        if not raw_path:
            return None

        expanded = Path(raw_path).expanduser()

        if allowed_roots:
            if expanded.is_absolute():
                resolved = _resolve_against_any_root(raw_path, expanded, allowed_roots)
            else:
                # Documented workspace-relative rule: relative paths resolve
                # against the first allowed root, not the process cwd.
                resolved = resolve_workspace_path(expanded, allowed_roots[0])
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
                    # Relative deny pattern: glob patterns (*, ?, [) fnmatch each
                    # resolved path component; plain-text patterns fall back to a
                    # substring check so ".env" still blocks ".env.local".
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


async def auto_format_python(tool_name: str, action: str, args: dict, result: dict) -> dict | None:
    """Post-hook: run ruff format on edited Python files."""
    if not result.get("success"):
        return None

    file_path = args.get("file_path", "")
    if not file_path.endswith(".py"):
        return None

    from lionagi.ln.concurrency import run_sync
    from lionagi.tools.coding import _subprocess_sync

    await run_sync(_subprocess_sync, ["ruff", "format", file_path], False, 10.0, None)
    return None
