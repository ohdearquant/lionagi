# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Explicit session labels round-trip without becoming session activity."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from lionagi.state.db import StateDB


@pytest_asyncio.fixture
async def label_client(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("fastapi")
    import lionagi.state.db as state_db_mod
    from lionagi.studio.app import create_app

    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
    )
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    transport = httpx.ASGITransport(app=create_app(), client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        yield path, client


async def _seed_session(path: Path, session_id: str = "labelled", **fields: Any) -> None:
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
                "created_at": 10.0,
                "updated_at": 100.0,
                "started_at": 20.0,
                "last_message_at": 90.0,
                "status": "running",
                "node_metadata": {"existing": {"nested": [1, 2]}, "name": "metadata name"},
                **fields,
            }
        )


def _stored_row(path: Path, session_id: str = "labelled") -> dict[str, Any]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    assert row is not None
    return dict(row)


def _without_label(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "user_label"}


def _remove_label_column(path: Path) -> None:
    """Leave the same old schema when run against either side of the change."""
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "user_label" in columns:
            conn.execute("ALTER TABLE sessions DROP COLUMN user_label")


async def test_http_label_wins_on_reads_without_changing_activity_or_order(label_client):
    path, client = label_client
    await _seed_session(path)
    await _seed_session(path, "newer", updated_at=200.0)
    before = _stored_row(path)
    listed_before = (await client.get("/api/sessions/")).json()["sessions"]
    assert [row["id"] for row in listed_before] == ["newer", "labelled"]

    written = await client.patch("/api/sessions/labelled", json={"user_label": "  My label  "})
    assert written.status_code == 200
    assert written.json()["name"] == "My label"
    assert written.json()["user_label"] == "My label"
    assert _stored_row(path)["user_label"] == "My label"
    assert _without_label(_stored_row(path)) == _without_label(before)

    detail = await client.get("/api/sessions/labelled")
    assert detail.status_code == 200
    assert detail.json()["name"] == "My label"
    assert detail.json()["user_label"] == "My label"
    listed_after = (await client.get("/api/sessions/")).json()["sessions"]
    assert [row["id"] for row in listed_after] == ["newer", "labelled"]
    assert listed_after[1]["name"] == "My label"
    assert listed_after[1]["user_label"] == "My label"


@pytest.mark.parametrize("clear", [None, "", " \t\n "])
async def test_http_clear_restores_next_tier_and_omission_preserves_label(label_client, clear):
    path, client = label_client
    await _seed_session(path)
    before = _stored_row(path)
    written = await client.patch("/api/sessions/labelled", json={"user_label": "Chosen"})
    assert written.status_code == 200

    omitted = await client.patch("/api/sessions/labelled", json={})
    assert omitted.status_code == 200
    assert omitted.json()["name"] == "Chosen"
    assert _stored_row(path)["user_label"] == "Chosen"

    cleared = await client.patch("/api/sessions/labelled", json={"user_label": clear})
    assert cleared.status_code == 200
    assert cleared.json()["name"] == "Authored play"
    assert cleared.json()["user_label"] is None
    assert _stored_row(path)["user_label"] is None
    assert _without_label(_stored_row(path)) == _without_label(before)
    detail = (await client.get("/api/sessions/labelled")).json()
    listing = (await client.get("/api/sessions/")).json()["sessions"]
    assert detail["name"] == listing[0]["name"] == "Authored play"
    assert detail["user_label"] is listing[0]["user_label"] is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("  " + "x" * 160 + "  ", "x" * 160),
        (" " * 200 + "short" + " " * 200, "short"),
        ("  工作 café\u00a0notes  ", "工作 café\u00a0notes"),
    ],
)
async def test_http_label_strips_before_length_check_and_accepts_printable_text(
    label_client, label, expected
):
    path, client = label_client
    await _seed_session(path)
    response = await client.patch("/api/sessions/labelled", json={"user_label": label})
    assert response.status_code == 200
    assert response.json()["name"] == expected
    assert _stored_row(path)["user_label"] == expected


@pytest.mark.parametrize(
    "label",
    ["x" * 161, "a\x00b", "a\x7fb", "a\u0085b", "a\u202eb", "a\u2028b", "a\u2029b", 123],
)
async def test_http_invalid_label_is_rejected_without_changing_the_row(label_client, label):
    path, client = label_client
    await _seed_session(path)
    before = _stored_row(path)
    response = await client.patch("/api/sessions/labelled", json={"user_label": label})
    assert response.status_code == 422
    assert _stored_row(path) == before


async def test_http_missing_session_matches_get_and_does_not_create_store(label_client):
    path, client = label_client
    assert not path.exists()
    read = await client.get("/api/sessions/missing")
    written = await client.patch("/api/sessions/missing", json={"user_label": "No row"})
    assert written.status_code == read.status_code == 404
    assert written.json() == read.json()
    assert not path.exists()


