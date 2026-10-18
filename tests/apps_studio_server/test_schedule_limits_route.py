# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for GET /api/schedules/limits.

Covers the endpoint's response shape and the FastAPI route-shadowing hazard:
a literal "/schedules/limits" path must resolve to this handler, not be
captured by the "/schedules/{schedule_id}" param route.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod


def _patch_db(monkeypatch, db_path: Path) -> None:
    import lionagi.studio.services.schedules as schedules_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client() -> TestClient:
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_limits_route_returns_cap_and_inflight(tmp_path, monkeypatch):
    import lionagi.studio.config as studio_config
    from lionagi.studio.scheduler.engine import scheduler

    _patch_db(monkeypatch, tmp_path / "state.db")
    monkeypatch.setattr(studio_config, "MAX_SCHEDULED_CONCURRENT", 7)
    monkeypatch.setattr(scheduler, "_global_inflight", 3)
    monkeypatch.setattr(studio_config, "MAX_ADHOC_CONCURRENT", 5)
    monkeypatch.setattr(scheduler, "_adhoc_inflight", 2)

    client = _make_client()
    resp = client.get("/api/schedules/limits")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "max_scheduled_concurrent": 7,
        "current_inflight": 3,
        "current_holders": [],
        "max_adhoc_concurrent": 5,
        "current_adhoc_inflight": 2,
    }


def test_limits_route_resolves_before_schedule_id_param_route(tmp_path, monkeypatch):
    """The literal /schedules/limits path must not be captured by the
    /schedules/{schedule_id} GET route -- it must resolve to the limits
    handler (200 with the limits shape), not a 404 lookup for a schedule
    literally named "limits"."""
    _patch_db(monkeypatch, tmp_path / "state.db")

    client = _make_client()
    resp = client.get("/api/schedules/limits")

    assert resp.status_code == 200
    body = resp.json()
    assert "max_scheduled_concurrent" in body
    assert "current_inflight" in body
    # The param route's 404 body shape names the missing schedule id; the
    # limits route's body never does, so this also rules out captured routing.
    assert "not found" not in str(body).lower()


def test_limits_lists_poll_and_scheduled_holders_and_drops_released_claims(tmp_path, monkeypatch):
    import lionagi.studio.config as config
    import lionagi.studio.scheduler.engine as engine_mod

    _patch_db(monkeypatch, tmp_path / "state.db")
    monkeypatch.setattr(config, "MAX_SCHEDULED_CONCURRENT", 2)
    engine = engine_mod.SchedulerEngine(svc=AsyncMock())
    monkeypatch.setattr(engine_mod, "scheduler", engine)

    async def reserve():
        _, poll = await engine._reserve_global_slot("poll-schedule", path="github_event")
        _, scheduled = await engine._reserve_global_slot("timed-schedule", path="scheduled")
        return poll, scheduled

    poll, scheduled = asyncio.run(reserve())
    client = _make_client()
    try:
        data = client.get("/api/schedules/limits").json()
        assert data["current_inflight"] == 2
        assert data["current_holders"] == [
            {
                "schedule_id": "poll-schedule",
                "path": "github_event",
                "acquired_at": poll.acquired_at,
            },
            {
                "schedule_id": "timed-schedule",
                "path": "scheduled",
                "acquired_at": scheduled.acquired_at,
            },
        ]
        poll.release()
        data = client.get("/api/schedules/limits").json()
        assert data["current_inflight"] == 1
        assert [holder["schedule_id"] for holder in data["current_holders"]] == ["timed-schedule"]
        scheduled.release()
        data = client.get("/api/schedules/limits").json()
        assert data["current_inflight"] == 0
        assert data["current_holders"] == []
    finally:
        poll.release()
        scheduled.release()
