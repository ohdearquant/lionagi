# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Flow control targeting and lifecycle boundary regressions."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy import event

from lionagi.cli._runs import teardown_persist
from lionagi.cli.monitor import _session_to_row
from lionagi.cli.orchestrate import flow
from lionagi.cli.orchestrate._control import _enqueue_control_inner
from lionagi.cli.status import EXIT_UNKNOWN
from lionagi.state.db import StateDB
from tests.cli.orchestrate.test_control_poller import _FakeExecutor, _make_session, _queue_control
from tests.cli.orchestrate.test_control_poller_lifecycle import _plan_and_dag
from tests.cli.orchestrate.test_flow_phases import _FakeBranch, _make_env


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    monkeypatch.setattr("lionagi.state.db.settings", SimpleNamespace(LIONAGI_STATE_DB_URL=None))
    return path


async def _named_session(db, session_id, *, invocation_id=None):
    progression_id = f"progression-{session_id}"
    await db.create_progression(progression_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": progression_id,
            "invocation_kind": "flow",
            "invocation_id": invocation_id,
            "status": "running",
            "started_at": 1.0,
            "node_metadata": {"flow_control_poller": "active"},
        }
    )
    return session_id


@pytest.mark.parametrize("store_exists", [False, True])
async def test_unknown_invocation_fails_before_setup_or_persistence(
    db_path, monkeypatch, store_exists
):
    if store_exists:
        async with StateDB(db_path):
            pass
    setup = AsyncMock(side_effect=AssertionError("setup ran before invocation validation"))
    monkeypatch.setattr(flow, "setup_orchestration", setup)
    with pytest.raises(flow.FlowPlanError, match="li invoke start"):
        await flow._run_flow("fake/model", "work", invocation_id="self-chosen")
    setup.assert_not_awaited()
    if store_exists:
        async with StateDB(db_path) as db:
            assert await db.fetch_all("SELECT id FROM sessions") == []
    else:
        assert not db_path.exists()


async def test_registered_exact_invocation_reaches_setup(db_path, monkeypatch):
    async with StateDB(db_path) as db:
        await db.create_invocation({"id": "ab12", "skill": "flow", "started_at": 1.0})

    class SetupReached(Exception):
        pass

    setup = AsyncMock(side_effect=SetupReached)
    monkeypatch.setattr(flow, "setup_orchestration", setup)
    with pytest.raises(SetupReached):
        await flow._run_flow("fake/model", "work", invocation_id="ab12")
    setup.assert_awaited_once()


@pytest.mark.parametrize("invocation_id", [None, "registered-run"])
async def test_foreground_prints_persisted_session_before_inner_run(
    db_path, tmp_path, monkeypatch, caplog, invocation_id
):
    if invocation_id:
        async with StateDB(db_path) as db:
            await db.create_invocation({"id": invocation_id, "skill": "flow", "started_at": 1.0})
    env = _make_env(tmp_path)
    env.run.run_id = "test-run"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None

    async def start(env, **kwargs):
        db = StateDB(db_path)
        await db.open()
        sid = await _make_session(db)
        assert kwargs["extra_node_metadata"]["flow_control_poller"] == "pending"
        await db.merge_session_node_metadata(sid, kwargs["extra_node_metadata"])
        if invocation_id:
            await db.update_session(sid, invocation_id=invocation_id)
        row = await db.get_session(sid)
        env._live_persist = {
            "db": db,
            "session_id": sid,
            "session_prog_id": row["progression_id"],
            "shared_db_registered": False,
        }
        env.test_session_id = sid

    async def inner(*args, **kwargs):
        assert f"Session: {env.test_session_id}" in caplog.text
        return "done"

    monkeypatch.setattr(flow, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(flow, "start_live_persist", start)
    monkeypatch.setattr(flow, "_run_flow_inner", inner)
    monkeypatch.setattr(
        "lionagi.state.completion_evidence.check_completion_evidence",
        lambda cwd: {"checked": False},
    )
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_run_notify_outcome_scope",
        lambda *args, **kwargs: None,
    )
    caplog.set_level("INFO", logger="lionagi.cli.hint")
    result, _status = await flow._run_flow("fake/model", "work", invocation_id=invocation_id)
    assert result == "done"
    async with StateDB(db_path) as db:
        row = await db.get_session(env.test_session_id)
        assert row["invocation_id"] == invocation_id


