# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable adapter logs carry stable reasons rather than configuration or output."""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import subprocess

import pytest

from lionagi.state.lifecycle import notify_settings, settings_delivery
from lionagi.state.lifecycle.reconciler import DeliveryOutcome
from lionagi.studio.services.terminal_reconciliation import prepare_global_settings_delivery


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline test forbids processes, signals and networking")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, forbidden)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, forbidden, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def row():
    return {
        "transition_id": "event-1",
        "entity_type": "session",
        "entity_id": "session-1",
        "previous_status": "running",
        "terminal_status": "failed",
        "reason_code": "safe",
        "occurred_at": 1.0,
    }


class Recorder:
    def __init__(self):
        self.records = []
        self.stderr_artifacts = []

    def record(self, scope, *, ok, exit_code, stderr_text, reason):
        if stderr_text:
            self.stderr_artifacts.append(stderr_text)
        self.records.append({"scope": scope, "ok": ok, "exit_code": exit_code, "reason": reason})
        return "owner-only-artifact" if stderr_text else None


@pytest.mark.asyncio
async def test_python_import_failure_logs_only_stable_reason(monkeypatch, caplog):
    recorder = Recorder()
    secret = "SENTINEL_IMPORT_PRIVATE_VALUE"
    ref = "SECRET_MODULE:SENTINEL_PY_REF"
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": ref}}}},
    )

    def failed_import(_ref):
        raise ImportError(secret)

    monkeypatch.setattr(notify_settings, "_make_python_handler", failed_import)
    prepared = await prepare_global_settings_delivery(row(), None, recorder=recorder)
    with caplog.at_level("WARNING"):
        assert prepared.call() is DeliveryOutcome.FAILURE
    assert "python_adapter_unresolvable" in caplog.text
    assert secret not in caplog.text and ref not in caplog.text
    assert recorder.records == [
        {"scope": "global", "ok": False, "exit_code": None, "reason": "python_adapter_unresolvable"}
    ]
    assert secret not in repr(recorder.records) and ref not in repr(recorder.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["launch", "nonzero"])
async def test_exec_failure_logs_no_argv_exception_or_stderr(monkeypatch, caplog, mode):
    argv_secret, stderr_secret = "SENTINEL_ARGV_PRIVATE", "SENTINEL_STDERR_PRIVATE"
    exception_secret = "SENTINEL_EXCEPTION_PRIVATE"
    recorder = Recorder()
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "exec", "argv": [argv_secret]}}}},
    )

    class FakeProcess:
        pid = 42
        returncode = 7

        async def communicate(self, _payload):
            return b"", stderr_secret.encode()

    async def fake_spawn(*_args, **_kwargs):
        if mode == "launch":
            raise OSError(exception_secret)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(notify_settings, "observe_process_group_leader", lambda *_a, **_k: None)
    prepared = await prepare_global_settings_delivery(row(), None, recorder=recorder)
    with caplog.at_level("DEBUG"):
        result = prepared.call()
        assert inspect.isawaitable(result)
        assert await result is DeliveryOutcome.FAILURE
    reason = "exec_adapter_launch_failed" if mode == "launch" else "exec_adapter_nonzero_exit"
    assert reason in caplog.text
    for secret in (argv_secret, stderr_secret, exception_secret):
        assert secret not in caplog.text and secret not in repr(recorder.records)
    if mode == "nonzero":
        assert recorder.stderr_artifacts == [stderr_secret]


def test_legacy_python_import_logging_is_unchanged(monkeypatch, caplog):
    ref, secret = "legacy:handler", "legacy-import-detail"

    def failed_import(_ref):
        raise ImportError(secret)

    monkeypatch.setattr(notify_settings, "_make_python_handler", failed_import)
    with caplog.at_level("WARNING"):
        assert (
            notify_settings.build_handler(notify_settings.ResolvedNotifyHandler(python_ref=ref))
            is None
        )
    assert ref in caplog.text and secret in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_python_execution_exception_has_no_payload_in_durable_log(
    monkeypatch, caplog, is_async
):
    secret = "SENTINEL_EXECUTION_PRIVATE"
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": "fake:hook"}}}},
    )

    def sync_handler(_envelope):
        raise RuntimeError(secret)

    async def async_handler(_envelope):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        notify_settings,
        "_make_python_handler",
        lambda _ref: async_handler if is_async else sync_handler,
    )
    recorder = Recorder()
    prepared = await prepare_global_settings_delivery(row(), None, recorder=recorder)
    with caplog.at_level("WARNING"):
        value = prepared.call()
        result = await value if inspect.isawaitable(value) else value
    assert result is DeliveryOutcome.FAILURE
    assert "python_adapter_execution_failed" in caplog.text and secret not in caplog.text
    assert recorder.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_exec_cleanup_error_preserves_primary_and_never_acknowledges(
    tmp_path, monkeypatch, caplog, mode
):
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle import reconciler as reconciliation
    from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
    from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer

    entered, cleanup_entered, cleanup_release, observed = (asyncio.Event() for _ in range(4))
    observed_cancelled = []
    recorder = Recorder()
    cleanup_secret = "SENTINEL_CLEANUP_PRIVATE"

    class FakeProcess:
        pid = 42
        returncode = None

        async def communicate(self, _payload):
            entered.set()
            await asyncio.Event().wait()

    async def fake_spawn(*_args, **_kwargs):
        return FakeProcess()

    async def fake_terminate(*_args, **_kwargs):
        cleanup_entered.set()
        await cleanup_release.wait()
        raise OSError(cleanup_secret)

    original_observe = reconciliation._observe_cancelled_hook

    def observe(task):
        observed_cancelled.append(task.cancelled())
        original_observe(task)
        observed.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(notify_settings, "observe_process_group_leader", lambda *_a, **_k: None)
    monkeypatch.setattr(notify_settings, "aterminate_process_group", fake_terminate)
    monkeypatch.setattr(
        notify_settings, "HANDLER_BUDGET_SECONDS", 0.05 if mode == "timeout" else 10
    )
    monkeypatch.setattr(reconciliation, "_observe_cancelled_hook", observe)
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "exec", "argv": ["fake"]}}}},
    )

    async def prepare(row, db):
        return await prepare_global_settings_delivery(row, db, recorder=recorder)

    async with StateDB(tmp_path / "state.db") as db:
        await register_consumer(db, "safe-global", start="all")
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
        reconciler = reconciliation.TerminalReconciler("safe-global", prepare, start="all")
        task = asyncio.create_task(reconciler.run_pass(db))
        try:
            with caplog.at_level("WARNING"):
                await asyncio.wait_for(entered.wait(), 1)
                if mode == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                await asyncio.wait_for(cleanup_entered.wait(), 1)
                cleanup_release.set()
                if mode == "cancel":
                    await asyncio.wait_for(observed.wait(), 1)
                    assert observed_cancelled == [True] and recorder.records == []
                else:
                    stats = await asyncio.wait_for(task, 1)
                    assert stats["failed"] == 1 and stats["acknowledged"] == 0
                    assert recorder.records == [
                        {
                            "scope": "global",
                            "ok": False,
                            "exit_code": None,
                            "reason": "exec_adapter_timeout",
                        }
                    ]
                assert cleanup_secret not in caplog.text
                assert "exec_cleanup_unconfirmed" in caplog.text
                assert not await is_acknowledged(db, row["id"], "safe-global")
        finally:
            cleanup_release.set()
            await asyncio.gather(task, return_exceptions=True)
