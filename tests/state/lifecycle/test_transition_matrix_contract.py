# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Independent golden for every managed lifecycle transition graph."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text

from lionagi.dispatch import enqueue_dispatch, get_dispatch
from lionagi.state.db import StateDB
from lionagi.state.lifecycle import (
    ActorRecord,
    JsonValue,
    LifecycleValidationError,
    ReasonRecord,
    TransitionCommand,
)
from lionagi.state.lifecycle.models import ActorType
from lionagi.state.lifecycle.policy import DEFAULT_REGISTRY
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService
from lionagi.state.reasons import (
    DispatchReasons,
    PlayReasons,
    RunReasons,
    ScheduleReasons,
    SessionReasons,
    ShowReasons,
    TeamReasons,
)


@dataclass(frozen=True)
class _ExpectedEdge:
    source: str
    target: str
    actor_types: frozenset[ActorType] | None = None
    required_patch_fields: frozenset[str] = frozenset()
    required_guard_fields: frozenset[str] = frozenset()


_SESSION_STATUSES = frozenset(
    {"running", "completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
)
_SESSION_TERMINAL = frozenset(
    {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
)
_SHOW_STATUSES = frozenset({"active", "completed", "aborted", "imported"})
_SHOW_TERMINAL = frozenset({"completed", "aborted"})
_PLAY_STATUSES = frozenset(
    {
        "pending",
        "prepared",
        "running",
        "running_complete",
        "gated",
        "gate_failed",
        "redoing",
        "merged",
        "escalated",
        "blocked",
        "aborted_after_finish",
    }
)
_PLAY_TERMINAL = frozenset(
    {"merged", "escalated", "gate_failed", "blocked", "aborted_after_finish"}
)
_TEAM_STATUSES = frozenset({"active", "archived"})
_TEAM_TERMINAL = frozenset({"archived"})
_SCHEDULE_RUN_STATUSES = frozenset(
    {
        "queued",
        "waiting_dependency",
        "running",
        "retry_wait",
        "completed",
        "failed",
        "timed_out",
        "skipped",
        "cancelled",
    }
)
_SCHEDULE_RUN_TERMINAL = frozenset({"completed", "failed", "timed_out", "skipped", "cancelled"})
_DISPATCH_STATUSES = frozenset(
    {"pending", "delivering", "delivered", "acked", "dead_letter", "expired"}
)
_DISPATCH_TERMINAL = frozenset({"delivered", "acked", "dead_letter", "expired"})


def _edge(source: str, target: str) -> _ExpectedEdge:
    return _ExpectedEdge(source, target)


def _dense_nonterminal_graph(
    statuses: frozenset[str], terminal_statuses: frozenset[str]
) -> tuple[_ExpectedEdge, ...]:
    return tuple(
        _edge(source, target)
        for source in sorted(statuses - terminal_statuses)
        for target in sorted(statuses - {source})
    )


_EXPECTED_STATUSES = {
    "session": _SESSION_STATUSES,
    "invocation": _SESSION_STATUSES,
    "show": _SHOW_STATUSES,
    "play": _PLAY_STATUSES,
    "team": _TEAM_STATUSES,
    "schedule_run": _SCHEDULE_RUN_STATUSES,
    "dispatch": _DISPATCH_STATUSES,
}

_EXPECTED_TERMINAL = {
    "session": _SESSION_TERMINAL,
    "invocation": _SESSION_TERMINAL,
    "show": _SHOW_TERMINAL,
    "play": _PLAY_TERMINAL,
    "team": _TEAM_TERMINAL,
    "schedule_run": _SCHEDULE_RUN_TERMINAL,
    "dispatch": _DISPATCH_TERMINAL,
}

_EXPECTED_EDGES: dict[str, tuple[_ExpectedEdge, ...]] = {
    "session": tuple(_edge("running", status) for status in sorted(_SESSION_TERMINAL)),
    "invocation": tuple(_edge("running", status) for status in sorted(_SESSION_TERMINAL)),
    "show": _dense_nonterminal_graph(_SHOW_STATUSES, _SHOW_TERMINAL),
    "play": _dense_nonterminal_graph(_PLAY_STATUSES, _PLAY_TERMINAL),
    "team": (_edge("active", "archived"),),
    "schedule_run": (
        _edge("queued", "waiting_dependency"),
        _edge("queued", "running"),
        _edge("queued", "skipped"),
        _edge("queued", "cancelled"),
        _edge("waiting_dependency", "queued"),
        _edge("waiting_dependency", "cancelled"),
        _edge("running", "completed"),
        _edge("running", "failed"),
        _edge("running", "timed_out"),
        _edge("running", "retry_wait"),
        _edge("running", "queued"),
        _edge("running", "cancelled"),
        _edge("retry_wait", "queued"),
        _edge("retry_wait", "cancelled"),
    ),
    "dispatch": (
        _edge("pending", "delivering"),
        _edge("pending", "expired"),
        _edge("pending", "acked"),
        _ExpectedEdge(
            "delivering",
            "delivering",
            required_guard_fields=frozenset({"attempt"}),
        ),
        _edge("delivering", "pending"),
        _edge("delivering", "delivered"),
        _edge("delivering", "acked"),
        _edge("delivering", "dead_letter"),
        _edge("delivering", "expired"),
        _ExpectedEdge(
            "dead_letter",
            "pending",
            actor_types=frozenset({"operator"}),
            required_patch_fields=frozenset({"attempt", "next_attempt_at", "last_error"}),
        ),
        _ExpectedEdge(
            "expired",
            "pending",
            actor_types=frozenset({"operator"}),
            required_patch_fields=frozenset({"attempt", "next_attempt_at", "last_error"}),
        ),
    ),
}

_REASON_CODES = {
    "session": RunReasons.COMPLETED_OK,
    "invocation": RunReasons.COMPLETED_OK,
    "show": ShowReasons.ACTIVE_CREATED,
    "play": PlayReasons.PENDING_CREATED,
    "team": TeamReasons.ARCHIVED_OPERATOR,
    "schedule_run": ScheduleReasons.FIRED_DUE,
    "dispatch": DispatchReasons.PENDING_ENQUEUED,
}

_TABLES = {
    "session": "sessions",
    "invocation": "invocations",
    "show": "shows",
    "play": "plays",
    "team": "teams",
    "schedule_run": "schedule_runs",
    "dispatch": "dispatch_outbox",
}


def _actual_edges(entity_type: str) -> tuple[_ExpectedEdge, ...]:
    policy = DEFAULT_REGISTRY.get(entity_type)
    return tuple(
        _ExpectedEdge(
            source,
            edge.to_status,
            actor_types=edge.actor_types,
            required_patch_fields=edge.required_patch_fields,
            required_guard_fields=edge.required_guard_fields,
        )
        for source, edges in policy.edges.items()
        for edge in edges
    )


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[StateDB]:
    state = StateDB(tmp_path / "state.db")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return uuid.uuid4().hex


async def _create_base_entity(db: StateDB, entity_type: str) -> str:
    entity_id = _uid()
    if entity_type == "session":
        progression_id = _uid()
        await db.create_progression(progression_id)
        await db.create_session(
            {"id": entity_id, "progression_id": progression_id, "status": "running"}
        )
    elif entity_type == "invocation":
        await db.create_invocation(
            {
                "id": entity_id,
                "skill": "transition-matrix",
                "started_at": time.time(),
                "status": "running",
            }
        )
    elif entity_type == "show":
        await db.create_show(
            {
                "id": entity_id,
                "topic": f"transition-{entity_id[:8]}",
                "show_dir": "/tmp/transition-matrix",
                "status": "active",
            }
        )
    elif entity_type == "play":
        show_id = await _create_base_entity(db, "show")
        await db.create_play(
            {
                "id": entity_id,
                "show_id": show_id,
                "name": f"transition-{entity_id[:8]}",
                "status": "pending",
                "started_at": time.time(),
            }
        )
    elif entity_type == "team":
        now = time.time()
        async with db._tx() as connection:  # noqa: SLF001 - no public team create facade
            await connection.execute(
                text(
                    "INSERT INTO teams (id, name, created_at, updated_at, status) "
                    "VALUES (:id, :name, :now, :now, 'active')"
                ),
                {"id": entity_id, "name": f"transition-{entity_id[:8]}", "now": now},
            )
    elif entity_type == "schedule_run":
        schedule_id = _uid()
        await db.create_schedule(
            {
                "id": schedule_id,
                "name": f"transition-{schedule_id[:8]}",
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
            }
        )
        await db.create_schedule_run(
            {
                "id": entity_id,
                "schedule_id": schedule_id,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": "queued",
                "fired_at": time.time(),
            }
        )
    elif entity_type == "dispatch":
        entity_id = await enqueue_dispatch(
            db,
            kind="terminal_notify",
            deliver_to=f"seat-{entity_id[:8]}",
        )
    else:  # pragma: no cover - closed by the literal parameter sets
        raise AssertionError(f"unknown entity type {entity_type!r}")
    return entity_id


async def _make_entity(db: StateDB, entity_type: str, status: str) -> str:
    entity_id = await _create_base_entity(db, entity_type)
    table = _TABLES[entity_type]
    async with db._tx() as connection:  # noqa: SLF001 - fixture state setup
        await connection.execute(
            text(f"UPDATE {table} SET status = :status WHERE id = :id"),
            {"status": status, "id": entity_id},
        )
    return entity_id


async def _version(db: StateDB, entity_type: str, entity_id: str) -> float:
    row = await db.fetch_one(
        f"SELECT updated_at FROM {_TABLES[entity_type]} WHERE id = ?",
        (entity_id,),
    )
    assert row is not None
    return row["updated_at"]


def _command(
    entity_type: str,
    entity_id: str,
    edge: _ExpectedEdge,
    *,
    expected_version: float | None = None,
) -> TransitionCommand:
    patch_values = {
        "attempt": 0,
        "next_attempt_at": 0.0,
        "last_error": "operator recovery",
    }
    actor_type: ActorType = min(edge.actor_types) if edge.actor_types else "system"
    return TransitionCommand(
        entity_type=entity_type,
        entity_id=entity_id,
        to_status=edge.target,
        reason=ReasonRecord(code=_REASON_CODES[entity_type]),
        actor=ActorRecord(type=actor_type, id="transition-matrix"),
        patch={field: patch_values[field] for field in edge.required_patch_fields},
        expected_version=expected_version,
    )


@pytest.mark.parametrize("entity_type", tuple(_EXPECTED_EDGES))
def test_policy_declares_the_exact_independent_edge_golden(entity_type: str) -> None:
    policy = DEFAULT_REGISTRY.get(entity_type)

    assert policy.statuses == _EXPECTED_STATUSES[entity_type]
    assert policy.terminal_statuses == _EXPECTED_TERMINAL[entity_type]
    assert _actual_edges(entity_type) == _EXPECTED_EDGES[entity_type]


_ALL_ALLOWED_EDGES = tuple(
    (entity_type, edge) for entity_type, edges in _EXPECTED_EDGES.items() for edge in edges
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_type,edge",
    _ALL_ALLOWED_EDGES,
    ids=lambda value: (
        f"{value.source}-to-{value.target}" if isinstance(value, _ExpectedEdge) else value
    ),
)
async def test_every_allowed_edge_applies(
    db: StateDB,
    entity_type: str,
    edge: _ExpectedEdge,
) -> None:
    entity_id = await _make_entity(db, entity_type, edge.source)
    expected_version = (
        await _version(db, entity_type, entity_id) if edge.required_guard_fields else None
    )

    outcome = await SQLAlchemyLifecycleService(db).transition(
        _command(entity_type, entity_id, edge, expected_version=expected_version)
    )

    assert outcome.result == "applied"
    assert outcome.previous_status == edge.source
    assert outcome.current_status == edge.target
    assert outcome.transition_id is not None


_TERMINAL_EXIT_CASES = tuple(
    (
        entity_type,
        terminal_status,
        target,
    )
    for entity_type, statuses in _EXPECTED_TERMINAL.items()
    for terminal_status in sorted(statuses)
    for target in sorted(_EXPECTED_STATUSES[entity_type] - {terminal_status})
    if not any(
        edge.source == terminal_status and edge.target == target
        for edge in _EXPECTED_EDGES[entity_type]
    )
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_type,terminal_status,target",
    _TERMINAL_EXIT_CASES,
    ids=lambda value: value,
)
async def test_every_undeclared_terminal_exit_fails_closed(
    db: StateDB,
    entity_type: str,
    terminal_status: str,
    target: str,
) -> None:
    entity_id = await _make_entity(db, entity_type, terminal_status)
    edge = _ExpectedEdge(terminal_status, target)

    outcome = await SQLAlchemyLifecycleService(db).transition(
        _command(entity_type, entity_id, edge)
    )

    assert outcome.result == "rejected"
    assert outcome.previous_status == terminal_status
    assert outcome.current_status == terminal_status
    assert outcome.transition_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor_type,patch,error",
    (
        (
            "system",
            {"attempt": 0, "next_attempt_at": 0.0, "last_error": "operator recovery"},
            "actor type",
        ),
        (
            "operator",
            {"next_attempt_at": 0.0, "last_error": "operator recovery"},
            "requires patch field",
        ),
        (
            "operator",
            {"attempt": 0, "last_error": "operator recovery"},
            "requires patch field",
        ),
        (
            "operator",
            {"attempt": 0, "next_attempt_at": 0.0},
            "requires patch field",
        ),
    ),
)
async def test_dispatch_recovery_constraints_fail_before_row_mutation(
    db: StateDB,
    actor_type: ActorType,
    patch: dict[str, JsonValue],
    error: str,
) -> None:
    dispatch_id = await _make_entity(db, "dispatch", "dead_letter")
    dispatch_before = await get_dispatch(db, dispatch_id)
    assert dispatch_before is not None
    history_before = await db.fetch_all(
        "SELECT id FROM status_transitions WHERE entity_type = 'dispatch' AND entity_id = ?",
        (dispatch_id,),
    )

    with pytest.raises(LifecycleValidationError, match=error):
        await SQLAlchemyLifecycleService(db).transition(
            TransitionCommand(
                entity_type="dispatch",
                entity_id=dispatch_id,
                to_status="pending",
                reason=ReasonRecord(code=DispatchReasons.PENDING_ENQUEUED),
                actor=ActorRecord(type=actor_type, id="transition-matrix"),
                patch=patch,
            )
        )

    dispatch_after = await get_dispatch(db, dispatch_id)
    assert dispatch_after is not None
    fields = ("status", "attempt", "next_attempt_at", "last_error")
    assert {field: dispatch_after[field] for field in fields} == {
        field: dispatch_before[field] for field in fields
    }
    history_after = await db.fetch_all(
        "SELECT id FROM status_transitions WHERE entity_type = 'dispatch' AND entity_id = ?",
        (dispatch_id,),
    )
    assert history_after == history_before


@pytest.mark.asyncio
async def test_selected_same_status_rule_refreshes_reason_and_appends_history(
    db: StateDB,
) -> None:
    session_id = await _make_entity(db, "session", "running")
    service = SQLAlchemyLifecycleService(db)

    outcome = await service.transition(
        TransitionCommand(
            entity_type="session",
            entity_id=session_id,
            to_status="running",
            reason=ReasonRecord(
                code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
                summary="heartbeat evidence refreshed",
            ),
            actor=ActorRecord(type="system", id="transition-matrix"),
        )
    )

    assert outcome.result == "applied"
    assert outcome.previous_status == "running"
    assert outcome.current_status == "running"
    assert outcome.transition_id is not None
    session = await db.get_session(session_id)
    assert session is not None
    assert session["status"] == "running"
    assert session["status_reason_code"] == SessionReasons.HEALTH_STALE_NO_HEARTBEAT
    assert session["status_reason_summary"] == "heartbeat evidence refreshed"

    history = await db.fetch_all(
        "SELECT previous_status, status, reason_code, reason_summary "
        "FROM status_transitions WHERE entity_type = 'session' AND entity_id = ? "
        "ORDER BY created_at, id",
        (session_id,),
    )
    assert history[-1] == {
        "previous_status": "running",
        "status": "running",
        "reason_code": SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        "reason_summary": "heartbeat evidence refreshed",
    }


@pytest.mark.asyncio
async def test_companion_fields_and_history_share_the_status_cas(
    db: StateDB,
) -> None:
    session_id = await _make_entity(db, "session", "running")
    stale_version = await _version(db, "session", session_id)
    history_before = await db.fetch_all(
        "SELECT id FROM status_transitions WHERE entity_type = 'session' AND entity_id = ?",
        (session_id,),
    )
    async with db._tx() as connection:  # noqa: SLF001 - simulates a concurrent row writer
        await connection.execute(
            text("UPDATE sessions SET updated_at = updated_at + 1 WHERE id = :id"),
            {"id": session_id},
        )

    outcome = await SQLAlchemyLifecycleService(db).transition(
        TransitionCommand(
            entity_type="session",
            entity_id=session_id,
            to_status="completed",
            reason=ReasonRecord(code=RunReasons.COMPLETED_OK),
            actor=ActorRecord(type="system", id="transition-matrix"),
            patch={"ended_at": 400.0, "input_tokens": 99},
            expected_version=stale_version,
        )
    )

    assert outcome.result == "conflict"
    assert outcome.previous_status == "running"
    assert outcome.current_status == "running"
    assert outcome.transition_id is None
    session = await db.get_session(session_id)
    assert session is not None
    assert session["status"] == "running"
    assert session["ended_at"] is None
    assert session["input_tokens"] is None
    history_after = await db.fetch_all(
        "SELECT id FROM status_transitions WHERE entity_type = 'session' AND entity_id = ?",
        (session_id,),
    )
    assert history_after == history_before
