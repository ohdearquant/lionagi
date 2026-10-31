# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Studio Operator `cancel_run` lifecycle service/adapter.

Covers: successful mutation (allow -> real process/DB cancellation), denial
(run left untouched, mutation callback never invoked), and no-op paths where
no mutation callback runs at all (already-terminal, not-found, ambiguous).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

from lionagi.studio.operator.cancel_run import (
    CancelRunInput,
    MissingOwnerContextError,
    cancel_run,
    execute_cancel_command,
)
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import OperatorStore


def _patch_state_db(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import lionagi.cli._runs as runs_mod
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", path.parent / "runs")


DEFAULT_TEST_PROJECT = "/Users/admin/test-project"


async def _seed_session(
    db,
    *,
    status: str = "running",
    pid: int | None = None,
    name: str | None = None,
    playbook_name: str | None = None,
    started_at: float | None = None,
    project: str | None = DEFAULT_TEST_PROJECT,
) -> str:
    sid = str(uuid.uuid4())
    progression_id = str(uuid.uuid4())
    await db.create_progression(progression_id)
    node_meta = {"pid": pid} if pid is not None else {}
    await db.create_session(
        {
            "id": sid,
            "progression_id": progression_id,
            "status": status,
            "name": name,
            "playbook_name": playbook_name,
            "started_at": started_at if started_at is not None else time.time(),
            "node_metadata": node_meta,
            "project": project,
        }
    )
    return sid


async def _wait_proposal(store: OperatorStore, request_id: str, *, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await store.list_proposals_for_request(request_id)
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise TimeoutError("cancel_run proposal did not appear")


async def _make_running_turn(
    store: OperatorStore, *, context: dict | None = None
) -> tuple[str, str]:
    """A conversation + turn in 'running' status: the precondition
    `create_proposal` enforces before it will accept a proposal."""
    conversation = await store.create_conversation()
    cid = conversation["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="stop the run",
        context=context
        or {"space": "mission", "route": "/", "filters": {}, "project": DEFAULT_TEST_PROJECT},
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    return cid, accepted["requestId"]


def _set_identity(monkeypatch, path: Path, cid: str, request_id: str) -> None:
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)


def _command_executor(calls: list):
    """Simulates the step-7 coordinator wiring this module's adapter needs
    (see cancel_run.py's module docstring): dispatches command_type=="cancel"
    to `execute_cancel_command`, exactly what `_execute_application_command`
    in coordinator.py must do once wired."""

    async def execute(command_type, command):
        calls.append((command_type, command))
        if command_type == "cancel":
            return await execute_cancel_command(command)
        raise ValueError(f"unexpected command_type {command_type!r}")

    return execute


# ── cancel_run(): proposal summary names more than a bare id ────────────


async def test_cancel_run_proposal_summary_names_project_name_status_and_elapsed(
    tmp_path, monkeypatch
):
    """A human approving a destructive cancel needs more than a 12-char id
    and an optional raw reason -- the proposal summary must also name the
    project, the run's own name, its status, and how long it has run."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(
            db,
            status="running",
            name="nightly-triage",
            started_at=time.time() - 125,
        )
        await db.update_session(run_id, project="/Users/admin/acme-research")

    store = OperatorStore(path)
    calls: list = []
    coordinator = OperatorCoordinator(store=store, command_executor=_command_executor(calls))
    await coordinator.startup()
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/acme-research",
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    task = asyncio.create_task(cancel_run({"run": run_id, "reason": "stuck"}))
    proposal = await _wait_proposal(store, request_id)

    assert "acme-research" in proposal["summary"]
    assert "nightly-triage" in proposal["summary"]
    assert "running" in proposal["summary"]
    assert "2m" in proposal["summary"]
    assert "stuck" in proposal["summary"]
    assert run_id[:12] in proposal["summary"]

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=False,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    await asyncio.wait_for(task, timeout=2)
    assert decision["status"] == "failed"
    await coordinator.shutdown()


# ── cancel_run(): allow path -> real mutation ────────────────────────────


async def test_cancel_run_allow_path_terminates_and_persists_cancel(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None)

    store = OperatorStore(path)
    calls: list = []
    coordinator = OperatorCoordinator(store=store, command_executor=_command_executor(calls))
    await coordinator.startup()
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    task = asyncio.create_task(cancel_run({"run": run_id, "reason": "stuck"}))
    proposal = await _wait_proposal(store, request_id)
    assert not task.done()
    assert proposal["commandType"] == "cancel"
    assert proposal["command"] == {
        "session_id": run_id,
        "reason": "stuck",
        "project": DEFAULT_TEST_PROJECT,
    }
    assert proposal["risk"] == "execute"

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "succeeded"
    assert calls == [
        ("cancel", {"session_id": run_id, "reason": "stuck", "project": DEFAULT_TEST_PROJECT})
    ]
    assert result == {
        "cancelled": True,
        "status": "terminal",
        "id": run_id,
        "signal": "no_pid",
        "run_untouched": False,
    }

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "cancelled"
    await coordinator.shutdown()


# ── cancel_run(): deny path -> unchanged state, no mutation callback ────


async def test_cancel_run_deny_path_leaves_run_untouched(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None)

    store = OperatorStore(path)
    calls: list = []
    coordinator = OperatorCoordinator(store=store, command_executor=_command_executor(calls))
    await coordinator.startup()
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    task = asyncio.create_task(cancel_run({"run": run_id}))
    proposal = await _wait_proposal(store, request_id)

    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=False,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "failed"
    assert decision["error"]["code"] == "denied"
    assert calls == []  # the mutation callback never ran
    assert result == {
        "cancelled": False,
        "reason": "denied",
        "run_untouched": True,
        "id": run_id,
    }

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "running"
    await coordinator.shutdown()


# ── cancel_run(): already-terminal -> idempotent, no proposal at all ────


async def test_cancel_run_already_terminal_creates_no_proposal(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="completed", pid=None)

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": run_id})

    assert result == {
        "cancelled": False,
        "status": "already_terminal",
        "id": run_id,
        "run_untouched": True,
    }
    assert await store.list_proposals_for_request(request_id) == []

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "completed"


# ── cancel_run(): not found / ambiguous -> fail closed, no proposal ─────


async def test_cancel_run_not_found_creates_no_proposal(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB():
        pass

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "deadbeef00000000"})

    assert result == {"cancelled": False, "reason": "not_found", "run_untouched": True}
    assert await store.list_proposals_for_request(request_id) == []


async def test_cancel_run_ambiguous_reference_returns_candidates_not_a_guess(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        first = await _seed_session(db, status="running", name="nightly-triage-1")
        second = await _seed_session(db, status="running", name="nightly-triage-2")

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "nightly"})

    assert result["cancelled"] is False
    assert result["reason"] == "ambiguous_reference"
    assert result["run_untouched"] is True
    assert set(result["candidates"]) == {first, second}
    assert await store.list_proposals_for_request(request_id) == []


# ── "current" reference resolution ────────────────────────────────────────


async def test_cancel_run_current_resolves_via_the_turns_own_selection(tmp_path, monkeypatch):
    """ "current" reads the turn's OWN frozen selection (key "s"), not a
    later-reported live view -- see cancel_run.py::_current_run_id."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="completed", pid=None)

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "history",
            "route": "/fleet",
            "filters": {},
            "selection": {"s": run_id},
            "project": DEFAULT_TEST_PROJECT,
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "current"})

    assert result["id"] == run_id
    assert result["status"] == "already_terminal"


