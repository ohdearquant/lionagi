# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's durable settings-notification consumer."""

from __future__ import annotations

import logging
from typing import Any

from lionagi._settings import load_settings
from lionagi.state.db import StateDB, state_db_known_absent
from lionagi.state.lifecycle.callbacks import Correlation, EntityRef, RunTerminalEnvelope
from lionagi.state.lifecycle.notify_settings import resolve_notify_config
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler
from lionagi.state.lifecycle.settings_delivery import prepare_resolved_delivery

logger = logging.getLogger(__name__)
CONSUMER_NAME = "notify.settings.on_terminal"
_reconciler: TerminalReconciler | None = None
_reconciler_url: str | None = None


async def _project_for_transition(
    row: dict[str, Any], db: StateDB
) -> tuple[str | None, str | None]:
    """Return a project and an inapplicability reason, without choosing an arbitrary session."""
    kind, ident = row["entity_type"], row["entity_id"]
    if kind == "session":
        session = await db.get_session(ident)
        return (session.get("project"), None) if session else (None, "no_session")
    if kind == "play":
        play = await db.get_play(ident)
        session_id = play.get("session_id") if play else None
        session = await db.get_session(session_id) if session_id else None
        return (session.get("project"), None) if session else (None, "no_session")
    if kind == "schedule_run":
        run = await db.get_schedule_run(ident)
        ident = run.get("invocation_id") if run else None
    elif kind != "invocation":
        return None, "no_session"
    if not ident:
        return None, "no_session"
    projects = await db.fetch_all(
        "SELECT DISTINCT project FROM sessions "
        "WHERE invocation_id = :id AND project IS NOT NULL LIMIT 2",
        {"id": ident},
    )
    if not projects:
        return None, "no_session"
    if len(projects) > 1:
        return None, "ambiguous_project"
    return projects[0]["project"], None


async def _prepare_settings_delivery(
    row: dict[str, Any], db: StateDB
) -> PreparedDelivery | DeliveryOutcome:
    _, reason = await _project_for_transition(row, db)
    if reason == "no_session":
        return DeliveryOutcome.NO_SESSION
    if reason == "ambiguous_project":
        return DeliveryOutcome.AMBIGUOUS_PROJECT

    kind, ident = row["entity_type"], row["entity_id"]
    correlations = {
        "session": Correlation(session_id=ident),
        "invocation": Correlation(invocation_id=ident),
        "schedule_run": Correlation(schedule_run_id=ident),
    }
    envelope = RunTerminalEnvelope(
        event_id=row["transition_id"],
        entity=EntityRef(kind=kind, id=ident),
        previous_status=row["previous_status"],
        terminal_status=row["terminal_status"],
        reason_code=row["reason_code"],
        occurred_at=float(row["occurred_at"]),
        correlation=correlations.get(kind, Correlation()),
    )

    def deliver_from_current_settings() -> Any:
        # File access and configured-module imports can block; the reconciler
        # tracks this synchronous work through timeout and late completion.
        try:
            settings = load_settings(include_project=False)
        except Exception:
            logger.exception("Cannot load terminal notification settings")
            return DeliveryOutcome.FAILURE
        resolution = resolve_notify_config(settings=settings)
        if resolution.reason is not None:
            return DeliveryOutcome.FAILURE
        if resolution.handler is None:
            return DeliveryOutcome.NOT_APPLICABLE
        prepared = prepare_resolved_delivery(resolution.handler, envelope)
        return prepared.call() if isinstance(prepared, PreparedDelivery) else prepared

    return PreparedDelivery(deliver_from_current_settings)


async def run_terminal_reconciliation() -> int:
    """Run one bounded pass against the daemon's store and log its summary."""
    global _reconciler, _reconciler_url
    if state_db_known_absent():
        return 0
    async with StateDB() as db:
        if _reconciler is None or _reconciler_url != db.url:
            _reconciler = TerminalReconciler(CONSUMER_NAME, _prepare_settings_delivery)
            _reconciler_url = db.url
        summary = await _reconciler.run_pass(db)
    logger.info("Terminal notification reconciliation: %s", summary)
    return summary["acknowledged"]
