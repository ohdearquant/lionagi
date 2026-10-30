# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A kill records cancellation only for the session execution it observed."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from lionagi.cli import kill as kill_mod
from lionagi.state import db as db_mod
from lionagi.state.compat import StateCompatibilityRefused
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons


async def _session(db, *, generation, metadata):
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression)
    await db.create_session(
        {
            "id": sid,
            "progression_id": progression,
            "status": "running",
            "cli_worker_generation": generation,
            "node_metadata": metadata,
        }
    )
    return sid


@pytest.mark.asyncio
async def test_null_generation_still_cancels_the_unchanged_running_session(tmp_path, monkeypatch):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _session(db, generation=None, metadata={})
        before = await db.get_session(sid)
        assert before["cli_worker_generation"] is None
        result = await kill_mod._kill_one(db, "session", sid, before, user_reason="")
        after = await db.get_session(sid)
        assert result["signal"] == "no_pid"
        assert result["cancel_status"] == "applied"
        assert after["status"] == "cancelled"
        assert after["cli_worker_generation"] is None
        transitions = await db.fetch_all(
            "SELECT status, source FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running'",
            (sid,),
        )
        assert transitions == [{"status": "cancelled", "source": "admin"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{}, {"pid": "not-a-pid"}], ids=["missing", "malformed"])
@pytest.mark.parametrize("change", ["generation", "status"])
async def test_no_pid_kill_does_not_cancel_a_changed_or_ended_execution(
    tmp_path, monkeypatch, metadata, change
):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _session(db, generation=None, metadata=metadata)
        before = await db.get_session(sid)
        guard = db.cancel_session_if_worker_generation
        new_generation = str(uuid.uuid4())
        calls = []

        async def change_before_guard(session_id, generation, **kwargs):
            calls.append(generation)
            if change == "generation":
                await db.update_session(sid, cli_worker_generation=new_generation)
            else:
                await db.update_status(
                    "session",
                    sid,
                    new_status="completed",
                    reason_code=RunReasons.COMPLETED_OK,
                    source="executor",
                    actor=sid,
                    expected_statuses={"running"},
                )
            return await guard(session_id, generation, **kwargs)

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", change_before_guard)
        result = await kill_mod._kill_one(db, "session", sid, before, user_reason="")
        after = await db.get_session(sid)
        assert calls == [None]
        assert result["signal"] == "no_pid"
        # A replaced generation is a different execution. The same execution ending on its
        # own is the outcome the kill wanted, and its own record stands.
        if change == "generation":
            assert result["cancel_status"] == "conflict"
            assert result["cancel_error"] == "session changed before cancellation was recorded"
        else:
            assert result["cancel_status"] == "already_terminal"
            assert "cancel_error" not in result
        assert after["status"] == ("running" if change == "generation" else "completed")
        assert after["cli_worker_generation"] == (
            new_generation if change == "generation" else None
        )
        transitions = await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? AND source = 'admin'",
            (sid,),
        )
        assert transitions == []


@pytest.mark.asyncio
async def test_no_pid_kill_refuses_when_runner_identity_changes_in_same_generation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    async with StateDB(tmp_path / "state.db") as db:
        generation = str(uuid.uuid4())
        sid = await _session(db, generation=generation, metadata={"pid": "not-a-pid"})
        before = await db.get_session(sid)
        guard = db.cancel_session_if_worker_generation

        async def replace_runner(session_id, captured_generation, **kwargs):
            await db.merge_session_node_metadata(sid, {"pid": 4343, "pid_create_time": 200.0})
            return await guard(session_id, captured_generation, **kwargs)

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", replace_runner)
        result = await kill_mod._kill_one(db, "session", sid, before, user_reason="")
        after = await db.get_session(sid)
        assert result["signal"] == "no_pid"
        assert result["cancel_status"] == "conflict"
        assert after["status"] == "running"
        assert after["cli_worker_generation"] == generation
        assert after["node_metadata"]["pid"] == 4343
        assert not await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? AND source = 'admin'",
            (sid,),
        )


