# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A kill of an old session leg cannot cancel a resumed leg."""

import asyncio
import logging
import threading
import uuid

import pytest

import lionagi.cli._kill_service as kill_service
from lionagi.cli import kill as kill_mod
from lionagi.cli._runs import _reopen_session_for_resume
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons


@pytest.mark.asyncio
@pytest.mark.parametrize("same_process", [False, True], ids=["new-process", "same-process"])
async def test_kill_grace_does_not_cancel_reopened_session(
    tmp_path, monkeypatch, caplog, same_process
):
    caplog.set_level(logging.ERROR)
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    old_generation, new_generation = str(uuid.uuid4()), str(uuid.uuid4())
    old_identity = {"pid": 4242, "pid_create_time": 100.0}
    new_identity = old_identity if same_process else {"pid": 4343, "pid_create_time": 200.0}
    entered = threading.Event()
    release = threading.Event()
    signals = []

    def wait_in_grace(pid, **kwargs):
        signals.append((pid, kwargs.get("expected_create_time")))
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("test did not release the fake grace period")
        return "sigterm"

    # Stub every OS process probe or signal: no process with these PIDs is contacted.
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: ("found", 100.0))
    monkeypatch.setattr(kill_service, "_terminate_pid", wait_in_grace)
    monkeypatch.setattr("lionagi.ln._proc.current_pid_markers", lambda: dict(new_identity))

    async with StateDB(path) as editor:
        sid = str(uuid.uuid4())
        progression = str(uuid.uuid4())
        await editor.create_progression(progression)
        await editor.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "cli_worker_generation": old_generation,
                "node_metadata": dict(old_identity),
            }
        )

        # `_terminate_pid` is synchronous. A separate event-loop thread lets the real
        # second SQLite connection finish and reopen the session during its grace wait.
        task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(kill_mod._do_kill(sid))))
        try:
            assert await asyncio.to_thread(entered.wait, 5), "kill did not enter fake grace"
            await editor.update_status(
                "session",
                sid,
                new_status="completed",
                reason_code=RunReasons.COMPLETED_OK,
                source="executor",
                actor=sid,
                expected_statuses={"running"},
            )
            closed = await editor.get_session(sid)
            assert await _reopen_session_for_resume(
                editor, sid, closed, generation_id=new_generation
            )
            reopened = await editor.get_session(sid)
            assert reopened["status"] == "running"
            assert reopened["cli_worker_generation"] == new_generation
            assert all(reopened["node_metadata"][k] == v for k, v in new_identity.items())
            release.set()
            exit_code = await asyncio.wait_for(task, timeout=10)
        finally:
            release.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=10)

        after = await editor.get_session(sid)
        assert after["status"] == "running"
        assert after["cli_worker_generation"] == new_generation
        assert after["node_metadata"] == reopened["node_metadata"]
        assert after["ended_at"] is None
        rows = await editor.fetch_all(
            "SELECT status, source FROM status_transitions WHERE entity_id = ?", (sid,)
        )
        assert not any(r["status"] == "cancelled" and r["source"] == "admin" for r in rows)
        assert signals == [(4242, 100.0)]
        assert exit_code == 1
        assert "changed" in caplog.text.lower()


@pytest.mark.asyncio
async def test_kill_succeeds_when_the_runner_records_its_own_sigterm_end_in_grace(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR)
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    generation = str(uuid.uuid4())
    identity = {"pid": 4242, "pid_create_time": 100.0}
    entered = threading.Event()
    release = threading.Event()

    def wait_in_grace(pid, **kwargs):
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("test did not release the fake grace period")
        return "sigterm"

    # Stub every OS process probe or signal: no process with these PIDs is contacted.
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: ("found", 100.0))
    monkeypatch.setattr(kill_service, "_terminate_pid", wait_in_grace)

    async with StateDB(path) as editor:
        sid = str(uuid.uuid4())
        progression = str(uuid.uuid4())
        await editor.create_progression(progression)
        await editor.create_session(
            {
                "id": sid,
                "progression_id": progression,
                "status": "running",
                "cli_worker_generation": generation,
                "node_metadata": dict(identity),
            }
        )
        task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(kill_mod._do_kill(sid))))
        try:
            assert await asyncio.to_thread(entered.wait, 5), "kill did not enter fake grace"
            # The runner obeys the SIGTERM: its teardown records the end before it exits.
            await editor.update_status(
                "session",
                sid,
                new_status="cancelled",
                reason_code=RunReasons.CANCELLED_SIGTERM,
                source="executor",
                actor=sid,
                expected_statuses={"running"},
            )
            release.set()
            exit_code = await asyncio.wait_for(task, timeout=10)
        finally:
            release.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=10)

        after = await editor.get_session(sid)
        rows = await editor.fetch_all(
            "SELECT status, source, reason_code FROM status_transitions WHERE entity_id = ?",
            (sid,),
        )
    assert exit_code == 0
    assert after["status"] == "cancelled"
    assert after["cli_worker_generation"] == generation
    assert [r for r in rows if r["status"] == "cancelled"] == [
        {"status": "cancelled", "source": "executor", "reason_code": RunReasons.CANCELLED_SIGTERM}
    ]
    assert "not recorded" not in caplog.text
