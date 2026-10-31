# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""An Operator turn's records when its store is in compatibility mode (ADR-0128 D6)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import pytest

import lionagi.cli._runs as runs_mod
from lionagi.state.compat import Closure, CompatibilityProfile, StateCompatibilityRefused
from lionagi.studio.operator import coordinator as coordinator_mod
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import OperatorStore
from lionagi.studio.operator.types import OperatorEngineEvent

# What a teardown the store refused records on the run's context, beside its terminal reason.
_TEARDOWN_REFUSAL = StateCompatibilityRefused("update_status", "operator-records@1", "refused")


def _profile() -> CompatibilityProfile:
    """A real profile: what ``StateDB.compatibility`` holds in compatibility mode."""
    return CompatibilityProfile(
        name="operator-records",
        version=1,
        closure=Closure(tables=()),
        writes={},
        operations={"run"},
    )


class _Engine:
    async def _stream(self, _turn):
        yield OperatorEngineEvent(
            "text", {"content": "reply", "format": "plain", "role": "assistant"}
        )

    def stream(self, turn):
        return self._stream(turn)


def _run_status(runs_root: Path) -> str | None:
    manifests = list(runs_root.glob("operator-*/run.json"))
    if not manifests:
        return None
    return json.loads(manifests[0].read_text()).get("status")


async def _await_terminal_run_status(runs_root: Path) -> str | None:
    """Wait for the turn's terminal run.json, then read it.

    The turn writes ``running`` first and its terminal record from the teardown
    path, so a read taken the moment ``submit()`` returns can still be the
    opening one.
    """
    deadline = time.monotonic() + 5
    status = None
    while time.monotonic() < deadline:
        status = await anyio.to_thread.run_sync(_run_status, runs_root)
        if status not in (None, "running"):
            return status
        await anyio.sleep(0.02)
    return status


async def _await_finished_turn(store: OperatorStore, request_id: str) -> dict:
    deadline = time.monotonic() + 5
    turn = await store.get_turn(request_id)
    while time.monotonic() < deadline and turn["status"] in ("queued", "running"):
        await anyio.sleep(0.02)
        turn = await store.get_turn(request_id)
    return turn


async def _drive(tmp_path, monkeypatch, *, compatibility, decided_status):
    """Run one Operator turn to its terminal records.

    ``decided_status`` is what the stand-in teardown returns; a ``failed`` one also
    sets a terminal reason, as a refused teardown does. Returns the turn's stored
    status, the run.json status, and the order of the finish_turn/teardown calls.
    """
    runs_root = tmp_path / "runs"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=compatibility)}

    order: list[tuple] = []

    async def teardown(ctx, *, status="completed", exception=None):
        order.append(("teardown", status))
        if decided_status == "cancelled-in-teardown":
            raise asyncio.CancelledError
        if decided_status == "failed":
            ctx["terminal_reason"] = ("failed", "The store refused a write: refused")
            ctx["store_refusal"] = _TEARDOWN_REFUSAL
        return decided_status

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_Engine
    )
    await coordinator.startup()
    try:
        real_finish = coordinator.store.finish_turn

        async def spy_finish(request_id, *, outcome, error=None):
            order.append(("finish_turn", outcome))
            return await real_finish(request_id, outcome=outcome, error=error)

        monkeypatch.setattr(coordinator.store, "finish_turn", spy_finish)
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        request_id = accepted["requestId"]
        if decided_status == "cancelled-in-teardown":
            status = None
            turn = await _await_finished_turn(coordinator.store, request_id)
        else:
            status = await _await_terminal_run_status(runs_root)
            turn = await coordinator.store.get_turn(request_id)
    finally:
        await coordinator.shutdown()
    return turn["status"], status, order


@pytest.mark.asyncio
async def test_a_non_terminal_teardown_status_keeps_the_turns_terminal_status(
    tmp_path, monkeypatch
):
    """A teardown that reads its session row back still ``running`` cannot put that status,
    with an end time, into run.json in compatibility mode."""
    turn_status, run_status, _ = await _drive(
        tmp_path, monkeypatch, compatibility=_profile(), decided_status="running"
    )

    assert run_status == "completed"
    assert turn_status == "completed"


@pytest.mark.asyncio
async def test_a_refused_teardown_fails_both_the_turn_and_its_run(tmp_path, monkeypatch):
    """In compatibility mode a turn the engine finished whose teardown the store refused
    reads ``failed`` in both its record and run.json, instead of ``failed`` beside ``completed``."""
    turn_status, run_status, order = await _drive(
        tmp_path, monkeypatch, compatibility=_profile(), decided_status="failed"
    )

    assert run_status == "failed"
    assert turn_status == "failed"
    assert order.index(("teardown", "completed")) < order.index(("finish_turn", "failed"))


