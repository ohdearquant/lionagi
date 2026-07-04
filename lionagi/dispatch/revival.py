# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Revival heartbeat reference implementation (ADR-0092 slice 1, item 5).

Demonstrates the durable-dispatch end-to-end case the ADR is built for: a
consumer that is dead at fire time. Enqueues one ``dispatch_outbox`` row per
seat with a ``dedup_key`` scoped to the reset epoch, so a re-fired heartbeat
(daemon restart, missed-fire recovery) cannot double-queue.

This is a plain library call, not a new schedule ``action_kind``: wiring a
brand-new action_kind through the scheduler's fire/subprocess-spawn machinery
would require rebuilding the ``schedules.action_kind`` CHECK constraint (the
same SQLite rename-rebuild migration ``_drop_legacy_action_kind_check``
performs for the existing enum) — out of proportion for a slice-1 reference
implementation. An operator wires this in today via any schedule action that
can call a Python function (or a thin wrapper script); a first-class
``action_kind`` is a natural follow-up once a concrete caller needs it.
"""

from __future__ import annotations

from typing import Any

from .outbox import DEFAULT_MAX_ATTEMPTS, enqueue_dispatch

__all__ = ("enqueue_revival_heartbeat",)


async def enqueue_revival_heartbeat(
    db: Any,
    seats: list[str],
    *,
    reset_epoch: float,
    ttl_seconds: float = 3600.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[str]:
    """Enqueue one ``revival_ping`` dispatch per seat; returns the dispatch ids.

    ``dedup_key=f"revival:{seat}:{reset_epoch}"`` makes a re-fire idempotent:
    calling this again with the same ``reset_epoch`` returns the same rows
    rather than double-queuing.
    """
    dispatch_ids: list[str] = []
    for seat in seats:
        dispatch_id = await enqueue_dispatch(
            db,
            kind="revival_ping",
            deliver_to=seat,
            body={"reset_epoch": reset_epoch},
            dedup_key=f"revival:{seat}:{reset_epoch}",
            ack_required=False,
            max_attempts=max_attempts,
            expires_at=(reset_epoch + ttl_seconds) if reset_epoch else None,
        )
        dispatch_ids.append(dispatch_id)
    return dispatch_ids
