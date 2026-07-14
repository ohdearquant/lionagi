# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the control poller *task lifecycle* wired into
`_execute_dag` — as opposed to `_apply_session_control`'s
pure state machine, already covered by `test_control_poller.py`.

These drive `_execute_dag` itself (same fakes as `test_flow_phases.py`) with a
`run_dag` stand-in that holds the run open long enough for `_control_poll_loop`
to tick against a real `StateDB`, so the actual `_ctl_task` wiring —
creation, polling on `_CONTROL_POLL_INTERVAL`, clean cancellation at run end,
and crash-isolation from the run it rides alongside — gets exercised end to
end instead of assumed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate import flow as _flow
from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
from lionagi.state.db import StateDB

from .test_control_poller import _FakeExecutor, _make_session, _queue_control
from .test_flow_phases import _FakeBranch, _make_env


def _plan_and_dag(node_id: str = "node-0"):
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[node_id],
        known_nodes={node_id},
        deps_by_node={node_id: []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    return plan_result, dag_state


async def test_ctl_task_applies_a_queued_control_during_a_live_run(tmp_path, monkeypatch):
    """The poller must actually be created and wired: a control row queued
    before the run starts is picked up mid-flight (not just by calling
    `_apply_session_control` directly) and its result persisted to the DB."""
    monkeypatch.setattr(_flow, "_CONTROL_POLL_INTERVAL", 0.02)

    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        control = await _queue_control(db, sid, "pause")
        control_finalized = asyncio.Event()
        finalize_session_control = StateDB.finalize_session_control

        async def finalize_and_signal(self, control_id, *, result):
            await finalize_session_control(self, control_id, result=result)
            if control_id == control["id"]:
                control_finalized.set()

        monkeypatch.setattr(StateDB, "finalize_session_control", finalize_and_signal)

        env = _make_env(tmp_path, live_persist={"db": db, "session_id": sid})
        env.session.include_branches(_FakeBranch("researcher"))
        plan_result, dag_state = _plan_and_dag()

        fake_executor = _FakeExecutor()

        async def _fake_run_dag(graph, *, executor_ref, **_kw):
            executor_ref["executor"] = fake_executor
            await asyncio.wait_for(control_finalized.wait(), timeout=5)
            return {"operation_results": {"node-0": "ok"}, "spawned_operations": 0}

        fake_engine_run = MagicMock()
        fake_engine_run.run_dag = _fake_run_dag

        from lionagi.engines import PlanningEngine

        with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
            await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

        assert fake_executor.paused == 1
        finalized = await db.get_session_control(control["id"])
        assert finalized["result"] == "applied"
        assert finalized["applied_at"] is not None


async def test_ctl_task_polls_repeatedly_not_once(tmp_path, monkeypatch):
    """A control queued *after* the run has already started (past the first
    poll tick) must still be picked up by a later tick — proving the poller
    loops on its interval rather than firing a single pass at startup."""
    monkeypatch.setattr(_flow, "_CONTROL_POLL_INTERVAL", 0.02)

    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)

        env = _make_env(tmp_path, live_persist={"db": db, "session_id": sid})
        env.session.include_branches(_FakeBranch("researcher"))
        plan_result, dag_state = _plan_and_dag()

        fake_executor = _FakeExecutor()
        late_control: dict = {}

        async def _fake_run_dag(graph, *, executor_ref, **_kw):
            executor_ref["executor"] = fake_executor
            await asyncio.sleep(0.05)  # let a tick or two pass with nothing queued
            late_control.update(await _queue_control(db, sid, "resume"))
            await asyncio.sleep(0.1)  # give the poller another tick to see it
            return {"operation_results": {"node-0": "ok"}, "spawned_operations": 0}

        fake_engine_run = MagicMock()
        fake_engine_run.run_dag = _fake_run_dag

        from lionagi.engines import PlanningEngine

        with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
            await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

        assert fake_executor.resumed == 1
        finalized = await db.get_session_control(late_control["id"])
        assert finalized["result"] == "applied"