@pytest.mark.asyncio
async def test_the_native_path_records_the_turn_after_teardown(tmp_path, monkeypatch):
    """The native turn keeps its own status while waiting for teardown."""
    turn_status, run_status, order = await _drive(
        tmp_path, monkeypatch, compatibility=None, decided_status="failed"
    )

    assert run_status == "completed"
    assert turn_status == "completed"
    assert order == [("teardown", "completed"), ("finish_turn", "completed")]


@pytest.mark.asyncio
async def test_a_store_stand_in_whose_compatibility_is_a_mock_takes_the_native_path(
    tmp_path, monkeypatch
):
    """Only a real ``CompatibilityProfile`` means compatibility mode; a Mock attribute does not."""
    turn_status, run_status, order = await _drive(
        tmp_path, monkeypatch, compatibility=Mock(), decided_status="failed"
    )

    assert run_status == "completed"
    assert turn_status == "completed"
    assert order == [("teardown", "completed"), ("finish_turn", "completed")]


@pytest.mark.asyncio
async def test_a_teardown_that_decides_another_terminal_status_fails_the_turn(
    tmp_path, monkeypatch
):
    """A turn record is completed, failed or cancelled; a run teardown ends ``timed_out``
    did not complete, so its turn reads ``failed``, never ``completed``."""
    turn_status, run_status, _ = await _drive(
        tmp_path, monkeypatch, compatibility=_profile(), decided_status="timed_out"
    )

    assert run_status == "timed_out"
    assert turn_status == "failed"


@pytest.mark.asyncio
async def test_a_cancel_during_teardown_still_finishes_the_turn(tmp_path, monkeypatch):
    """In compatibility mode the turn record waits for teardown; a cancel that lands during
    teardown must not leave the turn unfinished, as the native path never does."""
    turn_status, _, order = await _drive(
        tmp_path, monkeypatch, compatibility=_profile(), decided_status="cancelled-in-teardown"
    )

    assert turn_status == "completed"
    assert order == [("teardown", "completed"), ("finish_turn", "completed")]


class _BlockingEngine:
    """Holds the turn open mid-stream so a cancel lands while its run exists."""

    # Set once the coordinator has recorded the first event and asked for the next, so no
    # store write of the turn is in flight while the engine waits.
    parked: asyncio.Event | None = None

    async def _stream(self, _turn):
        yield OperatorEngineEvent(
            "text", {"content": "reply", "format": "plain", "role": "assistant"}
        )
        if self.parked is not None:
            self.parked.set()
        await asyncio.Event().wait()

    def stream(self, turn):
        return self._stream(turn)


class _HeldWriteEngine:
    """Replies once and arms the store stand-in for that reply's frame write.

    The stream starts, then yields its one text event; the coordinator's next
    store write is the frame for that event, which the stand-in holds inside its
    transaction so a stop lands while the write is in flight.
    """

    armed: dict | None = None

    async def _stream(self, _turn):
        if self.armed is not None:
            self.armed["on"] = True
        yield OperatorEngineEvent(
            "text", {"content": "first event", "format": "plain", "role": "assistant"}
        )
        await asyncio.Event().wait()

    def stream(self, turn):
        return self._stream(turn)


class _ExplodingEngine:
    """Fails the turn after its run exists, on the generic exception path."""

    async def _stream(self, _turn):
        raise RuntimeError("the engine fell over")
        yield  # pragma: no cover -- makes this an async generator

    def stream(self, turn):
        return self._stream(turn)


