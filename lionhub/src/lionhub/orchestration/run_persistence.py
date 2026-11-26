# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The state-store persistence handlers, and the set a hub run installs on its session's bus.

A lionagi ``Session`` bus starts with no handlers, so a session used through the SDK writes
nothing to the state store. A run started through the hub (the CLI and the MCP server's agent
runs, by way of :mod:`lionhub.orchestration.runs`) records its session and branches there, so
before it emits a lifecycle point it installs these handlers on its session's bus.

Importing this module registers the five persistence handlers by name with
:func:`lionagi.hooks.loader.register_handler`, so an agent profile's ``hooks`` section and
``build_session_bus(agent_hooks=...)`` can name them.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from lionagi import ln
from lionagi.hooks.bus import HookBus, HookHandler, HookPoint, _current_emitting_bus
from lionagi.hooks.loader import _set_store_message_handler, register_handler

__all__ = (
    "RUN_PERSISTENCE_HOOKS",
    "install_run_persistence",
    "persist_branch_end",
    "persist_branch_provenance",
    "persist_message",
    "persist_session_end",
    "persist_session_start",
)

# The handlers kept their logger name when they left lionagi.hooks.builtins, so logging
# configuration that names it still reaches them.
logger = logging.getLogger("lionagi.hooks.builtins")


async def _db(db: Any = None):
    """The store a persistence handler writes through.

    A run hands its own open handle to these handlers on the event, so each writes through the
    run's handle and meets whatever the store requires of that run. A caller that carries no
    handle reaches the process-wide store the way it always has, looked up by url.
    """
    if db is not None:
        return db
    from lionhub.state.db import get_shared_db

    return await get_shared_db()


async def persist_session_start(
    *,
    session_id: str,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    agent_name: str | None = None,
    agent_hash: str | None = None,
    invocation_id: str | None = None,
    db: Any = None,
    **_unused: Any,
) -> None:
    """Write the session provenance set + open the lifecycle window.

    A run that hands its own handle to this handler on the event has written the session row
    with every one of these fields when it created it, so the handler writes nothing. Emitted
    without a handle (a direct bus emit), it writes them as before."""
    from lionhub.state.db import SESSION_TERMINAL_STATUSES
    from lionhub.state.reasons import RunReasons

    if db is not None:
        return
    db = await _db(db)
    row = await db.get_session(session_id)
    if row is None:
        return
    current_status = row.get("status")
    if current_status in SESSION_TERMINAL_STATUSES:
        return

    # Neither a pre-existing STARTED_OK row nor a CLI-prestamped started_at
    # tells this handler a genuine first emission from a duplicate, so writes
    # here are idempotent: provenance fields safely re-write, and started_at
    # is "first writer wins" via an atomic COALESCE rather than read-then-write.
    provenance = {
        "model": model,
        "provider": provider,
        "effort": effort,
        "agent_name": agent_name,
        "agent_hash": agent_hash,
        "invocation_id": invocation_id,
    }
    fields = {k: v for k, v in provenance.items() if v is not None}
    fields["started_at"] = time.time()

    if row.get("status_reason_code") == RunReasons.STARTED_OK:
        await db.update_session(session_id, set_if_null=frozenset({"started_at"}), **fields)
        return
    await db.update_session(
        session_id,
        # Explicit reason_code avoids tripping the deprecation shim, which
        # would swallow the transition and silently drop provenance fields.
        reason_code=RunReasons.STARTED_OK,
        status="running",
        set_if_null=frozenset({"started_at"}),
        **fields,
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
    db: Any = None,
    **_unused: Any,
) -> None:
    """Stamp ended_at/status + usage on the session row.

    A run hands its own handle to this handler on the event and owns the terminal status write
    itself, made once the emit returns; this handler then writes only the usage fields the event
    carries, leaving the status, its reason and the end time to that write. Emitted without a
    handle (a direct bus emit), it stamps the status itself as before."""
    from lionhub.state.db import SESSION_TERMINAL_STATUSES
    from lionhub.state.reasons import RunReasons

    teardown_owns_status = db is not None
    db = await _db(db)
    row = await db.get_session(session_id)
    if row is None:
        return

    already_terminal = row.get("status") in SESSION_TERMINAL_STATUSES

    fields: dict[str, Any] = {}
    if not already_terminal and not teardown_owns_status:
        fields["ended_at"] = time.time()
        if error is not None:
            # Pre-serialize (raw SQL param, no JSON bindparam) and merge
            # onto existing node_metadata rather than clobbering it.
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

    if already_terminal or teardown_owns_status:
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
    db: Any = None,
    **_unused: Any,
) -> None:
    """Persist per-branch model / provider / agent_name provenance.

    A run that hands its own handle to this handler on the event created the branch row with
    these fields, so the handler writes nothing; emitted without a handle, it writes them."""
    if db is not None:
        return
    db = await _db(db)
    await db.update_branch(
        branch_id,
        model=model,
        provider=provider,
        agent_name=agent_name,
    )