async def test_ctl_task_and_hb_task_are_cancelled_cleanly_at_run_end(tmp_path, monkeypatch):
    """Both background tasks must be gone (not leaked, not raising) once
    `_execute_dag` returns — the `finally` block cancels and awaits both."""
    monkeypatch.setattr(_flow, "_CONTROL_POLL_INTERVAL", 0.02)

    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)

        env = _make_env(tmp_path, live_persist={"db": db, "session_id": sid})
        env.session.include_branches(_FakeBranch("researcher"))
        plan_result, dag_state = _plan_and_dag()

        fake_executor = _FakeExecutor()

        async def _fake_run_dag(graph, *, executor_ref, **_kw):
            executor_ref["executor"] = fake_executor
            await asyncio.sleep(0.05)
            return {"operation_results": {"node-0": "ok"}, "spawned_operations": 0}

        fake_engine_run = MagicMock()
        fake_engine_run.run_dag = _fake_run_dag

        from lionagi.engines import PlanningEngine

        tasks_before = asyncio.all_tasks()
        with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
            await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

        # No orphaned poller/heartbeat task left running past the call.
        leaked = asyncio.all_tasks() - tasks_before
        assert leaked == set()


async def test_ctl_task_crash_does_not_kill_the_run(tmp_path, monkeypatch):
    """A poller-side failure (e.g. a transient DB error listing pending
    controls) must not propagate out of `_execute_dag` — the run it rides
    alongside completes normally regardless."""
    monkeypatch.setattr(_flow, "_CONTROL_POLL_INTERVAL", 0.02)

    class _AlwaysFailsListingDB:
        def __init__(self, real_db: StateDB):
            self._real = real_db

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def list_pending_session_controls(self, *_a, **_kw):
            raise RuntimeError("database is locked")

    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        flaky_db = _AlwaysFailsListingDB(db)

        env = _make_env(tmp_path, live_persist={"db": flaky_db, "session_id": sid})
        env.session.include_branches(_FakeBranch("researcher"))
        plan_result, dag_state = _plan_and_dag()

        fake_executor = _FakeExecutor()

        async def _fake_run_dag(graph, *, executor_ref, **_kw):
            executor_ref["executor"] = fake_executor
            await asyncio.sleep(0.1)  # several ticks, every one raises inside the poller
            return {"operation_results": {"node-0": "ok"}, "spawned_operations": 0}

        fake_engine_run = MagicMock()
        fake_engine_run.run_dag = _fake_run_dag

        from lionagi.engines import PlanningEngine

        with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
            exec_result = await _execute_dag(
                env, plan_result, dag_state, max_concurrent=1, max_ops=0
            )

        assert exec_result.agent_results[0]["response"] == "ok"


async def test_ctl_task_not_yet_available_window_is_skipped_not_fatal(tmp_path, monkeypatch):
    """Before `run_dag` populates `executor_ref`, the poller must find no
    executor and skip the tick — not raise — for as many ticks as it takes."""
    monkeypatch.setattr(_flow, "_CONTROL_POLL_INTERVAL", 0.02)

    async with StateDB(tmp_path / "state.db") as db:
        sid = await _make_session(db)
        await _queue_control(db, sid, "pause")

        env = _make_env(tmp_path, live_persist={"db": db, "session_id": sid})
        env.session.include_branches(_FakeBranch("researcher"))
        plan_result, dag_state = _plan_and_dag()

        async def _fake_run_dag(graph, *, executor_ref, **_kw):
            # Simulate a slow-to-construct executor: several poll ticks pass
            # with executor_ref empty before it becomes available.
            await asyncio.sleep(0.06)
            assert "executor" not in executor_ref
            return {"operation_results": {"node-0": "ok"}, "spawned_operations": 0}

        fake_engine_run = MagicMock()
        fake_engine_run.run_dag = _fake_run_dag

        from lionagi.engines import PlanningEngine

        with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
            exec_result = await _execute_dag(
                env, plan_result, dag_state, max_concurrent=1, max_ops=0
            )

        assert exec_result.agent_results[0]["response"] == "ok"
        # The row is untouched — no executor ever became available to apply it.
        pending = await db.list_pending_session_controls(sid)
        assert len(pending) == 1