@pytest.mark.asyncio
async def test_guard_deadline_is_reported_as_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _session(db, generation=None, metadata={})
        before = await db.get_session(sid)

        async def deadline_passed(session_id, generation, **kwargs):
            return "timed_out"

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", deadline_passed)
        result = await kill_mod._kill_one(db, "session", sid, before, user_reason="")
        after = await db.get_session(sid)
        assert result["cancel_status"] == "timed_out"
        assert result["cancel_error"] == "session cancellation did not finish before the deadline"
        assert after["status"] == "running"


@pytest.mark.asyncio
async def test_ended_execution_with_a_replaced_runner_is_still_a_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    async with StateDB(tmp_path / "state.db") as db:
        generation = str(uuid.uuid4())
        sid = await _session(db, generation=generation, metadata={"pid": "not-a-pid"})
        before = await db.get_session(sid)
        guard = db.cancel_session_if_worker_generation

        async def replace_runner_then_end(session_id, captured_generation, **kwargs):
            await db.merge_session_node_metadata(sid, {"pid": 4343, "pid_create_time": 200.0})
            await db.update_status(
                "session",
                sid,
                new_status="completed",
                reason_code=RunReasons.COMPLETED_OK,
                source="executor",
                actor=sid,
                expected_statuses={"running"},
            )
            return await guard(session_id, captured_generation, **kwargs)

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", replace_runner_then_end)
        result = await kill_mod._kill_one(db, "session", sid, before, user_reason="")
        after = await db.get_session(sid)
        assert result["cancel_status"] == "conflict"
        assert after["status"] == "completed"
        assert after["cli_worker_generation"] == generation


@pytest.mark.asyncio
async def test_a_resumed_leg_that_ended_is_still_a_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    async with StateDB(tmp_path / "state.db") as db:
        generation, resumed = str(uuid.uuid4()), str(uuid.uuid4())
        sid = await _session(db, generation=generation, metadata={"pid": "not-a-pid"})
        before = await db.get_session(sid)
        guard = db.cancel_session_if_worker_generation

        async def resume_then_end(session_id, captured_generation, **kwargs):
            await db.update_session(sid, cli_worker_generation=resumed)
            await db.update_status(
                "session",
                sid,
                new_status="completed",
                reason_code=RunReasons.COMPLETED_OK,
                source="executor",
                actor=sid,
                expected_statuses={"running"},
            )
            return await guard(session_id, captured_generation, **kwargs)

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", resume_then_end)
        result = await kill_mod._kill_one(db, "session", sid, before, user_reason="")
        after = await db.get_session(sid)
        assert result["cancel_status"] == "conflict"
        assert after["status"] == "completed"
        assert after["cli_worker_generation"] == resumed


