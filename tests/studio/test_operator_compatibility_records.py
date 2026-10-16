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
from lionagi.state.compat import Closure, CompatibilityProfile
from lionagi.studio.operator import coordinator as coordinator_mod
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import OperatorStore
from lionagi.studio.operator.types import OperatorEngineEvent


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
async def test_the_native_path_records_the_turn_before_teardown(tmp_path, monkeypatch):
    """Natively the turn record is written before teardown and keeps the turn's own status."""
    turn_status, run_status, order = await _drive(
        tmp_path, monkeypatch, compatibility=None, decided_status="failed"
    )

    assert run_status == "completed"
    assert turn_status == "completed"
    assert order == [("finish_turn", "completed"), ("teardown", "completed")]


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
    assert order == [("finish_turn", "completed"), ("teardown", "completed")]


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

    async def _stream(self, _turn):
        yield OperatorEngineEvent(
            "text", {"content": "reply", "format": "plain", "role": "assistant"}
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
async def test_a_native_cancelled_turn_records_cancelled_before_teardown(tmp_path, monkeypatch):
    """Natively the record is still written as the cancellation lands, before teardown."""
    turn_status, run_status, order = await _drive_interrupted(
        tmp_path, monkeypatch, compatibility=None, decided_status="failed"
    )

    assert turn_status == "cancelled"
    assert run_status == "cancelled"
    assert order[0] == ("finish_turn", "cancelled")
    assert order.index(("finish_turn", "cancelled")) < order.index(("teardown", "cancelled"))


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
        turn = await _await_finished_turn(coordinator.store, request_id)
        frames = await coordinator.store.list_frames(cid)
    finally:
        await coordinator.shutdown()

    errors = [f for f in frames if f["type"] == "error"]
    assert turn["status"] == "failed"
    assert turn["errorCode"] == "store_refused"
    assert len(errors) == 1
    assert errors[0]["payload"]["error"]["code"] == "store_refused"
    assert errors[0]["payload"]["error"]["message"] == str(refusal)
    assert errors[0]["payload"]["error"]["retryable"] is False
    assert errors[0]["payload"]["error"]["details"] == {"runId": None}


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
