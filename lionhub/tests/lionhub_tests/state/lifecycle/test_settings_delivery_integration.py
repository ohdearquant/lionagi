# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Settings adapter outcomes control durable acknowledgements and retry."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid

import pytest

from lionhub.orchestration.scheduler import terminal_reconciliation
from lionhub.state.db import StateDB
from lionhub.state.lifecycle import notify_settings, settings_delivery
from lionhub.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionhub.state.lifecycle.deliveries import is_acknowledged, reconcile_unacknowledged
from lionhub.state.lifecycle.reconciler import TerminalReconciler

pytestmark = pytest.mark.unit
CONSUMER = "settings-integration"


async def _seed(db):
    session_id, progression_id = uuid.uuid4().hex, uuid.uuid4().hex
    await db.create_progression(progression_id)
    await db.create_session(
        {"id": session_id, "progression_id": progression_id, "status": "running"}
    )
    # The durable test starts after an offline transition, without push handlers.
    db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
    assert await db.update_status(
        "session", session_id, new_status="failed", reason_code="session.stale.no_heartbeat"
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type=:kind AND entity_id=:id AND status=:status",
        {"kind": "session", "id": session_id, "status": "failed"},
    )
    assert row is not None
    return row["id"]


def _settings(monkeypatch, adapter):
    calls = []

    def load_global_settings_layer():
        calls.append(adapter)
        return {"notify": {"on_terminal": {"enabled": True, "adapter": adapter}}}

    monkeypatch.setattr(settings_delivery, "load_global_settings_layer", load_global_settings_layer)
    return calls


