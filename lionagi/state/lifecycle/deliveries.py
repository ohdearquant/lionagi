# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable registration and acknowledgement for terminal-event consumers.

Delivery is at least once: independent reconcilers may deliver the same event,
whose stable identity is its transition id. The push callback registry does
not write acknowledgements.
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from .policy import DEFAULT_REGISTRY, PolicyRegistry

if TYPE_CHECKING:
    from lionagi.state.db import StateDB

__all__ = (
    "ConsumerCompatibilityError",
    "ConsumerNotRegisteredError",
    "ConsumerRegistration",
    "ConsumerRetiredError",
    "ConsumerSchemaError",
    "EligibilityCounts",
    "ack_delivery",
    "eligibility_counts",
    "is_acknowledged",
    "reconcile_unacknowledged",
    "register_consumer",
    "retire_consumer",
)

_CLASSIFICATION_KEY = "migration.terminal_delivery_eligibility"
_MAX_EXCLUSIONS = 4096
_MAX_EXCLUSIONS_BYTES = 1024 * 1024


class ConsumerCompatibilityError(RuntimeError):
    """Consumer operations are unavailable in a compatibility-mode store."""


class ConsumerSchemaError(RuntimeError):
    """The store lacks the schema required for durable consumers."""


class ConsumerNotRegisteredError(LookupError):
    """The consumer identity has not been registered."""


class ConsumerRetiredError(ValueError):
    """Reactivation requires explicit consent from the caller."""


@dataclass(frozen=True, slots=True)
class EligibilityCounts:
    """NULL counts; the difference is exact only while transitions are retained."""

    initial_null_count: int
    current_null_count: int
    post_migration_null_count: int


@dataclass(frozen=True, slots=True)
class ConsumerRegistration:
    name: str
    registered_at: float
    retired_at: float | None
    created: bool
    reactivated: bool
    counts: EligibilityCounts


def _consumer_name(consumer: str) -> None:
    if not isinstance(consumer, str) or not consumer.strip():
        raise ValueError("consumer must be a non-empty string")


def _refuse_compatibility(db: StateDB) -> None:
    if db.mode == "compatibility":
        raise ConsumerCompatibilityError("terminal consumers are unavailable in compatibility mode")


async def _check_schema(conn: Any) -> dict[str, Any]:
    """Check actual schema before using it, including for unmigrated read-only stores."""

    def inspect_shape(sync_conn):
        inspector = inspect(sync_conn)
        tables = set(inspector.get_table_names())
        columns = {
            table: {col["name"] for col in inspector.get_columns(table)}
            for table in (
                "schema_meta",
                "status_transitions",
                "terminal_consumers",
                "terminal_deliveries",
            )
            if table in tables
        }
        return columns

    columns = await conn.run_sync(inspect_shape)
    version = None
    if {"key", "value"} <= columns.get("schema_meta", set()):
        version = (
            await conn.execute(text("SELECT value FROM schema_meta WHERE key = 'version'"))
        ).scalar_one_or_none()
    required = {
        "schema_meta": {"key", "value"},
        "status_transitions": {"id", "delivery_eligible", "created_at"},
        "terminal_consumers": {"name", "registered_at", "retired_at"},
        "terminal_deliveries": {"transition_id", "consumer", "acked_at"},
    }
    if any(not names <= columns.get(table, set()) for table, names in required.items()):
        raise ConsumerSchemaError(
            f"terminal consumer schema unavailable at schema version {version!s}"
        )
    marker = (
        await conn.execute(
            text("SELECT value FROM schema_meta WHERE key = :key"), {"key": _CLASSIFICATION_KEY}
        )
    ).scalar_one_or_none()
    try:
        parsed = json.loads(marker) if isinstance(marker, str) else marker
        initial = parsed["initial_null_count"]
        classified_at = parsed["classified_at"]
        if isinstance(initial, bool) or not isinstance(initial, int) or initial < 0:
            raise ValueError("invalid initial NULL count")
        if isinstance(classified_at, bool) or not isinstance(classified_at, (int, float)):
            raise ValueError("invalid classification time")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ConsumerSchemaError(
            f"terminal consumer classification unavailable at schema version {version!s}"
        ) from exc
    return parsed


