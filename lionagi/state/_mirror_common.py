# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared write-side helpers for the transcript mirrors (Claude Code, Codex).

Both mirrors tail an external tool's transcript into StateDB, so status
reconciliation and lineage linking are identical once the session id is known.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import StateDB

__all__ = ("reconcile_status", "link_lineage")


async def reconcile_status(
    db: StateDB,
    sid: str,
    *,
    now: float,
    live_window: float,
    actor: str,
) -> None:
    """Align a mirrored session's status with its live/idle state, both directions.
    Liveness keys off ``last_message_at``, never ``updated_at`` — see docs/internals/runtime.md."""
    from lionagi.state.db import SESSION_TERMINAL_STATUSES
    from lionagi.state.reasons import RunReasons

    existing = await db.get_session(sid)
    if not existing:
        return
    live = (now - float(existing.get("last_message_at") or 0.0)) <= live_window
    desired = "running" if live else "completed"
    previous = existing.get("status")
    if previous == desired:
        return

    previous_terminal = previous in SESSION_TERMINAL_STATUSES
    if previous_terminal and desired != "running":
        return

    reactivating = previous_terminal and desired == "running"
    await db.update_status(
        "session",
        sid,
        new_status=desired,
        reason_code=RunReasons.STARTED_OK if desired == "running" else RunReasons.COMPLETED_OK,
        reason_summary=(
            "mirror session reactivated because transcript resumed within live_window"
            if reactivating
            else "mirror session became idle"
        ),
        evidence_refs=[{"kind": "session", "id": sid}],
        source="system",
        actor=actor,
        expected_statuses={previous},
        expected_updated_at=existing.get("updated_at"),
        override=reactivating,
        override_actor=actor if reactivating else None,
        override_justification=(
            "mirror session terminal reactivation: transcript resumed within live_window"
            if reactivating
            else None
        ),
    )


async def link_lineage(
    db: StateDB,
    *,
    child_sid: str,
    parent_sid: str,
    parent_uid: str,
    parent_event_uuid: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record that one mirrored session continues another, on the child's node_metadata.
    Idempotent: the lineage entry is rewritten wholesale rather than appended."""
    existing = await db.get_session(child_sid)
    if existing is None:
        return
    meta = dict(existing.get("node_metadata") or {})
    lineage: dict[str, Any] = {
        "parent_session_id": parent_sid,
        "parent_session_uid": parent_uid,
        "parent_event_uuid": parent_event_uuid,
    }
    if extra:
        lineage.update(extra)
    meta["lineage"] = lineage
    await db.set_session_provenance(child_sid, node_metadata=meta)
