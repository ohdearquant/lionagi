# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CLI Engine ownership boundaries with in-process store and owner stand-ins."""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest_asyncio.fixture(loop_scope="function")
async def engine_workers(monkeypatch):
    import lionagi.cli.engine as cli
    import lionagi.cli.kill as kill_module
    import lionagi.session.session as session_module
    import lionagi.state.cli_workers as worker_module
    import lionagi.state.db as db_module
    from lionagi._cli_worker_context import (
        _bind_cli_worker_recorder,
        _get_cli_worker_recorder,
    )

    state = SimpleNamespace(
        cli=cli,
        mode="normal",
        rows={},
        owners=[],
        tasks=[],
        events=[],
        updates=[],
        warnings=[],
        constructor_contexts=[],
        worker_contexts=[],
        setup_contexts=[],
        spawn_records=[],
        outcomes=[],
        lineage=[],
        constructor_started=asyncio.Event(),
        worker_started=asyncio.Event(),
        drain_started=asyncio.Event(),
        registration_started=asyncio.Event(),
        worker_release=asyncio.Event(),
        drain_release=asyncio.Event(),
    )
    state.worker_release.set()
    state.drain_release.set()

    class Store:
        async def open(self):
            state.setup_contexts.append(_get_cli_worker_recorder())
            state.events.append("open")

        async def insert_engine_run(self, **record):
            state.engine_record = record

        async def get_session(self, session_id):
            if state.mode == "preexisting" and not state.rows:
                state.rows[session_id] = {
                    "id": session_id,
                    "status": "completed",
                    "cli_worker_generation": "other-generation",
                    "name": "other session",
                }
            row = state.rows.get(session_id)
            return dict(row) if row is not None else None

        async def create_progression(self, progression_id):
            state.progression_id = progression_id

        async def create_session(self, row):
            state.created = dict(row)
            if state.mode == "signal_failure":
                raise OSError("scripted signal admission failure")
            if state.mode == "compatibility":
                from lionagi.state.compat import StateCompatibilityRefused

                raise StateCompatibilityRefused("create_session", "fixture", "not owned")
            if state.mode == "missing":
                return
            stored = dict(row)
            if state.mode == "different_generation":
                stored["cli_worker_generation"] = "different-generation"
            if state.mode == "terminal":
                stored["status"] = "completed"
            state.rows[row["id"]] = stored

        async def set_engine_run_lineage(
            self, run_id, *, invocation_id, signal_session_id, parent_session_id
        ):
            state.lineage.append((run_id, invocation_id, signal_session_id, parent_session_id))

        async def update_engine_run(self, run_id, status, **fields):
            state.events.append("engine-terminal")
            state.updates.append(("engine", run_id, status))

        async def record_engine_run_outcome(self, run_id, outcome):
            state.outcomes.append(outcome)
            state.events.append("outcome")

        async def register_cli_worker_spawn(self, record):
            state.registration_started.set()
            await state.worker_release.wait()
            state.spawn_records.append(record)
            state.events.append("registered")

        async def update_status(self, entity, session_id, *, new_status, **fields):
            state.events.append("session-terminal")
            state.updates.append((entity, session_id, new_status))

        async def close(self):
            state.events.append("db-close")

    state.db = Store()

    class Owner:
        def __init__(self, db, **association):
            assert db is state.db
            self.association = association
            self.closed = False
            state.owners.append(self)

        def bind(self):
            return _bind_cli_worker_recorder(self)

        async def aclose(self):
            if self.closed:
                return
            state.events.append("drain-start")
            state.drain_started.set()
            await state.drain_release.wait()
            if state.tasks:
                await asyncio.gather(*state.tasks)
            self.closed = True
            state.events.append("drained")

    class RuntimeSession:
        def __init__(self):
            self.id = "distinct-runtime-session"
            self.observer = SimpleNamespace(bind_db_persistence=self.bind_persistence)

        def bind_persistence(self, session_id, *, db):
            state.signal_binding = session_id

    class Engine:
        _total_agent_failure = False
        emission_failures = ()

        def __init__(self, **kwargs):
            self.run_id = "distinct-short-engine-run"
            state.constructor_contexts.append(_get_cli_worker_recorder())
            state.constructor_started.set()

            async def worker():
                state.worker_contexts.append(_get_cli_worker_recorder())
                state.worker_started.set()
                await state.worker_release.wait()

            state.tasks.append(asyncio.create_task(worker()))

        async def run(self, spec, *, on_event, session, **kwargs):
            await asyncio.sleep(0)
            state.runtime_session = session
            if state.mode == "exception":
                state.body_error = ValueError("scripted engine failure")
                raise state.body_error
            if state.mode == "cancelled":
                raise asyncio.CancelledError("scripted engine cancellation")
            return "done"

    def load_engine(module, name):
        if state.mode == "import_failure":
            raise ImportError("scripted engine import failure")
        return Engine

    monkeypatch.setattr(cli, "_import_engine_class", load_engine)
    state.engine_class = Engine
    state.owner_class = Owner
    state.real_owner_class = worker_module._WorkerSpawnOwner
    monkeypatch.setattr(cli, "progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "log_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "warn", state.warnings.append)
    monkeypatch.setattr(kill_module, "current_pid_markers", lambda: {"runner": "fixture"})
    monkeypatch.setattr(db_module, "StateDB", lambda: state.db)
    monkeypatch.setattr(worker_module, "_WorkerSpawnOwner", Owner)
    monkeypatch.setattr(session_module, "Session", RuntimeSession)
    try:
        yield state
    finally:
        state.worker_release.set()
        state.drain_release.set()
        for task in state.tasks:
            if not task.done():
                task.cancel()
        if state.tasks:
            await asyncio.gather(*state.tasks, return_exceptions=True)


