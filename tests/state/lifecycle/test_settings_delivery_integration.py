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

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import notify_settings
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import is_acknowledged, reconcile_unacknowledged
from lionagi.state.lifecycle.reconciler import TerminalReconciler
from lionagi.studio.services import terminal_reconciliation

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

    def load_settings(*, include_project):
        calls.append(include_project)
        return {"notify": {"on_terminal": {"enabled": True, "adapter": adapter}}}

    monkeypatch.setattr(terminal_reconciliation, "load_settings", load_settings)
    return calls


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
    monkeypatch.setattr(notify_settings, "observe_process_group_leader", lambda *a, **k: None)
    monkeypatch.setattr(notify_settings, "aterminate_process_group", terminate)
    async with StateDB(tmp_path / "state.db") as db:
        transition_id = await _seed(db)
        clock = [100.0]
        reconciler = TerminalReconciler(
            CONSUMER,
            terminal_reconciliation._prepare_settings_delivery,
            batch_size=1,
            clock=lambda: clock[0],
        )
        await _failure_then_retry(db, reconciler, transition_id, clock, events)
    assert launches == [("fake-test-adapter",), ("fake-test-adapter",)]
    assert calls == [False, False]
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
            terminal_reconciliation._prepare_settings_delivery,
            batch_size=1,
            clock=lambda: clock[0],
        )
        await _failure_then_retry(db, reconciler, transition_id, clock, events)
    assert events == [transition_id, transition_id]
    assert calls == [False, False]


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
            CONSUMER, terminal_reconciliation._prepare_settings_delivery, batch_size=1
        )
        result = await reconciler.run_pass(db)
        assert result["attempted"] == result["acknowledged"] == 1
        assert result["failed"] == 0
        assert await is_acknowledged(db, transition_id, CONSUMER)
        assert await reconcile_unacknowledged(db, CONSUMER) == []
        assert events == [transition_id]
        assert calls == [False]
