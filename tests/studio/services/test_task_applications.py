# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0101 D1 slice 2: the task-application submit surface.

Covers submit_task's round-trip write, the CAS-governed queued -> cancelled
cancel path, rejection of a malformed TaskApplication, and the negative vocab
test proving transitions.transition() actively rejects queued -> running for
schedule_run in this slice (not merely unexercised).
"""

from __future__ import annotations

import json
import socket

import pytest

from lionagi.state.db import StateDB
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition
from lionagi.studio.services.task_applications import TaskApplication, cancel_task, submit_task


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _actor() -> Actor:
    return Actor(type="operator", id="test")


# ── 1. Submit round-trip ─────────────────────────────────────────────────


async def test_submit_task_writes_queued_row_with_every_field(db: StateDB) -> None:
    app = TaskApplication(
        action_kind="agent",
        args={"prompt": "hello"},
        execution_target="host",
        required_capabilities=["gpu-exclusive", "lean-toolchain"],
        library_ref="ns/name@1.0.0",
        library_content_hash="deadbeef",
        idempotency_key="idem-1",
    )

    run_id = await submit_task(db, app)

    row = await db.fetch_one("SELECT * FROM schedule_runs WHERE id = ?", (run_id,))
    assert row is not None
    assert row["schedule_id"] is None
    assert row["status"] == "queued"
    assert row["queued_at"] is not None
    assert row["action_kind"] == "agent"
    assert row["action_args"] == '{"prompt": "hello"}'
    assert row["execution_target"] == "host"
    assert row["library_ref"] == "ns/name@1.0.0"
    assert row["library_content_hash"] == "deadbeef"
    assert row["leased_by"] is None
    assert row["lease_expires_at"] is None
    assert json.loads(row["required_capabilities"]) == ["gpu-exclusive", "lean-toolchain"]

    expected_key = f"{socket.gethostname()}:gpu-exclusive+lean-toolchain"
    assert row["concurrency_key"] == expected_key


async def test_submit_task_normalizes_playbook_alias(db: StateDB) -> None:
    app = TaskApplication(
        action_kind="playbook",
        args={},
        execution_target="host",
    )
    run_id = await submit_task(db, app)
    row = await db.fetch_one("SELECT action_kind FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["action_kind"] == "play"


async def test_submit_task_workflow_kind_accepted(db: StateDB) -> None:
    app = TaskApplication(action_kind="workflow", args={}, execution_target="host")
    run_id = await submit_task(db, app)
    row = await db.fetch_one("SELECT action_kind FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["action_kind"] == "workflow"


async def test_submit_task_no_capabilities_no_concurrency_key(db: StateDB) -> None:
    app = TaskApplication(action_kind="agent", args={}, execution_target="host")
    run_id = await submit_task(db, app)
    row = await db.fetch_one("SELECT concurrency_key FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["concurrency_key"] is None


# ── 2. CAS-governed queued -> cancelled ──────────────────────────────────


async def test_cancel_task_succeeds_via_transition_store(db: StateDB) -> None:
    app = TaskApplication(action_kind="agent", args={}, execution_target="host")
    run_id = await submit_task(db, app)

    applied = await cancel_task(db, run_id, actor=_actor())
    assert applied is True

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "cancelled"

    audit = await db.fetch_all(
        "SELECT previous_status, status, entity_type FROM status_transitions WHERE entity_id = ?",
        (run_id,),
    )
    assert len(audit) == 1
    assert audit[0]["previous_status"] == "queued"
    assert audit[0]["status"] == "cancelled"
    assert audit[0]["entity_type"] == "schedule_run"


async def test_cancel_task_twice_is_not_applied_second_time(db: StateDB) -> None:
    app = TaskApplication(action_kind="agent", args={}, execution_target="host")
    run_id = await submit_task(db, app)

    assert await cancel_task(db, run_id, actor=_actor()) is True
    # Second cancel: the row is no longer "queued" (it's "cancelled"), so the
    # CAS guard's from_state="queued" mismatch reports a conflict rather than
    # re-applying — ordinary CAS behavior, unchanged by this slice's vocab gate.
    assert await cancel_task(db, run_id, actor=_actor()) is False


# ── 3. Rejection paths ───────────────────────────────────────────────────


async def test_submit_task_rejects_unknown_action_kind(db: StateDB) -> None:
    app = TaskApplication(action_kind="not_a_real_kind", args={}, execution_target="host")
    with pytest.raises(ValueError, match="unknown action_kind"):
        await submit_task(db, app)


async def test_submit_task_rejects_malformed_required_capabilities_not_a_list(
    db: StateDB,
) -> None:
    app = TaskApplication(
        action_kind="agent",
        args={},
        execution_target="host",
        required_capabilities={"gpu-exclusive": True},  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="required_capabilities"):
        await submit_task(db, app)


async def test_submit_task_rejects_malformed_required_capabilities_non_string_element(
    db: StateDB,
) -> None:
    app = TaskApplication(
        action_kind="agent",
        args={},
        execution_target="host",
        required_capabilities=["gpu-exclusive", 7],  # type: ignore[list-item]
    )
    with pytest.raises(ValueError, match="required_capabilities"):
        await submit_task(db, app)


async def test_submit_task_rejects_unknown_execution_target(db: StateDB) -> None:
    app = TaskApplication(action_kind="agent", args={}, execution_target="mars")
    with pytest.raises(ValueError, match="execution_target"):
        await submit_task(db, app)


# ── 4. Negative vocab test — queued -> running is actively rejected ─────


async def test_transition_store_rejects_queued_to_running_for_schedule_run(
    db: StateDB,
) -> None:
    """The declared vocabulary is actively narrow, not merely unexercised:
    attempting queued -> running through transitions.transition() must fail
    even though the CAS guard itself would otherwise happily apply it."""
    app = TaskApplication(action_kind="agent", args={}, execution_target="host")
    run_id = await submit_task(db, app)

    with pytest.raises(ValueError, match="not in the declared transition vocabulary"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="queued",
                to_state="running",
                reason=StateReason(code="run.started.ok"),
                actor=_actor(),
                idempotency_key="idem-reject",
            ),
        )

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "queued"  # untouched — the rejected write never landed
