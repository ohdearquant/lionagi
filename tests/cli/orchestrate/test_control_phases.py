# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Control delivery and persisted phases across the flow caller lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from lionagi.cli.monitor import _machine_entity, _session_to_row
from lionagi.cli.orchestrate import flow
from lionagi.cli.orchestrate._control import _enqueue_control_inner
from lionagi.cli.status import EXIT_UNKNOWN, _build_view, _render_human
from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB
from tests.cli._worker_fixtures import create_worker_session
from tests.cli.orchestrate.test_control_poller import (
    _FakeExecutor,
    _make_session,
    _queue_control,
)
from tests.cli.orchestrate.test_flow_phases import _make_env


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    monkeypatch.setattr("lionagi.state.db.settings", SimpleNamespace(LIONAGI_STATE_DB_URL=None))
    return path


@pytest.mark.parametrize("verb", ["pause", "resume", "message"])
async def test_held_planner_exposes_deferred_controls(db_path, tmp_path, monkeypatch, verb):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.merge_session_node_metadata(sid, {"flow_control_poller": "pending"})
        env = _make_env(tmp_path, live_persist={"db": db, "session_id": sid})
        entered = asyncio.Event()

        async def held_plan(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(flow, "plan", held_plan)
        task = asyncio.create_task(flow._run_flow_inner("fake/model", "plan work", env=env))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            row = await db.get_session(sid)
            assert row["current_phase"] == "planning"
            output, rc = await _enqueue_control_inner(
                entity_id=sid, verb=verb, payload={"text": "use precise evidence"}
            )
            assert rc == 0
            assert "deferred until the DAG execution poller starts" in output
            assert "execution poller" in output
            assert "applies within" not in output
            if verb == "message":
                assert "operations prepared after delivery" in output
            else:
                assert "operation boundaries" in output
                assert "not interrupted" in output
            pending = await db.list_pending_session_controls(sid)
            assert len(pending) == 1
            assert pending[0]["applied_at"] is None
            assert pending[0]["result"] is None
            assert (await db.get_session(sid))["current_phase"] == "planning"
            view = await _build_view(db, command="ctl", entity_type="session", row=row)
            assert "controls wait for the DAG execution poller" in _render_human(view)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_pause_resume_phase_changes_only_when_consumed(db_path):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.update_session(sid, current_phase="executing")
        executor = _FakeExecutor()
        for verb, before, after in [
            ("pause", "executing", "paused"),
            ("resume", "paused", "executing"),
        ]:
            row = await _queue_control(db, sid, verb)
            assert (await db.get_session(sid))["current_phase"] == before
            assert await flow._apply_session_control(db, executor, row) == "applied"
            stored = await db.get_session(sid)
            assert stored["current_phase"] == after
            view = await _build_view(db, command="ctl", entity_type="session", row=stored)
            assert view["current_phase"] == after
            assert _session_to_row(stored)["phase"] == after
            assert _machine_entity("session", stored)["current_phase"] == after
        assert executor.paused == executor.resumed == 1


@pytest.mark.parametrize("verb,phase", [("pause", "executing"), ("resume", "paused")])
async def test_rejected_executor_action_preserves_phase(db_path, verb, phase):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.update_session(sid, current_phase=phase)
        executor = _FakeExecutor()
        setattr(executor, verb, Mock(side_effect=RuntimeError("gate unavailable")))
        row = await _queue_control(db, sid, verb)
        result = await flow._apply_session_control(db, executor, row)
        assert result.startswith("rejected:error:")
        assert (await db.get_session(sid))["current_phase"] == phase


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "timed_out"])
async def test_flow_finalization_clears_persisted_phase(db_path, tmp_path, monkeypatch, outcome):
    env = _make_env(tmp_path)
    env.run.run_id = "test-run"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None
    entered = asyncio.Event()
    timeout_scope = flow.CancelScope()

    async def start(env, **kwargs):
        db = StateDB(db_path)
        await db.open()
        ctx = await create_worker_session(db, run_id=env.run.run_id)
        sid = ctx["session_id"]
        env._live_persist = {**ctx, "shared_db_registered": False}
        env.test_session_id = sid

    async def inner(*args, **kwargs):
        await flow._persist_session_phase(env, "planning")
        entered.set()
        if outcome == "failed":
            raise flow.FlowPlanError("no usable plan")
        await flow._persist_session_phase(env, "synthesizing")
        return "finished"

    async def held_plan(*args, **kwargs):
        entered.set()
        if outcome == "timed_out":
            timeout_scope.cancel()
        await asyncio.Event().wait()

    monkeypatch.setattr(flow, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(flow, "start_live_persist", start)
    if outcome in {"cancelled", "timed_out"}:
        monkeypatch.setattr(flow, "plan", held_plan)
        if outcome == "timed_out":
            monkeypatch.setattr(flow, "move_on_after", lambda seconds: timeout_scope)
    else:
        monkeypatch.setattr(flow, "_run_flow_inner", inner)
    monkeypatch.setattr(
        "lionagi.state.completion_evidence.check_completion_evidence",
        lambda cwd: {"checked": False},
    )
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_run_notify_outcome_scope",
        lambda *args, **kwargs: None,
    )
    task = asyncio.create_task(
        flow._run_flow("fake/model", "plan work", timeout=0.1 if outcome == "timed_out" else None)
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "failed":
        with pytest.raises(flow.FlowPlanError):
            await task
    elif outcome == "timed_out":
        with pytest.raises(flow.LionTimeoutError):
            await task
    else:
        assert await task == ("finished", "completed")

    async with StateDB(db_path) as db:
        row = await db.get_session(env.test_session_id)
    assert row["status"] == outcome
    assert row["ended_at"] is not None
    assert row["current_phase"] is None
    assert env._run_manifest["status"] == outcome
    assert env._live_persist is None


@pytest.mark.parametrize("status", sorted(SESSION_TERMINAL_STATUSES))
async def test_terminal_session_hides_legacy_live_phase(db_path, status):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.update_session(sid, status=status, current_phase="paused")
        row = await db.get_session(sid)
        view = await _build_view(db, command="ctl", entity_type="session", row=row)
    assert view["terminal"] is True
    assert view["current_phase"] is None
    assert "phase:" not in _render_human(view)
    assert _session_to_row(row)["phase"] == "-"
    assert _machine_entity("session", row)["current_phase"] is None


@pytest.mark.parametrize(
    "invocation_status,session_status", [("running", "completed"), ("completed", "running")]
)
async def test_invocation_view_only_exposes_a_live_session_phase(
    db_path, invocation_status, session_status
):
    async with StateDB(db_path) as db:
        await db.create_invocation(
            {
                "id": "registered-run",
                "skill": "flow",
                "status": invocation_status,
                "started_at": 1.0,
            }
        )
        sid = await _make_session(db)
        await db.update_session(
            sid, invocation_id="registered-run", status=session_status, current_phase="executing"
        )
        row = await db.get_invocation("registered-run")
        view = await _build_view(db, command="ctl", entity_type="invocation", row=row)
    assert view["current_phase"] is None


async def test_unregistered_invocation_label_requires_a_resolvable_id(db_path):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
    output, rc = await _enqueue_control_inner(entity_id="chosen-label", verb="pause", payload=None)
    assert rc == EXIT_UNKNOWN
    assert "printed session ID" in output
    assert "li invoke start" in output
    output, rc = await _enqueue_control_inner(entity_id=sid, verb="pause", payload=None)
    assert rc == 0
