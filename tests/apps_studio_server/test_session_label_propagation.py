# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Explicit labels remain the run's name across searches and Operator actions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from lionagi.state.db import StateDB

PROJECT = "label-project"
SID = "abcde000-0000-4000-8000-000000000001"
SECOND = "abcde000-0000-4000-8000-000000000002"
FOREIGN = "abcde000-0000-4000-8000-000000000003"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    import lionagi.state.db as state_db_mod

    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
    )
    return path


@pytest_asyncio.fixture
async def label_client(db_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("fastapi")
    from lionagi.studio.app import create_app

    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    transport = httpx.ASGITransport(app=create_app(), client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        yield client


async def _seed(
    path: Path,
    session_id: str = SID,
    *,
    label: str | None = "Pinned label",
    **fields: Any,
) -> None:
    async with StateDB(path) as db:
        progression_id = f"{session_id}-progression"
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression_id,
                "name": "System prompt name",
                "show_play_name": "Authored play",
                "playbook_name": "Authored playbook",
                "agent_name": "Worker",
                "invocation_kind": "flow",
                "status": "completed",
                "project": PROJECT,
                "created_at": 10.0,
                "updated_at": 100.0,
                "started_at": 60.0,
                "last_message_at": 90.0,
                **fields,
            }
        )
        assert await db.set_session_user_label(session_id, label)


def _stored(path: Path, session_id: str = SID) -> dict[str, Any]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    assert row is not None
    return dict(row)


def _drop_label_column(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN user_label")


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}


@pytest.mark.parametrize("reader", ["list", "count"])
@pytest.mark.parametrize(
    ("label", "query", "decoy"),
    [
        ("Pinned LABEL", "pinned label", "Different label"),
        ("task_100%", "task_100%", "taskX100more"),
        (r"path\part", r"path\part", "pathpart"),
    ],
)
async def test_session_label_search_is_literal_and_project_scoped(
    db_path, reader, label, query, decoy
):
    from lionagi.studio.services.sessions import SessionFilter, count_sessions, list_sessions

    await _seed(db_path, label=label)
    await _seed(db_path, SECOND, label=decoy)
    await _seed(db_path, FOREIGN, label=label, project="another-project")
    where = SessionFilter(search=query, project=PROJECT)
    if reader == "list":
        result = await list_sessions(where=where)
        assert [(row["id"], row["name"]) for row in result] == [(SID, label)]
    else:
        assert await count_sessions(where) == 1

    # Searching a pre-label store retains the old fields without migrating it.
    _drop_label_column(db_path)
    where = SessionFilter(search="SYSTEM PROMPT", project=PROJECT)
    if reader == "list":
        assert {row["id"] for row in await list_sessions(where=where)} == {SID, SECOND}
    else:
        assert await count_sessions(where) == 2
    assert "user_label" not in _columns(db_path)


async def test_http_run_search_finds_explicit_label(db_path, label_client):
    await _seed(db_path)
    await _seed(db_path, FOREIGN, project="another-project")

    response = await label_client.get(
        "/api/runs/", params={"search": "pinned label", "project": PROJECT}
    )
    assert response.status_code == 200
    payload = response.json()
    rows = payload["runs"]
    assert [(row["id"], row["name"]) for row in rows] == [(SID, "Pinned label")]
    assert payload["total"] == 1


async def test_progress_resolves_labels_and_shows_labelled_text_candidates(db_path, monkeypatch):
    from lionagi.studio.operator import run_progress as progress

    async def allowed_project():
        return PROJECT

    monkeypatch.setattr(progress, "_allowed_project", allowed_project)
    await _seed(db_path)
    await _seed(db_path, FOREIGN, project="another-project")
    result = await progress.resolve_run("pinned label")
    assert result == {"found": True, "ambiguous": False, "session_id": SID}

    await _seed(db_path, SECOND, label="Pinned label second")
    ambiguous = await progress.resolve_run("pinned label")
    assert ambiguous["ambiguous"] is True
    assert {(row["id"], row["name"]) for row in ambiguous["candidates"]} == {
        (SID, "Pinned label"),
        (SECOND, "Pinned label second"),
    }


