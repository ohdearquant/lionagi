"""Status display mapping (ADR-0012).

Raw statuses from the show skill's state machine are preserved in the database.
This module provides display mappings for the UI.
"""

from __future__ import annotations

# F-A1-7 (ADR-0011, ADR-0017): LIFECYCLE_MAP must only contain values present
# in the closed ADR vocabularies.
#
# ADR-0011 plays.status CHECK vocabulary (11 values):
#   pending, prepared, running, running_complete, gated, gate_failed,
#   redoing, merged, escalated, blocked, aborted_after_finish
#
# ADR-0017 sessions.status CHECK vocabulary (4 values):
#   running, completed, failed, aborted
#
# Removed entries that appeared in neither ADR vocabulary:
#   "done"      → not in any ADR CHECK constraint
#   "success"   → not in any ADR CHECK constraint
#   "finished"  → not in any ADR CHECK constraint
#   "error"     → not in any ADR CHECK constraint
#   "cancelled" → not in any ADR CHECK constraint (ADR uses "aborted")
#   "canceled"  → not in any ADR CHECK constraint (ADR uses "aborted")
LIFECYCLE_MAP: dict[str, str] = {
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
    # session statuses (ADR-0017)
    "completed": "completed",
    "failed": "failed",
    "aborted": "aborted",
}


def display_status(raw: str | None) -> str:
    if not raw:
        return "pending"
    return LIFECYCLE_MAP.get(raw.lower().strip(), raw)


def gate_badge(gate_passed: int | bool | None) -> str | None:
    if gate_passed is None:
        return None
    return "passed" if gate_passed else "failed"


def integration_badge(merged_at: float | str | None) -> str:
    return "merged" if merged_at else "local"
