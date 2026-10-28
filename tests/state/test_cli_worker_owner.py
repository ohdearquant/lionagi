# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

_SESSION = "persisted-owner"
_GENERATION = "902646b9-53e2-4863-952f-da579eb0dabc"
_OTHER_GENERATION = "c06b1529-665f-4ee4-9ff1-c85d5fb2d9da"


@pytest.fixture
def api():
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
    from lionagi.state.cli_workers import (
        _consume_worker_spawn_continuation,
        _prepare_worker_spawn_continuation,
        _WorkerSpawnOwner,
    )

    return SimpleNamespace(
        Owner=_WorkerSpawnOwner,
        Error=_WorkerSpawnAdmissionError,
        prepare=_prepare_worker_spawn_continuation,
        consume=_consume_worker_spawn_continuation,
    )


@pytest.fixture
def store():
    class Store:
        def __init__(self):
            self.record_entered = asyncio.Event()
            self.commit = asyncio.Event()
            self.read_entered = asyncio.Event()
            self.read_ready = asyncio.Event()
            self.read_ready.set()
            self.inputs = []
            self.records = []
            self.registration_error = None
            self.read_error = None
            self.reads = []
            self.close_calls = 0
            self.session = {
                "id": _SESSION,
                "status": "running",
                "cli_worker_generation": _GENERATION,
            }

        async def register_cli_worker_spawn(self, record):
            self.inputs.append(dict(record))
            self.record_entered.set()
            await self.commit.wait()
            if self.registration_error is not None:
                raise self.registration_error
            self.records.append(dict(record))

        async def get_session(self, session_id):
            self.reads.append(session_id)
            self.read_entered.set()
            await self.read_ready.wait()
            if self.read_error is not None:
                raise self.read_error
            return self.session

        async def close(self):
            self.close_calls += 1

    return Store()


@pytest_asyncio.fixture(loop_scope="function")
async def spawn_task(store):
    tasks = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    try:
        yield spawn
    finally:
        store.commit.set()
        store.read_ready.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def facts():
    return {
        "pid": 12345,
        "pgid": 12345,
        "pid_create_time": 1000.25,
        "pid_host": "synthetic-host",
        "pid_boot_time": 10.5,
        "process_identity_mode": "local",
    }


def owner(api, store, **kwargs):
    return api.Owner(store, session_id=_SESSION, generation_id=_GENERATION, **kwargs)


@pytest.mark.asyncio(loop_scope="function")
async def test_a_worker_attempt_keeps_one_id_and_exact_association_for_replay(api, store):
    owned = owner(api, store, run_id="parent-run", engine_run_id="engine-run", branch_id="branch")
    admitted = owned.admit()
    identity = facts()
    store.commit.set()
    try:
        await admitted.record(identity)
        identity["pid_host"] = "changed-after-record"
        await admitted.record(facts())
        first, replay = store.records
        assert first == replay, "an identical replay must keep the original attempt and fields"
        assert set(first) == {
            "spawn_id",
            "session_id",
            "generation_id",
            "run_id",
            "engine_run_id",
            "branch_id",
            "pid",
            "pgid",
            "pid_create_time",
            "pid_host",
            "pid_boot_time",
            "process_identity_mode",
        }
        assert first["session_id"] == _SESSION and first["generation_id"] == _GENERATION
        assert (first["run_id"], first["engine_run_id"], first["branch_id"]) == (
            "parent-run",
            "engine-run",
            "branch",
        )
        assert first["pid_host"] == "synthetic-host"
        with pytest.raises(api.Error, match="identity changed"):
            await admitted.record(identity)
        assert len(store.inputs) == 2
    finally:
        admitted.finish()
        admitted.finish()
        await owned.aclose()
        await owned.aclose()
    assert store.close_calls == 0


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "borrowed"},
        {"pid": True},
        {"pgid": 12346},
        {"process_identity_mode": "remote"},
    ],
    ids=["association-override", "boolean-pid", "different-group", "foreign-mode"],
)
@pytest.mark.asyncio(loop_scope="function")
async def test_invalid_identity_refuses_before_the_store(api, store, change):
    owned = owner(api, store)
    admitted = owned.admit()
    try:
        with pytest.raises(api.Error) as raised:
            await admitted.record({**facts(), **change})
        assert str(raised.value) == "invalid observed worker identity"
        assert raised.value.retryable is False
        assert store.inputs == [], "invalid facts must not reach the durable write"
    finally:
        admitted.finish()
        await owned.aclose()


