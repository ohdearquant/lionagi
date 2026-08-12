# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Operator proposal adapters for live pause, gate release, and steering."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.run_control import (
    PAUSE_RUN_COMMAND_TYPE,
    RELEASE_RUN_PAUSE_COMMAND_TYPE,
    STEER_RUN_COMMAND_TYPE,
    PauseRunInput,
    ReleaseRunPauseInput,
    SteerRunInput,
    execute_run_control_command,
    pause_run,
    release_run_pause,
    steer_run,
)
from lionagi.studio.operator.store import OperatorStore

PROJECT = "operator-control-project"


def _patch_state_db(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)


async def _seed_run(db: Any, *, kind: str, status: str = "running") -> str:
    session_id = str(uuid.uuid4())
    progression_id = str(uuid.uuid4())
    await db.create_progression(progression_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": progression_id,
            "status": status,
            "started_at": time.time(),
            "invocation_kind": kind,
            "run_id": session_id if kind == "agent" else None,
            "node_metadata": {"drains_controls": True},
            "project": PROJECT,
        }
    )
    return session_id


async def _running_turn(
    store: OperatorStore, monkeypatch: pytest.MonkeyPatch, path: Path
) -> tuple[str, str]:
    conversation = await store.create_conversation(project=PROJECT)
    conversation_id = conversation["id"]
    accepted = await store.submit_turn(
        conversation_id,
        instruction="control this run",
        context={
            "space": "history",
            "route": "/history",
            "selection": {},
            "filters": {},
            "project": PROJECT,
        },
        expected_last_sequence=0,
    )
    request_id = accepted["requestId"]
    assert await store.mark_running(request_id)
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", conversation_id)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)
    return conversation_id, request_id


async def _wait_proposal(store: OperatorStore, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        proposals = await store.list_proposals_for_request(request_id)
        if proposals:
            return proposals[0]
        await asyncio.sleep(0.01)
    raise TimeoutError("run-control proposal did not appear")


@pytest.mark.parametrize(
    ("model", "arguments"),
    [
        (PauseRunInput, {"run": ""}),
        (ReleaseRunPauseInput, {"run": "run", "unexpected": True}),
        (SteerRunInput, {"run": "run", "message": "   "}),
    ],
)
def test_run_control_inputs_are_strict(model, arguments):
    with pytest.raises(ValidationError):
        model.model_validate(arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "arguments", "kind", "command_type", "verb", "payload"),
    [
        (pause_run, {}, "flow", PAUSE_RUN_COMMAND_TYPE, "pause", None),
        (
            release_run_pause,
            {},
            "play",
            RELEASE_RUN_PAUSE_COMMAND_TYPE,
            "resume",
            None,
        ),
        (
            steer_run,
            {"message": "Use the cached result"},
            "agent",
            STEER_RUN_COMMAND_TYPE,
            "message",
            {"text": "Use the cached result"},
        ),
    ],
)
async def test_allowed_run_control_queues_the_exact_existing_transport_verb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[dict[str, Any]], Any],
    arguments: dict[str, Any],
    kind: str,
    command_type: str,
    verb: str,
    payload: dict[str, Any] | None,
):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        session_id = await _seed_run(db, kind=kind)

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store)
    await coordinator.startup()
    conversation_id, request_id = await _running_turn(store, monkeypatch, path)

    task = asyncio.create_task(handler({"run": session_id, **arguments}))
    proposal = await _wait_proposal(store, request_id)
    assert not task.done()
    assert proposal["commandType"] == command_type
    assert proposal["command"] == {
        "session_id": session_id,
        "verb": verb,
        "payload": payload,
        "project": PROJECT,
    }
    assert proposal["risk"] == "mutate"

    decision = await coordinator.decide(
        conversation_id,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "succeeded"
    assert result["queued"] is True
    assert result["status"] == "queued"
    assert result["id"] == session_id
    assert result["verb"] == verb
    assert isinstance(result["controlId"], str)

    async with StateDB() as db:
        rows = await db.list_pending_session_controls(session_id)
    assert [(row["verb"], row["payload"]) for row in rows] == [(verb, payload)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_denied_control_never_queues_a_transport_row(tmp_path: Path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        session_id = await _seed_run(db, kind="flow")

    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store)
    await coordinator.startup()
    conversation_id, request_id = await _running_turn(store, monkeypatch, path)

    task = asyncio.create_task(pause_run({"run": session_id}))
    proposal = await _wait_proposal(store, request_id)
    decision = await coordinator.decide(
        conversation_id,
        proposal["id"],
        allow=False,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "failed"
    assert result == {"queued": False, "reason": "denied", "id": session_id}
    async with StateDB() as db:
        assert await db.list_pending_session_controls(session_id) == []
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_pause_refuses_agent_without_creating_a_proposal(tmp_path: Path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        session_id = await _seed_run(db, kind="agent")

    store = OperatorStore(path)
    await store.ensure_schema()
    _conversation_id, request_id = await _running_turn(store, monkeypatch, path)
    result = await pause_run({"run": session_id})

    assert result == {
        "queued": False,
        "reason": "unsupported_kind",
        "id": session_id,
        "kind": "agent",
    }
    assert await store.list_proposals_for_request(request_id) == []


@pytest.mark.asyncio
async def test_execution_rechecks_project_and_terminal_status_before_queueing(
    tmp_path: Path, monkeypatch
):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        session_id = await _seed_run(db, kind="flow")

    base = {
        "session_id": session_id,
        "verb": "pause",
        "payload": None,
        "project": PROJECT,
    }
    with pytest.raises(ValueError, match="not found"):
        await execute_run_control_command({**base, "project": "foreign-project"})

    async with StateDB() as db:
        await db.update_session(session_id, status="completed", ended_at=time.time())
    with pytest.raises(ValueError, match="no longer running"):
        await execute_run_control_command(base)

    async with StateDB() as db:
        assert await db.list_pending_session_controls(session_id) == []