async def test_cancel_run_current_with_no_selection_is_not_found(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "current"})
    assert result == {"cancelled": False, "reason": "not_found", "run_untouched": True}


# ── strict input validation ───────────────────────────────────────────────


def test_cancel_run_input_rejects_unknown_fields():
    with pytest.raises(Exception):
        CancelRunInput.model_validate({"run": "abc", "extra": "nope"})


def test_cancel_run_input_requires_a_non_empty_run():
    with pytest.raises(Exception):
        CancelRunInput.model_validate({"run": ""})


# ── execute_cancel_command(): the adapter, unit-tested directly ─────────


async def test_execute_cancel_command_no_pid_still_persists_cancel(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None)

    result = await execute_cancel_command(
        {"session_id": run_id, "reason": "no pid on this run", "project": DEFAULT_TEST_PROJECT}
    )
    assert result == {"status": "terminal", "id": run_id, "signal": "no_pid"}

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "cancelled"
        transition = await db.fetch_one(
            "SELECT * FROM status_transitions WHERE entity_id = ? AND status = 'cancelled'",
            (run_id,),
        )
        assert transition is not None
        assert transition["reason_code"] == RunReasons.CANCELLED_MANUAL_KILL


async def test_execute_cancel_command_already_terminal_is_a_no_op(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="failed", pid=None)

    result = await execute_cancel_command({"session_id": run_id, "project": DEFAULT_TEST_PROJECT})
    assert result == {"status": "already_terminal", "id": run_id}

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "failed"
        transitions = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (run_id,)
        )
        assert transitions == []