@pytest.mark.asyncio(loop_scope="function")
async def test_store_refusal_remains_typed_and_keeps_its_primary_cause(api, store):
    owned = owner(api, store)
    admitted = owned.admit()
    primary = ValueError("synthetic current-generation refusal")
    store.registration_error = primary
    store.commit.set()
    try:
        with pytest.raises(api.Error) as raised:
            await admitted.record(facts())
        assert isinstance(raised.value, RuntimeError) and raised.value.retryable is False
        assert str(raised.value) == "worker spawn registration refused"
        assert raised.value.__cause__ is primary
        assert store.records == []
    finally:
        admitted.finish()
        await owned.aclose()


@pytest.mark.asyncio(loop_scope="function")
async def test_a_completed_registration_refusal_does_not_block_owner_drain(api, store, spawn_task):
    owned = owner(api, store)
    admitted = owned.admit()
    primary = ValueError("synthetic durable refusal")
    store.registration_error = primary
    closing = asyncio.Event()

    async def work():
        try:
            await admitted.record(facts())
        finally:
            admitted.finish()

    async def close():
        closing.set()
        await owned.aclose()

    worker = spawn_task(work())
    admitted.retain(worker)
    await asyncio.wait_for(store.record_entered.wait(), 1.0)
    closer = spawn_task(close())
    await asyncio.wait_for(closing.wait(), 1.0)
    assert not closer.done(), "registration is still pending before the refusal completes"
    store.commit.set()
    completed, _pending = await asyncio.wait({worker, closer}, timeout=1.0)
    assert completed == {worker, closer}, "a completed failed attempt is not pending ownership"
    with pytest.raises(api.Error) as raised:
        worker.result()
    assert raised.value.__cause__ is primary
    assert closer.result() is None and store.records == []
    assert store.close_calls == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_cancelled_close_drains_registration_and_late_cleanup_before_returning(
    api, store, spawn_task
):
    owned = owner(api, store)
    admitted = owned.admit()
    cleanup_entered, cleanup_done, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close_cancellation = []

    async def work():
        try:
            await admitted.record(facts())
            cleanup_entered.set()
            await cleanup_done.wait()
        finally:
            admitted.finish()

    async def close():
        closing.set()
        try:
            await owned.aclose()
        except asyncio.CancelledError as exc:
            close_cancellation.append(exc)
            raise

    worker = spawn_task(work())
    admitted.retain(worker)
    await asyncio.wait_for(store.record_entered.wait(), 1.0)
    closer = spawn_task(close())
    await asyncio.wait_for(closing.wait(), 1.0)
    assert not closer.done() and not worker.done()
    with pytest.raises(api.Error, match="closing"):
        owned.admit()
    closer.cancel()
    turn = asyncio.Event()
    asyncio.get_running_loop().call_soon(turn.set)
    await asyncio.wait_for(turn.wait(), 1.0)
    assert not closer.done(), "consumer cancellation must not discard an admitted registration"
    store.commit.set()
    await asyncio.wait_for(cleanup_entered.wait(), 1.0)
    assert not closer.done(), "owner drain must include the retained late-cleanup work"
    cleanup_done.set()
    await asyncio.wait_for(worker, 1.0)
    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(closer, 1.0)
    assert len(close_cancellation) == 1
    assert (
        raised.value is close_cancellation[0] or raised.value.__context__ is close_cancellation[0]
    )
    assert len(store.records) == 1 and store.close_calls == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_owner_drain_also_waits_for_a_retained_task_after_its_token_finishes(
    api, store, spawn_task
):
    owned = owner(api, store)
    admitted = owned.admit()
    finished, cleanup_done, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        admitted.finish()
        finished.set()
        await cleanup_done.wait()

    async def close():
        closing.set()
        await owned.aclose()

    worker = spawn_task(work())
    admitted.retain(worker)
    await asyncio.wait_for(finished.wait(), 1.0)
    closer = spawn_task(close())
    await asyncio.wait_for(closing.wait(), 1.0)
    assert not closer.done()
    with pytest.raises(api.Error):
        api.prepare(owned, db_close_witness=store)
    cleanup_done.set()
    await asyncio.wait_for(worker, 1.0)
    await asyncio.wait_for(closer, 1.0)
    assert api.prepare(owned, db_close_witness=store) is not None


