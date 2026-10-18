# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule definition policy, provenance and transaction-owned transitions."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lionagi.state import lifecycle
from lionagi.state.db import StateDB
from lionagi.state.lifecycle import (
    DEFAULT_REGISTRY,
    ActorRecord,
    EdgePolicy,
    InitialStateCommand,
    LifecycleNotFoundError,
    LifecycleValidationError,
    ReasonRecord,
    TransitionCommand,
)
from lionagi.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionagi.state.lifecycle.policy import PolicyRegistry
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService


@pytest_asyncio.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _create(db, schedule_id="definition", *, enabled=1):
    await db.create_schedule(
        {
            "id": schedule_id,
            "name": f"Schedule {schedule_id}",
            "enabled": enabled,
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
            "action_prompt": "Private execution prompt",
        }
    )
    return schedule_id


def _command(schedule_id, target="disabled", **overrides):
    cause = "unspecified" if target == "disabled" else "observed"
    values = {
        "entity_type": "schedule",
        "entity_id": schedule_id,
        "to_status": target,
        "reason": ReasonRecord(code=f"schedule_definition.{target}.{cause}"),
        "actor": ActorRecord(type="operator", id="test-operator"),
    }
    values.update(overrides)
    return TransitionCommand(**values)


async def _events(db, schedule_id):
    async with db._read() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM status_transitions WHERE entity_type = 'schedule' "
                        "AND entity_id = :id ORDER BY created_at, id"
                    ),
                    {"id": schedule_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _metadata(event):
    value = event["metadata"]
    return json.loads(value) if isinstance(value, str) else value


def _context(**overrides):
    context_type = getattr(lifecycle, "ScheduleAuditContext", None)
    assert context_type is not None, "Schedule provenance needs a typed public context"
    values = {
        "actor": "operator:alice",
        "submitting_cwd": "/work/submitted-project",
        "origin": "cli",
        "source": "cli",
        "assurance": "client_resolved",
        "actor_status": "available",
        "cwd_status": "available",
        "failure_category": None,
    }
    values.update(overrides)
    return context_type(**values)


async def _change_with_context(db, schedule_id, audit_context):
    adapter = SQLAlchemyLifecycleService(db).schedule_adapter
    async with db._tx() as conn:
        before = await adapter.read_for_update(conn, schedule_id)
        after = {**before, "enabled": 0}
        adapter.validate_change(before, after, audit_context=audit_context)
        await conn.execute(
            text("UPDATE schedules SET enabled = 0 WHERE id = :id"),
            {"id": schedule_id},
        )
        await adapter.record_change(conn, before, after, audit_context=audit_context)


@pytest.mark.asyncio
async def test_typed_transition_uses_enabled_and_hard_delete(db):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    for previous, target in (
        ("enabled", "disabled"),
        ("disabled", "enabled"),
        ("enabled", "deleted"),
    ):
        outcome = await service.transition(_command(schedule_id, target))
        assert (outcome.result, outcome.previous_status, outcome.current_status) == (
            "applied",
            previous,
            target,
        )
        assert outcome.transition_id is not None
        row = await db.get_schedule(schedule_id)
        if target == "deleted":
            assert row is None
        else:
            assert row["enabled"] == int(target == "enabled")
    events = await _events(db, schedule_id)
    assert [event["status"] for event in events] == ["enabled", "disabled", "enabled", "deleted"]
    assert all(event["actor"] == "test-operator" for event in events[1:])
    assert _metadata(events[1])["provenance"]["assurance"] == "client_reported"


@pytest.mark.asyncio
async def test_same_state_returns_applied_without_history_or_bookkeeping_write(db):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    await service.transition(_command(schedule_id))
    before = await db.get_schedule(schedule_id)
    history = await _events(db, schedule_id)
    outcome = await service.transition(_command(schedule_id))
    assert (outcome.result, outcome.previous_status, outcome.current_status) == (
        "applied",
        "disabled",
        "disabled",
    )
    assert outcome.transition_id is None
    assert await db.get_schedule(schedule_id) == before
    assert await _events(db, schedule_id) == history


@pytest.mark.asyncio
async def test_adapter_consumes_the_supplied_policy_registry(db):
    schedule_id = await _create(db, enabled=0)
    registry = PolicyRegistry()
    registry.register(
        replace(
            DEFAULT_REGISTRY.get("schedule"),
            edges={"disabled": (EdgePolicy(to_status="enabled"),)},
        )
    )
    registry.seal()
    service = SQLAlchemyLifecycleService(db, registry=registry)
    assert (await service.transition(_command(schedule_id, "enabled"))).result == "applied"
    history = await _events(db, schedule_id)
    outcome = await service.transition(_command(schedule_id))
    assert outcome.result == "rejected"
    assert (await db.get_schedule(schedule_id))["enabled"] == 1
    assert await _events(db, schedule_id) == history


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["enabled", "disabled"])
async def test_typed_initializer_observes_actual_initial_enabled_state(db, target):
    schedule_id = "initial"
    service = SQLAlchemyLifecycleService(db)
    async with db._tx() as conn:
        stmt, params = db._build_schedule_insert_stmt(
            {
                "id": schedule_id,
                "name": "Initial schedule",
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
                "enabled": int(target == "enabled"),
            }
        )
        await conn.execute(stmt, params)
        transition_id = await service.initialize_in_transaction(
            conn,
            InitialStateCommand(
                entity_type="schedule",
                entity_id=schedule_id,
                status=target,
                reason=ReasonRecord(code="schedule_definition.created.observed"),
                actor=ActorRecord(type="operator", id="initializer"),
            ),
        )
    events = await _events(db, schedule_id)
    assert len(events) == 1
    assert (events[0]["id"], events[0]["previous_status"], events[0]["status"]) == (
        transition_id,
        None,
        target,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_context_is_preserved_or_explicitly_unavailable(db, unavailable):
    schedule_id = await _create(db)
    context = None if unavailable else _context()
    await _change_with_context(db, schedule_id, context)
    event = (await _events(db, schedule_id))[-1]
    metadata = _metadata(event)
    assert metadata["schedule_name"] == "Schedule definition"
    assert "Private execution prompt" not in json.dumps(metadata)
    assert event["actor"] == (None if unavailable else "operator:alice")
    assert event["source"] == ("programmatic" if unavailable else "cli")
    assert metadata["provenance"] == {
        "origin": "programmatic" if unavailable else "cli",
        "submitting_cwd": None if unavailable else "/work/submitted-project",
        "actor_status": "unavailable" if unavailable else "available",
        "cwd_status": "unavailable" if unavailable else "available",
        "assurance": "unavailable" if unavailable else "client_resolved",
        "failure_category": "not_provided" if unavailable else None,
    }


@pytest.mark.asyncio
async def test_reason_metadata_cannot_replace_observed_provenance(db):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    reason = ReasonRecord(
        code="schedule_definition.disabled.manual",
        summary="Pause this definition during maintenance",
        metadata={
            "schedule_name": "Forged name",
            "provenance": {"origin": "scheduler", "assurance": "system"},
            "ticket": "maintenance-42",
        },
    )
    assert (await service.transition(_command(schedule_id, reason=reason))).result == "applied"
    event = (await _events(db, schedule_id))[-1]
    metadata = _metadata(event)
    assert event["reason_summary"] == reason.summary
    assert metadata["ticket"] == "maintenance-42"
    assert metadata["schedule_name"] == "Schedule definition"
    assert metadata["provenance"]["origin"] == "programmatic"
    assert metadata["provenance"]["assurance"] == "client_reported"


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["", " \n\t"])
async def test_explicit_manual_reason_requires_its_explanation(db, summary):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    assert (
        await service.transition(
            _command(
                schedule_id,
                reason=ReasonRecord(
                    code="schedule_definition.disabled.manual", summary="Maintenance"
                ),
            )
        )
    ).result == "applied"
    assert (await service.transition(_command(schedule_id, "enabled"))).result == "applied"
    before, history = await db.get_schedule(schedule_id), await _events(db, schedule_id)
    with pytest.raises(LifecycleValidationError, match="explanation"):
        await service.transition(
            _command(
                schedule_id,
                reason=ReasonRecord(code="schedule_definition.disabled.manual", summary=summary),
            )
        )
    assert await db.get_schedule(schedule_id) == before
    assert await _events(db, schedule_id) == history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"actor": " "},
        {"actor": None},
        {"actor_status": "unavailable"},
        {"submitting_cwd": "relative/project"},
        {"submitting_cwd": None},
        {"cwd_status": "unavailable"},
        {"origin": "made-up"},
        {"source": ""},
        {"assurance": "authenticated"},
        {"failure_category": "raw resolver traceback"},
        {"origin": "http", "source": "http", "assurance": "system"},
        {"origin": "browser", "source": "browser"},
    ],
)
async def test_invalid_context_refuses_before_mutation(db, invalid):
    control_id = await _create(db, "control")
    await _change_with_context(db, control_id, _context())
    assert (await db.get_schedule(control_id))["enabled"] == 0
    schedule_id = await _create(db)
    before, history = await db.get_schedule(schedule_id), await _events(db, schedule_id)
    with pytest.raises(LifecycleValidationError):
        await _change_with_context(db, schedule_id, _context(**invalid))
    assert await db.get_schedule(schedule_id) == before
    assert await _events(db, schedule_id) == history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason_code",
    [
        "schedule.skipped.precondition",
        "run.completed.ok",
        "schedule_definition.enabled.observed",
        "schedule_definition.deleted.observed",
        "schedule_definition.created.observed",
        "schedule_definition.disabled.invented",
    ],
)
async def test_reason_must_belong_to_the_definition_target(db, reason_code):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    assert (await service.transition(_command(schedule_id))).result == "applied"
    assert (await service.transition(_command(schedule_id, "enabled"))).result == "applied"
    before, history = await db.get_schedule(schedule_id), await _events(db, schedule_id)
    with pytest.raises(LifecycleValidationError):
        await service.transition(_command(schedule_id, reason=ReasonRecord(code=reason_code)))
    assert await db.get_schedule(schedule_id) == before
    assert await _events(db, schedule_id) == history


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["running", "", "ENABLED"])
async def test_invalid_state_target_refuses_before_mutation(db, target):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    assert (await service.transition(_command(schedule_id))).result == "applied"
    before, history = await db.get_schedule(schedule_id), await _events(db, schedule_id)
    with pytest.raises(LifecycleValidationError):
        await service.transition(
            _command(
                schedule_id,
                target,
                reason=ReasonRecord(code="schedule_definition.enabled.observed"),
            )
        )
    assert await db.get_schedule(schedule_id) == before
    assert await _events(db, schedule_id) == history


