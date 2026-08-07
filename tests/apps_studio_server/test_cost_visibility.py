# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for aggregate cost visibility: cost column + sort, spend panel
aggregate, and session-grain per-dimension rollups.

The contract under test throughout: a nullable total_cost_usd means the
provider never reported a cost (unknown); a genuine 0.0 is a real, distinct
value. No query here may coerce NULL into 0 for a group that contains real
records — every aggregate must instead surface how many rows were
unreported alongside the number it does report.
"""

from __future__ import annotations

import asyncio
import sqlite3
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


async def _seed_session(
    db_path: Path,
    *,
    status: str = "completed",
    ended_at: float | None = None,
    started_at: float | None = None,
    project: str | None = None,
    agent_name: str | None = None,
    playbook_name: str | None = None,
    total_cost_usd: float | None = None,
) -> str:
    sid = str(uuid.uuid4())
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid,
                "name": "test-session",
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "project": project,
                "agent_name": agent_name,
                "playbook_name": playbook_name,
            }
        )
    # create_session has no total_cost_usd parameter — it is written later,
    # on a separate finalize path. Inject it directly, same as the activity
    # stats tests do for status.
    if total_cost_usd is not None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE sessions SET total_cost_usd = ? WHERE id = ?", (total_cost_usd, sid)
            )
            conn.commit()
    return sid


def _make_client(monkeypatch, db_path: Path) -> TestClient:
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


# ---------------------------------------------------------------------------
# Cost column on run/session lists
# ---------------------------------------------------------------------------


def test_runs_list_preserves_null_cost_as_unreported(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_seed_session(db_path, total_cost_usd=None))
    _run(_seed_session(db_path, total_cost_usd=0.0))
    _run(_seed_session(db_path, total_cost_usd=3.5))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/runs/")
    assert r.status_code == 200
    rows = r.json()["runs"]
    costs = {row["total_cost_usd"] for row in rows}
    assert None in costs
    assert 0.0 in costs
    assert 3.5 in costs


def test_session_detail_preserves_null_cost(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = _run(_seed_session(db_path, total_cost_usd=None))
    client = _make_client(monkeypatch, db_path)

    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["total_cost_usd"] is None


def test_session_detail_preserves_genuine_zero_cost(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = _run(_seed_session(db_path, total_cost_usd=0.0))
    client = _make_client(monkeypatch, db_path)

    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["total_cost_usd"] == 0.0


def test_cost_sort_puts_unreported_after_all_reported_including_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _run(_seed_session(db_path, total_cost_usd=None))
    _run(_seed_session(db_path, total_cost_usd=0.0))
    _run(_seed_session(db_path, total_cost_usd=9.0))
    _run(_seed_session(db_path, total_cost_usd=2.0))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/runs/?sort=cost")
    assert r.status_code == 200
    costs = [row["total_cost_usd"] for row in r.json()["runs"]]
    assert costs == [9.0, 2.0, 0.0, None]


def test_invalid_sort_returns_422(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/runs/?sort=bogus")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Spend panel aggregate (/api/stats/spend)
# ---------------------------------------------------------------------------


def test_spend_stats_empty_db_is_unreported_not_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend")
    assert r.status_code == 200
    data = r.json()
    assert data["reported_usd"] is None
    assert data["reported_count"] == 0
    assert data["unreported_count"] == 0
    assert data["total_count"] == 0
    assert data["coverage"] is None


def test_spend_stats_all_unreported_never_becomes_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=None))
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=None))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend")
    data = r.json()
    # The regression this test guards: a bare COALESCE(SUM(...), 0) would
    # report 0.0 here, reading as "this cost nothing" instead of "unknown".
    assert data["reported_usd"] is None
    assert data["reported_count"] == 0
    assert data["unreported_count"] == 2
    assert data["total_count"] == 2
    assert data["coverage"] == pytest.approx(0.0)


def test_spend_stats_mixed_reported_and_unreported_excludes_unreported_from_sum(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=12.34))
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=None))
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=None))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend")
    data = r.json()
    assert data["reported_usd"] == pytest.approx(12.34)
    assert data["reported_count"] == 1
    assert data["unreported_count"] == 2
    assert data["total_count"] == 3
    assert data["coverage"] == pytest.approx(1 / 3)


def test_spend_stats_reported_zero_with_no_unreported_is_a_real_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=0.0))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend")
    data = r.json()
    assert data["reported_usd"] == 0.0
    assert data["reported_count"] == 1
    assert data["unreported_count"] == 0
    assert data["coverage"] == pytest.approx(1.0)


def test_spend_stats_counts_in_flight_running_sessions(tmp_path, monkeypatch):
    """Coverage must include non-terminal records, not just finished ones."""
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=5.0))
    _run(_seed_session(db_path, status="running", started_at=now, total_cost_usd=None))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend")
    data = r.json()
    assert data["total_count"] == 2
    assert data["unreported_count"] == 1
    assert data["coverage"] == pytest.approx(0.5)


def test_spend_stats_window_matches_activity_stats_boundary(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, total_cost_usd=7.0))
    # Outside the 24h window entirely.
    _run(_seed_session(db_path, ended_at=now - 30 * 3600, total_cost_usd=99.0))
    client = _make_client(monkeypatch, db_path)

    activity = client.get("/api/stats/activity?window=24h").json()
    spend = client.get("/api/stats/spend?window=24h").json()
    assert activity["total"] == 1
    assert spend["total_count"] == 1
    assert spend["reported_usd"] == pytest.approx(7.0)


def test_spend_stats_invalid_window_returns_422(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend?window=30d")
    assert r.status_code == 422


def test_spend_stats_missing_db_file_is_not_created_by_read(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend")
    assert r.status_code == 200
    assert not db_path.exists()


# ---------------------------------------------------------------------------
# Per-dimension rollups (/api/stats/spend/rollup)
# ---------------------------------------------------------------------------


def test_rollup_by_project_sums_reported_and_counts_unreported_per_group(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, project="alpha", total_cost_usd=10.0))
    _run(_seed_session(db_path, ended_at=now, project="alpha", total_cost_usd=None))
    _run(_seed_session(db_path, ended_at=now, project="beta", total_cost_usd=3.0))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend/rollup")
    assert r.status_code == 200
    by_project = {row["key"]: row for row in r.json()["by_project"]}

    assert by_project["alpha"]["reported_usd"] == pytest.approx(10.0)
    assert by_project["alpha"]["reported_count"] == 1
    assert by_project["alpha"]["unreported_count"] == 1
    assert by_project["beta"]["reported_usd"] == pytest.approx(3.0)
    assert by_project["beta"]["unreported_count"] == 0


def test_rollup_group_with_only_unreported_rows_stays_unreported(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, project="ghost", total_cost_usd=None))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend/rollup")
    by_project = {row["key"]: row for row in r.json()["by_project"]}
    assert by_project["ghost"]["reported_usd"] is None
    assert by_project["ghost"]["unreported_count"] == 1


def test_rollup_by_agent_and_playbook_are_session_grain(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(
        _seed_session(
            db_path,
            ended_at=now,
            agent_name="r1",
            playbook_name="research",
            total_cost_usd=4.0,
        )
    )
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend/rollup")
    data = r.json()
    agents = {row["key"] for row in data["by_agent"]}
    playbooks = {row["key"] for row in data["by_playbook"]}
    assert "r1" in agents
    assert "research" in playbooks


def test_rollup_sorts_highest_reported_spend_first(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    now = time.time()
    _run(_seed_session(db_path, ended_at=now, project="low", total_cost_usd=1.0))
    _run(_seed_session(db_path, ended_at=now, project="high", total_cost_usd=100.0))
    _run(_seed_session(db_path, ended_at=now, project="none", total_cost_usd=None))
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend/rollup")
    keys = [row["key"] for row in r.json()["by_project"]]
    assert keys.index("high") < keys.index("low") < keys.index("none")


def test_rollup_invalid_window_returns_422(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/spend/rollup?window=30d")
    assert r.status_code == 422


def test_rollup_limit_is_bounded_server_side(tmp_path, monkeypatch):
    """Even with a large number of distinct groups, spend_rollup's own cap
    (not caller-controlled) bounds how many rows a single request returns —
    no fetch-all-then-sum, and no unbounded response."""
    db_path = tmp_path / "state.db"
    now = time.time()

    async def seed_many():
        async with StateDB(db_path) as db:
            for i in range(80):
                pid = str(uuid.uuid4())
                await db.create_progression(pid)
                await db.create_session(
                    {
                        "id": str(uuid.uuid4()),
                        "progression_id": pid,
                        "status": "completed",
                        "ended_at": now,
                        "project": f"proj-{i}",
                    }
                )

    _run(seed_many())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET total_cost_usd = 1.0")
        conn.commit()

    async def rollup_direct():
        async with StateDB(db_path) as db:
            return await db.spend_rollup(window_start=0, dimension="project", limit=10_000)

    rows = _run(rollup_direct())
    assert len(rows) == StateDB._SPEND_ROLLUP_MAX_LIMIT
