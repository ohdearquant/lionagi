# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline real-store Operator turn ordering and terminal record probes."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.cli import _runs as runs_mod
from lionagi.state.db import StateDB
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import OperatorStore
from tests.apps_studio_server.test_operator_protocol import (
    MessageWritingEngine,
    _patch_state_db,
    _wait_done,
    _wait_done_since,
)

_CONTEXT = {"space": "mission", "route": "/", "filters": {}}


def _done_for(frames, request_id):
    return [f for f in frames if f["type"] == "done" and f["requestId"] == request_id]


@pytest.mark.asyncio
async def test_done_waits_for_teardown_before_next_turn(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    entered, release = asyncio.Event(), asyncio.Event()
    real_teardown = runs_mod.teardown_agent_persist
    real_finish = store.finish_turn
    records = []
    calls = 0

    async def pause_first_teardown(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await real_teardown(*args, **kwargs)

    async def record_finish(request_id, *args, **kwargs):
        records.append(request_id)
        return await real_finish(request_id, *args, **kwargs)

    monkeypatch.setattr(runs_mod, "teardown_agent_persist", pause_first_teardown)
    monkeypatch.setattr(store, "finish_turn", record_finish)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="turn race"))["conversation"]["id"]
    try:
        first = await coordinator.submit(
            cid, instruction="first", context=_CONTEXT, expected_last_sequence=0
        )
        await asyncio.wait_for(entered.wait(), 5)
        # If done is exposed while teardown is held, immediately exercise the
        # next turn; otherwise release teardown before waiting for completion.
        held_frames = await store.list_frames(cid)
        early_done = bool(_done_for(held_frames, first["requestId"]))
        if early_done:
            first_frames = held_frames
        else:
            assert records.count(first["requestId"]) == 0
            assert (await store.get_conversation(cid))["activeRequestId"] == first["requestId"]
            release.set()
            first_frames = await _wait_done(store, cid)
        first_done = _done_for(first_frames, first["requestId"])
        assert len(first_done) == 1 and first_done[0]["payload"]["outcome"] == "completed"
        last_seq = first_frames[-1]["sequence"]
        second = await coordinator.submit(
            cid, instruction="second", context=_CONTEXT, expected_last_sequence=last_seq
        )
        later_frames = await _wait_done_since(store, cid, last_seq)
        second_done = _done_for(later_frames, second["requestId"])
        assert not [
            f
            for f in later_frames
            if f["type"] == "error" and f["payload"]["error"]["code"] == "model_failure"
        ]
        assert len(second_done) == 1 and second_done[0]["payload"]["outcome"] == "completed"
        assert not early_done, "first done was visible before its teardown finished"
        assert records.count(first["requestId"]) == records.count(second["requestId"]) == 1
    finally:
        release.set()
        await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_cancel_during_teardown_writes_one_turn_record(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    entered, release = asyncio.Event(), asyncio.Event()
    cancel_processed = asyncio.Event()
    real_teardown, real_finish = runs_mod.teardown_agent_persist, store.finish_turn
    real_request_cancel = store.request_cancel
    records = []

    async def paused_teardown(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_teardown(*args, **kwargs)

    async def record_finish(request_id, *args, **kwargs):
        records.append(request_id)
        return await real_finish(request_id, *args, **kwargs)

    async def track_cancel(*args, **kwargs):
        result = await real_request_cancel(*args, **kwargs)
        cancel_processed.set()
        return result

    monkeypatch.setattr(runs_mod, "teardown_agent_persist", paused_teardown)
    monkeypatch.setattr(store, "finish_turn", record_finish)
    monkeypatch.setattr(store, "request_cancel", track_cancel)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    cancel_task = None
    try:
        accepted = await coordinator.submit(
            cid, instruction="first", context=_CONTEXT, expected_last_sequence=0
        )
        await asyncio.wait_for(entered.wait(), 5)
        task = coordinator._tasks[accepted["requestId"]]
        cancel_task = asyncio.create_task(coordinator.cancel(cid, accepted["requestId"]))
        await asyncio.wait_for(cancel_processed.wait(), 5)
        held_frames = await store.list_frames(cid)
        assert _done_for(held_frames, accepted["requestId"]) == []
        assert (await store.get_conversation(cid))["activeRequestId"] == accepted["requestId"]
        release.set()
        result = await asyncio.wait_for(cancel_task, 5)
        assert result["cancelRequested"] is True
        await asyncio.gather(task, return_exceptions=True)
        frames = await _wait_done(store, cid)
        assert len(_done_for(frames, accepted["requestId"])) == 1
        assert records.count(accepted["requestId"]) == 1
        assert (await store.get_conversation(cid))["activeRequestId"] is None
        link = next(
            frame["payload"]["result"]
            for frame in frames
            if frame["type"] == "tool_result" and frame["payload"].get("result", {}).get("runId")
        )
        async with StateDB(path) as db:
            assert (await db.get_session(link["runId"]))["status"] == "completed"
    finally:
        release.set()
        if cancel_task is not None:
            await asyncio.gather(cancel_task, return_exceptions=True)
        await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_repeat_cancel_and_shutdown_do_not_recancel_teardown(tmp_path, monkeypatch):
    class TrackingTask(asyncio.Task):
        def __init__(self, coroutine, **kwargs):
            super().__init__(coroutine, **kwargs)
            self.cancel_calls = 0

        def cancel(self, *args, **kwargs):
            self.cancel_calls += 1
            return super().cancel(*args, **kwargs)

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    entered, release, interrupted, shutdown_started = (asyncio.Event() for _ in range(4))
    real_teardown = runs_mod.teardown_agent_persist

    async def paused_teardown(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise
        return await real_teardown(*args, **kwargs)

    async def start_shutdown():
        shutdown_started.set()
        await coordinator.shutdown()

    monkeypatch.setattr(runs_mod, "teardown_agent_persist", paused_teardown)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    shutdown_task = None
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    try:
        loop.set_task_factory(lambda loop, coro, **kwargs: TrackingTask(coro, loop=loop, **kwargs))
        try:
            accepted = await coordinator.submit(
                cid, instruction="first", context=_CONTEXT, expected_last_sequence=0
            )
        finally:
            loop.set_task_factory(previous_factory)
        request_id = accepted["requestId"]
        await asyncio.wait_for(entered.wait(), 5)
        turn_task = coordinator._tasks[request_id]
        assert isinstance(turn_task, TrackingTask)
        assert (await coordinator.cancel(cid, request_id))["cancelRequested"] is True
        shutdown_task = asyncio.create_task(start_shutdown())
        await asyncio.wait_for(shutdown_started.wait(), 5)
        assert (await coordinator.cancel(cid, request_id))["cancelRequested"] is False
        assert turn_task.cancel_calls == 1
        assert not interrupted.is_set()
        assert not shutdown_task.done()
        release.set()
        await asyncio.wait_for(shutdown_task, 10)
        frames = await store.list_frames(cid)
        assert len(_done_for(frames, request_id)) == 1
        assert (await store.get_conversation(cid))["activeRequestId"] is None
        link = next(
            frame["payload"]["result"]
            for frame in frames
            if frame["type"] == "tool_result" and frame["payload"].get("result", {}).get("runId")
        )
        async with StateDB(path) as db:
            assert (await db.get_session(link["runId"]))["status"] == "completed"
    finally:
        loop.set_task_factory(previous_factory)
        release.set()
        if shutdown_task is not None:
            await asyncio.gather(shutdown_task, return_exceptions=True)
        else:
            await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_teardown_exception_writes_one_turn_record(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    real_teardown, real_finish = runs_mod.teardown_agent_persist, store.finish_turn
    records = []

    async def teardown_then_raise(*args, **kwargs):
        # Complete real cleanup first, then fail at the teardown boundary.
        await real_teardown(*args, **kwargs)
        raise RuntimeError("injected teardown failure")

    async def record_finish(request_id, *args, **kwargs):
        records.append(request_id)
        return await real_finish(request_id, *args, **kwargs)

    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown_then_raise)
    monkeypatch.setattr(store, "finish_turn", record_finish)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    try:
        accepted = await coordinator.submit(
            cid, instruction="first", context=_CONTEXT, expected_last_sequence=0
        )
        frames = await _wait_done(store, cid)
        assert len(_done_for(frames, accepted["requestId"])) == 1
        assert records.count(accepted["requestId"]) == 1
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_failure_before_live_setup_writes_one_turn_record(tmp_path, monkeypatch):
    from lionagi.studio.operator import coordinator as coordinator_mod

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    real_finish = store.finish_turn
    records = []

    def fail_compilation(*_args, **_kwargs):
        raise RuntimeError("injected history setup failure")

    async def record_finish(request_id, *args, **kwargs):
        records.append(request_id)
        return await real_finish(request_id, *args, **kwargs)

    monkeypatch.setattr(coordinator_mod, "compile_operator_history", fail_compilation)
    monkeypatch.setattr(store, "finish_turn", record_finish)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    try:
        accepted = await coordinator.submit(
            cid, instruction="first", context=_CONTEXT, expected_last_sequence=0
        )
        frames = await _wait_done(store, cid)
        done = _done_for(frames, accepted["requestId"])
        assert len(done) == 1 and done[0]["payload"]["outcome"] == "failed"
        assert records.count(accepted["requestId"]) == 1
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_unhandled_base_exception_finishes_turn_as_failed(tmp_path, monkeypatch):
    from lionagi.studio.operator import coordinator as coordinator_mod

    class TurnInterrupted(BaseException):
        pass

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    real_finish = store.finish_turn
    records = []

    def interrupt_compilation(*_args, **_kwargs):
        raise TurnInterrupted("injected interruption")

    async def record_finish(request_id, *args, **kwargs):
        records.append(request_id)
        return await real_finish(request_id, *args, **kwargs)

    monkeypatch.setattr(coordinator_mod, "compile_operator_history", interrupt_compilation)
    monkeypatch.setattr(store, "finish_turn", record_finish)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    try:
        accepted = await coordinator.submit(
            cid, instruction="first", context=_CONTEXT, expected_last_sequence=0
        )
        request_id = accepted["requestId"]
        frames = await _wait_done(store, cid)
        done = _done_for(frames, request_id)
        assert len(done) == 1 and done[0]["payload"]["outcome"] == "failed"
        assert records.count(request_id) == 1
        assert (await store.get_turn(request_id))["status"] == "failed"
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_cancel_during_setup_failure_recovery_finishes_turn(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    entered, release = asyncio.Event(), asyncio.Event()
    real_finish = store.finish_turn
    records = []

    async def fail_setup(*_args, **_kwargs):
        raise RuntimeError("injected canonical setup failure")

    async def held_recovery(run_id):
        assert run_id.startswith("operator-")
        entered.set()
        await release.wait()
        return None

    async def record_finish(request_id, *args, **kwargs):
        records.append(request_id)
        return await real_finish(request_id, *args, **kwargs)

    monkeypatch.setattr(runs_mod, "setup_agent_persist", fail_setup)
    monkeypatch.setattr(runs_mod, "find_incomplete_session_for_run", held_recovery)
    monkeypatch.setattr(store, "finish_turn", record_finish)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    submission = asyncio.create_task(
        coordinator.submit(cid, instruction="first", context=_CONTEXT, expected_last_sequence=0)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        request_id = (await store.get_conversation(cid))["activeRequestId"]
        assert request_id is not None
        task = coordinator._tasks[request_id]
        assert (await coordinator.cancel(cid, request_id))["cancelRequested"] is True
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(submission, 5)
        frames = await store.list_frames(cid)
        done = _done_for(frames, request_id)
        assert len(done) == 1 and done[0]["payload"]["outcome"] == "cancelled"
        errors = [
            frame["payload"]["error"]
            for frame in frames
            if frame["type"] == "error" and frame["requestId"] == request_id
        ]
        assert len(errors) == 1 and errors[0]["code"] == "cancelled"
        assert records.count(request_id) == 1
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        release.set()
        await asyncio.gather(submission, return_exceptions=True)
        await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_cancel_before_run_turn_body_starts_writes_one_turn_record(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=MessageWritingEngine)
    real_finish = store.finish_turn
    records = []
    begin = asyncio.Event()
    entered_run_turn = False

    async def record_finish(request_id, *args, **kwargs):
        records.append(request_id)
        return await real_finish(request_id, *args, **kwargs)

    monkeypatch.setattr(store, "finish_turn", record_finish)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    try:
        # Create the actual durable pending row, then gate entry to _run_turn.
        # This models a request cancelled before its scheduled coroutine body
        # starts, without relying on scheduler timing or intercepting create_task.
        accepted = await store.submit_turn(
            cid, instruction="first", context=_CONTEXT, expected_last_sequence=0
        )
        request_id = accepted["requestId"]
        ready = asyncio.Event()

        async def delayed_entry():
            nonlocal entered_run_turn
            await begin.wait()
            entered_run_turn = True
            await coordinator._run_turn(request_id, ready)

        task = asyncio.create_task(delayed_entry())
        coordinator._tasks[request_id] = task
        result = await coordinator.cancel(cid, request_id)
        await asyncio.gather(task, return_exceptions=True)
        assert result["cancelRequested"] is True
        assert not entered_run_turn
        frames = await _wait_done(store, cid)
        done = _done_for(frames, request_id)
        assert len(done) == 1 and done[0]["payload"]["outcome"] == "cancelled"
        assert records.count(request_id) == 1
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        begin.set()
        await asyncio.wait_for(coordinator.shutdown(), 10)


class _ObservableBlockingEngine:
    def __init__(self):
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def _stream(self, _turn):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield  # pragma: no cover

    def stream(self, turn):
        return self._stream(turn)


@pytest.mark.asyncio
async def test_shutdown_does_not_interrupt_engine_cancellation_cleanup(tmp_path, monkeypatch):
    class CleanupEngine:
        def __init__(self):
            self.entered = asyncio.Event()
            self.cleanup_entered = asyncio.Event()
            self.release = asyncio.Event()
            self.cleanup_interrupted = asyncio.Event()
            self.cleanup_finished = asyncio.Event()

        async def _stream(self, _turn):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cleanup_entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cleanup_interrupted.set()
                    raise
                self.cleanup_finished.set()
                raise
            yield  # pragma: no cover

        def stream(self, turn):
            return self._stream(turn)

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    engine = CleanupEngine()
    coordinator = OperatorCoordinator(store=store, engine_factory=lambda: engine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    shutdown_task = None
    try:
        accepted = await coordinator.submit(
            cid, instruction="wait", context=_CONTEXT, expected_last_sequence=0
        )
        request_id = accepted["requestId"]
        await asyncio.wait_for(engine.entered.wait(), 5)
        assert (await coordinator.cancel(cid, request_id))["cancelRequested"] is True
        await asyncio.wait_for(engine.cleanup_entered.wait(), 5)

        shutdown_read = asyncio.Event()
        real_get_turn = store.get_turn

        async def track_shutdown_read(*args, **kwargs):
            result = await real_get_turn(*args, **kwargs)
            shutdown_read.set()
            return result

        monkeypatch.setattr(store, "get_turn", track_shutdown_read)
        shutdown_task = asyncio.create_task(coordinator.shutdown())
        await asyncio.wait_for(shutdown_read.wait(), 5)
        # Allow a cancellation delivered by shutdown to reach the engine.
        await asyncio.sleep(0)
        assert not engine.cleanup_interrupted.is_set()
        assert not shutdown_task.done()
        engine.release.set()
        await asyncio.wait_for(shutdown_task, 10)
        assert engine.cleanup_finished.is_set()
        assert not engine.cleanup_interrupted.is_set()
        frames = await store.list_frames(cid)
        done = _done_for(frames, request_id)
        assert len(done) == 1 and done[0]["payload"]["outcome"] == "cancelled"
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        engine.release.set()
        if shutdown_task is not None:
            await asyncio.gather(shutdown_task, return_exceptions=True)
        else:
            await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_repeated_cancel_delivers_missing_task_cancellation(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    engine = _ObservableBlockingEngine()
    coordinator = OperatorCoordinator(store=store, engine_factory=lambda: engine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    try:
        accepted = await coordinator.submit(
            cid, instruction="wait", context=_CONTEXT, expected_last_sequence=0
        )
        request_id = accepted["requestId"]
        await asyncio.wait_for(engine.entered.wait(), 5)
        task = coordinator._tasks[request_id]
        # Simulates a caller interrupted after the durable flag was committed,
        # before it reached the task cancellation step.
        first = await store.request_cancel(cid, request_id)
        assert first["cancelRequested"] is True
        assert not task.done()
        assert not engine.cancelled.is_set()
        second = await coordinator.cancel(cid, request_id)
        assert second["cancelRequested"] is False
        await asyncio.wait_for(engine.cancelled.wait(), 5)
        await asyncio.gather(task, return_exceptions=True)
        frames = await _wait_done(store, cid)
        done = _done_for(frames, request_id)
        assert len(done) == 1 and done[0]["payload"]["outcome"] == "cancelled"
        assert (await store.get_turn(request_id))["status"] == "cancelled"
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        await asyncio.wait_for(coordinator.shutdown(), 10)


@pytest.mark.asyncio
async def test_concurrent_cancels_produce_one_terminal_record(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    engine = _ObservableBlockingEngine()
    coordinator = OperatorCoordinator(store=store, engine_factory=lambda: engine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    try:
        accepted = await coordinator.submit(
            cid, instruction="wait", context=_CONTEXT, expected_last_sequence=0
        )
        request_id = accepted["requestId"]
        await asyncio.wait_for(engine.entered.wait(), 5)
        task = coordinator._tasks[request_id]
        results = await asyncio.gather(
            coordinator.cancel(cid, request_id), coordinator.cancel(cid, request_id)
        )
        assert sorted(result["cancelRequested"] for result in results) == [False, True]
        await asyncio.wait_for(engine.cancelled.wait(), 5)
        await asyncio.gather(task, return_exceptions=True)
        frames = await _wait_done(store, cid)
        done = _done_for(frames, request_id)
        assert len(done) == 1 and done[0]["payload"]["outcome"] == "cancelled"
        assert (await store.get_turn(request_id))["status"] == "cancelled"
        assert (await store.get_conversation(cid))["activeRequestId"] is None
    finally:
        await asyncio.wait_for(coordinator.shutdown(), 10)
