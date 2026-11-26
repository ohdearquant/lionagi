# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Global replay reports each transition's session and project context after ACK."""

from __future__ import annotations

import time
import uuid
from inspect import isawaitable

import pytest
import pytest_asyncio

from lionhub.orchestration.scheduler import terminal_reconciliation as terminal
from lionhub.state.db import StateDB
from lionhub.state.lifecycle import notify_settings, settings_delivery
from lionhub.state.lifecycle.callbacks import TerminalCallbackRegistry
from lionhub.state.lifecycle.deliveries import is_acknowledged
from lionhub.state.lifecycle.reconciler import DeliveryOutcome, TerminalReconciler


@pytest_asyncio.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    try:
        yield state
    finally:
        await state.close()


def _id() -> str:
    return str(uuid.uuid4())


async def _invocation(db: StateDB) -> str:
    ident = _id()
    await db.create_invocation({"id": ident, "skill": "context-test", "started_at": time.time()})
    return ident


async def _session(db: StateDB, *, project=None, invocation=None) -> str:
    sid, pid = _id(), _id()
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": "running",
            "project": project,
            "invocation_id": invocation,
        }
    )
    return sid


async def _schedule_run(db: StateDB, invocation: str | None) -> str:
    schedule_id, run_id = _id(), _id()
    await db.create_schedule(
        {
            "id": schedule_id,
            "name": "ctx-" + schedule_id,
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": schedule_id,
            "invocation_id": invocation,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "running",
            "fired_at": time.time(),
        }
    )
    return run_id


def _row(kind: str, ident: str) -> dict:
    return {
        "entity_type": kind,
        "entity_id": ident,
        "transition_id": "event-" + ident,
        "previous_status": "running",
        "terminal_status": "failed",
        "reason_code": "run.failed.exception",
        "occurred_at": time.time(),
    }


async def _context_diagnostics(row, db):
    labels = []
    await terminal.observe_global_context_after_ack(row, db, labels.append, remaining_seconds=1)
    return labels


@pytest.mark.asyncio
async def test_session_and_play_use_their_linked_session(db, tmp_path):
    sid = await _session(db, project="alpha")
    no_project = await _session(db)
    show_id, play_id, null_project_play, orphan_play = _id(), _id(), _id(), _id()
    await db.create_show({"id": show_id, "topic": "test", "show_dir": str(tmp_path)})
    await db.create_play({"id": play_id, "show_id": show_id, "name": "linked", "session_id": sid})
    await db.create_play(
        {
            "id": null_project_play,
            "show_id": show_id,
            "name": "no-project",
            "session_id": no_project,
        }
    )
    await db.create_play({"id": orphan_play, "show_id": show_id, "name": "no-session"})
    assert await _context_diagnostics(_row("session", sid), db) == []
    assert await _context_diagnostics(_row("session", no_project), db) == ["null_project"]
    assert await _context_diagnostics(_row("session", _id()), db) == ["no_session"]
    assert await _context_diagnostics(_row("play", play_id), db) == []
    assert await _context_diagnostics(_row("play", null_project_play), db) == ["null_project"]
    assert await _context_diagnostics(_row("play", orphan_play), db) == ["no_session"]
    assert await _context_diagnostics(_row("play", _id()), db) == ["no_session"]


@pytest.mark.parametrize("kind", ["invocation", "schedule_run"])
@pytest.mark.parametrize(
    ("projects", "expected"),
    [
        pytest.param([], ["no_session"], id="no-sessions"),
        pytest.param([None, None], ["null_project"], id="all-null"),
        pytest.param(["alpha"], [], id="one-project"),
        pytest.param(["alpha", "alpha"], [], id="one-distinct-project"),
        pytest.param(["alpha", None, "alpha"], ["null_project"], id="one-project-and-null"),
        pytest.param(["alpha", "beta"], ["ambiguous_project"], id="multiple-projects"),
        pytest.param(
            [None, "alpha", "beta"], ["ambiguous_project", "null_project"], id="mixed-projects"
        ),
    ],
)
@pytest.mark.asyncio
async def test_invocation_and_schedule_resolve_distinct_project_set(db, kind, projects, expected):
    invocation_id = await _invocation(db)
    for project in projects:
        await _session(db, project=project, invocation=invocation_id)
    ident = invocation_id if kind == "invocation" else await _schedule_run(db, invocation_id)
    assert await _context_diagnostics(_row(kind, ident), db) == expected


@pytest.mark.asyncio
async def test_schedule_without_invocation_reports_missing_session_context(db):
    run_id = await _schedule_run(db, None)
    assert await _context_diagnostics(_row("schedule_run", run_id), db) == ["no_session"]


@pytest.mark.parametrize("filter_key", ["kinds", "ids"])
@pytest.mark.asyncio
async def test_settings_filters_apply_to_resolved_transition(db, tmp_path, monkeypatch, filter_key):
    home = tmp_path / "home"
    home.mkdir()
    settings = home / ".lionagi"
    settings.mkdir()
    value = "[play]" if filter_key == "kinds" else "[another-id]"
    (settings / "settings.yaml").write_text(
        "notify:\n  on_terminal:\n    enabled: true\n"
        f"    filter:\n      {filter_key}: {value}\n"
        "    adapter:\n      kind: exec\n      argv: [never-invoked]\n"
    )
    monkeypatch.setenv("HOME", str(home))
    build_attempts = []

    def record_build(*args, **kwargs):
        build_attempts.append(args)

    monkeypatch.setattr(settings_delivery, "build_handler", record_build)
    sid = await _session(db)
    prepared = await terminal.prepare_global_settings_delivery(_row("session", sid), db)
    value = prepared.call()
    outcome = await value if isawaitable(value) else value
    assert outcome is DeliveryOutcome.NOT_APPLICABLE
    assert build_attempts == []


@pytest.mark.asyncio
async def test_missing_and_ambiguous_context_are_observed_after_global_ack(db, monkeypatch):
    delivered = []
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "python", "ref": "test:notify"}}}},
    )
    monkeypatch.setattr(
        notify_settings, "_make_python_handler", lambda _ref: lambda e: delivered.append(e.event_id)
    )
    empty, ambiguous = await _invocation(db), await _invocation(db)
    await _session(db, project="alpha", invocation=ambiguous)
    await _session(db, project="beta", invocation=ambiguous)
    db._lifecycle_service()._terminal_callbacks = TerminalCallbackRegistry()
    for ident in (empty, ambiguous):
        assert await db.update_status(
            "invocation",
            ident,
            new_status="failed",
            reason_code="run.failed.exception",
            source="system",
            actor="test",
        )
    outcomes = {}
    consumer = "global-replay-context"

    async def observe(row, state, remaining):
        transition = row["transition_id"]
        acked = await is_acknowledged(state, transition, consumer)
        labels = []
        await terminal.observe_global_context_after_ack(
            row, state, labels.append, remaining_seconds=remaining
        )
        outcomes[row["entity_id"]] = (transition, acked, labels)

    reconciler = TerminalReconciler(
        consumer, terminal.prepare_global_settings_delivery, pass_budget=5
    )
    result = await reconciler.run_pass(db, observe_outcome=observe)
    assert result["attempted"] == result["acknowledged"] == 2
    assert result["not_applicable"] == result["no_session"] == result["ambiguous_project"] == 0
    assert set(outcomes) == {empty, ambiguous}
    assert outcomes[empty][1:] == (True, ["no_session"])
    assert outcomes[ambiguous][1:] == (True, ["ambiguous_project"])
    assert set(delivered) == {outcomes[empty][0], outcomes[ambiguous][0]}
    assert len(delivered) == 2
