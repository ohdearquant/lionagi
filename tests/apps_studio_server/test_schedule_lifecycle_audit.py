# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule lifecycle provenance contracts (#3134)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB  # noqa: E402


def _patch_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _client() -> TestClient:
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def _spec(name: str) -> dict[str, object]:
    return {
        "name": name,
        "trigger_type": "interval",
        "interval_sec": 300,
        "action_kind": "agent",
        "action_prompt": "check the fleet",
    }


def test_disable_requires_reason_and_detail_preserves_disable_after_reenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()
    identity = {
        "x-lionagi-actor": "operator:alice",
        "x-lionagi-cwd": "/projects/monitoring",
    }

    created = client.post("/api/schedules/", json=_spec("audit-one"), headers=identity)
    assert created.status_code == 201
    schedule_id = created.json()["id"]

    missing_reason = client.post(f"/api/schedules/{schedule_id}/disable", headers=identity)
    assert missing_reason.status_code == 400
    assert "reason" in missing_reason.json()["detail"].lower()

    disabled = client.post(
        f"/api/schedules/{schedule_id}/disable",
        json={"reason": "Pause while rotating the provider credential"},
        headers=identity,
    )
    assert disabled.status_code == 200

    enabled = client.post(f"/api/schedules/{schedule_id}/enable", headers=identity)
    assert enabled.status_code == 200

    detail = client.get(f"/api/schedules/{schedule_id}").json()
    history = detail["lifecycle_history"]
    assert [row["status"] for row in history[:3]] == ["enabled", "disabled", "enabled"]
    disable_row = history[1]
    assert disable_row["actor"] == "operator:alice"
    assert disable_row["reason_summary"] == "Pause while rotating the provider credential"
    assert disable_row["metadata"]["request_cwd"] == "/projects/monitoring"
    assert disable_row["created_at"] > 0
    assert detail["last_lifecycle_change"] == history[0]


def test_sequential_disable_sweep_writes_one_attributed_row_per_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()
    schedule_ids = [
        client.post("/api/schedules/", json=_spec(f"sweep-{i}")).json()["id"] for i in range(3)
    ]

    for schedule_id in schedule_ids:
        response = client.post(
            f"/api/schedules/{schedule_id}/disable",
            json={"reason": "Incident sweep: stop noisy monitors"},
            headers={
                "x-lionagi-actor": "automation:safety-sweep",
                "x-lionagi-cwd": "/srv/sweep-worker",
            },
        )
        assert response.status_code == 200

    async def _rows() -> list[dict]:
        async with StateDB(readonly=True) as db:
            placeholders = ",".join(f":id{i}" for i in range(len(schedule_ids)))
            return await db.fetch_all(
                "SELECT id, entity_id, status, actor, reason_summary, created_at, metadata "
                "FROM status_transitions WHERE entity_type = 'schedule' "
                f"AND entity_id IN ({placeholders}) AND status = 'disabled' "
                "ORDER BY created_at, id",
                {f"id{i}": value for i, value in enumerate(schedule_ids)},
            )

    import asyncio

    rows = asyncio.run(_rows())
    assert [row["entity_id"] for row in rows] == schedule_ids
    assert len({row["id"] for row in rows}) == 3
    assert all(row["actor"] == "automation:safety-sweep" for row in rows)
    assert all(row["reason_summary"] == "Incident sweep: stop noisy monitors" for row in rows)
    assert all(json.loads(row["metadata"])["request_cwd"] == "/srv/sweep-worker" for row in rows)
    assert [row["created_at"] for row in rows] == sorted(row["created_at"] for row in rows)


async def test_legacy_schedule_has_empty_history_without_a_fabricated_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    schedule_id = uuid.uuid4().hex[:12]
    async with StateDB() as db:
        stmt, params = db._build_schedule_insert_stmt(  # noqa: SLF001 - migration-shape fixture
            {"id": schedule_id, **_spec("legacy-no-audit")}
        )
        async with db.transaction() as conn:
            await conn.execute(stmt, params)

    from lionagi.studio.services.schedules import get_schedule

    detail = await get_schedule(schedule_id)
    assert detail is not None
    assert detail["lifecycle_history"] == []
    assert detail["last_lifecycle_change"] is None


def test_delete_appends_a_durable_tombstone_before_removing_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _client()
    schedule_id = client.post("/api/schedules/", json=_spec("delete-audit")).json()["id"]

    deleted = client.delete(
        f"/api/schedules/{schedule_id}", headers={"x-lionagi-actor": "operator:bob"}
    )
    assert deleted.status_code == 200
    assert client.get(f"/api/schedules/{schedule_id}").status_code == 404

    async def _row() -> dict | None:
        async with StateDB(readonly=True) as db:
            return await db.fetch_one(
                "SELECT status, actor, reason_code, created_at FROM status_transitions "
                "WHERE entity_type = 'schedule' AND entity_id = :id "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                {"id": schedule_id},
            )

    import asyncio

    tombstone = asyncio.run(_row())
    assert tombstone is not None
    assert tombstone["status"] == "deleted"
    assert tombstone["actor"] == "operator:bob"
    assert tombstone["reason_code"] == "schedule.deleted.request"