@pytest.mark.asyncio
async def test_compatibility_refusal_reports_signal_without_unguarded_cancel(
    tmp_path, monkeypatch, capsys, caplog
):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    signals = []

    def terminate(pid, **_kwargs):
        signals.append(pid)
        return "sigterm"

    monkeypatch.setattr(kill_mod, "_terminate_pid", terminate)
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _session(
            db,
            generation=str(uuid.uuid4()),
            metadata={"pid": 4242, "pid_create_time": 100.0},
        )
        before = await db.get_session(sid)

        async def refuse_guard(*_args, **_kwargs):
            raise StateCompatibilityRefused(
                "cancel_session_if_worker_generation", "fixture", "synthetic refusal"
            )

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", refuse_guard)
        unguarded = AsyncMock(side_effect=AssertionError("unguarded status update"))
        monkeypatch.setattr(db, "update_status", unguarded)
        result = await kill_mod._kill_one(db, "session", sid, before, user_reason="")
        wire = json.loads(json.dumps(result))
        assert wire["signal"] == "sigterm"
        assert wire["pid"] == 4242
        assert wire["cancel_status"] == "unavailable"
        assert "synthetic" in wire["cancel_error"].lower()
        assert signals == [4242]
        unguarded.assert_not_awaited()
        assert (await db.get_session(sid))["status"] == "running"

        # Exercise the operator-facing path with the same guarded refusal.
        @asynccontextmanager
        async def existing_db():
            yield db

        monkeypatch.setattr(db_mod, "StateDB", existing_db)
        monkeypatch.setattr(
            kill_mod, "_resolve_entity", AsyncMock(return_value=("sessions", "session", before))
        )
        monkeypatch.setattr(kill_mod, "_try_recorded_worker_cleanup", AsyncMock(return_value=None))
        rc = await kill_mod._do_kill(sid)
        output = capsys.readouterr()
        text = (output.out + output.err + caplog.text).lower()
        assert rc == 1
        assert "sigterm" in text
        assert "record" in text
        assert "synthetic" in text
        assert (await db.get_session(sid))["status"] == "running"
        unguarded.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_generation_snapshot_fails_closed_without_calling_guard(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    signals = []

    def terminate(pid, **_kwargs):
        signals.append(pid)
        return "sigterm"

    monkeypatch.setattr(kill_mod, "_terminate_pid", terminate)
    async with StateDB(tmp_path / "state.db") as db:
        sid = await _session(
            db,
            generation=None,
            metadata={"pid": 4242, "pid_create_time": 100.0},
        )
        snapshot = await db.get_session(sid)
        assert snapshot.pop("cli_worker_generation") is None
        guard = AsyncMock(side_effect=AssertionError("missing generation must not call CAS"))
        unguarded = AsyncMock(side_effect=AssertionError("unguarded status update"))
        monkeypatch.setattr(db, "cancel_session_if_worker_generation", guard)
        monkeypatch.setattr(db, "update_status", unguarded)

        result = await kill_mod._kill_one(db, "session", sid, snapshot, user_reason="")

        assert result["signal"] == "sigterm"
        assert result["cancel_status"] == "unavailable"
        assert "generation" in result["cancel_error"].lower()
        assert signals == [4242]
        guard.assert_not_awaited()
        unguarded.assert_not_awaited()
        assert (await db.get_session(sid))["status"] == "running"
        assert not await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? AND source = 'admin'",
            (sid,),
        )


@pytest.mark.asyncio
async def test_recursive_child_conflict_reports_signal_and_returns_failure(
    tmp_path, monkeypatch, capsys, caplog
):
    monkeypatch.setattr(kill_mod, "_degraded_run", lambda _sid: None)
    signals = []

    def terminate(pid, **_kwargs):
        signals.append(pid)
        return "sigterm"

    monkeypatch.setattr(kill_mod, "_terminate_pid", terminate)
    async with StateDB(tmp_path / "state.db") as db:
        parent = await _session(db, generation=str(uuid.uuid4()), metadata={})
        child_generation = str(uuid.uuid4())
        child = await _session(
            db,
            generation=child_generation,
            metadata={"pid": 4242, "pid_create_time": 100.0},
        )
        parent_row = await db.get_session(parent)
        child_row = await db.get_session(child)
        new_generation = str(uuid.uuid4())
        guard = db.cancel_session_if_worker_generation
        calls = []

        async def resume_child_before_guard(session_id, generation, **kwargs):
            calls.append((session_id, generation))
            if session_id == child:
                await db.update_session(child, cli_worker_generation=new_generation)
            return await guard(session_id, generation, **kwargs)

        monkeypatch.setattr(db, "cancel_session_if_worker_generation", resume_child_before_guard)

        @asynccontextmanager
        async def existing_db():
            yield db

        monkeypatch.setattr(db_mod, "StateDB", existing_db)
        monkeypatch.setattr(
            kill_mod, "_resolve_entity", AsyncMock(return_value=("sessions", "session", parent_row))
        )
        monkeypatch.setattr(
            kill_mod,
            "_list_running_children",
            AsyncMock(return_value=[("sessions", "session", child_row)]),
        )
        monkeypatch.setattr(kill_mod, "_try_recorded_worker_cleanup", AsyncMock(return_value=None))

        rc = await kill_mod._do_kill(parent, recursive=True)

        output = capsys.readouterr()
        text = (output.out + output.err + caplog.text).lower()
        assert rc == 1
        assert child[:12] in text
        assert "child session" in text
        assert "signal=sigterm" in text
        assert "cancellation was not recorded" in text
        assert "changed" in text
        assert signals == [4242]
        assert (child, child_generation) in calls
        after = await db.get_session(child)
        assert after["status"] == "running"
        assert after["cli_worker_generation"] == new_generation
        assert not await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? AND source = 'admin'",
            (child,),
        )