async def test_execute_cancel_command_not_found(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB():
        pass

    result = await execute_cancel_command({"session_id": "does-not-exist"})
    assert result == {"status": "not_found", "id": "does-not-exist"}


async def test_execute_cancel_command_missing_session_id_raises():
    with pytest.raises(ValueError, match="session_id"):
        await execute_cancel_command({"reason": "no id given"})


# ── execute_cancel_command(): running children must not be orphaned ──────


async def test_execute_cancel_command_reaps_a_running_child_invocation(tmp_path, monkeypatch):
    """Cancelling a session must not leave its owning invocation's process
    running -- the same deepest-first traversal `li kill --recursive` uses
    (lionagi/cli/kill.py::_list_running_children/_kill_one) must also run
    here, or the child process is orphaned."""
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    invocation_id = str(uuid.uuid4())
    async with StateDB() as db:
        await db.create_invocation(
            {
                "id": invocation_id,
                "skill": "resume:agent",
                "plugin": "studio_run_resume",
                "prompt": "continue",
                "started_at": time.time(),
                "status": "running",
                "node_metadata": {"pid": None},
            }
        )
        progression_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": run_id,
                "progression_id": progression_id,
                "status": "running",
                "name": "parent-run",
                "started_at": time.time(),
                "invocation_id": invocation_id,
                "project": DEFAULT_TEST_PROJECT,
            }
        )

    result = await execute_cancel_command(
        {"session_id": run_id, "reason": "stuck", "project": DEFAULT_TEST_PROJECT}
    )
    assert result == {"status": "terminal", "id": run_id, "signal": "no_pid"}

    async with StateDB() as db:
        session_row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert session_row["status"] == "cancelled"

        invocation_row = await db.fetch_one(
            "SELECT status FROM invocations WHERE id = ?", (invocation_id,)
        )
        assert invocation_row["status"] == "cancelled"

        invocation_transition = await db.fetch_one(
            "SELECT * FROM status_transitions WHERE entity_id = ? AND status = 'cancelled'",
            (invocation_id,),
        )
        assert invocation_transition is not None
        assert invocation_transition["reason_code"] == RunReasons.CANCELLED_MANUAL_KILL


async def test_execute_cancel_command_a_terminal_child_invocation_is_left_alone(
    tmp_path, monkeypatch
):
    """A child that already finished on its own must not get a spurious
    cancellation transition -- only running children are reaped."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    invocation_id = str(uuid.uuid4())
    async with StateDB() as db:
        await db.create_invocation(
            {
                "id": invocation_id,
                "skill": "resume:agent",
                "plugin": "studio_run_resume",
                "prompt": "continue",
                "started_at": time.time(),
                "status": "completed",
            }
        )
        progression_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": run_id,
                "progression_id": progression_id,
                "status": "running",
                "name": "parent-run",
                "started_at": time.time(),
                "invocation_id": invocation_id,
                "project": DEFAULT_TEST_PROJECT,
            }
        )

    result = await execute_cancel_command({"session_id": run_id, "project": DEFAULT_TEST_PROJECT})
    assert result == {"status": "terminal", "id": run_id, "signal": "no_pid"}

    async with StateDB() as db:
        invocation_row = await db.fetch_one(
            "SELECT status FROM invocations WHERE id = ?", (invocation_id,)
        )
        assert invocation_row["status"] == "completed"
        transitions = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (invocation_id,)
        )
        assert transitions == []


# ── execute_cancel_command(): false-success regressions ──────────────────


async def test_execute_cancel_command_identity_mismatch_does_not_report_cancelled(
    tmp_path, monkeypatch
):
    """An identity-mismatched process is never signalled and _kill_one never
    persists a status change for it; the adapter must not claim otherwise."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=999999)

    async def fake_kill_one(db, entity_type, entity_id, row, **kwargs):
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "signal": "identity_mismatch",
            "pid": row.get("node_metadata", {}).get("pid"),
        }

    import lionagi.cli._kill_service as kill_service
    import lionagi.cli.kill as kill_mod

    monkeypatch.setattr(kill_mod, "_kill_one", fake_kill_one)
    monkeypatch.setattr(kill_service, "kill_one", fake_kill_one)

    result = await execute_cancel_command({"session_id": run_id, "project": DEFAULT_TEST_PROJECT})
    assert result == {"status": "not_stopped", "id": run_id, "signal": "identity_mismatch"}

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "running"


