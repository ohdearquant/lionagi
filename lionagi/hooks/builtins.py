# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0023 built-in handlers wired to ADR-0019/0020/0022 persistence helpers."""

from __future__ import annotations

import logging
import time
import warnings
from typing import Any

logger = logging.getLogger("lionagi.hooks.builtins")


async def _db():
    from lionagi.state.db import get_shared_db

    return await get_shared_db()


__all__ = (
    "persist_session_start",
    "persist_session_end",
    "persist_branch_provenance",
    "persist_message",
    "log_api_metrics",
    "log_tool_call",
    "log_tool_use",
)


async def persist_session_start(
    *,
    session_id: str,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    agent_name: str | None = None,
    agent_hash: str | None = None,
    invocation_id: str | None = None,
    **_unused: Any,
) -> None:
    """Write the ADR-0022 provenance set + open the lifecycle window."""
    from lionagi.state.reasons import RunReasons

    db = await _db()
    await db.update_session(
        session_id,
        # status="running" routes through update_status(), which requires
        # a reason_code — pass it explicitly so the transition records a
        # canonical "started" cause instead of tripping the deprecation
        # shim (which would raise on the running status and be swallowed by
        # the bus, silently dropping all the provenance fields above).
        reason_code=RunReasons.STARTED_OK,
        model=model,
        provider=provider,
        effort=effort,
        agent_name=agent_name,
        agent_hash=agent_hash,
        invocation_id=invocation_id,
        status="running",
        started_at=time.time(),
    )


async def persist_session_end(
    *,
    session_id: str,
    status: str = "completed",
    error: str | None = None,
    **_unused: Any,
) -> None:
    """Stamp the terminal status + ended_at on the session row."""
    fields: dict[str, Any] = {"status": status, "ended_at": time.time()}
    if error is not None:
        # node_metadata is JSON — overwriting is destructive, so keep the
        # error in a dedicated field and let the caller merge if needed.
        fields["node_metadata"] = {"error": error}
    db = await _db()
    await db.update_session(session_id, **fields)


async def persist_branch_provenance(
    *,
    branch_id: str,
    model: str | None = None,
    provider: str | None = None,
    agent_name: str | None = None,
    **_unused: Any,
) -> None:
    """ADR-0022 per-branch model / provider / agent_name."""
    db = await _db()
    await db.update_branch(
        branch_id,
        model=model,
        provider=provider,
        agent_name=agent_name,
    )


async def persist_message(
    *,
    message: dict[str, Any],
    session_id: str,
    branch_id: str | None = None,
    branch_progression_id: str | None = None,
    session_progression_id: str | None = None,
    # Legacy alias kept for callers predating the dual-progression split.
    progression_id: str | None = None,
    **_unused: Any,
) -> None:
    """ADR-0019 + ADR-0009 message persistence; ``progression_id`` is a legacy alias."""
    effective_branch_prog = branch_progression_id or progression_id

    db = await _db()
    await db.insert_message(message)
    if effective_branch_prog is not None:
        await db.append_to_progression(effective_branch_prog, message["id"])
    if session_progression_id is not None:
        await db.append_to_progression(session_progression_id, message["id"])
    if message.get("role") == "system" and branch_id is not None:
        await db.update_branch(branch_id, system_msg_id=message["id"])
    await db.touch_session_activity(session_id)


async def log_api_metrics(
    *,
    model: str | None = None,
    provider: str | None = None,
    tokens: dict[str, int] | None = None,
    latency_ms: float | None = None,
    **_unused: Any,
) -> None:
    """Structured log line for API call observability."""
    if tokens:
        logger.info(
            "api.post_call model=%s provider=%s tokens=%s latency_ms=%s",
            model,
            provider,
            tokens.get("total"),
            latency_ms,
        )
    else:
        logger.info(
            "api.post_call model=%s provider=%s latency_ms=%s",
            model,
            provider,
            latency_ms,
        )


async def log_tool_call(
    *,
    tool_name: str,
    action: str | None = None,
    args: dict[str, Any] | None = None,
    **_unused: Any,
) -> None:
    """Structured log for tool dispatch — readable but not metric-y."""
    logger.info(
        "tool.pre tool=%s action=%s args=%s",
        tool_name,
        action,
        list(args.keys()) if args else [],
    )


async def log_tool_use(
    *,
    tool_name: str,
    action: str | None = None,
    args: dict[str, Any] | None = None,
    **_unused: Any,
) -> None:
    """Deprecated: use log_tool_call instead."""
    warnings.warn(
        "log_tool_use is deprecated and will be removed in a future minor release. "
        "Use log_tool_call instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    await log_tool_call(tool_name=tool_name, action=action, args=args)