async def persist_branch_end(
    *,
    branch_id: str,
    status: str = "completed",
    ended_at: float | None = None,
    db: Any = None,
    **_unused: Any,
) -> None:
    """Stamp the branch row's terminal status/ended_at — the BRANCH_END
    counterpart to BRANCH_CREATE's persist_branch_provenance.

    Delegates the guard to StateDB.finalize_branch(), which no-ops on a
    non-terminal status, on a branch already at any terminal status (so a
    more specific outcome recorded elsewhere, e.g. the DAG runner's own
    NodeCompleted/NodeFailed updates, is never clobbered), and on a branch
    row that doesn't exist yet. In compatibility mode it also refuses a branch row this run
    did not create, or that no longer carries the run's owner, so a store opened in compatibility
    mode never reaches another run's branch through this handler.
    """
    db = await _db(db)
    await db.finalize_branch(branch_id, status=status, ended_at=ended_at or time.time())


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
    """Persist a message; ``progression_id`` is a legacy alias."""
    effective_branch_prog = branch_progression_id or progression_id

    db = await _db()
    from lionhub.state.message_retry import MessagePersistRetryQueue, PendingMessageEvent

    bus = _current_emitting_bus()
    if bus is None:
        await db._persist_live_message(
            message,
            session_id=session_id,
            branch_progression_id=effective_branch_prog,
            session_progression_id=session_progression_id,
            system_branch_id=branch_id if message.get("role") == "system" else None,
            system_branch_update_before_activity=True,
        )
        return

    queue_key = (
        id(db),
        session_id,
        branch_id,
        effective_branch_prog,
        session_progression_id,
    )
    # The bus flushes these queues at teardown by reading this attribute by name.
    queues = getattr(bus, "_message_retry_queues", None)
    if queues is None:
        queues = {}
        bus._message_retry_queues = queues
    retry_queue = queues.get(queue_key)
    if retry_queue is None:
        retry_queue = MessagePersistRetryQueue(
            db,
            logger=logger,
            owner=f"hook session {session_id}",
        )
        queues[queue_key] = retry_queue

    await retry_queue.submit(
        PendingMessageEvent(
            message=ln.copy(message),
            session_id=session_id,
            branch_progression_id=effective_branch_prog,
            session_progression_id=session_progression_id,
            system_branch_id=branch_id if message.get("role") == "system" else None,
            system_branch_update_before_activity=True,
        )
    )


for _name, _handler in (
    ("persist_session_start", persist_session_start),
    ("persist_session_end", persist_session_end),
    ("persist_branch_provenance", persist_branch_provenance),
    ("persist_branch_end", persist_branch_end),
    ("persist_message", persist_message),
):
    register_handler(_name, _handler)
del _name, _handler

# A live message route removes exactly this object from a session bus (lionagi.hooks.persist).
_set_store_message_handler(persist_message)


RUN_PERSISTENCE_HOOKS: Mapping[HookPoint, tuple[HookHandler, ...]] = MappingProxyType(
    {
        HookPoint.SESSION_START: (persist_session_start,),
        HookPoint.SESSION_END: (persist_session_end,),
        HookPoint.BRANCH_CREATE: (persist_branch_provenance,),
        HookPoint.BRANCH_END: (persist_branch_end,),
    }
)


def install_run_persistence(bus: HookBus) -> HookBus:
    """Register each persistence handler on *bus* once, and return *bus*.

    A handler already registered at its point is left where it is, so installing again on the
    same session, as a run's setup and its teardown both do, never registers a handler twice and
    never writes a row twice. Handlers the caller registered stay ahead of these.
    """
    for point, handlers in RUN_PERSISTENCE_HOOKS.items():
        present = bus.handlers_for(point)
        for handler in handlers:
            if handler not in present:
                bus.on(point, handler)
    return bus