async def test_execute_cancel_command_reports_an_imported_row_that_was_not_stopped(
    tmp_path, monkeypatch
):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None)
        await db.execute("UPDATE sessions SET source_kind = 'imported_fs' WHERE id = ?", (run_id,))

    result = await execute_cancel_command({"session_id": run_id, "project": DEFAULT_TEST_PROJECT})

    assert result == {
        "status": "not_stopped",
        "id": run_id,
        "signal": "imported_no_process",
    }
    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
    assert row["status"] == "running"


async def test_execute_cancel_command_terminalized_during_approval_is_not_cancelled(
    tmp_path, monkeypatch
):
    """The run finishes on its own between the pre-kill check and the actual
    persist (a race the human approval window can create). `_persist_cancel`
    is a guarded no-op in that case -- the adapter must read the real
    post-persist row rather than assume the signal outcome means success."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None)

    real_kill_one = None

    async def racing_kill_one(db, entity_type, entity_id, row, **kwargs):
        # Simulate the run completing naturally right before the real
        # primitive would persist "cancelled" -- its own guard then makes
        # the write a no-op.
        from lionagi.state.reasons import RunReasons

        await db.update_status(
            "session",
            entity_id,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_summary="finished before cancel could persist",
            source="system",
            actor="test",
        )
        return await real_kill_one(db, entity_type, entity_id, row, **kwargs)

    import lionagi.cli._kill_service as kill_service
    import lionagi.cli.kill as kill_mod

    real_kill_one = kill_mod._kill_one
    monkeypatch.setattr(kill_mod, "_kill_one", racing_kill_one)
    monkeypatch.setattr(kill_service, "kill_one", racing_kill_one)

    result = await execute_cancel_command({"session_id": run_id, "project": DEFAULT_TEST_PROJECT})
    assert result["status"] == "already_terminal"
    assert result["id"] == run_id

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "completed"


# ── cancel_run(): full allow-path regression for a false-success outcome ──


async def test_cancel_run_allow_path_identity_mismatch_reports_not_cancelled(tmp_path, monkeypatch):
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=999999)

    async def fake_kill_one(db, entity_type, entity_id, row, **kwargs):
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "signal": "identity_mismatch",
            "pid": row.get("node_metadata", {}).get("pid"),
        }

    import lionagi.cli._kill_service as kill_service
    import lionagi.cli.kill as kill_mod

    monkeypatch.setattr(kill_mod, "_kill_one", fake_kill_one)
    monkeypatch.setattr(kill_service, "kill_one", fake_kill_one)

    store = OperatorStore(path)
    calls: list = []
    coordinator = OperatorCoordinator(store=store, command_executor=_command_executor(calls))
    await coordinator.startup()
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    task = asyncio.create_task(cancel_run({"run": run_id}))
    proposal = await _wait_proposal(store, request_id)
    decision = await coordinator.decide(
        cid,
        proposal["id"],
        allow=True,
        expected_command_hash=proposal["commandHash"],
        expected_target_version=proposal["targetVersion"],
    )
    result = await asyncio.wait_for(task, timeout=2)

    assert decision["status"] == "succeeded"  # the executor ran without raising
    assert result == {
        "cancelled": False,
        "status": "not_stopped",
        "id": run_id,
        "signal": "identity_mismatch",
        "run_untouched": True,
    }

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "running"
    await coordinator.shutdown()


# ── reference resolution: project isolation ───────────────────────────────


async def test_cancel_run_text_search_is_scoped_to_the_turns_project(tmp_path, monkeypatch):
    """A name-substring reference must not resolve to -- or even reveal the
    existence of -- a same-named run in a different project."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        in_scope = await _seed_session(db, status="completed", name="nightly-triage")
        await db.update_session(in_scope, project="/Users/admin/acme-research")
        out_of_scope = await _seed_session(db, status="completed", name="nightly-triage")
        await db.update_session(out_of_scope, project="/Users/admin/acme-ops")

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/acme-research",
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "nightly-triage"})

    # Resolves uniquely to the in-scope run -- the out-of-scope same-named
    # run is excluded from the search entirely, not merely deduplicated.
    assert result == {
        "cancelled": False,
        "status": "already_terminal",
        "id": in_scope,
        "run_untouched": True,
    }


