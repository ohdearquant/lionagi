# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for GET /api/schedules/{schedule_id}/runs.

Covers a previously-untested route: pagination, status filtering, and
serialization of JSON columns (including a run carrying a large multi-line
traceback in error_detail, the shape a genuinely failed scheduled run has
in production).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services.schedules import create_schedule  # noqa: E402


async def _seed_schedule() -> str:
    created = await create_schedule(
        {
            "name": f"runs-route-test-{uuid.uuid4().hex[:8]}",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    return created["id"]


async def _seed_run(
    schedule_id: str,
    *,
    status: str,
    fired_at: float,
    error_detail: str | None = None,
    chain_depth: int = 0,
    run_id: str | None = None,
) -> str:
    resolved_run_id = run_id or str(uuid.uuid4())
    async with StateDB() as db:
        await db.create_schedule_run(
            {
                "id": resolved_run_id,
                "schedule_id": schedule_id,
                "trigger_context": {"source": "cron"},
                "action_kind": "agent",
                "action_args": {"prompt": "ping"},
                "status": status,
                "chain_depth": chain_depth,
                "fired_at": fired_at,
                "error_detail": error_detail,
            }
        )
    return resolved_run_id


def _patch_db(monkeypatch, db_path: Path) -> None:
    """Point both the StateDB default and the schedules service's own bound
    name at the temp path -- must run before any seeding, or seed writes
    land in the real default DB."""
    import lionagi.studio.services.schedules as schedules_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client() -> TestClient:
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_completed_and_failed_runs_serialize_with_200(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    now = time.time()

    async def seed():
        sid = await _seed_schedule()
        await _seed_run(sid, status="completed", fired_at=now - 20)
        await _seed_run(
            sid,
            status="failed",
            fired_at=now - 10,
            error_detail=(
                "Traceback (most recent call last):\n"
                '  File "engine.py", line 42, in fire\n'
                "pydantic_core.ValidationError: Provider must be specified\n"
            ),
        )
        return sid

    sid = asyncio.run(seed())
    client = _make_client()

    resp = client.get(f"/api/schedules/{sid}/runs", params={"limit": 25})

    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 25
    assert body["offset"] == 0
    assert len(body["runs"]) == 2
    assert {r["status"] for r in body["runs"]} == {"completed", "failed"}

    failed = next(r for r in body["runs"] if r["status"] == "failed")
    assert "ValidationError" in failed["error_detail"]
    assert isinstance(failed["trigger_context"], dict)
    assert isinstance(failed["action_args"], dict)


def test_unknown_schedule_id_returns_empty_200(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    client = _make_client()

    resp = client.get("/api/schedules/does-not-exist/runs", params={"limit": 25})

    assert resp.status_code == 200
    assert resp.json() == {"runs": [], "limit": 25, "offset": 0, "has_next": False}


def test_status_filter_and_pagination(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    now = time.time()

    async def seed():
        sid = await _seed_schedule()
        for i in range(3):
            await _seed_run(sid, status="completed", fired_at=now - i)
        await _seed_run(sid, status="failed", fired_at=now - 100)
        return sid

    sid = asyncio.run(seed())
    client = _make_client()

    resp = client.get(f"/api/schedules/{sid}/runs", params={"status": "failed", "limit": 25})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["status"] == "failed"

    resp = client.get(f"/api/schedules/{sid}/runs", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["runs"]) == 2
    assert body["has_next"] is True


def test_schedule_summary_batches_recent_runs_with_stable_order(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    now = time.time()

    async def seed():
        first = await _seed_schedule()
        second = await _seed_schedule()
        await _seed_run(first, status="failed", fired_at=now - 10, run_id="run-a")
        await _seed_run(first, status="completed", fired_at=now, run_id="run-b")
        await _seed_run(first, status="failed", fired_at=now, run_id="run-c")
        await _seed_run(second, status="completed", fired_at=now, run_id="run-d")
        return first, second

    first, second = asyncio.run(seed())
    response = _make_client().get("/api/schedules/summary", params={"recent_runs_limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["summary_version"] == 1
    assert body["recent_runs_limit"] == 2
    assert {schedule["id"] for schedule in body["schedules"]} == {first, second}
    assert body["run_summaries"][first]["state"] == "ok"
    first_runs = body["run_summaries"][first]["runs"]
    assert [run["id"] for run in first_runs] == ["run-c", "run-b"]
    assert [run["fired_at"] for run in first_runs] == pytest.approx([now, now])
    assert body["run_summaries"][second]["state"] == "ok"
    assert [run["id"] for run in body["run_summaries"][second]["runs"]] == ["run-d"]


def test_schedule_summary_marks_each_slice_failed_when_batch_read_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    async def seed():
        return await _seed_schedule(), await _seed_schedule()

    first, second = asyncio.run(seed())

    async def fail_batch(*args, **kwargs):
        raise RuntimeError("summary read failed")

    monkeypatch.setattr(StateDB, "list_schedule_runs_batch", fail_batch, raising=False)

    response = _make_client().get("/api/schedules/summary")

    assert response.status_code == 200
    summaries = response.json()["run_summaries"]
    assert summaries[first] == {"state": "error", "runs": []}
    assert summaries[second] == {"state": "error", "runs": []}


# Every surface below serves the same declared run shape, so they are checked together:
# a projection applied to one emitter and not its siblings is the defect it was meant to fix.
_DECLARED_RUN_FIELDS = {
    "id",
    "schedule_id",
    "invocation_id",
    "trigger_context",
    "action_kind",
    "status",
    "exit_code",
    "chain_depth",
    "fired_at",
    "ended_at",
    "error_detail",
}
# Written by the seeder above and never part of the declared shape.
_OPERATIONAL_COLUMN = "action_args"


def _seeded_schedule_with_one_run(monkeypatch, db_path: Path) -> str:
    _patch_db(monkeypatch, db_path)

    async def seed():
        schedule_id = await _seed_schedule()
        await _seed_run(schedule_id, status="completed", fired_at=time.time())
        return schedule_id

    return asyncio.run(seed())


def test_the_summary_slice_serves_only_the_declared_run_fields(tmp_path, monkeypatch):
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get("/api/schedules/summary").json()
    runs = body["run_summaries"][schedule_id]["runs"]

    assert len(runs) == 1
    assert set(runs[0]) <= _DECLARED_RUN_FIELDS
    assert _OPERATIONAL_COLUMN not in runs[0]
    assert runs[0]["status"] == "completed"


def test_the_seeded_run_really_carries_the_operational_column(tmp_path, monkeypatch):
    """The control. Without this the three assertions above pass on an empty column set."""
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    async def read():
        async with StateDB() as db:
            return await db.list_schedule_runs(schedule_id, limit=10)

    stored = asyncio.run(read())

    assert len(stored) == 1
    assert _OPERATIONAL_COLUMN in stored[0]
    assert _DECLARED_RUN_FIELDS <= set(stored[0])


def test_a_column_added_to_schedule_runs_is_not_served_until_it_is_named(tmp_path, monkeypatch):
    """The projection is an allow-list, so a column added later is private by default.

    A deny-list naming today's operational columns would pass every test above and serve
    this one, which is the failure mode the allow-list exists to prevent.
    """
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")
    real_batch = StateDB.list_schedule_runs_batch

    async def batch_with_a_new_column(self, ids, **kwargs):
        grouped = await real_batch(self, ids, **kwargs)
        for rows in grouped.values():
            for row in rows:
                row["a_column_nobody_has_declared"] = "secret-value"
        return grouped

    monkeypatch.setattr(StateDB, "list_schedule_runs_batch", batch_with_a_new_column)

    body = _make_client().get("/api/schedules/summary").json()
    runs = body["run_summaries"][schedule_id]["runs"]

    assert len(runs) == 1
    assert "a_column_nobody_has_declared" not in runs[0]
    assert "secret-value" not in json.dumps(body)
    assert runs[0]["status"] == "completed"
