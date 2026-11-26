# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's settings replay retains capacity for its async delivery task."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from lionhub.orchestration.scheduler import terminal_reconciliation as terminal
from lionhub.state.db import StateDB
from lionhub.state.lifecycle import notify_settings, settings_delivery
from lionhub.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionhub.state.lifecycle.deliveries import is_acknowledged
from lionhub.state.lifecycle.reconciler import TerminalReconciler


@pytest.mark.asyncio
async def test_studio_replay_timeout_cancels_but_keeps_async_task_reserved(tmp_path, monkeypatch):
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, tasks = [], []

    async def handler(envelope):
        calls.append(envelope.event_id)
        tasks.append(asyncio.current_task())
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return None

    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": "test:handler"}}}},
    )
    monkeypatch.setattr(notify_settings, "_make_python_handler", lambda _ref: handler)

    async with StateDB(tmp_path / "studio.db") as db:
        session, progression = str(uuid.uuid4()), str(uuid.uuid4())
        await db.create_progression(progression)
        await db.create_session({"id": session, "progression_id": progression, "status": "running"})
        db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
        assert await db.update_status(
            "session", session, new_status="failed", reason_code="session.stale.no_heartbeat"
        )
        row = await db.fetch_one(
            "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
            ("session", session, "failed"),
        )
        transition = row["id"]
        first = TerminalReconciler(
            terminal.CONSUMER_NAME,
            terminal.prepare_global_settings_delivery,
            batch_size=1,
            timeout=0.05,
        )
        second = TerminalReconciler(
            terminal.CONSUMER_NAME,
            terminal.prepare_global_settings_delivery,
            batch_size=1,
            timeout=0.05,
        )
        try:
            timed_out = await first.run_pass(db)
            assert timed_out["attempted"] == timed_out["failed"] == 1
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.wait_for(cancelled.wait(), timeout=2)
            assert calls == [transition]
            blocked = await second.run_pass(db)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            assert not await is_acknowledged(db, transition, terminal.CONSUMER_NAME)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)

            async def wait_for_ack():
                while not await is_acknowledged(db, transition, terminal.CONSUMER_NAME):
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(wait_for_ack(), timeout=2)
            assert calls == [transition]
            assert (await second.run_pass(db))["attempted"] == 0
        finally:
            release.set()
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