async def test_exec_replay_of_historical_transition_uses_null_run_context(tmp_path, monkeypatch):
    store = tmp_path / "state.db"
    async with StateDB(store) as db:
        transition_id = await _seed(db)
        transition = await db.fetch_one(
            "SELECT entity_id, created_at, CAST(metadata AS TEXT) AS metadata "
            "FROM status_transitions WHERE id = :id",
            {"id": transition_id},
        )
        assert transition is not None
        assert json.loads(transition["metadata"]) == {}

    adapter = {"kind": "exec", "argv": ["fake-test-adapter"]}
    settings_calls = _settings(monkeypatch, adapter)
    launches = []
    payloads = []

    class FakeProcess:
        pid = None
        returncode = 0

        async def communicate(self, payload):
            payloads.append(json.loads(payload))
            return b"", b""

    async def launch(*argv, **kwargs):
        launches.append(argv)
        return FakeProcess()

    monkeypatch.setattr(notify_settings.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr("lionagi.ln._proc.observe_process_group_leader", lambda *a, **k: None)

    async with StateDB(store) as db:
        reconciler = TerminalReconciler(
            CONSUMER, terminal_reconciliation.prepare_global_settings_delivery
        )
        result = await reconciler.run_pass(db)
        assert result["attempted"] == result["acknowledged"] == 1
        assert result["failed"] == 0
        assert await is_acknowledged(db, transition_id, CONSUMER)
        assert await reconcile_unacknowledged(db, CONSUMER) == []

        restarted = TerminalReconciler(
            CONSUMER, terminal_reconciliation.prepare_global_settings_delivery
        )
        result = await restarted.run_pass(db)
        assert result["attempted"] == result["acknowledged"] == 0

    assert settings_calls == [adapter]
    assert launches == [("fake-test-adapter",)]
    assert payloads == [
        {
            "schema": "lionagi.run-terminal",
            "schema_version": 1,
            "event_id": transition_id,
            "durable": True,
            "entity": {"kind": "session", "id": transition["entity_id"]},
            "previous_status": "running",
            "terminal_status": "failed",
            "status": "failed",
            "reason_code": "session.stale.no_heartbeat",
            "occurred_at": transition["created_at"],
            "correlation": {
                "invocation_id": None,
                "session_id": transition["entity_id"],
                "schedule_run_id": None,
                "run_id": None,
            },
            "artifacts": [],
            "run_id": None,
            "label": None,
            "sender": None,
            "cwd": None,
            "output_dir": None,
        }
    ]


async def _failure_then_retry(db, reconciler, transition_id, clock, seen):
    failed = await reconciler.run_pass(db)
    assert failed["attempted"] == 1 and failed["failed"] == 1
    assert failed["acknowledged"] == 0
    assert not await is_acknowledged(db, transition_id, CONSUMER)
    pending = await reconcile_unacknowledged(db, CONSUMER)
    assert [row["transition_id"] for row in pending] == [transition_id]

    # The cursor must wrap before revisiting the failed event; advance the
    # injected monotonic clock beyond its initial backoff without sleeping.
    clock[0] += 2
    wrapped = await reconciler.run_pass(db)
    assert wrapped["attempted"] == 0
    assert not await is_acknowledged(db, transition_id, CONSUMER)
    retried = await reconciler.run_pass(db)
    assert retried["attempted"] == 1 and retried["acknowledged"] == 1
    assert retried["failed"] == 0
    assert await is_acknowledged(db, transition_id, CONSUMER)
    assert await reconcile_unacknowledged(db, CONSUMER) == []
    assert seen[-1] == transition_id


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "spawn"])
async def test_exec_failure_is_not_acked_and_successful_retry_is_acked(
    tmp_path, monkeypatch, failure
):
    calls = _settings(monkeypatch, {"kind": "exec", "argv": ["fake-test-adapter"]})
    launches, events, terminated = [], [], []

    class FakeProcess:
        pid = None

        def __init__(self, fail):
            self.fail = fail
            self.returncode = 7 if fail == "nonzero" else 0

        async def communicate(self, payload):
            events.append(json.loads(payload)["event_id"])
            if self.fail == "timeout":
                raise asyncio.TimeoutError
            return b"", b"adapter failed" if self.fail == "nonzero" else b""

        async def wait(self):
            return self.returncode

    async def launch(*argv, **kwargs):
        launches.append(argv)
        if len(launches) == 1 and failure == "spawn":
            raise OSError("injected spawn failure")
        return FakeProcess(failure if len(launches) == 1 else None)

    async def terminate(proc, **kwargs):
        terminated.append(proc)

    monkeypatch.setattr(notify_settings.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr("lionagi.ln._proc.observe_process_group_leader", lambda *a, **k: None)
    monkeypatch.setattr(notify_settings, "aterminate_process_group", terminate)
    async with StateDB(tmp_path / "state.db") as db:
        transition_id = await _seed(db)
        clock = [100.0]
        reconciler = TerminalReconciler(
            CONSUMER,
            terminal_reconciliation.prepare_global_settings_delivery,
            batch_size=1,
            clock=lambda: clock[0],
        )
        await _failure_then_retry(db, reconciler, transition_id, clock, events)
    assert launches == [("fake-test-adapter",), ("fake-test-adapter",)]
    assert len(calls) == 2
    assert len(terminated) == (1 if failure == "timeout" else 0)
    assert events == ([transition_id] if failure == "spawn" else [transition_id, transition_id])


@pytest.mark.parametrize("failure", ["exception", "false"])
async def test_python_failure_is_not_acked_and_normal_retry_is_acked(
    tmp_path, monkeypatch, failure
):
    adapter = types.ModuleType("terminal_settings_test_adapter")
    events = []

    async def handle(envelope):
        events.append(envelope.event_id)
        if len(events) == 1:
            if failure == "exception":
                raise RuntimeError("injected hook failure")
            return False
        return None

    adapter.handle = handle
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    calls = _settings(monkeypatch, {"kind": "python", "ref": f"{adapter.__name__}:handle"})
    async with StateDB(tmp_path / "state.db") as db:
        transition_id = await _seed(db)
        clock = [100.0]
        reconciler = TerminalReconciler(
            CONSUMER,
            terminal_reconciliation.prepare_global_settings_delivery,
            batch_size=1,
            clock=lambda: clock[0],
        )
        await _failure_then_retry(db, reconciler, transition_id, clock, events)
    assert events == [transition_id, transition_id]
    assert len(calls) == 2


async def test_python_normal_return_is_acknowledged_on_first_attempt(tmp_path, monkeypatch):
    adapter = types.ModuleType("terminal_settings_success_adapter")
    events = []

    async def handle(envelope):
        events.append(envelope.event_id)
        return None

    adapter.handle = handle
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    calls = _settings(monkeypatch, {"kind": "python", "ref": f"{adapter.__name__}:handle"})
    async with StateDB(tmp_path / "state.db") as db:
        transition_id = await _seed(db)
        reconciler = TerminalReconciler(
            CONSUMER, terminal_reconciliation.prepare_global_settings_delivery, batch_size=1
        )
        result = await reconciler.run_pass(db)
        assert result["attempted"] == result["acknowledged"] == 1
        assert result["failed"] == 0
        assert await is_acknowledged(db, transition_id, CONSUMER)
        assert await reconcile_unacknowledged(db, CONSUMER) == []
        assert events == [transition_id]
        assert len(calls) == 1