async def _drive_interrupted(
    tmp_path,
    monkeypatch,
    *,
    compatibility,
    decided_status,
    engine=_BlockingEngine,
    interrupt="cancel",
    teardown_holds=None,
):
    """Drive one turn that is interrupted after its run exists, then read both records.

    ``interrupt`` is ``cancel`` (the coordinator's cancel route) or ``none`` (the turn
    fails on its own), or ``shutdown-while-tearing-down``, which calls ``shutdown()``
    while teardown is still running.
    """
    runs_root = tmp_path / "runs"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=compatibility)}

    order: list[tuple] = []

    async def teardown(ctx, *, status="completed", exception=None):
        order.append(("teardown", status))
        if teardown_holds is not None:
            teardown_holds["started"].set()
            await teardown_holds["release"].wait()
        if decided_status == "failed":
            ctx["terminal_reason"] = ("failed", "The store refused a write: refused")
            ctx["store_refusal"] = _TEARDOWN_REFUSAL
        return decided_status

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=engine
    )
    await coordinator.startup()
    try:
        real_finish = coordinator.store.finish_turn

        async def spy_finish(request_id, *, outcome, error=None):
            order.append(("finish_turn", outcome))
            return await real_finish(request_id, outcome=outcome, error=error)

        monkeypatch.setattr(coordinator.store, "finish_turn", spy_finish)
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        request_id = accepted["requestId"]
        if interrupt == "cancel":
            await coordinator.cancel(cid, request_id)
        elif interrupt == "shutdown-while-tearing-down":
            await asyncio.wait_for(teardown_holds["started"].wait(), 5)
            stopping = asyncio.ensure_future(coordinator.shutdown())
            teardown_holds["release"].set()
            await asyncio.wait_for(stopping, 5)
        status = await _await_terminal_run_status(runs_root)
        turn = await _await_finished_turn(coordinator.store, request_id)
    finally:
        await coordinator.shutdown()
    return turn["status"], status, order


@pytest.mark.asyncio
async def test_a_cancelled_compatibility_turn_whose_teardown_refuses_fails_both(
    tmp_path, monkeypatch
):
    """A turn cancelled mid-stream waits for teardown like a completed one, so its record
    reads ``failed`` when the store refused a teardown write, never ``cancelled`` beside a
    run.json reading ``failed``."""
    turn_status, run_status, order = await _drive_interrupted(
        tmp_path, monkeypatch, compatibility=_profile(), decided_status="failed"
    )

    assert run_status == "failed"
    assert turn_status == "failed"
    assert order.index(("teardown", "cancelled")) < order.index(("finish_turn", "failed"))


@pytest.mark.asyncio
async def test_a_cancelled_compatibility_turn_whose_teardown_succeeds_stays_cancelled(
    tmp_path, monkeypatch
):
    """Teardown deciding ``cancelled`` matches the outcome the turn reached, so the record
    keeps the cancellation the engine's own endpoint recorded."""
    turn_status, run_status, order = await _drive_interrupted(
        tmp_path, monkeypatch, compatibility=_profile(), decided_status="cancelled"
    )

    assert run_status == "cancelled"
    assert turn_status == "cancelled"
    assert order == [("teardown", "cancelled"), ("finish_turn", "cancelled")]
    turn_error = await _turn_error(tmp_path)
    assert turn_error["code"] == "cancelled"
    assert turn_error["retryable"] is False


@pytest.mark.asyncio
async def test_a_failed_compatibility_turn_whose_teardown_refuses_fails_both(tmp_path, monkeypatch):
    """A turn failing with an exception after its run exists also waits for teardown."""
    turn_status, run_status, order = await _drive_interrupted(
        tmp_path,
        monkeypatch,
        compatibility=_profile(),
        decided_status="failed",
        engine=_ExplodingEngine,
        interrupt="none",
    )

    assert run_status == "failed"
    assert turn_status == "failed"
    assert order.index(("teardown", "failed")) < order.index(("finish_turn", "failed"))


@pytest.mark.asyncio
async def test_a_native_cancelled_turn_records_cancelled_after_teardown(tmp_path, monkeypatch):
    """A native cancellation remains active until teardown completes."""
    turn_status, run_status, order = await _drive_interrupted(
        tmp_path, monkeypatch, compatibility=None, decided_status="failed"
    )

    assert turn_status == "cancelled"
    assert run_status == "cancelled"
    assert order.index(("teardown", "cancelled")) < order.index(("finish_turn", "cancelled"))


