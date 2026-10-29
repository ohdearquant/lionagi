# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Settings replay resolves each transition's own session/project context."""

from __future__ import annotations

import time
import uuid
from inspect import isawaitable
from pathlib import Path

import pytest
import pytest_asyncio

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, PreparedDelivery, TerminalReconciler
from lionagi.studio.services import terminal_reconciliation as terminal


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


async def _deliver(row, db):
    prepared = await terminal._prepare_settings_delivery(row, db)
    if isinstance(prepared, PreparedDelivery):
        value = prepared.call()
        return await value if isawaitable(value) else value
    return prepared


@pytest.mark.asyncio
async def test_session_and_play_use_their_linked_session(db, tmp_path):
    sid = await _session(db, project="alpha")
    no_project = await _session(db)
    show_id, play_id, orphan_play = _id(), _id(), _id()
    await db.create_show({"id": show_id, "topic": "test", "show_dir": str(tmp_path)})
    await db.create_play({"id": play_id, "show_id": show_id, "name": "linked", "session_id": sid})
    await db.create_play({"id": orphan_play, "show_id": show_id, "name": "no-session"})
    assert await terminal._project_for_transition(_row("session", sid), db) == ("alpha", None)
    assert await terminal._project_for_transition(_row("session", no_project), db) == (None, None)
    assert await terminal._project_for_transition(_row("session", _id()), db) == (
        None,
        "no_session",
    )
    assert await terminal._project_for_transition(_row("play", play_id), db) == ("alpha", None)
    assert await terminal._project_for_transition(_row("play", orphan_play), db) == (
        None,
        "no_session",
    )
    assert await terminal._project_for_transition(_row("play", _id()), db) == (None, "no_session")


@pytest.mark.parametrize("kind", ["invocation", "schedule_run"])
@pytest.mark.parametrize(
    ("projects", "expected"),
    [
        ([], (None, "no_session")),
        ([None, None], (None, "no_session")),
        (["alpha"], ("alpha", None)),
        (["alpha", None, "alpha"], ("alpha", None)),
        (["alpha", "beta"], (None, "ambiguous_project")),
    ],
)
@pytest.mark.asyncio
async def test_invocation_and_schedule_resolve_distinct_project_set(db, kind, projects, expected):
    invocation_id = await _invocation(db)
    for project in projects:
        await _session(db, project=project, invocation=invocation_id)
    ident = invocation_id if kind == "invocation" else await _schedule_run(db, invocation_id)
    assert await terminal._project_for_transition(_row(kind, ident), db) == expected


@pytest.mark.asyncio
async def test_schedule_without_invocation_is_not_applicable(db):
    run_id = await _schedule_run(db, None)
    assert await terminal._project_for_transition(_row("schedule_run", run_id), db) == (
        None,
        "no_session",
    )


def _settings(path: Path, command: str) -> None:
    directory = path / ".lionagi"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "settings.yaml").write_text(f"notify:\n  on_terminal: '{command}'\n")


@pytest.mark.parametrize("path_kind", ["existing", "missing", "null", "no-project"])
@pytest.mark.asyncio
async def test_settings_use_global_never_project_or_daemon_cwd(
    db, tmp_path, monkeypatch, path_kind
):
    home = tmp_path / "home"
    daemon = tmp_path / "daemon"
    project_dir = tmp_path / "project"
    for directory in (home, daemon, project_dir):
        directory.mkdir()
    _settings(home, "global-marker")
    _settings(daemon, "daemon-marker")
    _settings(project_dir, "project-marker")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(daemon)
    project = None if path_kind == "no-project" else "alpha"
    path = {
        "existing": str(project_dir),
        "missing": str(tmp_path / "missing"),
        "null": None,
        "no-project": None,
    }[path_kind]
    if project:
        await db.create_project(project, path=path)
    sid = await _session(db, project=project)
    calls = []
    actual_load = terminal.load_settings

    def load(*args, **kwargs):
        calls.append(kwargs)
        return actual_load(*args, **kwargs)

    selected = []

    def capture(resolved, envelope):
        selected.append((resolved.argv, envelope.entity.id))
        return DeliveryOutcome.SUCCESS

    monkeypatch.setattr(terminal, "load_settings", load)
    monkeypatch.setattr(terminal, "prepare_resolved_delivery", capture)
    outcome = await _deliver(_row("session", sid), db)
    assert outcome is DeliveryOutcome.SUCCESS
    assert calls == [{"include_project": False}]
    assert selected == [(("global-marker",), sid)]


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
    sid = await _session(db)
    assert await _deliver(_row("session", sid), db) is (DeliveryOutcome.NOT_APPLICABLE)


@pytest.mark.asyncio
async def test_no_session_and_ambiguous_project_are_acked_and_counted(db):
    empty, ambiguous = await _invocation(db), await _invocation(db)
    await _session(db, project="alpha", invocation=ambiguous)
    await _session(db, project="beta", invocation=ambiguous)
    for ident in (empty, ambiguous):
        assert await db.update_status(
            "invocation",
            ident,
            new_status="failed",
            reason_code="run.failed.exception",
            source="system",
            actor="test",
        )
    reconciler = TerminalReconciler("f9-context", terminal._prepare_settings_delivery, batch_size=4)
    result = await reconciler.run_pass(db)
    assert result["acknowledged"] == result["not_applicable"] == 2
    assert result["no_session"] == 1 and result["ambiguous_project"] == 1