@pytest.mark.asyncio
async def test_missing_and_stale_claims_add_no_event(db):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    assert (await service.transition(_command(schedule_id))).result == "applied"
    before, history = await db.get_schedule(schedule_id), await _events(db, schedule_id)
    for changes in (
        {"expected_statuses": frozenset({"enabled"})},
        {"expected_version": before["updated_at"] - 1},
    ):
        outcome = await service.transition(_command(schedule_id, "enabled", **changes))
        assert outcome.result == "conflict"
        assert await db.get_schedule(schedule_id) == before
        assert await _events(db, schedule_id) == history
    with pytest.raises(LifecycleNotFoundError):
        await service.transition(_command("missing"))
    assert await _events(db, "missing") == []


@pytest.mark.asyncio
async def test_history_insert_failure_rolls_back_typed_deletion(db):
    schedule_id = await _create(db)
    service = SQLAlchemyLifecycleService(db)
    assert (await service.transition(_command(schedule_id))).result == "applied"
    before, history = await db.get_schedule(schedule_id), await _events(db, schedule_id)
    async with db._tx() as conn:
        await conn.execute(
            text(
                "CREATE TRIGGER refuse_definition_history BEFORE INSERT ON status_transitions "
                "WHEN NEW.entity_type = 'schedule' BEGIN SELECT RAISE(ABORT, 'audit refused'); END"
            )
        )
    with pytest.raises(IntegrityError, match="audit refused"):
        await service.transition(_command(schedule_id, "deleted"))
    assert await db.get_schedule(schedule_id) == before
    assert await _events(db, schedule_id) == history


@pytest.mark.asyncio
async def test_schedule_never_emits_execution_callbacks(db):
    registry = TerminalCallbackRegistry()
    seen = []
    registry.register("observe", seen.append)
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=registry)
    schedule_id = await _create(db)
    assert (await service.transition(_command(schedule_id, "deleted"))).result == "applied"
    assert seen == []
    await db.create_progression("progression")
    await db.create_session({"id": "run", "progression_id": "progression", "status": "running"})
    outcome = await service.transition(
        TransitionCommand(
            entity_type="session",
            entity_id="run",
            to_status="completed",
            reason=ReasonRecord(code="run.completed.ok"),
            actor=ActorRecord(type="executor", id="execution"),
        )
    )
    assert outcome.result == "applied"
    assert len(seen) == 1
    assert seen[0].entity.kind == "session"