@pytest.mark.asyncio
async def test_a_stop_cancelling_the_first_frame_write_releases_the_store(tmp_path, monkeypatch):
    """A stop that lands while the first event's frame write is inside its transaction still
    records the turn ``cancelled``, and does it without waiting out the store's busy timeout.

    The write runs ``BEGIN IMMEDIATE`` and then leaves a ``SELECT`` on its connection
    unfinalized, as a frame write cancelled mid-transaction does. SQLite keeps the store's
    write lock until that transaction ends, and it defers the close of a connection whose
    statements are still open, so a store that only closes the connection leaves the lock
    held: the stop's own write then waits out the whole busy timeout and the turn stays
    ``running``. The store's connection block ends the transaction as it exits, so the lock
    is free the moment the cancelled write unwinds.
    """
    import contextlib
    import time

    import lionagi.studio.operator.store as store_mod

    runs_root = tmp_path / "runs"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=None)}

    async def teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)

    # Patch at the narrowest point: wrap ``open_db`` as the store module resolves it, so
    # the connection whose transaction is held open is the store's own. The cursor a
    # cancelled coroutine leaves behind is the one this wrapper creates.
    armed = {"on": False}
    held: list = []
    inside = asyncio.Event()
    release = asyncio.Event()
    real_open_db = store_mod.open_db

    @contextlib.asynccontextmanager
    async def holding_open_db(path):
        async with real_open_db(path) as db:
            original_execute = db.execute

            async def execute(sql, *args, **kwargs):
                cursor = await original_execute(sql, *args, **kwargs)
                if armed["on"] and sql.strip().upper() == "BEGIN IMMEDIATE":
                    armed["on"] = False
                    # Kept referenced, so the statement stays unfinalized and the
                    # connection's close stays deferred, as the cancelled frame does.
                    held.append(await original_execute("SELECT 1 FROM studio_operator_frames"))
                    inside.set()
                    await release.wait()
                return cursor

            db.execute = execute
            yield db

    monkeypatch.setattr(store_mod, "open_db", holding_open_db)
    monkeypatch.setattr(_HeldWriteEngine, "armed", armed)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_HeldWriteEngine
    )
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        request_id = accepted["requestId"]
        await asyncio.wait_for(inside.wait(), 5)

        started = time.monotonic()
        stopping = asyncio.ensure_future(coordinator.cancel(cid, request_id))
        # The stop cancels the in-flight turn here as well. The coordinator's own cancel
        # reaches the turn the same way, but only after its own write to the store, which
        # waits on the lock the held write owns; issuing the turn's cancel directly is what
        # unwinds that write, and the stop completes only once the lock is free.
        coordinator._tasks[request_id].cancel()
        result = await asyncio.wait_for(stopping, 8)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        await coordinator.shutdown()

    turn = await coordinator.store.get_turn(request_id)
    assert result["requestId"] == request_id
    assert turn["status"] == "cancelled"
    # Well inside the store's 5 s busy timeout: the lock was free when the stop wrote.
    assert elapsed < 2.0, f"the stop took {elapsed:.2f}s, so it waited on the store's lock"


async def _turn_error(tmp_path) -> dict:
    """The error frame of the only conversation in a driven turn."""
    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    await coordinator.startup()
    conversations = await coordinator.store.list_conversations()
    frames = await coordinator.store.list_frames(conversations[0]["id"])
    await coordinator.shutdown()
    return next(f["payload"]["error"] for f in frames if f["type"] == "error")


@pytest.mark.asyncio
async def test_a_cancel_in_the_final_checkpoint_still_finishes_the_turn(tmp_path, monkeypatch):
    """A cancel landing in the ``finally``'s final snapshot write must not skip teardown or
    the deferred finish: the record still carries the status teardown decided."""
    runs_root = tmp_path / "runs"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)

    calls: list[int] = []
    real_snapshot = coordinator_mod.write_resumable_operator_snapshot

    async def snapshot(branch, snapshot_dir):
        calls.append(1)
        if len(calls) == 2:
            # The second call is the one the ``finally`` makes.
            raise asyncio.CancelledError
        await real_snapshot(branch, snapshot_dir)

    monkeypatch.setattr(coordinator_mod, "write_resumable_operator_snapshot", snapshot)

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=_profile())}

    async def teardown(ctx, *, status="completed", exception=None):
        ctx["terminal_reason"] = ("failed", "The store refused a write: refused")
        ctx["store_refusal"] = _TEARDOWN_REFUSAL
        return "failed"

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_Engine
    )
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        request_id = accepted["requestId"]
        await _await_finished_turn(coordinator.store, request_id)
        turn = await coordinator.store.get_turn(request_id)
        run_status = await _await_terminal_run_status(runs_root)
    finally:
        await coordinator.shutdown()

    assert len(calls) == 2, "the injected cancel must have landed in the final snapshot write"
    assert turn["status"] == "failed"
    assert run_status == "failed"


