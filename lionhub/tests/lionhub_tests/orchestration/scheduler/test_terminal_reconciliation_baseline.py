# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's consumer baselines history at first registration and drains owed rows within its budget."""

from __future__ import annotations

import os
import sys
import time
import uuid
from types import ModuleType

import pytest

import lionagi.state.db as state_db_mod
import lionhub.orchestration.scheduler.settings as scheduler_settings
from lionagi.state.db import StateDB
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import is_acknowledged, reconcile_unacknowledged
from lionhub.orchestration.scheduler import terminal_reconciliation as terminal


async def _seed(db: StateDB, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session({"id": sid, "progression_id": pid, "status": "running"})
        assert await db.update_status(
            "session", sid, new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
            ("session", sid, "failed"),
        )
        ids.append(row["id"])
    return ids


@pytest.mark.parametrize(("budget", "drained"), [(0.0, 4), (60.0, 6)], ids=["one-page", "budgeted"])
@pytest.mark.asyncio
async def test_first_pass_baselines_history_and_later_rows_drain_by_budget(
    tmp_path, monkeypatch, budget, drained
):
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    store = tmp_path / "state.db"
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": f"sqlite:///{store}"}),
    )
    monkeypatch.setattr(scheduler_settings, "TERMINAL_RECONCILE_ENABLED", True)
    monkeypatch.setattr(scheduler_settings, "TERMINAL_RECONCILE_BUDGET_SECONDS", budget)
    monkeypatch.setattr(terminal, "_reconciler", None)
    monkeypatch.setattr(terminal, "_reconciler_url", None)

    async with StateDB(store) as db:
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        history = await _seed(db, 6)

    # No notifier is configured, so a replayed row would be acknowledged as not applicable;
    # a baselined row is acknowledged without being attempted at all.
    assert await terminal.run_terminal_reconciliation() == 0
    async with StateDB(store) as db:
        for transition_id in history:
            assert await is_acknowledged(db, transition_id, terminal.CONSUMER_NAME)
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        later = await _seed(db, 6)

    assert await terminal.run_terminal_reconciliation() == drained
    async with StateDB(store) as db:
        acked = [await is_acknowledged(db, t, terminal.CONSUMER_NAME) for t in later]
    assert sum(acked) == drained


@pytest.mark.asyncio
async def test_replay_is_off_by_default_and_the_push_still_runs(tmp_path, monkeypatch):
    assert os.environ.get("LIONAGI_STUDIO_TERMINAL_RECONCILE") is None
    assert scheduler_settings.TERMINAL_RECONCILE_ENABLED is False
    store = tmp_path / "state.db"
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": f"sqlite:///{store}"}),
    )
    monkeypatch.setattr(terminal, "_reconciler", None)
    monkeypatch.setattr(terminal, "_reconciler_url", None)
    passes = []

    async def run_pass(self, db):
        passes.append(self.consumer)
        return {"acknowledged": 0}

    monkeypatch.setattr(terminal.TerminalReconciler, "run_pass", run_pass)
    pushed = []
    async with StateDB(store) as db:
        callbacks = TerminalCallbackRegistry()
        callbacks.register("push", lambda envelope: pushed.append(envelope.event_id))
        db._lifecycle_service()._terminal_callbacks = callbacks
        history = await _seed(db, 2)

    assert pushed == history
    assert await terminal.run_terminal_reconciliation() == 0
    assert passes == []
    assert terminal._reconciler is None
    async with StateDB(store) as db:
        consumers = await db.fetch_all("SELECT * FROM terminal_consumers")
    assert consumers == []