@pytest.mark.parametrize(
    ("label", "query", "decoy"),
    [
        ("Pinned LABEL", "pinned label", "Different label"),
        ("task_100%", "task_100%", "taskX100more"),
        (r"path\part", r"path\part", "pathpart"),
    ],
)
async def test_cancel_resolves_labels_with_literal_matching_and_project_scope(
    db_path, label, query, decoy
):
    from lionagi.studio.operator.cancel_run import _resolve_run

    await _seed(db_path, label=label)
    await _seed(db_path, SECOND, label=decoy)
    await _seed(db_path, FOREIGN, label=label, project="another-project")
    async with StateDB(db_path, readonly=True) as db:
        row = await _resolve_run(db, query, project=PROJECT)
    assert row is not None
    assert row["id"] == SID

    _drop_label_column(db_path)
    async with StateDB(db_path, readonly=True) as db:
        row = await _resolve_run(db, "System prompt", project="another-project")
    assert row is not None
    assert row["id"] == FOREIGN
    assert "user_label" not in _columns(db_path)


@pytest.mark.parametrize(
    ("fields", "label", "expected"),
    [
        ({}, "Pinned label", "Pinned label"),
        ({}, None, "Authored play"),
        ({"show_play_name": None}, None, "Authored playbook"),
        ({"show_play_name": None, "playbook_name": None}, None, "Worker · abcd · 00:01"),
    ],
)
async def test_progress_prefix_candidates_use_complete_display_name_fields(
    db_path, monkeypatch, fields, label, expected
):
    from lionagi.studio.operator import run_progress as progress

    async def allowed_project():
        return PROJECT

    monkeypatch.setattr(progress, "_allowed_project", allowed_project)
    await _seed(db_path, label=label, **fields)
    await _seed(db_path, SECOND, label="Second explicit name")
    await _seed(db_path, FOREIGN, project="another-project")
    result = await progress.resolve_run("abcde000")
    assert result["ambiguous"] is True
    names = {row["id"]: row["name"] for row in result["candidates"]}
    assert names == {SID: expected, SECOND: "Second explicit name"}

    _drop_label_column(db_path)
    result = await progress.resolve_run("abcde000")
    names = {row["id"]: row["name"] for row in result["candidates"]}
    assert names == {SID: "Authored play" if label else expected, SECOND: "Authored play"}
    assert "user_label" not in _columns(db_path)


@pytest.mark.parametrize("summary", ["cancel", "rename", "pause", "resume", "message"])
def test_operator_proposals_name_the_displayed_run(summary):
    from lionagi.studio.operator.cancel_run import _cancel_summary
    from lionagi.studio.operator.rename_session import _rename_summary
    from lionagi.studio.operator.run_control import _proposal_summary

    row = {
        "id": SID,
        "name": "System prompt name",
        "playbook_name": "Authored playbook",
        "user_label": "Pinned label",
        "project": PROJECT,
    }
    if summary == "cancel":
        text = _cancel_summary(row)
    elif summary == "rename":
        text = _rename_summary(row, "Approved new name")
    else:
        text = _proposal_summary(
            row, summary, {"text": "Continue"} if summary == "message" else None
        )
    assert "Pinned label" in text
    assert "System prompt name" not in text
    assert "Authored playbook" not in text


async def test_invocation_child_session_uses_label_then_authored_fallback(db_path):
    from lionagi.studio.services.invocations import get_invocation

    async with StateDB(db_path) as db:
        await db.execute(
            "INSERT INTO invocations (id, skill, status, created_at, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("label-invocation", "test:skill", "completed", 10.0, 10.0, 100.0),
        )
    await _seed(db_path, invocation_id="label-invocation")
    result = await get_invocation("label-invocation")
    assert result is not None
    assert result["sessions"][0]["name"] == "Pinned label"
    async with StateDB(db_path) as db:
        await db.set_session_user_label(SID, None)
    result = await get_invocation("label-invocation")
    assert result["sessions"][0]["name"] == "Authored play"


