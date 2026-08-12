from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_snapshot_rows(db_path: Path) -> None:
    now = time.time()
    async with StateDB(db_path) as db:
        for index in range(5):
            invocation_id = f"active-inv-{index}"
            await db.create_invocation(
                {
                    "id": invocation_id,
                    "skill": f"skill-{index}",
                    "status": "running",
                    "started_at": now - 100 + index,
                }
            )
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": f"active-run-{index}",
                    "progression_id": progression_id,
                    "name": f"active {index}",
                    "status": "running",
                    "started_at": now - 100 + index,
                    "last_message_at": now,
                    "invocation_id": invocation_id,
                    "project": "org/alpha" if index < 3 else "org/beta",
                }
            )

        for index in range(3):
            invocation_id = f"terminal-inv-{index}"
            await db.create_invocation(
                {
                    "id": invocation_id,
                    "skill": f"finished-skill-{index}",
                    "status": "completed",
                    "started_at": now - 300 - index,
                    "ended_at": now - index,
                }
            )
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": f"terminal-run-{index}",
                    "progression_id": progression_id,
                    "name": f"terminal {index}",
                    "status": "completed",
                    "started_at": now - 300 - index,
                    "ended_at": now - index,
                    "project": "org/alpha",
                }
            )


def _client(tmp_path, monkeypatch) -> TestClient:
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    _run(_seed_snapshot_rows(db_path))

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_active_snapshot_is_bounded_and_discloses_exact_omissions(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    response = client.get(
        "/api/active-snapshot",
        params={"run_limit": 2, "invocation_limit": 3, "recent_limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_version"]
    assert isinstance(payload["snapshot_at"], float)
    assert payload["active_run_total"] == 5
    assert payload["active_run_omitted"] == 3
    assert len(payload["active_runs"]) == 2
    assert {row["status"] for row in payload["active_runs"]} == {"running"}
    assert payload["active_invocation_total"] == 5
    assert payload["active_invocation_omitted"] == 2
    assert len(payload["active_invocations"]) == 3
    assert {row["status"] for row in payload["active_invocations"]} == {"running"}
    assert len(payload["recent_runs"]) == 2
    assert payload["recent_run_has_more"] is True
    assert len(payload["recent_invocations"]) == 2
    assert payload["recent_invocation_has_more"] is True
    assert payload["complete"] is False


def test_active_snapshot_scopes_invocations_and_totals_with_runs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    response = client.get(
        "/api/active-snapshot",
        params={"project": "org/alpha", "run_limit": 10, "invocation_limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_run_total"] == 3
    assert payload["active_invocation_total"] == 3
    assert payload["active_run_omitted"] == 0
    assert payload["active_invocation_omitted"] == 0
    assert {row["project"] for row in payload["active_runs"]} == {"org/alpha"}
    assert {row["id"] for row in payload["active_invocations"]} == {
        "active-inv-0",
        "active-inv-1",
        "active-inv-2",
    }
    assert payload["complete"] is True


def test_active_snapshot_rejects_unbounded_limits(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/active-snapshot", params={"run_limit": 501})

    assert response.status_code == 422
