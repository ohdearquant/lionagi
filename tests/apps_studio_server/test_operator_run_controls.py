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


async def _seed_foreign_run(db: Any, *, project: str, kind: str = "flow") -> str:
    session_id = str(uuid.uuid4())
    progression_id = str(uuid.uuid4())
    await db.create_progression(progression_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": progression_id,
            "status": "running",
            "started_at": time.time(),
            "invocation_kind": kind,
            "run_id": session_id if kind == "agent" else None,
            "node_metadata": {"drains_controls": True},
            "project": project,
        }
    )
    return session_id


async def _turn_without_project(
    store: OperatorStore, monkeypatch: pytest.MonkeyPatch, path: Path
) -> str:
    """A running turn whose context names no project at all."""
    conversation = await store.create_conversation(project=PROJECT)
    accepted = await store.submit_turn(
        conversation["id"],
        instruction="control this run",
        context={"space": "history", "route": "/history", "selection": {}, "filters": {}},
        expected_last_sequence=0,
    )
    request_id = accepted["requestId"]
    assert await store.mark_running(request_id)
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", conversation["id"])
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)
    return request_id


@pytest.mark.asyncio
async def test_a_turn_with_no_project_scope_proposes_no_control_at_all(tmp_path: Path, monkeypatch):
    """An unscoped turn cannot prove it owns any run, so it gets no proposal.

    A control mutates the run it names, so there is no version of "no scope"
    it can safely proceed under -- unlike the read paths, which fall open.
    """
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        session_id = await _seed_run(db, kind="flow")

    store = OperatorStore(path)
    await store.ensure_schema()
    request_id = await _turn_without_project(store, monkeypatch, path)

    result = await pause_run({"run": session_id})

    assert result == {"queued": False, "reason": "missing_owner_context"}
    assert await store.list_proposals_for_request(request_id) == []
    async with StateDB() as db:
        assert await db.list_pending_session_controls(session_id) == []


@pytest.mark.asyncio
async def test_the_unscoped_refusal_does_not_disclose_which_run_ids_exist(
    tmp_path: Path, monkeypatch
):
    """Authority is established before resolution, so the two refusals cannot
    be used as an existence oracle.

    Were the order reversed, "missing_owner_context" would mean the id resolved
    and "not_found" would mean it did not, which tells an unscoped turn exactly
    which run ids exist in projects it cannot see.
    """
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        real_id = await _seed_run(db, kind="flow")

    store = OperatorStore(path)
    await store.ensure_schema()
    await _turn_without_project(store, monkeypatch, path)

    for_real = await pause_run({"run": real_id})
    for_absent = await pause_run({"run": str(uuid.uuid4())})

    assert for_real == for_absent == {"queued": False, "reason": "missing_owner_context"}


@pytest.mark.asyncio
async def test_a_scoped_turn_cannot_propose_against_another_projects_run(
    tmp_path: Path, monkeypatch
):
    """A scoped turn cannot steer a run in a project it cannot see.

    Regression cover for the outcome, not for one mechanism. Two fences produce
    it and resolve_run's is the load-bearing one: it scopes every arm for a turn
    that declares a project, so neutering _propose_run_control's own comparison
    leaves this test green. The comparison is kept as a second fence, and the
    unscoped case below is where the ownership bind is what does the work.
    """
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        foreign_id = await _seed_foreign_run(db, project="someone-elses-project")

    store = OperatorStore(path)
    await store.ensure_schema()
    _conversation_id, request_id = await _running_turn(store, monkeypatch, path)

    result = await pause_run({"run": foreign_id})

    # Reported as absence, so the refusal carries no signal about a run the
    # turn is not entitled to know exists.
    assert result == {"queued": False, "reason": "not_found"}
    assert await store.list_proposals_for_request(request_id) == []
    async with StateDB() as db:
        assert await db.list_pending_session_controls(foreign_id) == []


