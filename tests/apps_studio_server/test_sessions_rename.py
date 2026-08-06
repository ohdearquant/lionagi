# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Studio session rename surface: PUT /api/sessions/{id}, the
user_label column, and resolve_session_display_name()."""

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


async def _seed_session(db_path: Path, session_id: str, **fields) -> None:
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        payload = {
            "id": session_id,
            "progression_id": pid,
            "status": fields.pop("status", "completed"),
            "started_at": fields.pop("started_at", time.time()),
            **fields,
        }
        await db.create_session(payload)


# ── PUT /api/sessions/{id} — set ──────────────────────────────────────────


def test_rename_route_sets_label(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid, name="agent"))
    client = _make_client(db_path, monkeypatch)

    r = client.put(f"/api/sessions/{sid}", json={"label": "My Renamed Run"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "session_id": sid,
        "user_label": "My Renamed Run",
        "display_name": "My Renamed Run",
    }

    # Persisted, not just echoed back.
    detail = client.get(f"/api/sessions/{sid}")
    assert detail.json()["user_label"] == "My Renamed Run"
    assert detail.json()["display_name"] == "My Renamed Run"


def test_rename_route_does_not_bump_updated_at(tmp_path, monkeypatch):
    """A label edit is not activity: it must not reorder session lists
    (sorted by updated_at DESC) or reset a running session's staleness
    clock -- both of which StateDB.update_session()'s unconditional
    updated_at bump would cause if the route used it for this write."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid, updated_at=100.0))
    client = _make_client(db_path, monkeypatch)

    before = client.get(f"/api/sessions/{sid}").json()["updated_at"]
    assert before == 100.0

    r = client.put(f"/api/sessions/{sid}", json={"label": "Renamed"})
    assert r.status_code == 200

    after = client.get(f"/api/sessions/{sid}").json()["updated_at"]
    assert after == before == 100.0


def test_rename_route_trims_whitespace(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.put(f"/api/sessions/{sid}", json={"label": "  Padded Name  "})
    assert r.status_code == 200
    assert r.json()["user_label"] == "Padded Name"


def test_rename_route_at_length_cap_is_accepted(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    label = "x" * 160
    r = client.put(f"/api/sessions/{sid}", json={"label": label})
    assert r.status_code == 200
    assert r.json()["user_label"] == label


# ── PUT /api/sessions/{id} — clear ────────────────────────────────────────


def test_rename_route_clears_label_on_whitespace_only_input(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid, name="agent"))
    client = _make_client(db_path, monkeypatch)

    set_r = client.put(f"/api/sessions/{sid}", json={"label": "Temporary"})
    assert set_r.status_code == 200

    clear_r = client.put(f"/api/sessions/{sid}", json={"label": "   "})
    assert clear_r.status_code == 200
    assert clear_r.json() == {
        "session_id": sid,
        "user_label": None,
        # Falls back to the existing chain (system-written name), not an error.
        "display_name": "agent",
    }

    detail = client.get(f"/api/sessions/{sid}")
    assert detail.json()["user_label"] is None
    assert detail.json()["display_name"] == "agent"


def test_rename_route_clears_label_on_empty_string(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    client.put(f"/api/sessions/{sid}", json={"label": "Temporary"})
    r = client.put(f"/api/sessions/{sid}", json={"label": ""})
    assert r.status_code == 200
    assert r.json()["user_label"] is None


# ── PUT /api/sessions/{id} — over-length ──────────────────────────────────


def test_rename_route_rejects_over_length_label(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.put(f"/api/sessions/{sid}", json={"label": "x" * 161})
    assert r.status_code == 422

    # Rejected, not silently truncated and applied.
    detail = client.get(f"/api/sessions/{sid}")
    assert detail.json()["user_label"] is None


def test_rename_route_over_length_counts_after_trim(tmp_path, monkeypatch):
    """161 non-whitespace chars plus padding must still be judged on the
    trimmed length (160 chars), not the raw (162 char) input."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.put(f"/api/sessions/{sid}", json={"label": " " + "x" * 160 + " "})
    assert r.status_code == 200
    assert r.json()["user_label"] == "x" * 160


# ── PUT /api/sessions/{id} — control characters ───────────────────────────


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
def test_rename_route_rejects_control_characters(tmp_path, monkeypatch, raw):
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.put(f"/api/sessions/{sid}", json={"label": raw})
    assert r.status_code == 422

    detail = client.get(f"/api/sessions/{sid}")
    assert detail.json()["user_label"] is None


def test_rename_route_whitespace_only_tab_input_clears_not_rejects(tmp_path, monkeypatch):
    """A label that is ONLY whitespace (including tabs/newlines) strips to
    empty before the control-character check runs, so it clears rather than
    422s -- str.strip() removes leading/trailing tabs and newlines too."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_session(db_path, sid))
    client = _make_client(db_path, monkeypatch)

    r = client.put(f"/api/sessions/{sid}", json={"label": "\t\n  \r"})
    assert r.status_code == 200
    assert r.json()["user_label"] is None


# ── PUT /api/sessions/{id} — unknown session ──────────────────────────────


async def _init_db(db_path: Path) -> None:
    async with StateDB(db_path):
        pass  # opens + applies schema, no sessions


def test_rename_route_404_for_unknown_session(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_init_db(db_path))
    client = _make_client(db_path, monkeypatch)

    unknown_id = str(uuid.uuid4())
    r = client.put(f"/api/sessions/{unknown_id}", json={"label": "x"})
    assert r.status_code == 404
    assert r.json()["detail"] == f"Session '{unknown_id}' not found"


def test_rename_route_404_on_fresh_install_with_no_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    assert not db_path.exists()

    unknown_id = str(uuid.uuid4())
    r = client.put(f"/api/sessions/{unknown_id}", json={"label": "x"})
    assert r.status_code == 404


# ── resolve_session_display_name() — direct unit coverage ────────────────


def test_resolve_display_name_priority_chain():
    from lionagi.studio.services.sessions import resolve_session_display_name

    assert (
        resolve_session_display_name(
            {
                "id": "abcdefgh1234",
                "user_label": "Renamed",
                "name": "agent",
                "agent_name": "worker",
                "playbook_name": "pb",
            }
        )
        == "Renamed"
    )
    assert (
        resolve_session_display_name(
            {
                "id": "abcdefgh1234",
                "user_label": None,
                "name": "agent",
                "agent_name": "worker",
                "playbook_name": "pb",
            }
        )
        == "agent"
    )
    assert (
        resolve_session_display_name(
            {
                "id": "abcdefgh1234",
                "user_label": "   ",
                "name": None,
                "agent_name": "worker",
                "playbook_name": "pb",
            }
        )
        == "worker"
    )
    assert (
        resolve_session_display_name(
            {
                "id": "abcdefgh1234",
                "user_label": None,
                "name": None,
                "agent_name": None,
                "playbook_name": "pb",
            }
        )
        == "pb"
    )
    assert (
        resolve_session_display_name(
            {
                "id": "abcdefgh1234",
                "user_label": None,
                "name": None,
                "agent_name": None,
                "playbook_name": None,
            }
        )
        == "abcdefgh"
    )


def test_resolve_display_name_missing_keys_fall_through_to_id():
    """A caller that only carries a subset of the fields (e.g. a narrow SELECT)
    must not KeyError -- absent keys are treated the same as blank ones."""
    from lionagi.studio.services.sessions import resolve_session_display_name

    assert resolve_session_display_name({"id": "onlyidxxxx"}) == "onlyidxx"