@pytest.mark.asyncio
async def test_shutdown_while_a_compatibility_turn_is_tearing_down_lets_teardown_finish(
    tmp_path, monkeypatch
):
    """A finished-stream turn still in teardown is not cancelled by shutdown; both records
    end terminal and equal."""
    holds = {"started": asyncio.Event(), "release": asyncio.Event()}

    turn_status, run_status, _order = await _drive_interrupted(
        tmp_path,
        monkeypatch,
        compatibility=_profile(),
        decided_status="failed",
        engine=_Engine,
        teardown_holds=holds,
        interrupt="shutdown-while-tearing-down",
    )

    assert turn_status == "failed"
    assert run_status == "failed"


@pytest.mark.asyncio
async def test_a_native_turn_is_still_cancelled_by_shutdown(tmp_path, monkeypatch):
    """Natively, shutdown cancels a turn that is not terminal, as before."""
    runs_root = tmp_path / "runs"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=None)}

    order: list[tuple] = []

    async def teardown(ctx, *, status="completed", exception=None):
        order.append(("teardown", status))
        return status

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)
    parked = asyncio.Event()
    monkeypatch.setattr(_BlockingEngine, "parked", parked)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_BlockingEngine
    )
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="hello",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    request_id = accepted["requestId"]
    # Wait until the turn is streaming: its run exists, so shutdown cancels it as before.
    while not list(runs_root.glob("operator-*/run.json")):
        await anyio.sleep(0.02)
    # And until the engine is parked past its first event: a shutdown that cancels a store
    # write in flight is a different case from the one this test reads.
    await asyncio.wait_for(parked.wait(), 5)
    await coordinator.shutdown()

    turn = await coordinator.store.get_turn(request_id)
    assert turn["status"] == "cancelled"
    assert order == [("teardown", "cancelled")]
    assert _run_status(runs_root) == "cancelled"


@pytest.mark.asyncio
async def test_a_refused_setup_records_a_store_refusal_not_a_retryable_model_failure(
    tmp_path, monkeypatch
):
    """An exception carrying a store refusal is not a retryable model failure: it is the
    store's refusal, with the same run details the branch already builds."""
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.state.compat import StateCompatibilityRefused

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    refusal = StateCompatibilityRefused("create_progression", "operator-records", "refused")

    async def setup(*args, **kwargs):
        raise BaseExceptionGroup("setup wrote", [refusal, ValueError("wrapped beside it")])

    recovered: list[str] = []

    async def recover(run_id):
        recovered.append(run_id)
        return None

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "find_incomplete_session_for_run", recover)

    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        request_id = accepted["requestId"]
        turn = await _await_finished_turn(coordinator.store, request_id)
        frames = await coordinator.store.list_frames(cid)
    finally:
        await coordinator.shutdown()

    # The refused setup was writing to a store in compatibility mode, which refuses the
    # recovery of a committed session row (ADR-0128 A.1), so none is attempted.
    assert recovered == []
    errors = [f for f in frames if f["type"] == "error"]
    assert turn["status"] == "failed"
    assert turn["errorCode"] == "store_refused"
    assert len(errors) == 1
    assert errors[0]["payload"]["error"]["code"] == "store_refused"
    assert errors[0]["payload"]["error"]["message"] == str(refusal)
    assert errors[0]["payload"]["error"]["retryable"] is False
    # Recovery was refused, so the absence of an observed run is not evidence
    # that no session row exists.
    assert errors[0]["payload"]["error"]["details"] == {}


@pytest.mark.asyncio
async def test_an_ordinary_setup_failure_stays_a_retryable_model_failure(tmp_path, monkeypatch):
    """A group holding only ordinary errors is still recorded as before."""
    from lionagi.ln.concurrency import BaseExceptionGroup

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    async def setup(*args, **kwargs):
        raise BaseExceptionGroup("setup wrote", [ValueError("one"), OSError("two")])

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)

    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        request_id = accepted["requestId"]
        await _await_finished_turn(coordinator.store, request_id)
        frames = await coordinator.store.list_frames(cid)
    finally:
        await coordinator.shutdown()

    (error,) = [f["payload"]["error"] for f in frames if f["type"] == "error"]
    assert error["code"] == "model_failure"
    assert error["retryable"] is True