def _args(*, no_persist=False):
    return argparse.Namespace(
        kind="research",
        spec="scripted work",
        test_cmd=None,
        export_dir=None,
        model=None,
        max_depth=None,
        max_agents=None,
        session_id="distinct-parent-session",
        invocation_id="distinct-invocation",
        no_persist=no_persist,
    )


async def test_engine_constructor_worker_inherits_the_signal_owner(engine_workers):
    from lionagi._cli_worker_context import _get_cli_worker_recorder

    h = engine_workers
    assert await h.cli._do_engine_run(_args()) == 0
    owner = h.owners[0]
    assert h.constructor_contexts == [owner]
    assert h.worker_contexts == [owner]
    signal_id = h.created["id"]
    generation = h.created["cli_worker_generation"]
    assert str(UUID(generation)) == generation
    assert h.rows[signal_id]["status"] == "running"
    assert owner.association == {
        "session_id": signal_id,
        "generation_id": generation,
        "engine_run_id": h.engine_record["run_id"],
    }
    assert signal_id == h.engine_record["run_id"] == h.signal_binding
    assert signal_id not in {
        _args().session_id,
        h.runtime_session.id,
        "distinct-short-engine-run",
    }
    assert h.engine_record["session_id"] == _args().session_id
    assert h.lineage == [(signal_id, "distinct-invocation", signal_id, "distinct-parent-session")]
    assert _get_cli_worker_recorder() is None


async def test_constructor_admitted_work_finishes_before_terminal_or_close(engine_workers):
    h = engine_workers
    h.worker_release.clear()
    call = asyncio.create_task(h.cli._do_engine_run(_args()))
    try:
        await asyncio.wait_for(h.worker_started.wait(), 1)
        await asyncio.wait_for(h.drain_started.wait(), 1)
        assert h.tasks and not h.tasks[0].done()
        assert not call.done()
        assert h.updates == []
        assert "db-close" not in h.events
        h.worker_release.set()
        assert await asyncio.wait_for(call, 1) == 0
        assert h.events.index("drained") < h.events.index("engine-terminal")
        assert h.events.index("drained") < h.events.index("session-terminal")
        assert h.events.index("drained") < h.events.index("db-close")
    finally:
        h.worker_release.set()
        h.drain_release.set()
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.parametrize("mode", ["normal", "exception", "cancelled", "import_failure"])
async def test_all_engine_exit_paths_drain_before_status_and_db_close(engine_workers, mode):
    h = engine_workers
    h.mode = mode
    h.drain_release.clear()
    call = asyncio.create_task(h.cli._do_engine_run(_args()))
    try:
        await asyncio.wait_for(h.drain_started.wait(), 1)
        assert not call.done()
        assert h.updates == []
        assert "db-close" not in h.events
        h.drain_release.set()
        if mode == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call, 1)
        else:
            assert await asyncio.wait_for(call, 1) == (0 if mode == "normal" else 1)
        assert h.events.index("drained") < h.events.index("engine-terminal")
        assert h.events.index("drained") < h.events.index("session-terminal")
        assert h.events.index("drained") < h.events.index("db-close")
    finally:
        h.drain_release.set()
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.parametrize("mode", ["missing", "different_generation", "terminal"])
async def test_post_create_collision_or_missing_row_refuses_before_constructor(
    engine_workers, mode
):
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    h = engine_workers
    h.mode = mode
    with pytest.raises(_WorkerSpawnAdmissionError):
        await h.cli._do_engine_run(_args())
    assert not h.constructor_started.is_set()
    assert h.owners == []
    assert h.updates == []
    assert h.events[-1] == "db-close"


async def test_compatibility_refusal_does_not_degrade_to_unrecorded_engine(engine_workers):
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
    from lionagi.state.compat import StateCompatibilityRefused

    h = engine_workers
    h.mode = "compatibility"
    with pytest.raises(_WorkerSpawnAdmissionError) as caught:
        await h.cli._do_engine_run(_args())
    assert isinstance(caught.value.__cause__, StateCompatibilityRefused)
    assert caught.value.retryable is False
    assert not h.constructor_started.is_set()
    assert h.owners == []
    assert h.updates == []
    assert h.events[-1] == "db-close"


