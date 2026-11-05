# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Terminal delivery through daemon startup and independent reconcilers."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import is_acknowledged, register_consumer
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler


async def _commit_offline_transition(db: StateDB) -> str:
    session_id, progression_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression_id)
    await db.create_session(
        {"id": session_id, "progression_id": progression_id, "status": "running"}
    )
    # No notification listener is running when this transition commits.
    db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
    applied = await db.update_status(
        "session",
        session_id,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
        source="system",
        actor="test",
    )
    assert applied
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", session_id, "failed"),
    )
    assert row is not None
    return row["id"]


@pytest.mark.asyncio
async def test_daemon_startup_replays_offline_transition_once(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import lionagi.state.db as state_db_module
    from lionagi.cli import _code_identity
    from lionagi.state.lifecycle import notify_settings
    from lionagi.studio.scheduler.engine import scheduler
    from lionagi.studio.services import launches, lifecycle, operator

    app_module = importlib.import_module("lionagi.studio.app")
    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".lionagi"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text(
        "notify:\n  on_terminal:\n    adapter:\n      kind: python\n"
        "      ref: terminal_test_adapter:handle\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(state_db_module, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(
        state_db_module,
        "settings",
        state_db_module.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
    )
    events = []
    adapter = types.ModuleType("terminal_test_adapter")

    async def handle(envelope):
        events.append(envelope.event_id)

    adapter.handle = handle
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)

    async def noop(*_args, **_kwargs):
        return 0

    # Retain the actual lifespan and its startup/periodic reconciliation path;
    # unrelated services do not run or touch the host in this test.
    monkeypatch.setattr(_code_identity, "snapshot_git_position", lambda: None)
    monkeypatch.setattr(notify_settings, "register_settings_terminal_callback", lambda: False)
    monkeypatch.setattr("lionagi.studio.config.TERMINAL_RECONCILE_ENABLED", True)
    monkeypatch.setattr(scheduler, "start", noop)
    monkeypatch.setattr(scheduler, "stop", noop)
    monkeypatch.setattr(operator, "operator_startup", noop)
    monkeypatch.setattr(operator, "operator_shutdown", noop)
    monkeypatch.setattr(launches, "shutdown_launches", noop)
    for name in (
        "reap_phantom_sessions",
        "reap_null_status_sessions",
        "reap_stale_invocations",
        "reap_stale_plays",
        "reap_stale_shows",
        "reap_stale_schedule_runs",
    ):
        monkeypatch.setattr(lifecycle, name, noop)
    monkeypatch.setattr(app_module, "_start_claude_mirror", lambda: (None, None))
    monkeypatch.setattr(app_module, "_stop_claude_mirror", noop)
    monkeypatch.setattr(app_module, "_start_reconnect_channel", lambda: None)
    monkeypatch.setattr(app_module, "_startup_warmup", noop)
    monkeypatch.setattr(app_module, "_finalize_warmup", noop)
    monkeypatch.setattr(app_module, "begin_serving", lambda: None)
    monkeypatch.setattr(app_module, "arm_traceback_dump", lambda: None)
    monkeypatch.setattr(app_module, "disarm_traceback_dump", lambda: None)
    monkeypatch.setattr(app_module, "_emit_startup_warnings", lambda: None)

    # The first startup registers the consumer; a transition that predates it is
    # acknowledged as history, never delivered.
    async with StateDB() as db:
        history_id = await _commit_offline_transition(db)
    async with app_module.lifespan(app_module.create_app()):
        assert events == []
    async with StateDB() as db:
        assert await is_acknowledged(db, history_id, "notify.settings.on_terminal")
        transition_id = await _commit_offline_transition(db)
    assert events == []

    app = app_module.create_app()
    async with app_module.lifespan(app):
        assert events == [transition_id]
        async with StateDB() as db:
            assert await is_acknowledged(db, transition_id, "notify.settings.on_terminal")
        await lifecycle.run_periodic_reapers()
        assert events == [transition_id]

    # A second daemon startup observes the durable ACK and does not redeliver.
    async with app_module.lifespan(app_module.create_app()):
        assert events == [transition_id]
    async with StateDB() as db:
        rows = await db.fetch_all(
            "SELECT transition_id FROM terminal_deliveries WHERE consumer = :consumer",
            {"consumer": "notify.settings.on_terminal"},
        )
    assert sorted(row["transition_id"] for row in rows) == sorted([history_id, transition_id])