@pytest.mark.asyncio
async def test_a_refusal_from_the_setup_recovery_is_recorded_not_suppressed(tmp_path, monkeypatch):
    """An ordinary setup failure is recovered on the store, and a store that refuses that
    recovery is the turn's refusal rather than a failure the recovery swallows."""
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    refusal = StateCompatibilityRefused("update_status", "operator-records", "refused")

    async def setup(*args, **kwargs):
        raise RuntimeError("setup fell over")

    async def recover(run_id):
        raise refusal

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "find_incomplete_session_for_run", recover)

    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        turn = await _await_finished_turn(coordinator.store, accepted["requestId"])
        frames = await coordinator.store.list_frames(cid)
    finally:
        await coordinator.shutdown()

    (error,) = [f["payload"]["error"] for f in frames if f["type"] == "error"]
    assert turn["status"] == "failed"
    assert error["code"] == "store_refused"
    assert error["message"] == f"{refusal}; run recovery did not complete"
    assert error["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_name, message",
    [
        ("SchemaTooNewError", "state.db schema version 900 is newer than this package"),
        ("ForeignLineageError", "state.db names 'manager' as the application that manages it"),
        ("UnrecognizedStoreError", "state.db holds tables this package does not create: app_users"),
    ],
    ids=["later-schema", "another-application", "unrecognized-store"],
)
async def test_a_setup_recovery_whose_native_open_is_refused_is_a_store_refusal(
    tmp_path, monkeypatch, error_name, message
):
    """The recovery opens the store natively, and a store at a later schema, one another
    application manages or one holding tables this package never creates refuses that open; no
    retry changes that, so the turn records it as the store's refusal."""
    from lionagi.state import db as db_mod

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    refused_open = getattr(db_mod, error_name)(message)

    async def setup(*args, **kwargs):
        raise RuntimeError("setup fell over")

    async def recover(run_id):
        raise refused_open

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "find_incomplete_session_for_run", recover)

    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    await coordinator.startup()
    try:
        cid, request_id = await _submit(coordinator)
        turn = await _await_finished_turn(coordinator.store, request_id)
        frames = await coordinator.store.list_frames(cid)
    finally:
        await coordinator.shutdown()

    (error,) = [f["payload"]["error"] for f in frames if f["type"] == "error"]
    assert turn["status"] == "failed"
    assert error["code"] == "store_refused"
    assert error["message"] == f"{refused_open}; run recovery did not complete"
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_a_retryable_engine_failure_then_a_refused_teardown_is_not_retryable(
    tmp_path, monkeypatch
):
    """The engine failed in a way the turn records as retryable, then teardown's own write was
    refused: the turn fails with that refusal, never as retryable work beside a failed run."""
    turn_status, run_status, _order = await _drive_interrupted(
        tmp_path,
        monkeypatch,
        compatibility=_profile(),
        decided_status="failed",
        engine=_ExplodingEngine,
        interrupt="none",
    )
    error = await _turn_error(tmp_path)

    assert (turn_status, run_status) == ("failed", "failed")
    assert error["code"] == "store_refused"
    assert error["message"] == str(_TEARDOWN_REFUSAL)
    assert error["retryable"] is False
    assert error["details"] == {"runId": "s1", "href": "/runs/s1"}


class _EndingEngine:
    """One reply, then the stream ends when ``end`` is set (at once when it is None)."""

    def __init__(self, end: asyncio.Event | None = None) -> None:
        self.end = end

    async def _stream(self, _turn):
        yield OperatorEngineEvent(
            "text", {"content": "reply", "format": "plain", "role": "assistant"}
        )
        if self.end is not None:
            await self.end.wait()

    def stream(self, turn):
        return self._stream(turn)


def _install_turn(monkeypatch, tmp_path, *, compatibility, teardown) -> Path:
    runs_root = tmp_path / "runs"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=compatibility)}

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)
    return runs_root


def _held_teardown(log: list, release: asyncio.Event):
    """Decides ``failed`` for a refused write once released; logs an interruption."""

    async def teardown(ctx, *, status="completed", exception=None):
        log.append(("teardown", status))
        try:
            await release.wait()
        except asyncio.CancelledError:
            log.append(("teardown", "interrupted"))
            raise
        ctx["terminal_reason"] = ("failed", "The store refused a write: refused")
        ctx["store_refusal"] = _TEARDOWN_REFUSAL
        return "failed"

    return teardown


async def _until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await anyio.sleep(0.02)
    return False


