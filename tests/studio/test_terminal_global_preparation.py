# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Global delivery preparation keeps context diagnostic-only."""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import subprocess

import pytest

from lionagi.state.lifecycle import settings_delivery
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery
from lionagi.studio.services import terminal_reconciliation


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline settings tests forbid process, signal, and network calls")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


class FakeDB:
    def __init__(self, scenario):
        self.scenario = scenario

    async def get_session(self, _id):
        if self.scenario == "read_failure":
            raise OSError("fake context read failure")
        if self.scenario == "no_session":
            return None
        return {"project": None if self.scenario == "null_project" else "one"}

    async def get_schedule_run(self, _id):
        return {"invocation_id": None}

    async def get_play(self, _id):
        return {"session_id": None}

    async def fetch_one(self, _query, _params):
        if self.scenario == "ambiguous_project":
            return {"total": 2, "present": 2, "distinct_projects": 2}
        if self.scenario == "no_session":
            return {"total": 0, "present": 0, "distinct_projects": 0}
        return {"total": 1, "present": 0, "distinct_projects": 0}


def _row(kind):
    return {
        "transition_id": "event-1",
        "entity_type": kind,
        "entity_id": "entity-1",
        "previous_status": "running",
        "terminal_status": "completed",
        "reason_code": "session.done",
        "occurred_at": 1.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "scenario", "expected"),
    [
        ("session", "no_session", "no_session"),
        ("session", "null_project", "null_project"),
        ("session", "read_failure", "context_read_failure"),
        ("invocation", "no_session", "no_session"),
        ("invocation", "null_project", "null_project"),
        ("invocation", "ambiguous_project", "ambiguous_project"),
        ("schedule_run", "no_session", "no_session"),
        ("play", "no_session", "no_session"),
    ],
)
async def test_missing_or_unreadable_context_does_not_suppress_global(
    monkeypatch, kind, scenario, expected
):
    seen = []
    labels = []
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "exec", "argv": ["fake"]}}}},
    )

    def fake_build(_resolved, *, outcome_fn=None, **_kwargs):
        async def handler(envelope):
            seen.append((envelope.event_id, envelope.entity.kind, envelope.entity.id))
            outcome_fn(ok=True, exit_code=0, stderr_text=None)

        return handler

    monkeypatch.setattr(settings_delivery, "build_handler", fake_build)
    prepared = await terminal_reconciliation.prepare_global_settings_delivery(
        _row(kind), FakeDB(scenario)
    )
    assert isinstance(prepared, PreparedDelivery)
    assert labels == []
    assert seen == []
    value = prepared.call()
    outcome = await value if inspect.isawaitable(value) else value
    assert outcome is DeliveryOutcome.SUCCESS
    assert seen == [("event-1", kind, "entity-1")]
    await terminal_reconciliation.observe_global_context_after_ack(
        _row(kind), FakeDB(scenario), labels.append, remaining_seconds=1
    )
    assert labels == [expected]


@pytest.mark.asyncio
async def test_context_observer_failure_does_not_block_global_delivery(monkeypatch):
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "exec", "argv": ["fake"]}}}},
    )
    seen = []

    def fake_build(_resolved, *, outcome_fn=None, **_kwargs):
        async def handler(envelope):
            seen.append(envelope.event_id)
            outcome_fn(ok=True, exit_code=0, stderr_text=None)

        return handler

    def broken_observer(_label):
        raise RuntimeError("observer must not block delivery")

    monkeypatch.setattr(settings_delivery, "build_handler", fake_build)
    prepared = await terminal_reconciliation.prepare_global_settings_delivery(
        _row("session"), FakeDB("no_session")
    )
    result = prepared.call()
    assert (await result if inspect.isawaitable(result) else result) is DeliveryOutcome.SUCCESS
    assert seen == ["event-1"]
    await terminal_reconciliation.observe_global_context_after_ack(
        _row("session"), FakeDB("no_session"), broken_observer, remaining_seconds=1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "expected_outcome"),
    [
        ({"notify": "bad"}, "failure"),
        ({"notify": {"on_terminal": ["bad"]}}, "failure"),
        ({"notify": {"on_terminal": {"enabled": False, "adapter": "bad"}}}, "not_applicable"),
    ],
    ids=["bad-notify-container", "bad-on-terminal", "disabled-wins"],
)
async def test_global_replay_validation_controls_real_ledger(
    tmp_path, monkeypatch, settings, expected_outcome
):
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
    from lionagi.state.lifecycle.deliveries import (
        is_acknowledged,
        reconcile_unacknowledged,
        register_consumer,
    )
    from lionagi.state.lifecycle.reconciler import TerminalReconciler

    monkeypatch.setattr(settings_delivery, "load_global_settings_layer", lambda: settings)
    async with StateDB(tmp_path / "state.db") as db:
        await register_consumer(db, "settings-test", start="all")
        await db.create_progression("progression")
        await db.create_session(
            {"id": "session", "progression_id": "progression", "status": "running"}
        )
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        assert await db.update_status(
            "session", "session", new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type=:kind AND entity_id=:id AND status=:status",
            {"kind": "session", "id": "session", "status": "failed"},
        )
        transition_id = row["id"]
        reconciler = TerminalReconciler(
            "settings-test", terminal_reconciliation.prepare_global_settings_delivery, start="all"
        )
        result = await reconciler.run_pass(db)
        assert result["attempted"] == 1
        acked = await is_acknowledged(db, transition_id, "settings-test")
        pending = await reconcile_unacknowledged(db, "settings-test")
        if expected_outcome == "failure":
            assert result["failed"] == 1 and result["acknowledged"] == 0
            assert not acked and [item["transition_id"] for item in pending] == [transition_id]
        else:
            assert result["not_applicable"] == 1 and result["acknowledged"] == 1
            assert acked and pending == []
        assert (await db.get_session("session"))["status"] == "failed"


