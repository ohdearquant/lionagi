# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's durable settings-notification consumer."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from anyio import move_on_after

from lionhub.state.db import StateDB, state_db_known_absent
from lionhub.state.lifecycle.callbacks import Correlation, EntityRef, RunTerminalEnvelope
from lionhub.state.lifecycle.notify_settings import DurableRunRecorder
from lionhub.state.lifecycle.reconciler import PreparedDelivery, TerminalReconciler
from lionhub.state.lifecycle.settings_delivery import prepare_current_global_delivery

logger = logging.getLogger("lionagi.studio.services.terminal_reconciliation")
CONSUMER_NAME = "notify.settings.on_terminal"
_CONTEXT_SUMMARY_KEYS = {
    "no_session": "context_no_session",
    "ambiguous_project": "context_ambiguous_project",
    "null_project": "context_null_project",
    "context_read_failure": "context_read_failure",
    "context_budget_exhausted": "context_budget_exhausted",
}
_reconciler: TerminalReconciler | None = None
_reconciler_url: str | None = None


async def prepare_global_settings_delivery(
    row: dict[str, Any],
    db: StateDB,
    *,
    recorder: DurableRunRecorder | None = None,
) -> PreparedDelivery:
    """Prepare global delivery independently of optional project context.

    The caller owns admission and ACKs.
    """
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
    return prepare_current_global_delivery(envelope, recorder=recorder)


async def observe_global_context_after_ack(
    row: dict[str, Any],
    db: StateDB,
    observe_context: Callable[[str], None],
    *,
    remaining_seconds: float,
) -> None:
    """Best-effort diagnostics after an ACK decision, within the caller's budget.

    The integrator must call this inline after deciding the ACK, using its
    remaining deadline. No task is detached. Cancellation is cooperative: a
    cancellation-resistant lookup or blocking observer can exceed the budget,
    so the caller must retain ownership until this function returns. Expiry or
    no remaining budget emits ``context_budget_exhausted``. A lookup that
    raises emits ``context_read_failure``. Neither changes the ACK decision.
    """
    diagnostics = ("context_budget_exhausted",)
    if remaining_seconds > 0:
        with move_on_after(remaining_seconds) as scope:
            try:
                diagnostics = await _global_context_diagnostics(row, db)
            except Exception:  # noqa: BLE001 -- diagnostics cannot change the ACK decision
                diagnostics = ("context_read_failure",)
        if scope.cancel_called:
            # Discard even a result returned after suppressing our cancellation.
            diagnostics = ("context_budget_exhausted",)
    for diagnostic in diagnostics:
        try:
            observe_context(diagnostic)
        except Exception:  # noqa: BLE001 -- observer failures are diagnostic only
            logger.warning("Could not record terminal notification context diagnostic")


async def _global_context_diagnostics(row: dict[str, Any], db: StateDB) -> tuple[str, ...]:
    kind, ident = row["entity_type"], row["entity_id"]
    if kind == "play":
        play = await db.get_play(ident)
        ident = play.get("session_id") if play else None
        kind = "session"
    if kind == "session":
        session = await db.get_session(ident) if ident else None
        if session is None:
            return ("no_session",)
        return ("null_project",) if session.get("project") is None else ()
    if kind == "schedule_run":
        run = await db.get_schedule_run(ident)
        ident = run.get("invocation_id") if run else None
    elif kind != "invocation":
        return ("no_session",)
    if not ident:
        return ("no_session",)
    counts = await db.fetch_one(
        "SELECT COUNT(*) AS total, COUNT(project) AS present, "
        "COUNT(DISTINCT project) AS distinct_projects FROM sessions WHERE invocation_id = :id",
        {"id": ident},
    )
    if not counts or counts["total"] == 0:
        return ("no_session",)
    diagnostics = []
    if counts["distinct_projects"] > 1:
        diagnostics.append("ambiguous_project")
    if counts["present"] < counts["total"]:
        diagnostics.append("null_project")
    return tuple(diagnostics)


async def run_terminal_reconciliation() -> int:
    """Run one budgeted pass against the daemon's store and log its summary."""
    global _reconciler, _reconciler_url
    from lionhub.orchestration.scheduler import settings as studio_config

    if not studio_config.TERMINAL_RECONCILE_ENABLED or state_db_known_absent():
        return 0
    context_counts = dict.fromkeys(_CONTEXT_SUMMARY_KEYS.values(), 0)

    def count_context(label: str) -> None:
        context_counts[_CONTEXT_SUMMARY_KEYS[label]] += 1

    async def observe_outcome(row: dict[str, Any], db: StateDB, remaining_seconds: float) -> None:
        await observe_global_context_after_ack(
            row, db, count_context, remaining_seconds=remaining_seconds
        )

    async with StateDB() as db:
        if _reconciler is None or _reconciler_url != db.url:
            from lionhub.orchestration.scheduler.settings import TERMINAL_RECONCILE_BUDGET_SECONDS

            # History that predates the consumer is acknowledged at registration,
            # never replayed into the notifier.
            _reconciler = TerminalReconciler(
                CONSUMER_NAME,
                prepare_global_settings_delivery,
                start="now",
                pass_budget=TERMINAL_RECONCILE_BUDGET_SECONDS,
            )
            _reconciler_url = db.url
        summary = await _reconciler.run_pass(db, observe_outcome=observe_outcome)
    summary.update(context_counts)
    logger.info("Terminal notification reconciliation: %s", summary)
    return summary["acknowledged"]
