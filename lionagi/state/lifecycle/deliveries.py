# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable reconciliation-consumer acknowledgment ledger. Written only by a
named reconciliation consumer, never by the fire-and-forget push path
(``TerminalCallbackRegistry``); see docs/internals/runtime.md.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .callbacks import EXECUTION_ENTITY_KINDS
from .policy import DEFAULT_REGISTRY, PolicyRegistry

if TYPE_CHECKING:
    from lionagi.state.db import StateDB

__all__ = ("ack_delivery", "is_acknowledged", "reconcile_unacknowledged")


async def ack_delivery(db: StateDB, transition_id: str, consumer: str) -> None:
    """Record that *consumer* has durably processed *transition_id*.
    Idempotent by construction (``ON CONFLICT DO NOTHING`` on the composite key).
    """
    await db.execute(
        "INSERT INTO terminal_deliveries (transition_id, consumer, acked_at) "
        "VALUES (:transition_id, :consumer, :acked_at) "
        "ON CONFLICT (transition_id, consumer) DO NOTHING",
        {"transition_id": transition_id, "consumer": consumer, "acked_at": time.time()},
    )


async def is_acknowledged(db: StateDB, transition_id: str, consumer: str) -> bool:
    row = await db.fetch_one(
        "SELECT 1 FROM terminal_deliveries WHERE transition_id = :transition_id "
        "AND consumer = :consumer",
        {"transition_id": transition_id, "consumer": consumer},
    )
    return row is not None


async def reconcile_unacknowledged(
    db: StateDB,
    consumer: str,
    *,
    kinds: frozenset[str] | None = None,
    registry: PolicyRegistry = DEFAULT_REGISTRY,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every terminal transition on an execution entity that *consumer* has
    not yet acknowledged, oldest first. "Terminal" is re-derived per entity
    kind from the same policy registry the lifecycle service itself consults
    (one definition, not two). Plain read; never writes an acknowledgment.
    """
    entity_kinds = kinds if kinds is not None else EXECUTION_ENTITY_KINDS
    clauses: list[str] = []
    params: dict[str, Any] = {"consumer": consumer}
    for i, kind in enumerate(sorted(entity_kinds)):
        policy = registry.get(kind)
        terminal = sorted(policy.terminal_statuses)
        if not terminal:
            continue
        kind_key = f"kind{i}"
        params[kind_key] = kind
        status_keys = []
        for j, status in enumerate(terminal):
            key = f"kind{i}_status{j}"
            params[key] = status
            status_keys.append(f":{key}")
        clauses.append(
            f"(st.entity_type = :{kind_key} AND st.status IN ({', '.join(status_keys)}))"
        )
    if not clauses:
        return []

    # clauses/params hold only bind placeholders built from the fixed policy
    # registry, never caller-supplied SQL text.
    sql = (
        "SELECT st.id AS transition_id, st.entity_type, st.entity_id, "  # noqa: S608
        "st.previous_status, st.status AS terminal_status, st.reason_code, "
        "st.created_at AS occurred_at "
        "FROM status_transitions st "
        "LEFT JOIN terminal_deliveries td "
        "ON td.transition_id = st.id AND td.consumer = :consumer "
        f"WHERE ({' OR '.join(clauses)}) "
        "AND st.previous_status IS NOT NULL AND st.previous_status != st.status "
        "AND td.transition_id IS NULL "
        "ORDER BY st.created_at ASC"
    )
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return await db.fetch_all(sql, params)