@pytest.mark.parametrize("mode", ["no_persist", "signal_failure", "preexisting"])
async def test_unpersisted_engine_masks_an_outer_owner(engine_workers, mode):
    from lionagi._cli_worker_context import (
        _bind_cli_worker_recorder,
        _get_cli_worker_recorder,
    )

    h = engine_workers
    h.mode = mode
    outer = object()
    with _bind_cli_worker_recorder(outer):
        assert await h.cli._do_engine_run(_args(no_persist=mode == "no_persist")) == 0
        assert _get_cli_worker_recorder() is outer
    assert h.constructor_contexts == [None]
    assert h.worker_contexts == [None]
    assert h.owners == []
    assert h.setup_contexts == ([] if mode == "no_persist" else [None])
    assert all(entity != "session" for entity, _key, _status in h.updates)
    if mode == "preexisting":
        row = next(iter(h.rows.values()))
        assert row["status"] == "completed"
        assert row["cli_worker_generation"] == "other-generation"
        assert row["name"] == "other session"
        assert any("already exists" in warning for warning in h.warnings)
    if mode == "no_persist":
        assert "open" not in h.events
        assert "db-close" not in h.events


@pytest.mark.parametrize("mode", ["normal", "exception"])
async def test_cancellation_during_real_owner_drain_terminalizes_then_closes(
    engine_workers, monkeypatch, mode
):
    import lionagi.state.cli_workers as worker_module
    from lionagi._cli_worker_context import _get_cli_worker_recorder

    h = engine_workers
    h.mode = mode
    h.worker_release.clear()

    class ObservedOwner(h.real_owner_class):
        async def aclose(self):
            h.drain_started.set()
            try:
                await super().aclose()
            except asyncio.CancelledError as exc:
                if not hasattr(h, "drain_cancel"):
                    h.drain_cancel = exc
                raise
            h.events.append("drained")

    class AdmittedEngine(h.engine_class):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            admission = _get_cli_worker_recorder().admit()

            async def register():
                try:
                    await admission.record(
                        {
                            "pid": 4242,
                            "pgid": 4242,
                            "pid_create_time": 100.0,
                            "pid_host": "fixture-host",
                            "pid_boot_time": 1.0,
                            "process_identity_mode": "local",
                        }
                    )
                finally:
                    admission.finish()

            task = asyncio.create_task(register())
            admission.retain(task)
            h.tasks.append(task)

    monkeypatch.setattr(worker_module, "_WorkerSpawnOwner", ObservedOwner)
    monkeypatch.setattr(h.cli, "_import_engine_class", lambda _module, _name: AdmittedEngine)
    call = asyncio.create_task(h.cli._do_engine_run(_args()))
    try:
        await asyncio.wait_for(h.registration_started.wait(), 1)
        await asyncio.wait_for(h.drain_started.wait(), 1)
        call.cancel()
        await asyncio.sleep(0)
        assert not call.done()
        assert h.spawn_records == []
        assert h.updates == []
        assert "db-close" not in h.events
        h.worker_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(call, 1)
        observed = caught.value if caught.value is h.drain_cancel else caught.value.__context__
        assert observed is h.drain_cancel
        assert call.cancelled()
        if mode == "exception":
            assert observed.__context__ is h.body_error
        assert len(h.spawn_records) == 1
        assert all(status == "cancelled" for _entity, _key, status in h.updates)
        assert {entity for entity, _key, _status in h.updates} == {"engine", "session"}
        assert h.outcomes[-1]["status"] == "cancelled"
        assert h.outcomes[-1]["reason_code"] == "cancelled"
        assert h.outcomes[-1]["timing"]["ended_at"] >= h.outcomes[-1]["timing"]["started_at"]
        assert h.events.index("registered") < h.events.index("drained")
        assert h.events.index("drained") < h.events.index("engine-terminal")
        assert h.events.index("drained") < h.events.index("session-terminal")
        assert h.events.index("session-terminal") < h.events.index("db-close")
    finally:
        h.worker_release.set()
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)


async def test_incomplete_owner_drain_never_terminalizes_or_closes(engine_workers, monkeypatch):
    import lionagi.state.cli_workers as worker_module

    h = engine_workers
    h.worker_release.clear()
    failure = RuntimeError("scripted drain mechanism failure")

    class IncompleteOwner(h.owner_class):
        async def aclose(self):
            h.drain_started.set()
            raise failure

    monkeypatch.setattr(worker_module, "_WorkerSpawnOwner", IncompleteOwner)
    with pytest.raises(RuntimeError) as caught:
        await h.cli._do_engine_run(_args())
    assert caught.value is failure
    assert h.worker_started.is_set()
    assert h.tasks and not h.tasks[0].done()
    assert h.updates == []
    assert h.outcomes == []
    assert "db-close" not in h.events
    assert not h.owners[0].closed