@pytest.mark.asyncio
async def test_two_reconcilers_share_capacity_for_the_same_event(tmp_path):
    path = tmp_path / "state.db"
    first_db, second_db = StateDB(path), StateDB(path)
    await first_db.open()
    await second_db.open()
    try:
        transition_id = await _commit_offline_transition(first_db)
        await register_consumer(first_db, "concurrent", start="all")
        entered = []
        started, release = asyncio.Event(), asyncio.Event()

        async def prepare(row, _db):
            async def deliver():
                entered.append(row["transition_id"])
                started.set()
                await release.wait()
                return DeliveryOutcome.SUCCESS

            return PreparedDelivery(deliver)

        first = TerminalReconciler("concurrent", prepare, batch_size=1, timeout=5)
        second = TerminalReconciler("concurrent", prepare, batch_size=1, timeout=5)
        first_pass = asyncio.create_task(first.run_pass(first_db))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            blocked = await asyncio.wait_for(second.run_pass(second_db), timeout=2)
            assert blocked["saturated"] == 1 and blocked["attempted"] == 0
            assert entered == [transition_id]
            release.set()
            completed = await asyncio.wait_for(first_pass, timeout=2)
            assert completed["attempted"] == completed["acknowledged"] == 1
            assert (await second.run_pass(second_db))["attempted"] == 0
        finally:
            release.set()
            await asyncio.gather(first_pass, return_exceptions=True)
        rows = await first_db.fetch_all(
            "SELECT transition_id FROM terminal_deliveries WHERE consumer = :consumer",
            {"consumer": "concurrent"},
        )
        assert rows == [{"transition_id": transition_id}]
    finally:
        await second_db.close()
        await first_db.close()


# Each child runs a fresh interpreter against the same file-backed store. The
# crash occurs in the handler itself, after its effect is flushed and before
# the reconciler can observe the return or write its acknowledgement.
_CRASH_REPLAY_CHILD = r"""
import asyncio
import os
import sys

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler

store_path, effect_path, mode = sys.argv[1:]

async def prepare(row, _db):
    async def deliver():
        with open(effect_path, 'a', encoding='utf-8') as stream:
            stream.write(row['transition_id'] + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if mode == 'crash':
            os._exit(73)
        return DeliveryOutcome.SUCCESS
    return PreparedDelivery(deliver)

async def run():
    async with StateDB(store_path) as db:
        reconciler = TerminalReconciler('crash-replay', prepare, batch_size=1)
        result = await reconciler.run_pass(db)
        if result['acknowledged'] != 1:
            raise AssertionError('replay did not acknowledge exactly one event')

asyncio.run(run())
"""


@pytest.mark.asyncio
async def test_process_crash_after_effect_replays_same_event_before_ack(tmp_path):
    store_path = tmp_path / "state.db"
    effect_path = tmp_path / "delivered-events.txt"
    child_home = tmp_path / "home"
    child_tmp = tmp_path / "tmp"
    child_home.mkdir()
    child_tmp.mkdir()
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.update(
        HOME=str(child_home),
        LIONAGI_HOME=str(child_home / ".lionagi"),
        TMPDIR=str(child_tmp),
        PYTHONPATH=os.pathsep.join((str(root), str(root / "lionhub" / "src"))),
    )
    env.pop("LIONAGI_STATE_DB_URL", None)

    async with StateDB(store_path) as db:
        transition_id = await _commit_offline_transition(db)

    async def run_child(mode):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-c",
            _CRASH_REPLAY_CHILD,
            str(store_path),
            str(effect_path),
            mode,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            pytest.fail(f"{mode} child did not exit within 20 seconds")
        return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    code, _stdout, stderr = await run_child("crash")
    assert code == 73, stderr
    assert effect_path.read_text(encoding="utf-8").splitlines() == [transition_id]
    async with StateDB(store_path) as db:
        assert not await is_acknowledged(db, transition_id, "crash-replay")

    code, _stdout, stderr = await run_child("replay")
    assert code == 0, stderr
    assert effect_path.read_text(encoding="utf-8").splitlines() == [transition_id, transition_id]
    async with StateDB(store_path) as db:
        assert await is_acknowledged(db, transition_id, "crash-replay")
        rows = await db.fetch_all(
            "SELECT transition_id FROM terminal_deliveries WHERE consumer = :consumer",
            {"consumer": "crash-replay"},
        )
    assert rows == [{"transition_id": transition_id}]