@pytest.mark.parametrize("length", [4, 6, 9])
async def test_unprinted_session_prefix_cannot_control_another_run(db_path, length):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
    output, rc = await _enqueue_control_inner(entity_id=sid[:length], verb="pause", payload=None)
    assert rc == EXIT_UNKNOWN
    assert "session ID" in output
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(sid) == []


@pytest.mark.parametrize("target_kind", ["session", "invocation"])
async def test_exact_short_ids_are_not_rejected_as_prefixes(db_path, target_kind):
    async with StateDB(db_path) as db:
        sid = await _named_session(db, "mine")
        if target_kind == "invocation":
            await db.create_invocation({"id": "ab12", "skill": "flow", "started_at": 1.0})
            await db.update_session(sid, invocation_id="ab12")
            target = "ab12"
        else:
            target = sid
    _output, rc = await _enqueue_control_inner(entity_id=target, verb="pause", payload=None)
    assert rc == 0
    async with StateDB(db_path) as db:
        assert len(await db.list_pending_session_controls(sid)) == 1


async def test_linked_invocation_and_colliding_label_do_not_target_another_session(db_path):
    async with StateDB(db_path) as db:
        await db.create_invocation({"id": "own-record", "skill": "flow", "started_at": 1.0})
        own = await _named_session(db, "owner-session", invocation_id="own-record")
        other = await _named_session(db, "ab12000000000000")
    _output, rc = await _enqueue_control_inner(entity_id="ab12", verb="pause", payload=None)
    assert rc == EXIT_UNKNOWN
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(other) == []
        assert await db.list_pending_session_controls(own) == []
    _output, rc = await _enqueue_control_inner(entity_id="own-record", verb="pause", payload=None)
    assert rc == 0
    async with StateDB(db_path) as db:
        assert len(await db.list_pending_session_controls(own)) == 1
        assert await db.list_pending_session_controls(other) == []


async def test_nonhex_eight_character_label_is_not_a_printed_prefix(db_path):
    async with StateDB(db_path) as db:
        sid = await _named_session(db, "a-label-long-session")
    _output, rc = await _enqueue_control_inner(entity_id=sid[:8], verb="pause", payload=None)
    assert rc == EXIT_UNKNOWN
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(sid) == []


async def test_printed_eight_character_prefix_can_control_session(db_path):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
    _output, rc = await _enqueue_control_inner(entity_id=sid[:8], verb="pause", payload=None)
    assert rc == 0
    async with StateDB(db_path) as db:
        assert len(await db.list_pending_session_controls(sid)) == 1


@pytest.mark.parametrize(
    "kind,name_field", [("agent", "agent_name"), ("flow", "agent_name"), ("play", "playbook_name")]
)
@pytest.mark.parametrize("phase", [None, "synthesizing"])
def test_terminal_monitor_hides_live_phase_but_keeps_recorded_name(kind, name_field, phase):
    row = {
        "id": "recorded-session",
        "status": "completed",
        "invocation_kind": kind,
        name_field: "recorded-name",
        "current_phase": phase,
    }
    assert _session_to_row(row)["phase"] == "recorded-name"


