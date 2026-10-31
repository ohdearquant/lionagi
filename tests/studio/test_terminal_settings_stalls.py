# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Stalled settings and adapter imports remain reserved until late completion."""

from __future__ import annotations

import asyncio
import threading
import types
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import notify_settings
from lionagi.state.lifecycle import reconciler as reconciliation
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import is_acknowledged
from lionagi.state.lifecycle.reconciler import TerminalReconciler
from lionagi.studio.services import terminal_reconciliation as terminal


async def _until(predicate, timeout=5.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def _offline_transition(db):
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression)
    await db.create_session({"id": sid, "progression_id": progression, "status": "running"})
    db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
    assert await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
        source="system",
        actor="test",
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", sid, "failed"),
    )
    assert row is not None
    return row["id"]


@pytest.mark.parametrize("stall", ["settings", "python-import"])
@pytest.mark.asyncio
async def test_settings_and_import_stalls_keep_one_worker_until_late_ack(
    tmp_path, monkeypatch, stall
):
    entered, release = threading.Event(), threading.Event()
    threads = []
    calls = []
    delivered = []

    def block():
        calls.append(stall)
        threads.append(threading.current_thread())
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release stalled worker")

    if stall == "settings":

        def load_settings(**_kwargs):
            block()
            return {}  # No configured notifier is an explicit no-op.

        monkeypatch.setattr(terminal, "load_settings", load_settings)
    else:
        monkeypatch.setattr(
            terminal,
            "load_settings",
            lambda **_kwargs: {
                "notify": {
                    "on_terminal": {
                        "adapter": {"kind": "python", "ref": "test_stall_adapter:handle"}
                    }
                }
            },
        )

        def import_module(name):
            assert name == "test_stall_adapter"
            block()
            return types.SimpleNamespace(handle=lambda event: delivered.append(event.event_id))

        monkeypatch.setattr(notify_settings, "import_module", import_module)

    first_db, second_db = StateDB(tmp_path / "state.db"), StateDB(tmp_path / "state.db")
    await first_db.open()
    await second_db.open()
    first_task = None
    event_id = None
    try:
        event_id = await _offline_transition(first_db)
        first = TerminalReconciler(
            terminal.CONSUMER_NAME, terminal._prepare_settings_delivery, timeout=0.25
        )
        second = TerminalReconciler(
            terminal.CONSUMER_NAME, terminal._prepare_settings_delivery, timeout=0.25
        )
        first_task = asyncio.create_task(first.run_pass(first_db))

        # The loop must continue while the synchronous seam is blocked. A broken
        # inline call eventually raises its safety timeout and fails this check.
        await _until(entered.is_set, timeout=2)
        await asyncio.sleep(0.01)
        assert not release.is_set() and not first_task.done()
        assert not await is_acknowledged(first_db, event_id, terminal.CONSUMER_NAME)

        result = await asyncio.wait_for(first_task, timeout=3)
        assert result["attempted"] == 1 and result["failed"] == 1
        assert result["acknowledged"] == 0
        assert not await is_acknowledged(first_db, event_id, terminal.CONSUMER_NAME)
        another = await asyncio.wait_for(second.run_pass(second_db), timeout=3)
        assert another["attempted"] == 0 and another["acknowledged"] == 0
        assert calls == [stall] and delivered == []
        assert not await is_acknowledged(second_db, event_id, terminal.CONSUMER_NAME)

        release.set()

        async def wait_for_ack():
            while not await is_acknowledged(first_db, event_id, terminal.CONSUMER_NAME):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_ack(), timeout=5)
        rows = await first_db.fetch_all(
            "SELECT transition_id FROM terminal_deliveries WHERE consumer = :consumer",
            {"consumer": terminal.CONSUMER_NAME},
        )
        assert rows == [{"transition_id": event_id}]
        assert calls == [stall]
        assert delivered == ([event_id] if stall == "python-import" else [])
    finally:
        release.set()
        if first_task is not None and not first_task.done():
            await asyncio.wait_for(first_task, timeout=6)
        for thread in threads:
            await asyncio.to_thread(thread.join, 6)
            assert not thread.is_alive()

        # A detached worker's ACK is completed on a separate cleanup thread.
        def no_reservation():
            with reconciliation._workers_lock:
                return not any(
                    key[1:] == (terminal.CONSUMER_NAME, event_id) for key in reconciliation._workers
                )

        await _until(no_reservation, timeout=6)
        await second_db.close()
        await first_db.close()