async def test_phantom_run_diagnostic_uses_label_and_legacy_authored_fallback(db_path, monkeypatch):
    from lionagi.studio.services import admin

    async def no_process(*args, **kwargs):
        return False, ""

    monkeypatch.setattr(admin, "_resolve_process_liveness_with_snapshot", no_process)
    monkeypatch.setattr(admin, "_classify_phantom", lambda *args, **kwargs: "process_dead")
    await _seed(db_path, status="running")
    rows = await admin.list_phantom_sessions()
    assert rows[0]["playbook"] == "Pinned label"

    _drop_label_column(db_path)
    rows = await admin.list_phantom_sessions()
    assert rows[0]["playbook"] == "Authored play"
    assert "user_label" not in _columns(db_path)


async def test_show_linked_session_uses_label_and_legacy_authored_fallback(
    db_path, tmp_path, monkeypatch
):
    from lionagi.studio.services import shows

    shows_root = tmp_path / "shows"
    shows_root.mkdir()
    monkeypatch.setattr(shows, "SHOWS_ROOT", shows_root)
    await _seed(db_path)
    async with StateDB(db_path) as db:
        await db.create_show(
            {"id": "label-show", "topic": "label-show", "show_dir": str(shows_root / "label-show")}
        )
        await db.create_play(
            {
                "id": "linked-play",
                "show_id": "label-show",
                "name": "Authored step",
                "session_id": SID,
            }
        )
        await db.create_play(
            {"id": "unlinked-play", "show_id": "label-show", "name": "Unlinked step"}
        )
    result = await shows.get_show("label-show")
    assert result is not None
    plays = {play["name"]: play for play in result["plays"]}
    assert plays["Authored step"]["session_name"] == "Pinned label"
    assert plays["Unlinked step"]["session_name"] is None

    _drop_label_column(db_path)
    result = await shows.get_show("label-show")
    plays = {play["name"]: play for play in result["plays"]}
    assert plays["Authored step"]["session_name"] == "Authored play"
    assert "user_label" not in _columns(db_path)


async def test_operator_rename_replaces_label_without_changing_system_name_or_activity(db_path):
    from lionagi.studio.operator.rename_session import execute_rename_session_command
    from lionagi.studio.services.sessions import get_session

    await _seed(db_path)
    before = _stored(db_path)
    refused = await execute_rename_session_command(
        {"session_id": SID, "name": "Foreign label", "project": "another-project"}
    )
    assert refused == {"status": "not_found", "id": SID}
    assert _stored(db_path) == before
    result = await execute_rename_session_command(
        {"session_id": SID, "name": "Approved new name", "project": PROJECT}
    )
    assert result == {"status": "renamed", "id": SID, "name": "Approved new name"}
    after = _stored(db_path)
    assert after["user_label"] == "Approved new name"
    assert after["name"] == "System prompt name"
    assert {k: v for k, v in after.items() if k != "user_label"} == {
        k: v for k, v in before.items() if k != "user_label"
    }
    detail = await get_session(SID)
    assert detail["name"] == "Approved new name"


async def test_operator_rename_reports_a_session_removed_before_label_write(db_path, monkeypatch):
    from lionagi.studio.operator.rename_session import execute_rename_session_command

    await _seed(db_path)

    async def removed_before_write(self, session_id, label):
        return False

    monkeypatch.setattr(StateDB, "set_session_user_label", removed_before_write)
    result = await execute_rename_session_command(
        {"session_id": SID, "name": "Approved new name", "project": PROJECT}
    )
    assert result == {"status": "not_found", "id": SID}


async def test_cli_session_status_uses_the_displayed_label(db_path):
    from lionagi.cli.status import _build_view

    await _seed(db_path)
    async with StateDB(db_path, readonly=True) as db:
        row = await db.get_session(SID)
        view = await _build_view(db, command="agent", entity_type="session", row=row)
    assert view["label"] == "Pinned label"


def test_cli_wait_line_uses_the_displayed_label():
    from lionagi.cli.monitor import _format_session_wait_line

    row = {
        "id": SID,
        "status": "completed",
        "agent_name": "Worker",
        "name": "System prompt name",
        "user_label": "Pinned label",
    }
    line = _format_session_wait_line(row)
    assert "name=Pinned label  status=completed" in line
    assert "Worker" not in line
