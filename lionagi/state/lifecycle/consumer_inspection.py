# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read-only terminal-consumer snapshots and non-reserving change previews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text

from .deliveries import (
    ConsumerNotRegisteredError,
    _check_schema,
    _consumer_name,
    _refuse_compatibility,
)

if TYPE_CHECKING:
    from lionagi.state.db import StateDB

__all__ = (
    "ConsumerInspectionBackendError",
    "ConsumerPreviewAlreadyActiveError",
    "ConsumerPreviewAlreadyRetiredError",
    "ConsumerSnapshot",
    "list_consumers",
    "preview_consumer_change",
)


class ConsumerInspectionBackendError(RuntimeError):
    """Read-only inspection is not supported for this backend in this release."""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        super().__init__(
            f"Terminal consumer list and dry-run are SQLite-only in this release "
            f"(backend: {backend}); a file-backed SQLite store is required."
        )


class ConsumerPreviewAlreadyActiveError(ValueError):
    """A preview cannot reactivate an active consumer."""


class ConsumerPreviewAlreadyRetiredError(ValueError):
    """A preview cannot retire an already retired consumer."""


@dataclass(frozen=True, slots=True)
class ConsumerSnapshot:
    name: str
    registered_at: float
    retired_at: float | None
    owed: int
    candidate: int


async def list_consumers(db: StateDB) -> list[ConsumerSnapshot]:
    """Read registrations and retained eligible unacknowledged counts in one snapshot.

    Retired consumers owe no delivery. Their candidate count describes currently
    retained unacknowledged transitions that an ``all`` reactivation could make owed;
    it is an observation, not a retention guarantee.
    """
    if db.dialect != "sqlite":
        raise ConsumerInspectionBackendError(db.dialect)
    _refuse_compatibility(db)
    async with db.read_snapshot() as conn:
        await _check_schema(conn)
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT c.name, c.registered_at, c.retired_at, "
                        "(SELECT COUNT(*) FROM status_transitions st "
                        "WHERE st.delivery_eligible IS TRUE AND NOT EXISTS "
                        "(SELECT 1 FROM terminal_deliveries td "
                        "WHERE td.transition_id = st.id AND td.consumer = c.name)) AS candidate "
                        "FROM terminal_consumers c ORDER BY c.name"
                    )
                )
            )
            .mappings()
            .all()
        )
        return [
            ConsumerSnapshot(
                name=row["name"],
                registered_at=float(row["registered_at"]),
                retired_at=float(row["retired_at"]) if row["retired_at"] is not None else None,
                owed=int(row["candidate"]) if row["retired_at"] is None else 0,
                candidate=int(row["candidate"]),
            )
            for row in rows
        ]


async def preview_consumer_change(
    db: StateDB,
    name: str,
    *,
    action: Literal["retire", "reactivate"],
    start: Literal["now", "all"] | None = None,
) -> dict[str, object]:
    """Preview a change without writing; concurrent changes can invalidate the preview."""
    _consumer_name(name)
    if action not in ("retire", "reactivate"):
        raise ValueError("action must be 'retire' or 'reactivate'")
    if action == "reactivate" and start not in ("now", "all"):
        raise ValueError("reactivation requires start='now' or start='all'")
    if action == "retire" and start is not None:
        raise ValueError("start applies only to reactivation")
    current = next((entry for entry in await list_consumers(db) if entry.name == name), None)
    if current is None:
        raise ConsumerNotRegisteredError(f"consumer {name!r} is not registered")
    if action == "retire" and current.retired_at is not None:
        raise ConsumerPreviewAlreadyRetiredError(f"consumer {name!r} is already retired")
    if action == "reactivate" and current.retired_at is None:
        raise ConsumerPreviewAlreadyActiveError(f"consumer {name!r} is already active")
    if action == "retire":
        projected_owed = 0
        projected_baselined = 0
    else:
        projected_owed = current.candidate if start == "all" else 0
        projected_baselined = current.candidate if start == "now" else 0
    return {
        "name": current.name,
        "action": action,
        "start": start,
        "registered_at": current.registered_at,
        "retired_at": current.retired_at,
        "owed_before": current.owed,
        "candidate_before": current.candidate,
        "would_change": True,
        "projected_owed": projected_owed,
        "projected_baselined": projected_baselined,
        "preview": True,
    }