async def test_cancel_run_unscoped_turn_is_refused_for_every_reference_form(tmp_path, monkeypatch):
    """Cancellation mutates the run it names, so a turn that declares no
    project is refused before anything is resolved -- the same position
    ``run_control`` takes for pause/resume/steer, and for the same two
    reasons: the turn cannot prove it owns the run, and refusing before
    resolution keeps the answer from becoming an oracle for which run ids
    exist in projects it cannot see. Every reference form gets the one
    refusal, including an exact full UUID, and no proposal is created."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        first = await _seed_session(db, status="completed", name="nightly-triage")
        await db.update_session(first, project="/Users/admin/acme-research")
        second = await _seed_session(db, status="completed", name="nightly-triage")
        await db.update_session(second, project="/Users/admin/acme-ops")
        running = await _seed_session(db, status="running", pid=None)

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "history",
            "route": "/fleet",
            "filters": {},
            "selection": {"s": first},
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    refusal = {"cancelled": False, "reason": "missing_owner_context", "run_untouched": True}

    # A name, an id prefix, and an exact full id of a row this turn cannot
    # prove it owns.
    assert await cancel_run({"run": "nightly-triage"}) == refusal
    assert await cancel_run({"run": first[:8]}) == refusal
    assert await cancel_run({"run": running}) == refusal
    # The same answer for an id that does not exist at all: a not-found
    # answer here would tell the turn that the previous id exists.
    assert await cancel_run({"run": str(uuid.uuid4())}) == refusal
    # And the run the human has open -- 'current' is a write target here,
    # not a read.
    assert await cancel_run({"run": "current"}) == refusal

    assert await store.list_proposals_for_request(request_id) == []
    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (running,))
        assert row["status"] == "running"


async def test_cancel_run_resolver_refuses_an_unscoped_turn_with_a_typed_error(
    tmp_path, monkeypatch
):
    """The tool reports the refusal as a structured reason, but the resolver
    beneath it still raises the typed error -- a caller that goes through the
    resolver instead of the tool handler gets a refusal too, never a silent
    unscoped lookup."""
    from lionagi.state.db import StateDB
    from lionagi.studio.operator.cancel_run import _resolve_reference

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None)

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store, context={"space": "mission", "route": "/", "filters": {}}
    )
    _set_identity(monkeypatch, path, cid, request_id)

    with pytest.raises(MissingOwnerContextError):
        await _resolve_reference(store, request_id, run_id)
    # Nothing is selected on this turn, so 'current' would read as not-found if it
    # were resolved before the refusal.
    with pytest.raises(MissingOwnerContextError):
        await _resolve_reference(store, request_id, "current")
    assert await store.list_proposals_for_request(request_id) == []


async def test_cancel_run_scoped_turn_cannot_reach_a_projectless_row_by_exact_id(
    tmp_path, monkeypatch
):
    """A turn that declares a project and names a running row that carries no
    project of its own gets the same not-found answer it gets for another
    project's row: ownership is an equality check, and an unscoped row is not
    a wildcard. No proposal is created and the row keeps running."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None, project=None)

    store = OperatorStore(path)
    calls: list = []
    coordinator = OperatorCoordinator(store=store, command_executor=_command_executor(calls))
    await coordinator.startup()
    cid, request_id = await _make_running_turn(store)
    _set_identity(monkeypatch, path, cid, request_id)

    # A correctly-scoped resolution returns "not_found" immediately -- it
    # never creates a proposal to wait on. Bound the wait so a regression
    # that resumes creating a proposal fails the test instead of hanging.
    result = await asyncio.wait_for(cancel_run({"run": run_id}), timeout=5)

    assert result == {"cancelled": False, "reason": "not_found", "run_untouched": True}
    assert await store.list_proposals_for_request(request_id) == []
    assert calls == []
    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "running"
    await coordinator.shutdown()