@pytest.mark.asyncio(loop_scope="function")
async def test_continuation_requires_drained_owner_and_the_exact_db_witness(api, store):
    owned = owner(api, store)
    admitted = owned.admit()
    with pytest.raises(api.Error):
        api.prepare(owned, db_close_witness=store)
    admitted.finish()
    await owned.aclose()
    with pytest.raises(api.Error):
        api.prepare(owned, db_close_witness=object())
    capability = api.prepare(owned, db_close_witness=store)
    assert capability is not None
    with pytest.raises(api.Error):
        api.prepare(owned, db_close_witness=store)
    assert store.close_calls == 0, "the adapter must not close the parent's DB"


@pytest.mark.parametrize("mismatch", ["missing", "terminal", "generation", "session"])
@pytest.mark.asyncio(loop_scope="function")
async def test_a_mismatching_continuation_read_burns_the_capability(api, store, mismatch):
    owned = owner(api, store)
    await owned.aclose()
    capability = api.prepare(owned, db_close_witness=store)
    if mismatch == "missing":
        store.session = None
    elif mismatch == "terminal":
        store.session["status"] = "completed"
    elif mismatch == "generation":
        store.session["cli_worker_generation"] = _OTHER_GENERATION
    else:
        store.session["id"] = "another-session"
    with pytest.raises(api.Error, match="no longer matches"):
        await api.consume(capability, store, run_id="child-run")
    with pytest.raises(api.Error, match="already consumed"):
        await api.consume(capability, store, run_id="child-run")
    assert store.reads == [_SESSION] and store.inputs == []


@pytest.mark.asyncio(loop_scope="function")
async def test_a_failed_continuation_read_also_burns_the_capability(api, store):
    owned = owner(api, store)
    await owned.aclose()
    capability = api.prepare(owned, db_close_witness=store)
    primary = OSError("synthetic read refusal")
    store.read_error = primary
    with pytest.raises(api.Error) as raised:
        await api.consume(capability, store)
    assert raised.value.__cause__ is primary
    with pytest.raises(api.Error, match="already consumed"):
        await api.consume(capability, store)
    assert store.reads == [_SESSION]


@pytest.mark.asyncio(loop_scope="function")
async def test_continuation_is_consumed_before_its_read_and_uses_child_diagnostics(
    api, store, spawn_task
):
    parent = owner(api, store, run_id="parent-run")
    await parent.aclose()
    capability = api.prepare(parent, db_close_witness=store)
    parent_record = {"spawn_id": "parent-attempt", "run_id": "parent-run", "recorded_at": 1.0}
    store.records.append(dict(parent_record))
    store.read_ready.clear()
    first = spawn_task(api.consume(capability, store, run_id="child-run", branch_id="child-branch"))
    await asyncio.wait_for(store.read_entered.wait(), 1.0)
    second = spawn_task(api.consume(capability, store, run_id="second-child"))
    completed, _pending = await asyncio.wait({second}, timeout=1.0)
    assert second in completed, "a consumed continuation must refuse before a second session read"
    with pytest.raises(api.Error, match="already consumed"):
        second.result()
    assert not first.done() and store.reads == [_SESSION]
    store.read_ready.set()
    child = await asyncio.wait_for(first, 1.0)
    assert child.session_id == parent.session_id and child.generation_id == parent.generation_id
    admitted = child.admit()
    store.commit.set()
    try:
        await admitted.record(facts())
    finally:
        admitted.finish()
        await child.aclose()
    assert store.records[0] == parent_record, "a join must not rewrite the parent's records"
    assert store.records[1]["run_id"] == "child-run"
    assert store.records[1]["branch_id"] == "child-branch"
    assert store.close_calls == 0


@pytest.mark.parametrize("capability", [None, object()], ids=["missing", "unrelated"])
@pytest.mark.asyncio(loop_scope="function")
async def test_known_ids_without_a_private_capability_do_not_authorize_a_join(
    api, store, capability
):
    with pytest.raises(api.Error, match="required"):
        await api.consume(capability, store, run_id="child-run")
    assert store.reads == [] and store.inputs == []
