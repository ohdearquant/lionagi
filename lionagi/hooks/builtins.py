# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0047 built-in handlers wired to ADR-0077/0020/0022 persistence helpers."""

from __future__ import annotations

import json
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
    """Write the ADR-0077 provenance set + open the lifecycle window."""
    from lionagi.state.db import SESSION_TERMINAL_STATUSES
    from lionagi.state.reasons import RunReasons

    db = await _db()
    row = await db.get_session(session_id)
    if row is None:
        return
    current_status = row.get("status")
    if current_status in SESSION_TERMINAL_STATUSES:
        return
    if row.get("status_reason_code") == RunReasons.STARTED_OK:
        return
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
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_cost_usd: float | None = None,
    num_turns: int | None = None,
    duration_ms: float | None = None,
    **_unused: Any,
) -> None:
    """Stamp ended_at/status + usage on the session row.

    teardown_persist() always stamps the terminal status (via
    _teardown_common()'s update_status() call) before emitting SESSION_END, so
    by the time this handler runs in the normal CLI flow the row is already
    terminal. In that case only the pure usage fields (input_tokens,
    output_tokens, total_cost_usd, num_turns, duration_ms) are written — the
    status/reason_code/ended_at transition is skipped (avoids a duplicate
    status_transitions row and keeps a genuine double-fire from clobbering an
    already-recorded status), and node_metadata is left untouched too, since
    _teardown_common() already owns it for a terminal row (its own
    extras/identity-markers write happens before update_status()) and
    update_session() does a plain column SET, not a merge — writing
    {"error": ...} here would clobber that richer data rather than add to it.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES
    from lionagi.state.reasons import RunReasons

    db = await _db()
    row = await db.get_session(session_id)
    if row is None:
        return

    already_terminal = row.get("status") in SESSION_TERMINAL_STATUSES

    fields: dict[str, Any] = {}
    if not already_terminal:
        fields["ended_at"] = time.time()
        if error is not None:
            # update_session() binds this as a raw SQL param (no JSON
            # bindparam), so pre-serialize to avoid sqlite3.InterfaceError.
            # update_session() also does a plain column SET, not a merge, so
            # start from whatever node_metadata the row already carries
            # (e.g. identity markers) rather than clobbering it.
            existing_metadata = row.get("node_metadata")
            if not isinstance(existing_metadata, dict):
                existing_metadata = {}
            fields["node_metadata"] = json.dumps({**existing_metadata, "error": error})
    if input_tokens is not None:
        fields["input_tokens"] = input_tokens
    if output_tokens is not None:
        fields["output_tokens"] = output_tokens
    if total_cost_usd is not None:
        fields["total_cost_usd"] = total_cost_usd
    if num_turns is not None:
        fields["num_turns"] = num_turns
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms

    if not fields:
        return

    if already_terminal:
        await db.update_session(session_id, **fields)
        return

    _status_reason_map: dict[str, str] = {
        "completed": RunReasons.COMPLETED_OK,
        "failed": RunReasons.FAILED_EXCEPTION,
        "timed_out": RunReasons.TIMED_OUT_DEADLINE,
        "aborted": RunReasons.ABORTED_USER,
        "cancelled": RunReasons.CANCELLED_SYSTEM,
    }
    await db.update_session(
        session_id,
        reason_code=_status_reason_map.get(status, RunReasons.FAILED_EXCEPTION),
        status=status,
        **fields,
    )


async def persist_branch_provenance(
    *,
    branch_id: str,
    model: str | None = None,
    provider: str | None = None,
    agent_name: str | None = None,
    **_unused: Any,
) -> None:
    """ADR-0077 per-branch model / provider / agent_name."""
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
    """ADR-0077 + ADR-0056 message persistence; ``progression_id`` is a legacy alias."""
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