@pytest.mark.parametrize(
    "verb,before,after", [("pause", "executing", "paused"), ("resume", "paused", "executing")]
)
async def test_applied_control_retries_one_failed_phase_write(db_path, verb, before, after):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.update_session(sid, current_phase=before)
        row = await _queue_control(db, sid, verb)
        executor = _FakeExecutor()

        class OnePhaseWriteFails:
            def __init__(self):
                self.attempts = 0

            def __getattr__(self, name):
                return getattr(db, name)

            async def update_session(self, session_id, **fields):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("injected transient phase failure")
                return await db.update_session(session_id, **fields)

        flaky = OnePhaseWriteFails()
        assert await flow._apply_session_control(flaky, executor, row) == "applied"
        assert getattr(executor, "paused" if verb == "pause" else "resumed") == 1
        assert flaky.attempts == 2
        assert (await db.get_session(sid))["current_phase"] == after


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "timed_out"])
async def test_terminal_write_clears_phase_in_same_statement(db_path, monkeypatch, outcome):
    db = StateDB(db_path)
    await db.open()
    sid = await _make_session(db)
    await db.update_session(sid, current_phase="planning")
    row = await db.get_session(sid)
    await db.execute(
        "CREATE TRIGGER terminal_phase_atomic BEFORE UPDATE OF status ON sessions "
        "WHEN NEW.status != 'running' AND NEW.current_phase IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'terminal status requires cleared phase'); END"
    )
    monkeypatch.setattr(
        "lionagi.state.completion_evidence.check_completion_evidence",
        lambda cwd: {"checked": False},
    )
    session_updates = []

    def record_session_update(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update sessions set "):
            session_updates.append(normalized)

    engine = db._engine.sync_engine
    event.listen(engine, "before_cursor_execute", record_session_update)
    try:
        status = await teardown_persist(
            {
                "db": db,
                "session_id": sid,
                "session_prog_id": row["progression_id"],
                "shared_db_registered": False,
            },
            status=outcome,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_session_update)
        await db.close()
    assert status == outcome
    async with StateDB(db_path) as reader:
        stored = await reader.get_session(sid)
    assert stored["status"] == outcome
    assert stored["ended_at"] is not None
    assert stored["current_phase"] is None
    written_fields = [
        {
            assignment.split("=", 1)[0].strip().strip('"')
            for assignment in sql.split(" set ", 1)[1].split(" where ", 1)[0].split(",")
        }
        for sql in session_updates
    ]
    terminal_writes = [fields for fields in written_fields if "status" in fields]
    assert len(terminal_writes) == 1, session_updates
    assert "current_phase" in terminal_writes[0], session_updates
    assert not any(
        "current_phase" in fields and "status" not in fields for fields in written_fields
    ), session_updates


@pytest.mark.parametrize("kind", ["flow", "play"])
@pytest.mark.parametrize("phase", ["planning", "executing", "synthesizing", None])
@pytest.mark.parametrize("poller_state", ["pending", "active", "stopped", None])
async def test_enqueue_obeys_poller_window_not_stale_phase(db_path, kind, phase, poller_state):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        metadata = {} if poller_state is None else {"flow_control_poller": poller_state}
        await db.update_session(
            sid, invocation_kind=kind, current_phase=phase, node_metadata=json.dumps(metadata)
        )
    for verb in ("pause", "resume", "message"):
        output, rc = await _enqueue_control_inner(entity_id=sid, verb=verb, payload=None)
        if poller_state in {"pending", "active"}:
            assert rc == 0
            assert "will not be applied after the poller stops" in output
            assert "planning completes" not in output
            if poller_state == "pending":
                assert "deferred until the DAG execution poller starts" in output
        else:
            assert rc == EXIT_UNKNOWN
            assert (
                "stopped" in output
                if poller_state == "stopped"
                else "predates control-delivery tracking" in output
            )
    async with StateDB(db_path) as db:
        pending = await db.list_pending_session_controls(sid)
    assert len(pending) == (3 if poller_state in {"pending", "active"} else 0)


@pytest.mark.parametrize("registered", [False, True])
def test_background_validates_invocation_before_spawn_and_prints_full_session(
    db_path, tmp_path, monkeypatch, registered
):
    import lionagi.cli.orchestrate as orchestrate

    async def create_invocation():
        async with StateDB(db_path) as db:
            await db.create_invocation({"id": "registered", "skill": "flow", "started_at": 1.0})

    if registered:
        asyncio.run(create_invocation())
    monkeypatch.chdir(tmp_path)
    spawned = []
    hints = []

    def spawn(argv, **kwargs):
        spawned.append(kwargs["env"]["LIONAGI_SESSION_ID"])
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(orchestrate, "hint", hints.append)
    parser = argparse.ArgumentParser()
    orchestrate.add_orchestrate_subparser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "o",
            "flow",
            "--background",
            "--save",
            str(tmp_path / "out"),
            "--invocation",
            "registered" if registered else "self-chosen",
            "fake/model",
            "work",
        ]
    )
    rc = orchestrate.run_orchestrate(args)
    if registered:
        assert rc == 0
        assert len(spawned) == 1
        assert any(f"Session: {spawned[0]} " in hint for hint in hints)
    else:
        assert rc != 0
        assert spawned == []
        assert not any(hint.startswith("Session:") for hint in hints)


