# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0019: run staleness detection.

Staleness is a **derived health indicator**, not a stored DB status.
The runs list and dashboard compute it at read time from each session's
``last_message_at`` and a kind-aware threshold. ADR-0024 layers the full
health classification (idle / unresponsive / stale / orphaned / zombie)
on top of this primitive.

Why kind-aware: a single-agent run with zero messages for 6 hours is
dead; a 10-agent flow legitimately runs that long. The thresholds let
operators distinguish "actually stuck" from "still working."
"""

from __future__ import annotations

import time
from typing import Any

# Per-invocation_kind activity thresholds (seconds). Single-shot runs
# are tight; multi-agent flows get headroom.
STALE_THRESHOLDS: dict[str, int] = {
    "agent": 6 * 3600,        # 6h — single agent
    "play": 6 * 3600,         # 6h — single-play run
    "flow": 12 * 3600,        # 12h — multi-agent DAG
    "fanout": 12 * 3600,      # 12h — parallel fanout
    "show-play": 12 * 3600,   # 12h — show-managed plays
}
DEFAULT_STALE_THRESHOLD: int = 6 * 3600


def staleness_check(
    session: dict[str, Any], *, now: float | None = None
) -> str | None:
    """Return ``"stale"`` if ``session`` is running past its activity threshold.

    Returns ``None`` for terminal sessions — ADR-0024's
    ``classify_session_health`` handles those (a ``completed`` session
    with stale locks is ``zombie``, not stale).

    The threshold lookup keys on ``invocation_kind``; missing/unknown
    kinds get :data:`DEFAULT_STALE_THRESHOLD`. ``last_message_at`` is
    preferred over ``updated_at`` because metadata writes shouldn't
    masquerade as activity. Both falling back to ``0`` means a session
    with no activity columns is always stale relative to ``now`` —
    the desired behavior for legacy rows that never wrote either.
    """
    if session.get("status") != "running":
        return None
    threshold = STALE_THRESHOLDS.get(
        session.get("invocation_kind"), DEFAULT_STALE_THRESHOLD
    )
    last_activity = (
        session.get("last_message_at")
        or session.get("updated_at")
        or 0
    )
    ts = now if now is not None else time.time()
    if ts - last_activity > threshold:
        return "stale"
    return None


def threshold_for_kind(invocation_kind: str | None) -> int:
    """Public lookup so callers can show "stale > 6h" in tooltips."""
    if invocation_kind is None:
        return DEFAULT_STALE_THRESHOLD
    return STALE_THRESHOLDS.get(invocation_kind, DEFAULT_STALE_THRESHOLD)
