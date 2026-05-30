# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone

import pytest

from lionagi.runtime import (
    VALID_TRANSITIONS,
    ControlRequest,
    ControlService,
    ControlVerb,
    LocalRunner,
    RunnerHandle,
    RunnerState,
    validate_transition,
)
from lionagi.state.db import StateDB


class _FakeRunner:
    def __init__(self, target_state: RunnerState = RunnerState.PAUSED) -> None:
        self.target_state = target_state
        self.calls: list[tuple[str, ControlVerb, str]] = []

    async def start(self, plan, **kwargs):  # noqa: ANN001
        raise AssertionError("FakeRunner.start should not be used in runtime tests")

    async def control(self, handle_id: str, verb: ControlVerb, reason: str) -> RunnerHandle:
        self.calls.append((handle_id, verb, reason))
        return RunnerHandle(
            session_id=handle_id,
            state=self.target_state,
            runner_type="local",
            pid=None,
            started_at=datetime.now(timezone.utc),
            metadata={"fake": True},
        )

    async def status(self, handle_id: str) -> RunnerHandle:
        raise AssertionError("FakeRunner.status should not be used in this test path")

    async def logs(self, handle_id: str, since=None):  # noqa: ANN001, ARG001
        raise AssertionError("FakeRunner.logs should not be used in this test path")


@pytest.fixture
async def db() -> StateDB:
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _create_session(db: StateDB, *, status: str = "running") -> str:
    progression_id = uuid.uuid4().hex
    await db.create_progression(progression_id)
    session_id = uuid.uuid4().hex
    await db.create_session(
        {
            "id": session_id,
            "progression_id": progression_id,
            "status": status,
        }
    )
    return session_id


def test_runner_state_transitions_valid():
    for state, targets in VALID_TRANSITIONS.items():
        for target in targets:
            assert validate_transition(state, target)


def test_runner_state_transitions_invalid_raises():
    for state in RunnerState:
        for target in RunnerState:
            if target in VALID_TRANSITIONS[state]:
                continue
            assert not validate_transition(state, target)
    assert not validate_transition(None, RunnerState.RUNNING)


def test_control_request_serialization():
    request = ControlRequest(
        verb=ControlVerb.KILL,
        actor_id="operator",
        reason="manual control test",
    )
    payload = request.model_dump()
    assert payload["verb"] == "kill"
    assert payload["actor_id"] == "operator"
    assert payload["reason"] == "manual control test"
    assert payload["requested_at"].tzinfo is not None

    restored = ControlRequest.model_validate(payload)
    assert restored == request


def test_runner_handle_serialization():
    started_at = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    handle = RunnerHandle(
        session_id="session-1",
        state=RunnerState.RUNNING,
        runner_type="local",
        pid=123,
        started_at=started_at,
        metadata={"check": "value"},
    )
    payload = handle.model_dump()
    assert payload["state"] == "running"
    assert payload["metadata"] == {"check": "value"}

    restored = RunnerHandle.model_validate(payload)
    assert restored == handle


@pytest.mark.asyncio
async def test_local_runner_start_and_status():
    runner = LocalRunner()
    session_id = uuid.uuid4().hex
    plan = {
        "session_id": session_id,
        "command": [sys.executable, "-c", "import time; time.sleep(10)"],
    }

    observed_session_id = await runner.start(plan)
    assert observed_session_id == session_id

    status = await runner.status(session_id)
    assert status.session_id == session_id
    assert status.state == RunnerState.RUNNING

    stop = await runner.control(session_id, ControlVerb.KILL, "cleanup")
    assert stop.state in {RunnerState.KILLED, RunnerState.FAILED}


@pytest.mark.asyncio
async def test_local_runner_cancel():
    runner = LocalRunner()
    session_id = uuid.uuid4().hex
    await runner.start(
        {
            "session_id": session_id,
            "command": [sys.executable, "-c", "import time; time.sleep(10)"],
        }
    )

    status = await runner.control(session_id, ControlVerb.CANCEL, "pause for test")
    assert status.session_id == session_id
    assert status.state in {
        RunnerState.CANCELLING,
        RunnerState.FAILED,
        RunnerState.KILLED,
    }

    final_state = None
    for _ in range(40):
        final_state = (await runner.status(session_id)).state
        if final_state in {RunnerState.FAILED, RunnerState.KILLED}:
            break
        await asyncio.sleep(0.05)
    assert final_state in {RunnerState.FAILED, RunnerState.KILLED}


@pytest.mark.asyncio
async def test_control_service_dispatch(db: StateDB):
    session_id = await _create_session(db)
    handle = RunnerHandle(
        session_id=session_id,
        state=RunnerState.RUNNING,
        runner_type="local",
        started_at=datetime.now(timezone.utc),
        pid=999,
        metadata={"runner": "fake"},
    )
    await db.upsert_runner_handle(handle)

    fake = _FakeRunner(target_state=RunnerState.PAUSED)
    service = ControlService(db=db, runners={"local": fake})

    request = ControlRequest(
        verb=ControlVerb.PAUSE,
        actor_id="ops",
        reason="operator pause",
    )
    updated = await service.dispatch("session", session_id, request)

    assert updated.state == RunnerState.PAUSED
    assert fake.calls == [(session_id, ControlVerb.PAUSE, "operator pause")]

    controls = await db.list_runner_controls(session_id)
    assert controls[0]["request_status"] == "completed"
    assert controls[0]["action"] == "pause"


@pytest.mark.asyncio
async def test_terminal_states_reject_control(db: StateDB):
    session_id = await _create_session(db)
    await db.upsert_runner_handle(
        RunnerHandle(
            session_id=session_id,
            state=RunnerState.COMPLETED,
            runner_type="local",
            started_at=datetime.now(timezone.utc),
        )
    )

    service = ControlService(db=db)
    request = ControlRequest(
        verb=ControlVerb.CANCEL,
        actor_id="ops",
        reason="should fail",
    )

    with pytest.raises(ValueError, match="terminal"):
        await service.dispatch("session", session_id, request)


def test_is_terminal_property():
    assert RunnerState.COMPLETED.is_terminal
    assert RunnerState.FAILED.is_terminal
    assert RunnerState.TIMED_OUT.is_terminal
    assert RunnerState.KILLED.is_terminal
    assert not RunnerState.RUNNING.is_terminal
    assert not RunnerState.CANCELLING.is_terminal