async def _counts(conn: Any, marker: dict[str, Any]) -> EligibilityCounts:
    current = int(
        (
            await conn.execute(
                text("SELECT COUNT(*) FROM status_transitions WHERE delivery_eligible IS NULL")
            )
        ).scalar_one()
    )
    initial = marker["initial_null_count"]
    return EligibilityCounts(initial, current, current - initial)


@asynccontextmanager
async def _registration_transaction(db: StateDB):
    """Pin enrollment's snapshot before baselining a newly registered identity."""
    if db.dialect == "sqlite":
        async with db.transaction() as conn:
            yield conn
    else:
        async with db._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                yield conn


def _serialization_failure(exc: DBAPIError) -> bool:
    original = exc.orig
    return (
        getattr(original, "sqlstate", None) == "40001"
        or getattr(original, "pgcode", None) == "40001"
    )


async def register_consumer(
    db: StateDB,
    consumer: str,
    *,
    start: Literal["now", "all"],
    reactivate: bool = False,
) -> ConsumerRegistration:
    """Register an identity and its baseline in one transaction.

    ``now`` acknowledges all eligible rows visible in the registration
    snapshot; ``all`` leaves them pending. An active registration is unchanged.
    Explicit reactivation retains prior acknowledgements and applies ``start``
    only to still-pending rows.
    """
    _consumer_name(consumer)
    _refuse_compatibility(db)
    if start not in ("now", "all"):
        raise ValueError("start must be 'now' or 'all'")
    for attempt in range(3):
        try:
            async with _registration_transaction(db) as conn:
                marker = await _check_schema(conn)
                registered_at = time.time()
                inserted = await conn.execute(
                    text(
                        "INSERT INTO terminal_consumers (name, registered_at, retired_at) "
                        "VALUES (:consumer, :registered_at, NULL) ON CONFLICT (name) DO NOTHING"
                    ),
                    {"consumer": consumer, "registered_at": registered_at},
                )
                created = inserted.rowcount == 1
                sql = "SELECT name, registered_at, retired_at FROM terminal_consumers WHERE name = :consumer"
                if db.dialect == "postgresql":
                    sql += " FOR UPDATE"
                row = (await conn.execute(text(sql), {"consumer": consumer})).mappings().one()
                reactivated = row["retired_at"] is not None
                if reactivated and not reactivate:
                    raise ConsumerRetiredError(
                        f"consumer {consumer!r} is retired; pass reactivate=True"
                    )
                if reactivated:
                    await conn.execute(
                        text(
                            "UPDATE terminal_consumers SET retired_at = NULL WHERE name = :consumer"
                        ),
                        {"consumer": consumer},
                    )
                if start == "now" and (created or reactivated):
                    await conn.execute(
                        text(
                            "INSERT INTO terminal_deliveries (transition_id, consumer, acked_at) "
                            "SELECT st.id, :consumer, :acked_at FROM status_transitions st "
                            "WHERE st.delivery_eligible IS TRUE "
                            "ON CONFLICT (transition_id, consumer) DO NOTHING"
                        ),
                        {
                            "consumer": consumer,
                            "acked_at": registered_at if reactivated else row["registered_at"],
                        },
                    )
                counts = await _counts(conn, marker)
                return ConsumerRegistration(
                    name=row["name"],
                    registered_at=float(row["registered_at"]),
                    retired_at=None,
                    created=created,
                    reactivated=reactivated,
                    counts=counts,
                )
        except DBAPIError as exc:
            if db.dialect != "postgresql" or not _serialization_failure(exc) or attempt == 2:
                raise
    raise AssertionError("unreachable")


async def retire_consumer(db: StateDB, consumer: str) -> bool:
    """Retire in one statement; keep ACKs and permit in-flight late ACKs."""
    _consumer_name(consumer)
    _refuse_compatibility(db)
    async with db.transaction() as conn:
        await _check_schema(conn)
        result = await conn.execute(
            text(
                "UPDATE terminal_consumers SET retired_at = :now WHERE name = :consumer AND retired_at IS NULL"
            ),
            {"consumer": consumer, "now": time.time()},
        )
        return result.rowcount == 1


