# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Flow control targeting and lifecycle boundary regressions."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy import event

from lionagi.cli._runs import teardown_persist
from lionagi.cli.monitor import _session_to_row
from lionagi.cli.orchestrate import flow
from lionagi.cli.orchestrate._control import _enqueue_control_inner
from lionagi.cli.status import EXIT_UNKNOWN
from lionagi.ln.concurrency import move_on_after
from lionagi.state import engine as state_engine
from lionagi.state.db import StateDB
from tests.cli._worker_fixtures import create_worker_session
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
        ctx = await create_worker_session(db, run_id=env.run.run_id)
        sid = ctx["session_id"]
        assert kwargs["extra_node_metadata"]["flow_control_poller"] == "pending"
        await db.merge_session_node_metadata(sid, kwargs["extra_node_metadata"])
        if invocation_id:
            await db.update_session(sid, invocation_id=invocation_id)
        env._live_persist = {**ctx, "shared_db_registered": False}
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


async def test_run_id_prefix_still_controls_its_session(db_path):
    async with StateDB(db_path) as db:
        sid = await _named_session(db, "run-backed-session")
        await db.update_session(sid, run_id="20260801T020202-abc123")
    _output, rc = await _enqueue_control_inner(entity_id="20260801T020", verb="pause", payload=None)
    assert rc == 0
    async with StateDB(db_path) as db:
        assert len(await db.list_pending_session_controls(sid)) == 1


async def test_label_prefixing_an_invocation_and_a_run_is_refused(db_path):
    async with StateDB(db_path) as db:
        await db.create_invocation({"id": "20260801T03-label", "skill": "flow", "started_at": 1.0})
        labelled = await _named_session(db, "labelled-session", invocation_id="20260801T03-label")
        run_backed = await _named_session(db, "run-backed-session")
        await db.update_session(run_backed, run_id="20260801T030303-abc123")
    output, rc = await _enqueue_control_inner(entity_id="20260801T03", verb="pause", payload=None)
    assert rc == EXIT_UNKNOWN
    assert "ambiguous" in output
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(labelled) == []
        assert await db.list_pending_session_controls(run_backed) == []


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
            "lionhub.orchestration.engines.PlanningEngine.new_run", lambda *args, **kw: engine_run
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


async def test_teardown_opens_the_store_writable_only_with_a_control_to_close(
    db_path, tmp_path, monkeypatch
):
    """A run that queued no control must not take the writer lock at exit.

    The sweep that closes queued controls needs a writable handle, and a
    writable open takes the store's writer lock and re-runs the schema
    reconciliation. Most runs queue no control, so the common exit must ask
    with a read-only handle first and only reopen writable when there is a row
    to close.
    """
    env = _make_env(tmp_path)
    env.run.run_id = "no-control"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None

    opens: list[bool] = []
    real_init = StateDB.__init__

    def record_init(self, *args, **kwargs):
        opens.append(bool(kwargs.get("readonly", False)))
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(StateDB, "__init__", record_init)

    async def start(env, **kwargs):
        db = StateDB(db_path)
        await db.open()
        ctx = await create_worker_session(db, run_id=env.run.run_id)
        sid = ctx["session_id"]
        await db.update_session(sid, invocation_kind="flow")
        await db.merge_session_node_metadata(sid, kwargs["extra_node_metadata"])
        env._live_persist = {**ctx, "shared_db_registered": False}
        env.test_session_id = sid

    async def inner(*args, **kwargs):
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
    opens.clear()
    result, _status = await flow._run_flow("fake/model", "work")
    # Snapshot before the verification open below, which is itself writable.
    teardown_opens = list(opens)

    assert result == "done"
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(env.test_session_id) == []
    assert True in teardown_opens, "teardown should probe with a read-only handle"
    assert False not in teardown_opens[teardown_opens.index(True) :], (
        "a run with no queued control must not open the store writable at exit"
    )


