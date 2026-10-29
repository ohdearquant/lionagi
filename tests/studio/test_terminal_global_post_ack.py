# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline controls for deferred global context diagnostics and Python rejection."""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import subprocess

import anyio
import pytest

from lionagi.state.lifecycle import notify_settings, settings_delivery
from lionagi.state.lifecycle.reconciler import DeliveryOutcome
from lionagi.studio.services import terminal_reconciliation as service


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline test forbids process, signal and network operations")


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


def _row():
    return {
        "transition_id": "event-1",
        "entity_type": "session",
        "entity_id": "s1",
        "previous_status": "running",
        "terminal_status": "failed",
        "reason_code": "session.stale.no_heartbeat",
        "occurred_at": 1.0,
    }


class Recorder:
    def __init__(self):
        self.records = []

    def record(self, scope, **kwargs):
        self.records.append((scope, kwargs))
        return None


class ContextDB:
    def __init__(self, mode):
        self.mode = mode
        self.calls = 0
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def get_session(self, _identifier):
        self.calls += 1
        self.entered.set()
        if self.mode in ("never", "resistant"):
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if self.mode == "resistant":
                    with anyio.CancelScope(shield=True):
                        await self.release.wait()
                else:
                    raise
        if self.mode == "error":
            raise OSError("private context data")
        return {"project": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["never", "error", "fast"])
async def test_context_lookup_occurs_only_after_delivery_and_ack_decision(monkeypatch, mode):
    db, seen, labels = ContextDB(mode), [], []
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": "fake:notify"}}}},
    )
    monkeypatch.setattr(
        notify_settings, "_make_python_handler", lambda _ref: lambda e: seen.append(e.event_id)
    )
    prepared = await service.prepare_global_settings_delivery(_row(), db)
    assert db.calls == 0
    outcome = prepared.call()
    outcome = await outcome if inspect.isawaitable(outcome) else outcome
    assert outcome is DeliveryOutcome.SUCCESS and seen == ["event-1"] and db.calls == 0
    # Integrator decides ACK before calling the diagnostic seam.
    ack_decision = "acknowledged"
    await service.observe_global_context_after_ack(
        _row(), db, labels.append, remaining_seconds=0.03
    )
    assert ack_decision == "acknowledged" and db.calls == 1
    assert (
        labels
        == {
            "never": ["context_read_failure"],
            "error": ["context_read_failure"],
            "fast": ["null_project"],
        }[mode]
    )
    if mode == "never":
        assert db.cancelled.is_set()


@pytest.mark.asyncio
async def test_cancellation_resistant_diagnostic_remains_owned_until_released():
    db = ContextDB("resistant")
    labels = []
    task = asyncio.create_task(
        service.observe_global_context_after_ack(_row(), db, labels.append, remaining_seconds=0.03)
    )
    try:
        await asyncio.wait_for(db.entered.wait(), 1)
        await asyncio.wait_for(db.cancelled.wait(), 1)
        assert not task.done()
        db.release.set()
        await asyncio.wait_for(task, 1)
        assert labels == ["context_read_failure"]
    finally:
        db.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_cancellation_of_diagnostic_propagates():
    db = ContextDB("never")
    labels = []
    task = asyncio.create_task(
        service.observe_global_context_after_ack(_row(), db, labels.append, remaining_seconds=10)
    )
    await asyncio.wait_for(db.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.cancelled.is_set() and labels == []


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0, -1])
async def test_no_remaining_diagnostic_budget_reports_failure_once_without_read(budget):
    db, labels = ContextDB("never"), []
    await service.observe_global_context_after_ack(
        _row(), db, labels.append, remaining_seconds=budget
    )
    assert db.calls == 0 and labels == ["context_read_failure"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("built", "reason"),
    [("missing", "python_adapter_unresolvable"), ("noncallable", "python_adapter_not_callable")],
)
async def test_python_build_rejection_records_global_and_leaves_ledger_owed(
    tmp_path, monkeypatch, built, reason
):
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
    from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer
    from lionagi.state.lifecycle.reconciler import TerminalReconciler

    def build(_ref):
        if built == "missing":
            raise ImportError("sensitive detail")
        return object()

    recorder = Recorder()
    monkeypatch.setattr(notify_settings, "_make_python_handler", build)
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": "fake:notify"}}}},
    )

    async def prepare(row, db):
        return await service.prepare_global_settings_delivery(row, db, recorder=recorder)

    async with StateDB(tmp_path / "state.db") as db:
        await register_consumer(db, "test-global", start="all")
        await db.create_progression("p1")
        await db.create_session({"id": "s1", "progression_id": "p1", "status": "running"})
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        assert await db.update_status(
            "session", "s1", new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_id = :id AND status = :status",
            {"id": "s1", "status": "failed"},
        )
        result = await TerminalReconciler("test-global", prepare, start="all").run_pass(db)
        assert result["failed"] == 1 and result["acknowledged"] == 0
        assert not await is_acknowledged(db, row["id"], "test-global")
        assert recorder.records == [
            ("global", {"ok": False, "exit_code": None, "stderr_text": None, "reason": reason})
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, True])
async def test_python_execution_outcome_does_not_record_global(monkeypatch, value):
    recorder = Recorder()
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": "fake:notify"}}}},
    )
    monkeypatch.setattr(notify_settings, "_make_python_handler", lambda _ref: lambda _e: value)
    prepared = await service.prepare_global_settings_delivery(
        _row(), ContextDB("never"), recorder=recorder
    )
    outcome = prepared.call()
    assert outcome is (DeliveryOutcome.FAILURE if value is False else DeliveryOutcome.SUCCESS)
    assert recorder.records == []


@pytest.mark.asyncio
async def test_never_returning_diagnostic_follows_real_ledger_ack(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
    from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer
    from lionagi.state.lifecycle.reconciler import TerminalReconciler

    seen = []
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": "fake:notify"}}}},
    )
    monkeypatch.setattr(
        notify_settings, "_make_python_handler", lambda _ref: lambda e: seen.append(e.event_id)
    )
    async with StateDB(tmp_path / "state.db") as db:
        await register_consumer(db, "test-global", start="all")
        await db.create_progression("p1")
        await db.create_session({"id": "s1", "progression_id": "p1", "status": "running"})
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        assert await db.update_status(
            "session", "s1", new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_id = :id AND status = :status",
            {"id": "s1", "status": "failed"},
        )
        event_id = row["id"]
        summary = await TerminalReconciler(
            "test-global", service.prepare_global_settings_delivery, start="all"
        ).run_pass(db)
        assert summary["acknowledged"] == 1 and seen == [event_id]
        assert await is_acknowledged(db, event_id, "test-global")
        context = ContextDB("never")
        diagnostic_row = dict(_row(), transition_id=event_id)
        await service.observe_global_context_after_ack(
            diagnostic_row, context, lambda _label: None, remaining_seconds=0.03
        )
        assert context.cancelled.is_set() and await is_acknowledged(db, event_id, "test-global")
