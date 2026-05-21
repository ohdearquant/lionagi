# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0023 built-in handlers.

These adapt the new HookBus to the persistence helpers that already
land via ADR-0019 / 0020 / 0022. The CLI hot paths still call those
helpers directly in this PR — wiring the bus into ``_setup_live_persist``
and ``start_live_persist`` is intentionally deferred to ADR-0023b so
the existing message-add path isn't disrupted while the bus is being
proven out.

Each handler is name-addressable via the loader's registry so agent
profiles can reference them as strings (``hooks.session.start:
[persist_session_start]``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("lionagi.hooks.builtins")


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
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        await db.update_session(
            session_id,
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
    from lionagi.state.db import StateDB

    fields: dict[str, Any] = {"status": status, "ended_at": time.time()}
    if error is not None:
        # node_metadata is JSON — overwriting is destructive, so keep the
        # error in a dedicated field and let the caller merge if needed.
        fields["node_metadata"] = {"error": error}
    async with StateDB() as db:
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
    from lionagi.state.db import StateDB

    async with StateDB() as db:
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
    """ADR-0019 + ADR-0009 message persistence path.

    Appends to both ``branch_progression_id`` and
    ``session_progression_id`` when provided. For system messages
    (``message["role"] == "system"``) also updates the branch row's
    ``system_msg_id`` pointer so the branch always knows its current
    system prompt.

    ``progression_id`` is a legacy alias for ``branch_progression_id``
    kept for backward compatibility.
    """
    from lionagi.state.db import StateDB

    effective_branch_prog = branch_progression_id or progression_id

    async with StateDB() as db:
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
    """Cheap structured log line for ad-hoc observability.

    Real metrics emission (Prometheus, OTel, etc.) is out of scope for
    this PR; this exists so the agent YAML loader can demonstrate
    declarative hook wiring against a non-DB handler.
    """
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


async def log_tool_use(
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