async def _submit(coordinator: OperatorCoordinator) -> tuple[str, str]:
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="hello",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    return cid, accepted["requestId"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("compatibility", "turn_status", "run_status"),
    [(_profile(), "failed", "failed"), (None, "completed", "completed")],
    ids=["compatibility", "native"],
)
async def test_a_cancel_during_teardown_lets_it_finish_before_both_records(
    tmp_path, monkeypatch, compatibility, turn_status, run_status
):
    """A cancel that lands while teardown runs is held until teardown, the turn's record and
    run.json are written, then re-raised. Interrupted, teardown left run.json ``running``."""
    log: list = []
    release = asyncio.Event()
    runs_root = _install_turn(
        monkeypatch, tmp_path, compatibility=compatibility, teardown=_held_teardown(log, release)
    )
    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_EndingEngine
    )
    await coordinator.startup()
    try:
        cid, request_id = await _submit(coordinator)
        assert await _until(lambda: ("teardown", "completed") in log)
        task = coordinator._tasks[request_id]
        await coordinator.cancel(cid, request_id)
        await anyio.sleep(0.1)
        release.set()
        turn = await _await_finished_turn(coordinator.store, request_id)
        final = await _await_terminal_run_status(runs_root)
        await asyncio.wait((task,), timeout=5)
    finally:
        release.set()
        await coordinator.shutdown()

    assert ("teardown", "interrupted") not in log
    assert (turn["status"], final) == (turn_status, run_status)
    # The held cancel is raised again once both records exist, so the turn ends cancelled.
    assert task.cancelled()


@pytest.mark.asyncio
async def test_a_cancel_landing_in_the_run_json_write_still_writes_it(tmp_path, monkeypatch):
    """The terminal run.json write is held like the turn's record: a cancel that lands while
    it is in flight waits for it and is raised after. Interrupted, run.json stayed running."""
    import threading

    async def teardown(ctx, *, status="completed", exception=None):
        return "completed"

    runs_root = _install_turn(monkeypatch, tmp_path, compatibility=None, teardown=teardown)
    inside, release = threading.Event(), threading.Event()
    real_write = runs_mod.RunDir.write_manifest

    def held_write(self, manifest):
        if manifest.get("status") != "running" and "ended_at" in manifest:
            inside.set()
            release.wait(5)
        return real_write(self, manifest)

    monkeypatch.setattr(runs_mod.RunDir, "write_manifest", held_write)
    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_EndingEngine
    )
    await coordinator.startup()
    try:
        cid, request_id = await _submit(coordinator)
        assert await _until(inside.is_set)
        task = coordinator._tasks[request_id]
        await coordinator.cancel(cid, request_id)
        await anyio.sleep(0.1)
        release.set()
        await asyncio.wait((task,), timeout=5)
        final = _run_status(runs_root)
    finally:
        release.set()
        await coordinator.shutdown()

    assert final == "completed"
    assert task.cancelled()


class _SessionEngine:
    """Names the provider's session, then replies; the stream ends when ``end`` is set."""

    end: asyncio.Event | None = None

    async def _stream(self, _turn):
        yield OperatorEngineEvent("session", {"providerSessionId": "provider-session-1"})
        yield OperatorEngineEvent(
            "text", {"content": "reply", "format": "plain", "role": "assistant"}
        )
        if self.end is not None:
            await self.end.wait()

    def stream(self, turn):
        return self._stream(turn)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("compatibility", "linked"),
    [(_profile(), []), (None, ["task", "last"])],
    ids=["compatibility", "native"],
)
async def test_a_compatibility_turn_does_not_stamp_the_mirrored_session(
    tmp_path, monkeypatch, compatibility, linked
):
    """The mirrored transcript is not a row the run created, so a turn on a store in
    compatibility mode makes neither the background stamp nor the last attempt."""
    calls: list[str] = []

    async def link(db, session_uid, *, parent_run_id):
        calls.append("task")
        return False

    async def link_once(session_uid, *, parent_run_id):
        calls.append("last")
        return False

    async def teardown(ctx, *, status="completed", exception=None):
        return "completed"

    monkeypatch.setattr(coordinator_mod, "_link_engine_child", link)
    monkeypatch.setattr(coordinator_mod, "_link_engine_child_once", link_once)
    runs_root = _install_turn(monkeypatch, tmp_path, compatibility=compatibility, teardown=teardown)
    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_SessionEngine
    )
    await coordinator.startup()
    try:
        _cid, request_id = await _submit(coordinator)
        await _await_finished_turn(coordinator.store, request_id)
        await _await_terminal_run_status(runs_root)
    finally:
        await coordinator.shutdown()

    assert calls == linked