@pytest.mark.asyncio
async def test_invalid_global_then_fixed_settings_retry_acks_same_event(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
    from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer
    from lionagi.state.lifecycle.reconciler import TerminalReconciler

    current = [{"notify": "bad"}]
    seen = []
    clock = [100.0]
    monkeypatch.setattr(settings_delivery, "load_global_settings_layer", lambda: current[0])

    def fake_build(_resolved, *, outcome_fn=None, **_kwargs):
        async def handler(envelope):
            seen.append(envelope.event_id)
            outcome_fn(ok=True, exit_code=0, stderr_text=None)

        return handler

    monkeypatch.setattr(settings_delivery, "build_handler", fake_build)
    async with StateDB(tmp_path / "state.db") as db:
        await register_consumer(db, "settings-test", start="all")
        await db.create_progression("progression")
        await db.create_session(
            {"id": "session", "progression_id": "progression", "status": "running"}
        )
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        assert await db.update_status(
            "session", "session", new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type=:kind AND entity_id=:id AND status=:status",
            {"kind": "session", "id": "session", "status": "failed"},
        )
        transition_id = row["id"]
        reconciler = TerminalReconciler(
            "settings-test",
            terminal_reconciliation.prepare_global_settings_delivery,
            start="all",
            batch_size=1,
            clock=lambda: clock[0],
        )
        failed = await reconciler.run_pass(db)
        assert failed["failed"] == 1 and not await is_acknowledged(
            db, transition_id, "settings-test"
        )
        current[0] = {"notify": {"on_terminal": {"adapter": {"kind": "exec", "argv": ["fake"]}}}}
        clock[0] += 2
        assert (await reconciler.run_pass(db))["attempted"] == 0
        retried = await reconciler.run_pass(db)
        assert retried["attempted"] == retried["acknowledged"] == 1
        assert seen == [transition_id]
        assert await is_acknowledged(db, transition_id, "settings-test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "should_ack"),
    [("null\n", False), ("~\n", False), ("# empty settings\n", True), ("{}\n", True)],
    ids=["explicit-null", "explicit-tilde", "empty-document", "empty-mapping"],
)
async def test_real_global_yaml_root_controls_ack(tmp_path, monkeypatch, source, should_ack):
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
    from lionagi.state.lifecycle.deliveries import (
        is_acknowledged,
        reconcile_unacknowledged,
        register_consumer,
    )
    from lionagi.state.lifecycle.reconciler import TerminalReconciler

    home = tmp_path / "home"
    settings_dir = home / ".lionagi"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.yaml").write_text(source)
    monkeypatch.setenv("HOME", str(home))
    async with StateDB(tmp_path / "state.db") as db:
        await register_consumer(db, "settings-test", start="all")
        await db.create_progression("progression")
        await db.create_session(
            {"id": "session", "progression_id": "progression", "status": "running"}
        )
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        assert await db.update_status(
            "session", "session", new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type=:kind AND entity_id=:id AND status=:status",
            {"kind": "session", "id": "session", "status": "failed"},
        )
        event_id = row["id"]
        reconciler = TerminalReconciler(
            "settings-test", terminal_reconciliation.prepare_global_settings_delivery, start="all"
        )
        summary = await reconciler.run_pass(db)
        assert summary["attempted"] == 1
        assert await is_acknowledged(db, event_id, "settings-test") is should_ack
        pending = await reconcile_unacknowledged(db, "settings-test")
        assert [item["transition_id"] for item in pending] == ([] if should_ack else [event_id])
