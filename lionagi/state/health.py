# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0057: six-level session health classification (see docs/reference/testing-state-session.md)."""

from __future__ import annotations

from enum import Enum
from typing import Any

from .staleness import staleness_check

# The 1h "quiet" floor below which a session is HEALTHY regardless of activity.
IDLE_THRESHOLD: int = 3600


class SessionHealth(str, Enum):
    """Six-level derived health (ADR-0057 D6)."""

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
    process_alive: bool | None,
    has_artifacts: bool,
    has_stale_locks: bool,
) -> SessionHealth:
    """Classify a session dict into a SessionHealth level; pure function, caller
    supplies liveness signals. ``process_alive`` is tri-state — see docs/internals/runtime.md."""
    status = session.get("status") or "completed"

    # Terminal sessions: done means done, unless they left litter.
    if status in {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}:
        if has_stale_locks:
            return SessionHealth.ZOMBIE
        # has_artifacts alone isn't zombie evidence — stale locks are the signal.
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

    is_stale = staleness_check(session, now=now) is not None

    if process_alive is not True:
        # Orphan check first (crashed before any message/artifact) — see docs/internals/runtime.md.
        if not has_artifacts and (session.get("message_count") or 0) == 0:
            return SessionHealth.ORPHANED
        if process_alive is False:
            # Confirmed dead outranks the activity guard below.
            return SessionHealth.STALE
        # Unknown liveness: activity is the stronger life-evidence here — see docs/internals/runtime.md.
        if not is_stale:
            if idle_seconds > IDLE_THRESHOLD:
                return SessionHealth.IDLE
            return SessionHealth.HEALTHY
        return SessionHealth.STALE

    # Process alive — classify by activity gap.
    if is_stale:
        return SessionHealth.UNRESPONSIVE
    if idle_seconds > IDLE_THRESHOLD:
        return SessionHealth.IDLE
    return SessionHealth.HEALTHY


def worst_health(values: list[SessionHealth]) -> SessionHealth:
    """Return the most severe health in values; returns HEALTHY for an empty list."""
    if not values:
        return SessionHealth.HEALTHY
    return max(values, key=lambda h: HEALTH_SEVERITY[h])
