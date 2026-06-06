# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Built-in hook implementations for coding agents.

Usage::

    from lionagi.agent.hooks import guard_destructive, log_tool_use

    config = AgentConfig.coding()
    config.pre("bash", guard_destructive)
    config.post("*", log_tool_use)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

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


def guard_paths(
    allowed_paths: list[str] | None = None,
    denied_paths: list[str] | None = None,
):
    """Factory: create a pre-hook that restricts file access by path.

    Usage::

        config.pre("reader", guard_paths(allowed_paths=["/Users/me/project/"]))
        config.pre("editor", guard_paths(denied_paths=[".env", "credentials"]))
    """

    allowed_roots = [Path(p).expanduser().resolve(strict=False) for p in (allowed_paths or [])]

    async def _guard(tool_name: str, action: str, args: dict) -> dict | None:
        raw_path = args.get("path") or args.get("file_path") or ""
        if not raw_path:
            return None

        resolved = Path(raw_path).expanduser().resolve(strict=False)

        if allowed_roots:
            if not any(resolved == root or root in resolved.parents for root in allowed_roots):
                raise PermissionError(f"Path not in allowed list: {raw_path}")
        if denied_paths:
            for denied in denied_paths:
                denied_path = Path(denied).expanduser()
                if denied_path.is_absolute():
                    denied_resolved = denied_path.resolve(strict=False)
                    if resolved == denied_resolved or denied_resolved in resolved.parents:
                        raise PermissionError(f"Path matches deny rule: {raw_path}")
                elif denied in raw_path or denied in resolved.name:
                    raise PermissionError(f"Path matches deny rule: {raw_path}")
        return None

    return _guard


async def log_tool_use(tool_name: str, action: str, args: dict, result: dict) -> dict | None:
    """Post-hook: log tool usage for observability."""
    success = result.get("success", result.get("return_code") == 0)
    logger.info("tool=%s action=%s success=%s", tool_name, action, success)
    return None


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