@pytest.mark.parametrize("fail_stopped_write", [False, True])
async def test_dag_poller_marker_lifecycle_and_failed_stop_write(
    db_path, tmp_path, monkeypatch, fail_stopped_write
):
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.merge_session_node_metadata(
            sid, {"flow_control_poller": "pending", "unrelated": "kept"}
        )
        original_merge = db.merge_session_node_metadata
        marker_writes = []

        async def merge(session_id, patch):
            state = patch.get("flow_control_poller")
            if state:
                marker_writes.append(state)
                if fail_stopped_write and state == "stopped":
                    raise RuntimeError("injected stopped-marker failure")
            return await original_merge(session_id, patch)

        monkeypatch.setattr(db, "merge_session_node_metadata", merge)
        env = _make_env(tmp_path, live_persist={"db": db, "session_id": sid})
        env.session.include_branches(_FakeBranch("researcher"))
        plan, dag = _plan_and_dag()

        async def run_dag(graph, *, executor_ref, **kwargs):
            executor_ref["executor"] = _FakeExecutor()
            metadata = (await db.get_session(sid))["node_metadata"]
            assert metadata["flow_control_poller"] == "active"
            assert metadata["unrelated"] == "kept"
            return {"operation_results": {"node-0": "done"}, "spawned_operations": 0}

        engine_run = MagicMock()
        engine_run.run_dag = run_dag
        monkeypatch.setattr(
            "lionagi.engines.PlanningEngine.new_run", lambda *args, **kw: engine_run
        )
        await flow._execute_dag(env, plan, dag, max_concurrent=1, max_ops=0)
        assert marker_writes == ["active", "stopped"]
        metadata = (await db.get_session(sid))["node_metadata"]
        assert metadata["flow_control_poller"] == ("active" if fail_stopped_write else "stopped")
        assert metadata["unrelated"] == "kept"
    output, rc = await _enqueue_control_inner(entity_id=sid, verb="pause", payload=None)
    if fail_stopped_write:
        assert rc == 0
        assert "will not be applied after the poller stops" in output
    else:
        assert rc == EXIT_UNKNOWN
        assert "stopped" in output


@pytest.mark.parametrize("route", ["compatibility", "service"])
async def test_terminal_phase_allowlist_does_not_admit_unrelated_field(db_path, route):
    from lionagi.state.lifecycle import ActorRecord, ReasonRecord, TransitionCommand
    from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService
    from lionagi.state.reasons import RunReasons

    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        with pytest.raises(ValueError, match="(extra_fields|patch field)"):
            if route == "compatibility":
                await db.update_status(
                    "session",
                    sid,
                    new_status="completed",
                    reason_code=RunReasons.COMPLETED_OK,
                    extra_fields={"agent_name": "unrelated"},
                )
            else:
                await SQLAlchemyLifecycleService(db).transition(
                    TransitionCommand(
                        entity_type="session",
                        entity_id=sid,
                        to_status="completed",
                        reason=ReasonRecord(code=RunReasons.COMPLETED_OK),
                        actor=ActorRecord(type="executor", id=sid),
                        patch={"agent_name": "unrelated"},
                    )
                )
        assert (await db.get_session(sid))["status"] == "running"


