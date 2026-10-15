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