@pytest.mark.parametrize(
    ("projects", "succeeds", "diagnostic"),
    [
        pytest.param((), True, "context_no_session", id="no-session-success"),
        pytest.param((), False, "context_no_session", id="no-session-failure"),
        pytest.param(("alpha", "beta"), True, "context_ambiguous_project", id="ambiguous-success"),
        pytest.param(("alpha", "beta"), False, "context_ambiguous_project", id="ambiguous-failure"),
        pytest.param(("alpha",), True, None, id="resolved-success"),
        pytest.param(("alpha",), False, None, id="resolved-failure"),
        pytest.param((None,), True, "context_null_project", id="null-project-success"),
        pytest.param(("alpha",), True, "context_read_failure", id="context-read-failure"),
    ],
)
@pytest.mark.asyncio
async def test_replay_delivers_global_notification_independently_of_context(
    tmp_path, monkeypatch, caplog, projects, succeeds, diagnostic
):
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / "settings.yaml").write_text(
        "notify:\n  on_terminal:\n    enabled: true\n    adapter:\n"
        "      kind: python\n      ref: terminal_replay_test_adapter:handle\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    store = tmp_path / "state.db"
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": f"sqlite:///{store}"}),
    )
    monkeypatch.setattr(scheduler_settings, "TERMINAL_RECONCILE_ENABLED", True)
    monkeypatch.setattr(scheduler_settings, "TERMINAL_RECONCILE_BUDGET_SECONDS", 5.0)
    monkeypatch.setattr(terminal, "_reconciler", None)
    monkeypatch.setattr(terminal, "_reconciler_url", None)

    seen = []
    adapter = ModuleType("terminal_replay_test_adapter")

    def handle(envelope):
        seen.append(envelope.event_id)
        return succeeds

    adapter.handle = handle
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)

    # Open the store and enroll before writing the eligible transition.
    async with StateDB(store):
        pass
    assert await terminal.run_terminal_reconciliation() == 0

    invocation_id = str(uuid.uuid4())
    async with StateDB(store) as db:
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        await db.create_invocation(
            {"id": invocation_id, "skill": "example", "started_at": time.time()}
        )
        for project in projects:
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": str(uuid.uuid4()),
                    "progression_id": progression_id,
                    "project": project,
                    "invocation_id": invocation_id,
                    "status": "running",
                }
            )
        assert await db.update_status(
            "invocation",
            invocation_id,
            new_status="failed",
            reason_code="run.failed.exception",
            source="system",
            actor="test",
        )
        row = await db.fetch_one(
            "SELECT id, delivery_eligible FROM status_transitions "
            "WHERE entity_type = 'invocation' AND entity_id = :id AND status = 'failed'",
            {"id": invocation_id},
        )
        assert row is not None
        assert row["delivery_eligible"] == 1
        event_id = row["id"]
        owed = await reconcile_unacknowledged(db, terminal.CONSUMER_NAME)
        assert [row["transition_id"] for row in owed] == [event_id]

    ack_at_context_read = []
    if diagnostic == "context_read_failure":
        fetch_one = StateDB.fetch_one

        async def fail_context_read(db, sql, params=None):
            if "COUNT(DISTINCT project)" in sql and params == {"id": invocation_id}:
                ack_at_context_read.append(
                    await is_acknowledged(db, event_id, terminal.CONSUMER_NAME)
                )
                raise OSError("could not read project context")
            return await fetch_one(db, sql, params)

        monkeypatch.setattr(StateDB, "fetch_one", fail_context_read)

    caplog.clear()
    with caplog.at_level("INFO", logger=terminal.logger.name):
        acknowledged = await terminal.run_terminal_reconciliation()

    assert seen == [event_id]
    assert acknowledged == int(succeeds)
    async with StateDB(store) as db:
        assert await is_acknowledged(db, event_id, terminal.CONSUMER_NAME) is succeeds
        owed = await reconcile_unacknowledged(db, terminal.CONSUMER_NAME)
        assert [row["transition_id"] for row in owed] == ([] if succeeds else [event_id])

    if diagnostic == "context_read_failure":
        assert ack_at_context_read == [True]

    summary = _logged_summary(caplog)
    assert summary["attempted"] == 1
    assert summary["failed"] == int(not succeeds)
    assert summary["not_applicable"] == 0
    for label in (
        "context_no_session",
        "context_ambiguous_project",
        "context_null_project",
        "context_read_failure",
        "context_budget_exhausted",
    ):
        assert summary[label] == int(label == diagnostic)


def _logged_summary(caplog):
    (summary,) = [
        record.args
        for record in caplog.records
        if record.name == terminal.logger.name
        and record.msg == "Terminal notification reconciliation: %s"
    ]
    return summary


@pytest.mark.asyncio
async def test_replay_context_summary_preserves_reconciler_outcomes(tmp_path, monkeypatch, caplog):
    store = tmp_path / "state.db"
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": f"sqlite:///{store}"}),
    )
    monkeypatch.setattr(scheduler_settings, "TERMINAL_RECONCILE_ENABLED", True)
    monkeypatch.setattr(terminal, "_reconciler", None)
    monkeypatch.setattr(terminal, "_reconciler_url", None)
    async with StateDB(store):
        pass

    async def run_pass(self, db, *, observe_outcome):
        row = {"entity_type": "session", "entity_id": "missing"}
        await observe_outcome(row, db, 1)
        await observe_outcome(row, db, 0)
        return {"no_session": 3, "ambiguous_project": 4, "acknowledged": 1}

    monkeypatch.setattr(terminal.TerminalReconciler, "run_pass", run_pass)
    with caplog.at_level("INFO", logger=terminal.logger.name):
        assert await terminal.run_terminal_reconciliation() == 1

    assert _logged_summary(caplog) == {
        "acknowledged": 1,
        "no_session": 3,
        "ambiguous_project": 4,
        "context_no_session": 1,
        "context_ambiguous_project": 0,
        "context_null_project": 0,
        "context_read_failure": 0,
        "context_budget_exhausted": 1,
    }
