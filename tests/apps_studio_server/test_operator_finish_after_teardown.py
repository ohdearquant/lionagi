# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline real-store Operator turn ordering and terminal record probes."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.cli import _runs as runs_mod
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
        # The old implementation exposes done while teardown is held. Submit
        # the next turn immediately in that case to exercise the real failure.
        # New ordering keeps done back, so release before waiting for it.
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
    finally:
        release.set()
        if cancel_task is not None:
            await asyncio.gather(cancel_task, return_exceptions=True)
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
        assert task.cancelling() == 0
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
