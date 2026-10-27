# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Revival heartbeat reference implementation (ADR-0059).

Demonstrates the durable-dispatch case of a consumer dead at fire time;
dedup_key scoped to the reset epoch keeps re-fires idempotent. Deliberately
a plain library call, not a new schedule action_kind — see docs/internals/runtime.md.
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

    ``dedup_key`` scoped to ``reset_epoch`` makes a re-fire idempotent.
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