@pytest.mark.asyncio
async def test_a_project_reassignment_between_check_and_insert_queues_nothing(
    tmp_path: Path, monkeypatch
):
    """The ownership predicate has to live in the INSERT, not beside it.

    execute_run_control_command reads the session, decides it is admissible,
    and only then inserts. A sequential test cannot tell whether ownership is
    enforced inside that insert or merely checked before it, because both
    arrangements pass when nothing moves in between. So this drives the
    interleaving directly, reassigning the run to another project in the exact
    window the insert has to close on its own: after the admission check has
    already approved it, before the insert statement runs.
    """
    from lionagi.state.db import StateDB
    from lionagi.studio.operator import run_control as run_control_mod

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        session_id = await _seed_run(db, kind="flow")

    admitted: list[str] = []
    real_admission = run_control_mod._admission_refusal

    def _record_admission(session: dict[str, Any], verb: str) -> str | None:
        refusal = real_admission(session, verb)
        if refusal is None:
            admitted.append(session["id"])
        return refusal

    reassigned: list[str] = []
    real_insert = StateDB.insert_session_control

    async def _reassign_then_insert(self, **kwargs):
        if not reassigned:
            reassigned.append(kwargs["session_id"])
            async with StateDB() as thief:
                await thief.update_session(
                    kwargs["session_id"], project="stolen-by-another-project"
                )
        return await real_insert(self, **kwargs)

    monkeypatch.setattr(run_control_mod, "_admission_refusal", _record_admission)
    monkeypatch.setattr(StateDB, "insert_session_control", _reassign_then_insert)

    with pytest.raises(ValueError):
        await execute_run_control_command(
            {"session_id": session_id, "verb": "pause", "payload": None, "project": PROJECT}
        )

    # Both halves of the race actually happened: the command was admitted for
    # its own project, and the row moved before the insert ran. Without these
    # the refusal could come from some earlier check and prove nothing.
    assert admitted == [session_id]
    assert reassigned == [session_id]
    async with StateDB() as db:
        assert await db.list_pending_session_controls(session_id) == []


@pytest.mark.asyncio
async def test_the_same_insert_still_queues_when_ownership_holds_throughout(
    tmp_path: Path, monkeypatch
):
    """The positive arm of the interleaving test above.

    Without it, the refusal there is equally consistent with an insert that
    never queues anything for any input.
    """
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        session_id = await _seed_run(db, kind="flow")

    result = await execute_run_control_command(
        {"session_id": session_id, "verb": "pause", "payload": None, "project": PROJECT}
    )

    assert result["status"] == "queued"
    async with StateDB() as db:
        rows = await db.list_pending_session_controls(session_id)
    assert [row["verb"] for row in rows] == ["pause"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "run_id_present", "expected"),
    [
        ({"drains_controls": True}, True, True),
        # A mirrored or imported agent session: same invocation_kind as a live
        # one, but no lionagi run owns it and nothing declared a drain.
        ({}, False, False),
        ({"drains_controls": True}, False, False),
        ({}, True, False),
    ],
)
async def test_an_agent_session_reports_whether_a_control_would_reach_anyone(
    tmp_path: Path,
    monkeypatch,
    metadata: dict[str, Any],
    run_id_present: bool,
    expected: bool,
):
    """The read surface answers the same question admission asks.

    A client that offers a steer this predicate refuses gets a control that can
    never queue, so both sides read one rule.
    """
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.run_control import (
        _admission_refusal,
        session_has_control_consumer,
    )
    from lionagi.studio.services.sessions import get_session

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    session_id = str(uuid.uuid4())
    progression_id = str(uuid.uuid4())
    async with StateDB() as db:
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression_id,
                "status": "running",
                "started_at": time.time(),
                "invocation_kind": "agent",
                "run_id": session_id if run_id_present else None,
                "node_metadata": metadata,
                "project": PROJECT,
            }
        )
        session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,)))

    assert session_has_control_consumer(session) is expected
    # Admission agrees, which is what makes the projected field safe to offer a
    # control on: a true here can never meet a "no_consumer" there.
    assert (_admission_refusal(session, "message") != "no_consumer") is expected

    monkeypatch.setenv("LIONAGI_DB_PATH", str(path))
    detail = await get_session(session_id)
    assert detail["has_control_consumer"] is expected


@pytest.mark.asyncio
async def test_a_flow_run_never_has_to_declare_a_drain_to_be_controllable(
    tmp_path: Path, monkeypatch
):
    """Only agent sessions can fail the consumer check.

    Without this arm the predicate could be returning False for everything and
    the negative cases above would still pass.
    """
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.run_control import session_has_control_consumer

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    session_id = str(uuid.uuid4())
    progression_id = str(uuid.uuid4())
    async with StateDB() as db:
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression_id,
                "status": "running",
                "started_at": time.time(),
                "invocation_kind": "flow",
                "run_id": None,
                "node_metadata": {},
                "project": PROJECT,
            }
        )
        session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,)))

    assert session_has_control_consumer(session) is True