async def test_execute_cancel_command_without_project_cannot_touch_an_owned_row(
    tmp_path, monkeypatch
):
    """A command carrying no project matches only a row that also has none.
    Against a row that belongs to a project it fails exactly like a
    nonexistent id -- a project-less command must never be treated as
    authorized for every project's runs."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        run_id = await _seed_session(db, status="running", pid=None)

    result = await execute_cancel_command({"session_id": run_id})
    assert result == {"status": "not_found", "id": run_id}

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        assert row["status"] == "running"


async def test_execute_cancel_command_projectless_pairing_requires_row_still_projectless(
    tmp_path, monkeypatch
):
    """The (no command project, no row project) pairing executes -- and its
    guard is the row STILL having no project. A row that gained an owner
    during the human's approval window fails closed as not_found."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        projectless = await _seed_session(db, status="running", pid=None, project=None)
        gained_owner = await _seed_session(db, status="running", pid=None, project=None)
        await db.update_session(gained_owner, project=DEFAULT_TEST_PROJECT)

    result = await execute_cancel_command({"session_id": projectless, "project": None})
    assert result == {"status": "terminal", "id": projectless, "signal": "no_pid"}

    stale = await execute_cancel_command({"session_id": gained_owner, "project": None})
    assert stale == {"status": "not_found", "id": gained_owner}

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (projectless,))
        assert row["status"] == "cancelled"
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (gained_owner,))
        assert row["status"] == "running"


async def test_cancel_run_id_prefix_window_counts_only_visible_rows(tmp_path, monkeypatch):
    """A run of the turn's own project must still be found when foreign rows
    fill the id-ordered window the prefix query reads: the window counts only
    rows the turn may see."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    owned = "deadbeef-0006-2222-3333-444455556666"
    async with StateDB() as db:
        prog_owned = str(uuid.uuid4())
        await db.create_progression(prog_owned)
        await db.create_session(
            {
                "id": owned,
                "progression_id": prog_owned,
                "status": "completed",
                "name": "owned-run",
                "project": "/Users/admin/acme-research",
                "started_at": time.time(),
            }
        )
        for index in range(6):
            foreign = f"deadbeef-{index:04d}-8888-7777-666655554444"
            prog_foreign = str(uuid.uuid4())
            await db.create_progression(prog_foreign)
            await db.create_session(
                {
                    "id": foreign,
                    "progression_id": prog_foreign,
                    "status": "completed",
                    "name": "foreign-run",
                    "project": "/Users/admin/acme-ops",
                    "started_at": time.time(),
                }
            )

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/acme-research",
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "deadbeef"})

    assert result["id"] == owned
    assert result["cancelled"] is False
    assert result["status"] == "already_terminal"


async def test_cancel_run_exact_id_of_a_foreign_project_terminal_run_is_not_found(
    tmp_path, monkeypatch
):
    """An exact/prefix id match must obey the same project ownership the
    text-search arm already enforces -- a foreign project's run must not even
    be visible as 'already terminal', which would confirm its existence."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        foreign_run = await _seed_session(db, status="completed", name="foreign-run")
        await db.update_session(foreign_run, project="/Users/admin/acme-ops")

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/acme-research",
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": foreign_run})

    assert result == {"cancelled": False, "reason": "not_found", "run_untouched": True}
    assert await store.list_proposals_for_request(request_id) == []


