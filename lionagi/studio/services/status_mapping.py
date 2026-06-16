"""ADR-0012 status display mapping — translates raw DB tokens to UI-friendly strings for plays and sessions."""

from __future__ import annotations

# ADR-0011, ADR-0017, ADR-0025: DISPLAY_MAP must only contain
# values present in the closed ADR vocabularies.
#
# ADR-0011 plays.status CHECK vocabulary (11 values):
#   pending, prepared, running, running_complete, gated, gate_failed,
#   redoing, merged, escalated, blocked, aborted_after_finish
#
# ADR-0025 sessions.status vocabulary (6 values; supersedes ADR-0017):
#   running, completed, failed, timed_out, aborted, cancelled
DISPLAY_MAP: dict[str, str] = {
    # play statuses (ADR-0011)
    "pending": "pending",
    "prepared": "pending",
    "running": "running",
    "running_complete": "awaiting_gate",
    "gated": "awaiting_gate",
    "gate_failed": "failed",
    "redoing": "redoing",
    "merged": "completed",
    "escalated": "escalated",
    "blocked": "blocked",
    "aborted_after_finish": "aborted",
    # session statuses (ADR-0025)
    "completed": "completed",
    "failed": "failed",
    "timed_out": "timed_out",
    "aborted": "aborted",
    "cancelled": "cancelled",
}


def display_status(raw: str | None) -> str:
    if not raw:
        return "pending"
    return DISPLAY_MAP.get(raw.lower().strip(), raw)


def gate_badge(gate_passed: int | bool | None) -> str | None:
    if gate_passed is None:
        return None
    return "passed" if gate_passed else "failed"


def integration_badge(merged_at: float | str | None) -> str:
    return "merged" if merged_at else "local"
