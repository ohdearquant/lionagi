# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Classify a newly written lifecycle history row for terminal delivery."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import inspect

from .lifecycle.callbacks import EXECUTION_ENTITY_KINDS
from .lifecycle.policy import DEFAULT_REGISTRY, PolicyRegistry

__all__ = ("HistoryWriteKind", "has_delivery_eligibility_column", "is_delivery_eligible")

HistoryWriteKind = Literal["initialization", "transition"]


async def has_delivery_eligibility_column(db, connection) -> bool:
    """Check a foreign store's actual shape; native stores migrate on open."""
    if db.mode != "compatibility":
        return True
    columns = await connection.run_sync(
        lambda conn: {col["name"] for col in inspect(conn).get_columns("status_transitions")}
    )
    return "delivery_eligible" in columns


def is_delivery_eligible(
    entity_type: str,
    previous_status: str | None,
    new_status: str,
    *,
    write_kind: HistoryWriteKind,
    registry: PolicyRegistry = DEFAULT_REGISTRY,
) -> bool:
    """Decide delivery at write time; initial history is never a transition."""
    if write_kind not in ("initialization", "transition"):
        raise ValueError(f"unknown history write kind: {write_kind!r}")
    if write_kind == "initialization" or entity_type not in EXECUTION_ENTITY_KINDS:
        return False
    return (
        previous_status != new_status and new_status in registry.get(entity_type).terminal_statuses
    )