@pytest.mark.asyncio
async def test_a_cancel_during_the_last_stamp_attempt_still_runs_teardown(tmp_path, monkeypatch):
    """The last stamp attempt is held like the background one: a cancel that lands in it is
    raised again only after teardown and both records. Interrupted, run.json stayed running."""
    inside, release = asyncio.Event(), asyncio.Event()
    log: list[str] = []

    async def link(db, session_uid, *, parent_run_id):
        return False

    async def link_once(session_uid, *, parent_run_id):
        inside.set()
        await release.wait()
        return True

    async def teardown(ctx, *, status="completed", exception=None):
        log.append(status)
        return status

    monkeypatch.setattr(coordinator_mod, "_link_engine_child", link)
    monkeypatch.setattr(coordinator_mod, "_link_engine_child_once", link_once)
    runs_root = _install_turn(monkeypatch, tmp_path, compatibility=None, teardown=teardown)
    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_SessionEngine
    )
    await coordinator.startup()
    try:
        cid, request_id = await _submit(coordinator)
        await asyncio.wait_for(inside.wait(), 5)
        task = coordinator._tasks[request_id]
        await coordinator.cancel(cid, request_id)
        await anyio.sleep(0.1)
        release.set()
        await _await_finished_turn(coordinator.store, request_id)
        final = await _await_terminal_run_status(runs_root)
        await asyncio.wait((task,), timeout=5)
    finally:
        release.set()
        await coordinator.shutdown()

    assert log == ["completed"]
    assert final == "completed"
    assert task.cancelled()


@pytest.mark.asyncio
async def test_shutdown_reading_the_turn_as_its_stream_ends_does_not_cancel_teardown(
    tmp_path, monkeypatch
):
    """Shutdown reads the turn after checking whether its stream ended. A stream that ends
    during that read starts teardown, and the stale read must not cancel it."""
    log: list = []
    end, release = asyncio.Event(), asyncio.Event()
    runs_root = _install_turn(
        monkeypatch, tmp_path, compatibility=_profile(), teardown=_held_teardown(log, release)
    )
    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=lambda: _EndingEngine(end)
    )
    await coordinator.startup()
    parked, gate = asyncio.Event(), asyncio.Event()
    armed = {"on": False}
    real_get = coordinator.store.get_turn

    async def gated_get(request_id):
        if armed["on"]:
            parked.set()
            await gate.wait()
        return await real_get(request_id)

    monkeypatch.setattr(coordinator.store, "get_turn", gated_get)
    try:
        _cid, request_id = await _submit(coordinator)
        task = coordinator._tasks[request_id]
        assert await _until(lambda: list(runs_root.glob("operator-*/run.json")))
        armed["on"] = True
        stopping = asyncio.ensure_future(coordinator.shutdown())
        await asyncio.wait_for(parked.wait(), 5)
        end.set()
        assert await _until(lambda: ("teardown", "completed") in log)
        gate.set()
        await anyio.sleep(0.2)
        release.set()
        await asyncio.wait_for(stopping, 10)
    finally:
        gate.set()
        release.set()
        monkeypatch.setattr(coordinator.store, "get_turn", real_get)
        await coordinator.shutdown()

    turn = await real_get(request_id)
    # The stale read is not acted on: the turn is never cancelled, not merely held to the end.
    assert not task.cancelled()
    assert ("teardown", "interrupted") not in log
    assert turn["status"] == "failed"
    assert _run_status(runs_root) == "failed"


@pytest.mark.asyncio
async def test_a_cancel_landing_in_the_deferred_finish_still_writes_both_records(
    tmp_path, monkeypatch
):
    """The deferred finish is a store write; a cancel that lands inside it is held until the
    record and run.json are written. Interrupted, it left both ``running``."""

    async def teardown(ctx, *, status="completed", exception=None):
        return "completed"

    runs_root = _install_turn(monkeypatch, tmp_path, compatibility=_profile(), teardown=teardown)
    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_EndingEngine
    )
    await coordinator.startup()
    inside, release = asyncio.Event(), asyncio.Event()
    real_finish = coordinator.store.finish_turn

    async def held_finish(request_id, *, outcome, error=None):
        inside.set()
        await release.wait()
        return await real_finish(request_id, outcome=outcome, error=error)

    monkeypatch.setattr(coordinator.store, "finish_turn", held_finish)
    try:
        cid, request_id = await _submit(coordinator)
        await asyncio.wait_for(inside.wait(), 5)
        await coordinator.cancel(cid, request_id)
        await anyio.sleep(0.1)
        release.set()
        turn = await _await_finished_turn(coordinator.store, request_id)
        final = await _await_terminal_run_status(runs_root)
    finally:
        release.set()
        monkeypatch.setattr(coordinator.store, "finish_turn", real_finish)
        await coordinator.shutdown()

    assert turn["status"] == "completed"
    assert final == "completed"