async def test_http_missing_session_does_not_migrate_an_existing_store(label_client):
    path, client = label_client
    await _seed_session(path)
    _remove_label_column(path)
    before = _stored_row(path)
    read = await client.get("/api/sessions/missing")
    written = await client.patch("/api/sessions/missing", json={"user_label": "No row"})
    assert written.status_code == read.status_code == 404
    assert written.json() == read.json()
    assert _stored_row(path) == before


async def test_legacy_store_reads_without_migration_then_accepts_a_label_write(label_client):
    from lionagi.cli.state import _collect_sessions

    path, client = label_client
    await _seed_session(path)
    _remove_label_column(path)
    before = _stored_row(path)

    detail = await client.get("/api/sessions/labelled")
    listed = await client.get("/api/sessions/")
    assert detail.status_code == listed.status_code == 200
    assert detail.json()["user_label"] is None
    assert listed.json()["sessions"][0]["user_label"] is None
    async with StateDB(path, readonly=True) as db:
        rows = await _collect_sessions(db, limit=10, status=None)
    assert rows[0]["name"] == "Authored play"
    assert _stored_row(path) == before

    written = await client.patch("/api/sessions/labelled", json={"user_label": "Migrated"})
    assert written.status_code == 200
    assert written.json()["name"] == "Migrated"
    assert _stored_row(path)["user_label"] == "Migrated"
    assert _without_label(_stored_row(path)) == before


async def test_state_open_adds_nullable_label_and_preserves_existing_writes(tmp_path):
    path = tmp_path / "state.db"
    await _seed_session(path)
    _remove_label_column(path)
    before = _stored_row(path)
    async with StateDB(path) as db:
        row = await db.get_session("labelled")
        assert row is not None
        assert "user_label" in row
        assert row["user_label"] is None
        assert _without_label(_stored_row(path)) == before
        assert await db.set_session_user_label("labelled", "  Stored label  ")
        await db.update_session(
            "labelled", name="New system prompt", node_metadata=json.dumps({"new": True})
        )
        row = await db.get_session("labelled")
        assert row["user_label"] == "Stored label"
        assert row["name"] == "New system prompt"
        assert row["node_metadata"] == {"new": True}
        assert await db.set_session_user_label("labelled", None)
        assert (await db.get_session("labelled"))["user_label"] is None

    async with StateDB(path) as db:
        assert (await db.get_session("labelled"))["user_label"] is None
    with sqlite3.connect(path) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(sessions)")}
        assert columns["user_label"][2] == "TEXT"
        assert columns["user_label"][3] == 0  # nullable


async def test_old_session_check_rebuild_keeps_the_new_label_column(tmp_path):
    from lionagi.state.db import _SCHEMA_PATH

    path = tmp_path / "old-check.db"
    # This store predates both explicit labels and the engine invocation kind.
    # Reconciliation adds the column before the legacy CHECK table is rebuilt.
    schema = "\n".join(
        line
        for line in _SCHEMA_PATH.read_text().splitlines()
        if not line.strip().startswith("user_label ")
    ).replace(
        "('agent', 'play', 'flow', 'fanout', 'show-play', 'engine')",
        "('agent', 'play', 'flow', 'fanout', 'show-play')",
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO progressions (id, created_at) VALUES ('old-progression', 1.0)")
        conn.execute(
            "INSERT INTO sessions "
            "(id, name, progression_id, created_at, updated_at, last_message_at, "
            "status, invocation_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("labelled", "Legacy name", "old-progression", 1.0, 50.0, 40.0, "running", "show-play"),
        )
    before = _stored_row(path)

    async with StateDB(path) as db:
        row = await db.get_session("labelled")
        assert row is not None
        assert "user_label" in row
        assert row["user_label"] is None
        assert _without_label(_stored_row(path)) == before
        assert await db.set_session_user_label("labelled", "After rebuild")
        assert (await db.get_session("labelled"))["user_label"] == "After rebuild"
    assert _without_label(_stored_row(path)) == before


async def test_persisted_label_reaches_cli_and_admin_display_names(label_client, monkeypatch):
    from lionagi.cli.state import _collect_sessions
    from lionagi.studio.services import admin

    async def no_live_process(_session, _artifacts):
        return False

    async def no_code_identity():
        return {}

    monkeypatch.setattr(admin, "resolve_process_liveness", no_live_process)
    monkeypatch.setattr(admin, "_code_identity_report", no_code_identity)
    path, client = label_client
    await _seed_session(path)
    response = await client.patch("/api/sessions/labelled", json={"user_label": "Visible here"})
    assert response.status_code == 200

    async with StateDB(path, readonly=True) as db:
        rows = await _collect_sessions(db, limit=10, status=None)
    assert rows[0]["name"] == "Visible here"
    health = await client.get("/api/admin/health")
    assert health.status_code == 200
    unhealthy = health.json()["sessions"]["unhealthy"]
    assert unhealthy[0]["session_id"] == "labelled"
    assert unhealthy[0]["name"] == "Visible here"