async def test_teardown_closes_controls_even_when_its_status_write_raises(
    db_path, tmp_path, monkeypatch
):
    """A teardown that raises after its terminal commit still closes queued controls.

    The session's terminal status is what makes a queued control undeliverable,
    and it can be written before a later teardown step fails (for instance the
    run-manifest write). The close must not be skipped by that failure, or the
    control sits pending against a consumer that is already gone.
    """
    env = _make_env(tmp_path)
    env.run.run_id = "raising-teardown"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None

    real_stop = flow.stop_live_persist

    async def start(env, **kwargs):
        db = StateDB(db_path)
        await db.open()
        ctx = await create_worker_session(db, run_id=env.run.run_id)
        sid = ctx["session_id"]
        await db.update_session(sid, invocation_kind="flow")
        await db.merge_session_node_metadata(sid, kwargs["extra_node_metadata"])
        env._live_persist = {**ctx, "shared_db_registered": False}
        env.test_session_id = sid

    async def inner(*args, **kwargs):
        _output, rc = await _enqueue_control_inner(
            entity_id=env.test_session_id, verb="pause", payload=None
        )
        assert rc == 0
        return "done"

    async def stop_then_raise(env, **kwargs):
        await real_stop(env, **kwargs)
        raise OSError("run manifest write failed")

    monkeypatch.setattr(flow, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(flow, "start_live_persist", start)
    monkeypatch.setattr(flow, "stop_live_persist", stop_then_raise)
    monkeypatch.setattr(flow, "_run_flow_inner", inner)
    monkeypatch.setattr(
        "lionagi.state.completion_evidence.check_completion_evidence",
        lambda cwd: {"checked": False},
    )
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_run_notify_outcome_scope",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(OSError, match="run manifest write failed"):
        await flow._run_flow("fake/model", "work")
    async with StateDB(db_path) as db:
        session = await db.get_session(env.test_session_id)
        assert session["status"] == "completed"
        assert await db.list_pending_session_controls(env.test_session_id) == []
        controls = await db.fetch_all(
            "SELECT result FROM session_controls WHERE session_id = ?",
            (env.test_session_id,),
        )
    assert len(controls) == 1
    assert controls[0]["result"].startswith("rejected: run reached terminal status")


async def test_marker_write_error_is_swallowed_without_raising(tmp_path, monkeypatch, caplog):
    """A store error on the marker write is telemetry, not a run failure.

    The marker is best-effort: a busy store or a failed statement must be
    reported and left behind, never propagated out of the bounded write, or a
    telemetry write would end the run it describes.
    """
    env = _make_env(tmp_path, live_persist={"db": object(), "session_id": "s"})

    async def raises(db, session_id, patch):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(flow, "_persist_node_metadata_patch", raises)
    warnings: list[str] = []
    monkeypatch.setattr(flow, "_warn", warnings.append)

    with caplog.at_level("DEBUG", logger="lionagi.cli.orchestrate.flow"):
        await flow._persist_control_poller_state_bounded(env, "stopped")

    assert not warnings, warnings
    assert any("could not be persisted" in record.message for record in caplog.records), (
        "a failed marker write must be recorded, not dropped silently"
    )


async def test_marker_write_is_cancelled_when_it_outlives_its_budget(tmp_path, monkeypatch):
    """A marker write that never returns must be cancelled, not left running.

    A write left running holds the store's write lock, and teardown's own
    status write is queued behind it, so the run's teardown would wait on a
    wedged store it was told it would not wait on. The bounded write cancels
    the write at its budget and records that the marker may be stale.
    """
    env = _make_env(tmp_path, live_persist={"db": None, "session_id": "s"})

    async def never_returns(db, session_id, patch):
        await asyncio.Event().wait()

    monkeypatch.setattr(flow, "_persist_node_metadata_patch", never_returns)
    warnings: list[str] = []
    monkeypatch.setattr(flow, "_warn", warnings.append)
    # The env carries a db handle only so the marker path is entered; the write
    # itself is replaced above, so the value is never opened.
    env._live_persist = {"db": object(), "session_id": "s"}
    monkeypatch.setattr(flow, "_DRAIN_GRACE_SECONDS", 0.05)

    task = asyncio.create_task(flow._persist_control_poller_state_bounded(env, "stopped"))
    await asyncio.wait({task}, timeout=2)

    assert task.done(), "the bounded marker write must return, not await a wedged store"
    assert any("marker write was cancelled after its budget" in w for w in warnings), warnings


_BUSY_TIMEOUT_ENV = "LIONAGI_SQLITE_BUSY_TIMEOUT_MS"


async def _write_lock_wait(db, *, window: float) -> float | None:
    """Seconds this task waited to take the store's write lock, or None if still held.

    The lionagi Lock wrapper has no non-blocking acquire, so a short timed
    acquire distinguishes held (the wait times out) from free (it takes at once).
    """
    start = time.monotonic()
    while True:
        # The timed acquire runs in this task: before Python 3.12 asyncio.wait_for runs its
        # awaitable in a task of its own, which would then hold the lock this task releases.
        with move_on_after(0.02) as scope:
            await db._write_lock.acquire()
        if scope.cancelled_caught:
            if time.monotonic() - start >= window:
                return None
            await asyncio.sleep(0.01)
            continue
        db._write_lock.release()
        return time.monotonic() - start


async def test_marker_write_parked_in_the_driver_busy_wait_holds_the_lock_to_busy_timeout(
    db_path, monkeypatch
):
    """A driver call already in flight cannot be cancelled, so the lock outlives the budget.

    The budget bounds the caller: the helper returns and reports the write as
    abandoned. It cannot bound the store. A write parked in SQLite's busy wait
    is inside the driver, and cancelling the task does not interrupt it, so the
    store's write lock is held until that call returns at the busy timeout
    (here 1.5 seconds). This test pins both halves: the helper returns at its
    budget, and the lock stays held until the busy timeout, then frees within
    it plus the grace period, measured from the helper call.
    """
    # A busy timeout well above the budget, so a helper that is bounded returns
    # long before the parked call ends and the two are far apart in time rather
    # than a scheduling accident.
    busy_timeout_ms = 1500
    monkeypatch.setenv(_BUSY_TIMEOUT_ENV, str(busy_timeout_ms))
    # The pragma reads the module attribute at connect time, so give it the
    # value the environment resolver makes of the variable above.
    monkeypatch.setattr(
        state_engine, "SQLITE_BUSY_TIMEOUT_MS", state_engine._busy_timeout_from_env()
    )
    # Wide enough that the write reaches the driver's busy wait before the
    # budget cancels it, even on a loaded machine; a cancel that lands before
    # the driver call unwinds cleanly and is not the case under test.
    grace = 0.2
    monkeypatch.setattr(flow, "_DRAIN_GRACE_SECONDS", grace)
    # Opening the store announces the busy timeout once per process; keep that
    # flag as it was so other tests see the state they expect.
    monkeypatch.setattr(
        state_engine, "_busy_timeout_announced", state_engine._busy_timeout_announced
    )

    db = StateDB(db_path)
    await db.open()
    sid = await _make_session(db)
    # Warm a pooled connection so its pragmas (busy_timeout included) are in
    # place before the external writer takes the lock.
    await db.get_session(sid)

    holder = sqlite3.connect(str(db_path), isolation_level=None, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    warnings: list[str] = []
    monkeypatch.setattr(flow, "_warn", warnings.append)
    env = _make_env(db_path, live_persist={"db": db, "session_id": sid})

    try:
        t0 = time.monotonic()
        await flow._persist_control_poller_state_bounded(env, "stopped")
        returned_after = time.monotonic() - t0
        held_still = await _write_lock_wait(db, window=0.0)
        freed_after = await _write_lock_wait(db, window=busy_timeout_ms / 1000 + 3.0)
        freed_at = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        await db.close()

    assert returned_after < 2 * grace + 0.25, (
        f"the helper returned after {returned_after:.2f}s, not at its budget "
        f"({2 * grace:.2f}s); a parked driver call must not hold the caller"
    )
    assert held_still is None, (
        "the store's write lock was free at the budget, so this test is not "
        "exercising a parked driver call"
    )
    assert freed_after is not None, (
        "the write lock was still held past the busy timeout plus grace; the "
        "parked driver call never returned"
    )
    assert freed_at >= busy_timeout_ms / 1000 * 0.8, (
        f"the lock freed {freed_at:.2f}s after the helper was called, well before "
        f"the busy timeout ({busy_timeout_ms}ms); the write was not parked in the driver"
    )
    assert freed_at <= busy_timeout_ms / 1000 + 2 * grace + 0.5, (
        f"the lock freed {freed_at:.2f}s after the helper was called, past "
        f"the busy timeout ({busy_timeout_ms}ms) plus grace ({grace}s)"
    )
    assert any("abandoned" in w for w in warnings), (
        f"a parked driver call must be reported as abandoned: {warnings}"
    )


async def test_marker_write_is_cancelled_with_its_caller_before_the_budget(tmp_path, monkeypatch):
    """A caller cancelled inside the budget cancels its write and re-raises.

    The write must not outlive the caller: it holds the store teardown waits
    on. Cancelled before the budget expires, the write is cancelled at once and
    the cancellation is re-raised to the caller, and the report names the
    caller as the cause rather than a budget that had not run out.
    """
    env = _make_env(tmp_path, live_persist={"db": object(), "session_id": "s"})
    started = asyncio.Event()
    cancelled_inside = False

    async def waits_forever(db, session_id, patch):
        nonlocal cancelled_inside
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_inside = True
            raise

    monkeypatch.setattr(flow, "_persist_node_metadata_patch", waits_forever)
    warnings: list[str] = []
    monkeypatch.setattr(flow, "_warn", warnings.append)
    monkeypatch.setattr(flow, "_DRAIN_GRACE_SECONDS", 5.0)

    task = asyncio.ensure_future(flow._persist_control_poller_state_bounded(env, "stopped"))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    await asyncio.wait({task}, timeout=5)

    assert task.done(), "the caller's cancellation must return"
    assert task.cancelled(), "the caller's cancellation was swallowed"
    assert cancelled_inside, "the marker write was not cancelled with its caller"
    assert any("with its caller" in w for w in warnings), warnings
    assert not any("after its budget" in w for w in warnings), (
        f"the budget (5s) had not expired, yet the report says: {warnings}"
    )


async def test_marker_write_that_landed_then_stalled_is_not_reported_stale(db_path, monkeypatch):
    """A cancelled write that committed is not reported as a lost marker.

    A cancel can land after the merge committed but before the task unwinds, so
    the helper cannot tell a written marker from a lost one. Reporting "stale"
    would assert a negative it has not established; it reports that the outcome
    is unknown instead. The merge here writes for real and then stalls.
    """
    db = StateDB(db_path)
    await db.open()
    sid = await _make_session(db)
    real = flow._persist_node_metadata_patch

    async def lands_then_stalls(db, session_id, patch):
        await real(db, session_id, patch)
        await asyncio.Event().wait()

    monkeypatch.setattr(flow, "_persist_node_metadata_patch", lands_then_stalls)
    warnings: list[str] = []
    monkeypatch.setattr(flow, "_warn", warnings.append)
    monkeypatch.setattr(flow, "_DRAIN_GRACE_SECONDS", 0.05)
    env = _make_env(db_path, live_persist={"db": db, "session_id": sid})

    try:
        await flow._persist_control_poller_state_bounded(env, "stopped")
        metadata = (await db.get_session(sid)).get("node_metadata") or {}
    finally:
        await db.close()

    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata.get("flow_control_poller") == "stopped", metadata
    assert not any("stale" in w for w in warnings), (
        f"the merge landed, but the report claims a stale marker: {warnings}"
    )
    assert any("not known" in w for w in warnings), warnings


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
    monkeypatch.setattr(
        "lionhub.orchestration.engines.PlanningEngine.new_run", lambda *args, **kw: engine_run
    )
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
        ctx = await create_worker_session(db, run_id=env.run.run_id)
        sid = ctx["session_id"]
        env.test_db = db
        env.test_session_id = sid
        env._live_persist = {**ctx, "shared_db_registered": False}
        env.test_worker_owner = ctx["worker_spawn_owner"]
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
            await env.test_worker_owner.aclose()
            await env.test_db.close()


async def test_poller_stop_is_published_before_the_loop_is_cancelled(
    db_path, tmp_path, monkeypatch
):
    """A control cannot be admitted in the window between the poll loop dying and the marker.

    The final exchange collect runs after the poll loop can no longer apply a
    control but before the historical teardown published the stop. The
    enqueue below runs inside exactly that window: it must be refused, since
    the only consumer is already gone.
    """
    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.merge_session_node_metadata(sid, {"flow_control_poller": "pending"})
        env = _make_env(tmp_path, live_persist={"db": db, "session_id": sid})
        env.session.include_branches(_FakeBranch("researcher"))
        plan, dag = _plan_and_dag()
        seen: dict = {}

        class Exchange:
            def __init__(self):
                self.done = asyncio.Event()

            async def run(self, interval):
                await self.done.wait()

            def stop(self):
                self.done.set()

            async def collect_all(self):
                marker = (await db.get_session(sid))["node_metadata"]
                seen["marker"] = marker.get("flow_control_poller")
                output, rc = await _enqueue_control_inner(entity_id=sid, verb="pause", payload=None)
                seen["rc"] = rc
                seen["output"] = output

        env.exchange = Exchange()

        async def run_dag(graph, *, executor_ref, **kwargs):
            executor_ref["executor"] = _FakeExecutor()
            return {"operation_results": {"node-0": "done"}, "spawned_operations": 0}

        engine_run = MagicMock()
        engine_run.run_dag = run_dag
        monkeypatch.setattr(
            "lionhub.orchestration.engines.PlanningEngine.new_run", lambda *a, **k: engine_run
        )
        await flow._execute_dag(env, plan, dag, max_concurrent=1, max_ops=0)

        assert seen["marker"] == "stopped"
        assert seen["rc"] != 0
        assert await db.list_pending_session_controls(sid) == []


async def test_kill_writer_clears_the_live_phase_with_the_terminal_status(db_path):
    """`li kill` ends a run, so its terminal row must not keep the live phase.

    A flow killed by signal leaves its last phase behind; the kill writer is
    what terminalizes that row, and the phase clear has to ride its one status
    write rather than a separate statement that can be skipped.
    """
    from lionagi.cli.kill import _persist_cancel
    from lionagi.state.reasons import RunReasons

    async with StateDB(db_path) as db:
        sid = await _make_session(db)
        await db.update_session(sid, current_phase="executing")
        await _persist_cancel(
            db,
            "session",
            sid,
            reason_code=RunReasons.CANCELLED_FORCE_KILL,
            reason_summary="Force-killed (SIGKILL after grace period).",
            evidence={"kind": "kill_event", "signal": "sigkill", "pid": None},
        )
        row = await db.get_session(sid)

    assert row["status"] == "cancelled"
    assert row["current_phase"] is None


async def test_unreadable_invocation_store_refuses_instead_of_traceback(db_path, monkeypatch):
    """A store that cannot be read is a refusal, not a traceback.

    An id is unproven when the store cannot confirm it, so a file that exists
    but is not a readable store must reach the caller on the same
    FlowPlanError channel every other plan-time failure uses, rather than
    letting the read error escape as a traceback.
    """
    db_path.write_bytes(b"this is not a sqlite database")
    setup = AsyncMock(side_effect=AssertionError("setup ran before invocation validation"))
    monkeypatch.setattr(flow, "setup_orchestration", setup)

    with pytest.raises(flow.FlowPlanError, match="could not be checked"):
        await flow._run_flow("fake/model", "work", invocation_id="self-chosen")
    setup.assert_not_awaited()


@pytest.mark.parametrize("entity_id", ["2", "2026", "20260801"])
async def test_a_run_id_prefix_short_of_the_t_cannot_control_a_run(db_path, entity_id):
    """Before the `T` a run-id prefix is all digits: a number, a date, or the
    start of an entity id. It acts on no run, while read-only status keeps the
    broader lookup.
    """
    from lionagi.cli import status

    async with StateDB(db_path) as db:
        sid = await _named_session(db, "run-backed-session")
        await db.update_session(sid, run_id="20260801T020202-abc123")
    _output, rc = await _enqueue_control_inner(entity_id=entity_id, verb="pause", payload=None)
    assert rc == EXIT_UNKNOWN
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(sid) == []
        hit = await status._resolve_any_target(db, entity_id)
    assert hit is not None
    assert hit[1]["id"] == sid


async def test_a_run_id_prefix_through_the_t_controls_its_run(db_path):
    async with StateDB(db_path) as db:
        sid = await _named_session(db, "run-backed-session")
        await db.update_session(sid, run_id="20260801T020202-abc123")
    _output, rc = await _enqueue_control_inner(entity_id="20260801T", verb="pause", payload=None)
    assert rc == 0
    async with StateDB(db_path) as db:
        assert len(await db.list_pending_session_controls(sid)) == 1


async def test_a_branch_prefix_that_also_begins_a_run_is_refused(db_path):
    """The branch fallback refuses a cross-space match, as the entity search does."""
    from lionagi.cli import status
    from lionagi.cli._util import AmbiguousIdError

    async with StateDB(db_path) as db:
        branch_owner = await _named_session(db, "branch-owner-session")
        await db.create_progression("progression-branch")
        await db.create_branch(
            {
                "id": "20260801-0000-4000-8000-00000000000c",
                "session_id": branch_owner,
                "progression_id": "progression-branch",
            }
        )
        run_backed = await _named_session(db, "run-backed-session")
        await db.update_session(run_backed, run_id="20260801T020202-aaaaaa")
        with pytest.raises(AmbiguousIdError):
            await status._resolve_any_target(db, "20260801")
    output, rc = await _enqueue_control_inner(entity_id="20260801", verb="pause", payload=None)
    assert rc == EXIT_UNKNOWN
    assert "ambiguous" in output
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(branch_owner) == []
        assert await db.list_pending_session_controls(run_backed) == []


async def test_the_insert_refuses_a_flow_that_lost_its_poller_after_the_read(db_path, monkeypatch):
    """The admission read saw a live poller; the poller stopped before the write.

    The insert re-checks the marker, so the control is refused, and the refusal
    names the poller rather than a terminal status the row never reached.
    """
    async with StateDB(db_path) as db:
        sid = await _named_session(db, "losing-poller-session")
    real_insert = StateDB.insert_session_control
    stopped: list[str] = []

    async def _stop_then_insert(self, **kwargs):
        stopped.append(kwargs["session_id"])
        async with StateDB(db_path) as other:
            await other.merge_session_node_metadata(
                kwargs["session_id"], {"flow_control_poller": "stopped"}
            )
        return await real_insert(self, **kwargs)

    monkeypatch.setattr(StateDB, "insert_session_control", _stop_then_insert)
    output, rc = await _enqueue_control_inner(entity_id=sid, verb="pause", payload=None)
    assert stopped == [sid]
    assert rc == EXIT_UNKNOWN
    assert "lost its DAG execution poller" in output
    assert "terminal" not in output
    async with StateDB(db_path) as db:
        assert await db.list_pending_session_controls(sid) == []
        assert (await db.get_session(sid))["status"] == "running"


async def test_teardown_closes_a_control_its_poller_never_applied(db_path, tmp_path, monkeypatch):
    """A control admitted while the run is live but never applied is closed at teardown.

    The inner run below admits a pause and ends without applying it, as a run
    does when the control lands after its poller's last poll. Once the session
    is terminal nothing will apply it, so teardown records it as rejected
    instead of leaving it pending.
    """
    env = _make_env(tmp_path)
    env.run.run_id = "test-run"
    env.run.write_manifest = Mock()
    env._run_manifest = {}
    env.orc_profile_name = None

    async def start(env, **kwargs):
        db = StateDB(db_path)
        await db.open()
        ctx = await create_worker_session(db, run_id=env.run.run_id)
        sid = ctx["session_id"]
        await db.update_session(sid, invocation_kind="flow")
        await db.merge_session_node_metadata(sid, kwargs["extra_node_metadata"])
        env._live_persist = {**ctx, "shared_db_registered": False}
        env.test_session_id = sid

    async def inner(*args, **kwargs):
        _output, rc = await _enqueue_control_inner(
            entity_id=env.test_session_id, verb="pause", payload=None
        )
        assert rc == 0
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
    result, _status = await flow._run_flow("fake/model", "work")
    assert result == "done"
    async with StateDB(db_path) as db:
        session = await db.get_session(env.test_session_id)
        assert session["status"] == "completed"
        assert await db.list_pending_session_controls(env.test_session_id) == []
        controls = await db.fetch_all(
            "SELECT verb, result, applied_at FROM session_controls WHERE session_id = ?",
            (env.test_session_id,),
        )
    assert len(controls) == 1
    assert controls[0]["verb"] == "pause"
    assert controls[0]["applied_at"] is not None
    assert controls[0]["result"].startswith("rejected: run reached terminal status")
