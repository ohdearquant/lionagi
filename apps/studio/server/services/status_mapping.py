"""Status display mapping (ADR-0012).

Raw statuses from the show skill's state machine are preserved in the database.
This module provides display mappings for the UI.
"""

from __future__ import annotations

LIFECYCLE_MAP: dict[str, str] = {
    "pending": "pending",
    "prepared": "pending",
    "running": "running",
    "running_complete": "awaiting_gate",
    "gated": "awaiting_gate",
    "completed": "completed",
    "done": "completed",
    "success": "completed",
    "finished": "completed",
    "failed": "failed",
    "error": "failed",
    "gate_failed": "failed",
    "aborted": "aborted",
    "aborted_after_finish": "aborted",
    "cancelled": "aborted",
    "canceled": "aborted",
    "redoing": "redoing",
    "blocked": "blocked",
    "escalated": "escalated",
    "merged": "completed",
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
