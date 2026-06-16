# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0024: six-level session health classification (see docs/reference/testing-state-session.md)."""

from __future__ import annotations

from enum import Enum
from typing import Any

from .staleness import DEFAULT_STALE_THRESHOLD, STALE_THRESHOLDS

# An idle session is alive and quiet; an unresponsive one is alive and
# past the kind-aware threshold. The 1h floor here is the "quiet"
# boundary — anything below is HEALTHY regardless of activity.
IDLE_THRESHOLD: int = 3600


class SessionHealth(str, Enum):
    """Six-level derived health (ADR-0024 §A)."""

    HEALTHY = "healthy"
    IDLE = "idle"
    UNRESPONSIVE = "unresponsive"
    STALE = "stale"
    ORPHANED = "orphaned"
    ZOMBIE = "zombie"


# Pre-sorted by severity so the dashboard "worst of group" calculation
# is a max-by-index. Keep aligned with the dashboard color tokens.
HEALTH_SEVERITY: dict[SessionHealth, int] = {
    SessionHealth.HEALTHY: 0,
    SessionHealth.IDLE: 1,
    SessionHealth.UNRESPONSIVE: 2,
    SessionHealth.STALE: 3,
    SessionHealth.ORPHANED: 4,
    SessionHealth.ZOMBIE: 5,
}


def classify_session_health(
    session: dict[str, Any],
    *,
    now: float,
    process_alive: bool,
    has_artifacts: bool,
    has_stale_locks: bool,
) -> SessionHealth:
    """Classify a session dict into a SessionHealth level; pure function, caller supplies liveness signals."""
    status = session.get("status") or "completed"

    # Terminal sessions: done means done, unless they left litter.
    if status in {"completed", "failed", "timed_out", "aborted", "cancelled"}:
        if has_stale_locks:
            return SessionHealth.ZOMBIE
        # ``has_artifacts`` alone isn't enough to mark zombie — artifacts
        # are a *good* outcome. Stale locks are the operational signal.
        return SessionHealth.HEALTHY

    # Below here: status == 'running' (or legacy NULL → treated as
    # completed above). Active sessions classify along live/dead axes.

    last_activity = (
        session.get("last_message_at")
        or session.get("updated_at")
        or session.get("started_at")
        or 0
    )
    idle_seconds = now - last_activity

    kind = session.get("invocation_kind")
    threshold = STALE_THRESHOLDS.get(kind, DEFAULT_STALE_THRESHOLD)

    if not process_alive:
        # Orphan check first: the session was advertised but never
        # produced a single message AND no artifacts on disk. This is
        # a session that crashed before doing anything; transitioning
        # it to failed is harmless, deleting it is also safe.
        if not has_artifacts and (session.get("message_count") or 0) == 0:
            return SessionHealth.ORPHANED
        return SessionHealth.STALE

    # Process alive — classify by activity gap.
    if idle_seconds > threshold:
        return SessionHealth.UNRESPONSIVE
    if idle_seconds > IDLE_THRESHOLD:
        return SessionHealth.IDLE
    return SessionHealth.HEALTHY


def worst_health(values: list[SessionHealth]) -> SessionHealth:
    """Return the most severe health in values; returns HEALTHY for an empty list."""
    if not values:
        return SessionHealth.HEALTHY
    return max(values, key=lambda h: HEALTH_SEVERITY[h])