async def ack_delivery(db: StateDB, transition_id: str, consumer: str) -> None:
    """Idempotently acknowledge a delivery, including one completed after retirement."""
    _consumer_name(consumer)
    _refuse_compatibility(db)
    async with db.transaction() as conn:
        await _check_schema(conn)
        exists = (
            await conn.execute(
                text("SELECT 1 FROM terminal_consumers WHERE name = :consumer"),
                {"consumer": consumer},
            )
        ).first()
        if exists is None:
            raise ConsumerNotRegisteredError(f"consumer {consumer!r} is not registered")
        await conn.execute(
            text(
                "INSERT INTO terminal_deliveries (transition_id, consumer, acked_at) "
                "VALUES (:transition_id, :consumer, :acked_at) "
                "ON CONFLICT (transition_id, consumer) DO NOTHING"
            ),
            {"transition_id": transition_id, "consumer": consumer, "acked_at": time.time()},
        )


async def is_acknowledged(db: StateDB, transition_id: str, consumer: str) -> bool:
    _consumer_name(consumer)
    _refuse_compatibility(db)
    async with db._read() as conn:
        await _check_schema(conn)
        row = (
            await conn.execute(
                text(
                    "SELECT 1 FROM terminal_deliveries WHERE transition_id = :id AND consumer = :consumer"
                ),
                {"id": transition_id, "consumer": consumer},
            )
        ).first()
        return row is not None


async def eligibility_counts(db: StateDB) -> EligibilityCounts:
    """Report unclassified rows and the difference from the migration baseline."""
    _refuse_compatibility(db)
    async with db.read_snapshot() as conn:
        marker = await _check_schema(conn)
        return await _counts(conn, marker)


async def reconcile_unacknowledged(
    db: StateDB,
    consumer: str,
    *,
    kinds: frozenset[str] | None = None,
    registry: PolicyRegistry = DEFAULT_REGISTRY,
    limit: int | None = None,
    after: tuple[float, str] | None = None,
    exclude_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Return eligible, unacknowledged rows in a stable keyset traversal.

    ``registry`` remains accepted for callers of the earlier API; persisted
    eligibility, not current policy, determines the replay set. ``exclude_ids``
    is one bound JSON array, so a retry map cannot exhaust SQL bind slots.
    A retired or unknown consumer has no pending deliveries.
    """
    _consumer_name(consumer)
    _refuse_compatibility(db)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    if after is not None and (len(after) != 2 or not isinstance(after[1], str)):
        raise ValueError("after must be a (timestamp, transition id) pair")
    excluded = list(exclude_ids)
    if len(excluded) > _MAX_EXCLUSIONS or any(not isinstance(item, str) for item in excluded):
        raise ValueError("exclude_ids must be a bounded collection of strings")
    encoded = json.dumps(excluded, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_EXCLUSIONS_BYTES:
        raise ValueError("exclude_ids exceeds the encoded byte limit")
    entity_kinds = kinds
    params: dict[str, Any] = {"consumer": consumer, "excluded": encoded}
    kind_names = []
    for i, kind in enumerate(sorted(entity_kinds or ())):
        key = f"kind{i}"
        params[key] = kind
        kind_names.append(f":{key}")
    async with db._read() as conn:
        await _check_schema(conn)
        if entity_kinds is not None and not kind_names:
            return []
        exclusion = (
            "SELECT value FROM json_each(:excluded)"
            if db.dialect == "sqlite"
            else "SELECT value FROM jsonb_array_elements_text(CAST(:excluded AS jsonb)) AS e(value)"
        )
        sql = (
            "SELECT st.id AS transition_id, st.entity_type, st.entity_id, "  # noqa: S608
            "st.previous_status, st.status AS terminal_status, st.reason_code, "
            "st.created_at AS occurred_at FROM status_transitions st "
            "JOIN terminal_consumers tc ON tc.name = :consumer AND tc.retired_at IS NULL "
            "LEFT JOIN terminal_deliveries td ON td.transition_id = st.id AND td.consumer = :consumer "
            "WHERE st.delivery_eligible IS TRUE AND td.transition_id IS NULL "
            f"AND st.id NOT IN ({exclusion}) "
        )
        if kind_names:
            sql += f"AND st.entity_type IN ({', '.join(kind_names)}) "
        if after is not None:
            sql += "AND (st.created_at > :after_time OR (st.created_at = :after_time AND st.id > :after_id)) "
            params.update(after_time=after[0], after_id=after[1])
        sql += "ORDER BY st.created_at ASC, st.id ASC"
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(row) for row in rows]
