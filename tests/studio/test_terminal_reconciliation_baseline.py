# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's consumer baselines history at first registration and drains owed rows within its budget."""

from __future__ import annotations

import os
import uuid

import pytest

import lionagi.state.db as state_db_mod
import lionagi.studio.config as studio_config
from lionagi.state.db import StateDB
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import is_acknowledged
from lionagi.studio.services import terminal_reconciliation as terminal


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
    monkeypatch.setattr(studio_config, "TERMINAL_RECONCILE_ENABLED", True)
    monkeypatch.setattr(studio_config, "TERMINAL_RECONCILE_BUDGET_SECONDS", budget)
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
    assert studio_config.TERMINAL_RECONCILE_ENABLED is False
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