async def test_cancel_run_exact_id_of_a_foreign_project_running_run_creates_no_proposal(
    tmp_path, monkeypatch
):
    """The dangerous half of the bypass: a foreign project's RUNNING run must
    not reach the proposal/execution path at all."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    async with StateDB() as db:
        foreign_run = await _seed_session(db, status="running", name="foreign-run")
        await db.update_session(foreign_run, project="/Users/admin/acme-ops")

    store = OperatorStore(path)
    calls: list = []
    coordinator = OperatorCoordinator(store=store, command_executor=_command_executor(calls))
    await coordinator.startup()
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/acme-research",
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    # A correctly-scoped resolution returns "not_found" immediately -- it
    # never creates a proposal to wait on. Bound the wait so a regression
    # that resumes creating a proposal fails the test instead of hanging.
    result = await asyncio.wait_for(cancel_run({"run": foreign_run}), timeout=5)

    assert result == {"cancelled": False, "reason": "not_found", "run_untouched": True}
    assert await store.list_proposals_for_request(request_id) == []
    assert calls == []

    async with StateDB() as db:
        row = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (foreign_run,))
        assert row["status"] == "running"
    await coordinator.shutdown()


async def test_cancel_run_ambiguous_id_prefix_hides_foreign_project_candidates(
    tmp_path, monkeypatch
):
    """A short id-prefix collision must drop foreign-project candidates from
    the ambiguity list -- and resolve uniquely when only one owned row
    survives, rather than reporting a foreign row as a candidate."""
    from lionagi.state.db import StateDB

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    shared_prefix = "deadbeef"
    owned = f"{shared_prefix}-1111-2222-3333-444455556666"
    foreign = f"{shared_prefix}-9999-8888-7777-666655554444"
    async with StateDB() as db:
        prog_owned = str(uuid.uuid4())
        await db.create_progression(prog_owned)
        await db.create_session(
            {
                "id": owned,
                "progression_id": prog_owned,
                "status": "completed",
                "name": "owned-run",
                "project": "/Users/admin/acme-research",
                "started_at": time.time(),
            }
        )
        prog_foreign = str(uuid.uuid4())
        await db.create_progression(prog_foreign)
        await db.create_session(
            {
                "id": foreign,
                "progression_id": prog_foreign,
                "status": "completed",
                "name": "foreign-run",
                "project": "/Users/admin/acme-ops",
                "started_at": time.time(),
            }
        )

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/acme-research",
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": shared_prefix})

    assert result == {
        "cancelled": False,
        "status": "already_terminal",
        "id": owned,
        "run_untouched": True,
    }


# ── cancel_run(): an id-prefix collision reports a bounded candidate window ──


async def _seed_prefixed_runs(path: Path, count: int, project: str) -> list[str]:
    """Seed *count* runs whose ids share one literal prefix, sorted by index,
    and return those ids in order."""
    from lionagi.state.db import StateDB

    ids = [f"deadbeef-{index:04d}-2222-3333-444455556666" for index in range(count)]
    async with StateDB() as db:
        for sid in ids:
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": progression_id,
                    "status": "running",
                    "name": "nightly-triage",
                    "project": project,
                    "started_at": time.time(),
                }
            )
    return ids


async def test_cancel_run_id_prefix_candidates_cap_at_the_listing_size(tmp_path, monkeypatch):
    """Seven in-scope runs share an id prefix: cancel proposes no target and
    reports candidates the same way the read-only resolver does -- at most the
    CLI helper's listing size, with truncation true because more rows matched
    than are returned."""
    from lionagi.cli._util import _CANDIDATES_SHOWN

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    project = "/Users/admin/acme-research"
    ids = await _seed_prefixed_runs(path, 7, project)

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": project,
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "deadbeef"})

    assert result["cancelled"] is False
    assert result["reason"] == "ambiguous_reference"
    assert result["run_untouched"] is True
    assert len(result["candidates"]) == _CANDIDATES_SHOWN
    assert set(result["candidates"]) == set(ids[:_CANDIDATES_SHOWN])
    assert result["truncated"] is True
    assert await store.list_proposals_for_request(request_id) == []


async def test_cancel_run_id_prefix_exactly_the_listing_size_is_not_truncated(
    tmp_path, monkeypatch
):
    """Exactly the listing size of in-scope runs share an id prefix: every
    match is returned, so nothing was dropped and truncation is false."""
    from lionagi.cli._util import _CANDIDATES_SHOWN

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    project = "/Users/admin/acme-research"
    ids = await _seed_prefixed_runs(path, _CANDIDATES_SHOWN, project)

    store = OperatorStore(path)
    cid, request_id = await _make_running_turn(
        store,
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": project,
        },
    )
    _set_identity(monkeypatch, path, cid, request_id)

    result = await cancel_run({"run": "deadbeef"})

    assert result["cancelled"] is False
    assert result["reason"] == "ambiguous_reference"
    assert len(result["candidates"]) == _CANDIDATES_SHOWN
    assert set(result["candidates"]) == set(ids)
    assert result["truncated"] is False
    assert await store.list_proposals_for_request(request_id) == []
