# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Session user-label write contract for Studio (GitHub #3126)."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _patch_db(monkeypatch, db_path)
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


async def _seed_session(
    db_path: Path,
    session_id: str,
    *,
    name: str = "Original run",
    status: str = "completed",
    updated_at: float = 100.0,
    last_message_at: float | None = 90.0,
) -> None:
    async with StateDB(db_path) as db:
        progression_id = str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression_id,
                "name": name,
                "status": status,
                "created_at": updated_at - 10,
                "updated_at": updated_at,
                "started_at": updated_at - 10,
                "last_message_at": last_message_at,
            }
        )


def test_put_sets_and_serves_the_user_label(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id))
    client = _make_client(db_path, monkeypatch)

    response = client.put(
        f"/api/sessions/{session_id}",
        json={"label": "My Renamed Run"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "user_label": "My Renamed Run",
        "display_name": "My Renamed Run",
    }
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["user_label"] == "My Renamed Run"
    assert detail["display_name"] == "My Renamed Run"
    # Backward-compatible `name` remains the resolved name served to existing clients.
    assert detail["name"] == "My Renamed Run"


def test_label_edit_does_not_change_activity_or_reorder_sessions(tmp_path, monkeypatch):
    """Renaming is presentation, never activity or a staleness-clock reset."""
    db_path = tmp_path / "state.db"
    older = str(uuid.uuid4())
    newer = str(uuid.uuid4())
    _run(
        _seed_session(
            db_path,
            older,
            status="running",
            updated_at=100.0,
            last_message_at=95.0,
        )
    )
    _run(_seed_session(db_path, newer, updated_at=200.0, last_message_at=190.0))
    client = _make_client(db_path, monkeypatch)

    before_detail = client.get(f"/api/sessions/{older}").json()
    before_order = [row["id"] for row in client.get("/api/sessions/").json()["sessions"]]
    assert before_order == [newer, older]

    response = client.put(f"/api/sessions/{older}", json={"label": "Renamed, not active"})
    assert response.status_code == 200

    after_detail = client.get(f"/api/sessions/{older}").json()
    after_order = [row["id"] for row in client.get("/api/sessions/").json()["sessions"]]
    assert after_detail["updated_at"] == before_detail["updated_at"] == 100.0
    assert after_detail["last_message_at"] == before_detail["last_message_at"] == 95.0
    assert after_order == before_order


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("  Padded Name  ", "Padded Name"),
        (" " + "x" * 160 + " ", "x" * 160),
    ],
    ids=["trimmed", "length-cap-after-trim"],
)
def test_label_is_trimmed_and_accepts_the_160_character_cap(tmp_path, monkeypatch, raw, stored):
    db_path = tmp_path / "state.db"
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id))
    client = _make_client(db_path, monkeypatch)

    response = client.put(f"/api/sessions/{session_id}", json={"label": raw})

    assert response.status_code == 200
    assert response.json()["user_label"] == stored


@pytest.mark.parametrize("raw", ["", "   ", "\t\n  \r"], ids=["empty", "spaces", "controls"])
def test_whitespace_only_label_clears_and_resolves_the_fallback(tmp_path, monkeypatch, raw):
    db_path = tmp_path / "state.db"
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id))
    client = _make_client(db_path, monkeypatch)
    assert client.put(f"/api/sessions/{session_id}", json={"label": "Temporary"}).status_code == 200

    response = client.put(f"/api/sessions/{session_id}", json={"label": raw})

    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "user_label": None,
        "display_name": "Original run",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "bad\nlabel",
        "bad\rlabel",
        "bad\tlabel",
        "bad\x00label",
        "bad\x7flabel",
        "bad\x1blabel",
    ],
    ids=["newline", "carriage-return", "tab", "nul", "del", "escape"],
)
def test_label_rejects_embedded_control_characters_without_writing(tmp_path, monkeypatch, raw):
    db_path = tmp_path / "state.db"
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id))
    client = _make_client(db_path, monkeypatch)

    response = client.put(f"/api/sessions/{session_id}", json={"label": raw})

    assert response.status_code == 422
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["user_label"] is None


def test_label_rejects_more_than_160_characters_without_truncating(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id))
    client = _make_client(db_path, monkeypatch)

    response = client.put(f"/api/sessions/{session_id}", json={"label": "x" * 161})

    assert response.status_code == 422
    assert client.get(f"/api/sessions/{session_id}").json()["user_label"] is None


def test_unknown_session_404s_before_the_label_writer(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    known_id = str(uuid.uuid4())
    unknown_id = str(uuid.uuid4())
    _run(_seed_session(db_path, known_id))

    import lionagi.studio.services.sessions as sessions_service

    called = False

    async def forbidden_writer(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unknown session reached the write path")

    monkeypatch.setattr(
        sessions_service,
        "write_session_user_label",
        forbidden_writer,
        raising=False,
    )
    client = _make_client(db_path, monkeypatch)

    response = client.put(f"/api/sessions/{unknown_id}", json={"label": "Never written"})

    assert response.status_code == 404
    assert response.json()["detail"] == f"Session '{unknown_id}' not found"
    assert called is False


def test_put_requires_json_content_type_and_preserves_bearer_auth(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id))
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "label-test-token")
    client = _make_client(db_path, monkeypatch)

    unauthorized = client.put(
        f"/api/sessions/{session_id}",
        json={"label": "Denied"},
    )
    wrong_media = client.put(
        f"/api/sessions/{session_id}",
        content='{"label":"Denied"}',
        headers={
            "Authorization": "Bearer label-test-token",
            "Content-Type": "text/plain",
        },
    )
    accepted = client.put(
        f"/api/sessions/{session_id}",
        json={"label": "Authorized"},
        headers={"Authorization": "Bearer label-test-token"},
    )

    assert unauthorized.status_code == 401
    assert wrong_media.status_code == 415
    assert accepted.status_code == 200
    assert accepted.json()["user_label"] == "Authorized"


def test_list_and_detail_share_the_same_resolved_name(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    session_id = str(uuid.uuid4())
    _run(_seed_session(db_path, session_id))
    client = _make_client(db_path, monkeypatch)
    assert (
        client.put(f"/api/sessions/{session_id}", json={"label": "Shared label"}).status_code == 200
    )

    summary = client.get("/api/sessions/").json()["sessions"][0]
    detail = client.get(f"/api/sessions/{session_id}").json()

    assert summary["user_label"] == detail["user_label"] == "Shared label"
    assert summary["display_name"] == detail["display_name"] == "Shared label"
    assert summary["name"] == detail["name"] == "Shared label"