async def test_cancelled_stopped_marker_cannot_skip_exchange_stop(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    env.session.include_branches(_FakeBranch("researcher"))
    plan, dag = _plan_and_dag()

    class Exchange:
        def __init__(self):
            self.stopped = False
            self.done = asyncio.Event()
            self.task = None

        async def run(self, interval):
            self.task = asyncio.current_task()
            await self.done.wait()

        def stop(self):
            self.stopped = True
            self.done.set()

        async def collect_all(self):
            pass

    exchange = Exchange()
    env.exchange = exchange

    async def marker(env, state):
        if state == "stopped":
            raise asyncio.CancelledError

    async def run_dag(*args, **kwargs):
        await asyncio.sleep(0)
        return {"operation_results": {"node-0": "done"}, "spawned_operations": 0}

    engine_run = MagicMock()
    engine_run.run_dag = run_dag
    monkeypatch.setattr("lionagi.engines.PlanningEngine.new_run", lambda *args, **kw: engine_run)
    monkeypatch.setattr(flow, "_persist_control_poller_state", marker)
    try:
        with pytest.raises(asyncio.CancelledError):
            await flow._execute_dag(env, plan, dag, max_concurrent=1, max_ops=0)
        assert exchange.stopped
    finally:
        exchange.stop()
        if exchange.task is not None and not exchange.task.done():
            exchange.task.cancel()
            await asyncio.gather(exchange.task, return_exceptions=True)


async def test_double_native_cancel_has_no_standalone_phase_clear_barrier(
    db_path, tmp_path, monkeypatch
):
    env = _make_env(tmp_path)
    env.run.run_id = "double-cancel"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None
    planner_entered = asyncio.Event()
    teardown_boundary = asyncio.Event()
    never = asyncio.Event()
    stop_calls = []
    standalone_clears = []

    async def start(env, **kwargs):
        db = StateDB(db_path)
        await db.open()
        sid = await _make_session(db)
        env.test_db = db
        env.test_session_id = sid
        env._live_persist = {"db": db, "session_id": sid}
        original_update = db.update_session

        async def update(session_id, **fields):
            if "current_phase" in fields and fields["current_phase"] is None:
                standalone_clears.append(fields)
                teardown_boundary.set()
                await never.wait()
            return await original_update(session_id, **fields)

        monkeypatch.setattr(db, "update_session", update)

    async def plan(*args, **kwargs):
        planner_entered.set()
        await never.wait()

    async def stop(env, **kwargs):
        stop_calls.append(kwargs["status"])
        teardown_boundary.set()
        # Deliberately expose the existing precommit teardown cancellation
        # limit; this test does not claim native cancellation immunity.
        await never.wait()

    monkeypatch.setattr(flow, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(flow, "start_live_persist", start)
    monkeypatch.setattr(flow, "stop_live_persist", stop)
    monkeypatch.setattr(flow, "plan", plan)
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_run_notify_outcome_scope",
        lambda *args, **kwargs: None,
    )
    task = asyncio.create_task(flow._run_flow("fake/model", "work"))
    try:
        await asyncio.wait_for(planner_entered.wait(), 10)
        assert task.cancel()
        await asyncio.wait_for(teardown_boundary.wait(), 10)
        assert task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert standalone_clears == []
        assert stop_calls == ["cancelled"]
        row = await env.test_db.get_session(env.test_session_id)
        assert row["status"] == "running"  # The second cancel prevented commit.
        assert row["current_phase"] == "planning"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if getattr(env, "test_db", None) is not None:
            await env.test_db.close()
