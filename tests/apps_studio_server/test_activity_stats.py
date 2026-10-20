# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for GET /api/stats/activity windowed bucket aggregation."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


async def _seed_session(
    db_path: Path,
    *,
    status: str | None,
    ended_at: float | None = None,
    started_at: float | None = None,
    created_at: float | None = None,
) -> None:
    session = {
        "id": str(uuid.uuid4()),
        "name": "test-session",
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    # Left out unless given: the store stamps a missing created_at with its own
    # clock, and an explicit None would violate the column's NOT NULL constraint.
    if created_at is not None:
        session["created_at"] = created_at
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session({**session, "progression_id": pid})


@pytest.fixture
def now(monkeypatch) -> float:
    """One instant for the seeded rows and the route's own clock read.

    The route cuts its buckets from the time it reads when it answers, so a run
    that crosses an hour (or, for the 7d window, a day) between seeding and the
    request would count a row seeded now in the previous bucket.
    """
    import types

    import lionagi.studio.services.stats as stats_mod

    pinned = time.time()
    monkeypatch.setattr(stats_mod, "time", types.SimpleNamespace(time=lambda: pinned))
    return pinned


def _make_client(monkeypatch, db_path: Path) -> TestClient:
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_empty_db_returns_dense_zero_buckets(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/activity")
    assert r.status_code == 200
    data = r.json()
    assert data["window"] == "24h"
    assert len(data["buckets"]) == 24
    assert data["total"] == 0
    assert data["completion_rate"] is None
    for b in data["buckets"]:
        assert b["completed"] == 0
        assert b["failed"] == 0
        assert b["cancelled"] == 0
        assert b["running"] == 0


def test_mixed_statuses_land_in_right_buckets_with_right_counts(tmp_path, monkeypatch, now):
    db_path = tmp_path / "state.db"

    async def seed():
        # Anchor timestamps in "now" hour bucket -> completed x2, failed x1
        await _seed_session(db_path, status="completed", ended_at=now)
        await _seed_session(db_path, status="completed", ended_at=now)
        await _seed_session(db_path, status="failed", ended_at=now)
        # Anchor timestamp ~5 hours ago -> cancelled x1
        await _seed_session(db_path, status="cancelled", ended_at=now - 5 * 3600)
        # Running session (no ended_at) anchored on started_at, in "now" bucket
        await _seed_session(db_path, status="running", started_at=now)

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    r = client.get("/api/stats/activity?window=24h")
    assert r.status_code == 200
    data = r.json()

    assert data["total"] == 5
    # completed=2, failed=1, cancelled=1 -> denom=4 -> rate=0.5
    assert data["completion_rate"] == pytest.approx(0.5)

    last_bucket = data["buckets"][-1]
    assert last_bucket["completed"] == 2
    assert last_bucket["failed"] == 1
    assert last_bucket["running"] == 1

    older_bucket = data["buckets"][-6]
    assert older_bucket["cancelled"] == 1


def test_window_7d_returns_seven_daily_buckets(tmp_path, monkeypatch, now):
    db_path = tmp_path / "state.db"

    async def seed():
        await _seed_session(db_path, status="completed", ended_at=now)
        await _seed_session(db_path, status="failed", ended_at=now - 3 * 24 * 3600)

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    r = client.get("/api/stats/activity?window=7d")
    assert r.status_code == 200
    data = r.json()
    assert data["window"] == "7d"
    assert len(data["buckets"]) == 7
    assert data["total"] == 2
    assert data["buckets"][-1]["completed"] == 1
    assert data["buckets"][-4]["failed"] == 1


def test_invalid_window_returns_422(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/activity?window=30d")
    assert r.status_code == 422


def test_running_sessions_counted_in_total_and_running_not_completion_denominator(
    tmp_path, monkeypatch, now
):
    db_path = tmp_path / "state.db"

    async def seed():
        await _seed_session(db_path, status="completed", ended_at=now)
        await _seed_session(db_path, status="running", started_at=now)

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    r = client.get("/api/stats/activity")
    data = r.json()

    assert data["total"] == 2
    # denom excludes running -> completed=1, failed=0, cancelled=0 -> rate=1.0
    assert data["completion_rate"] == pytest.approx(1.0)
    assert data["buckets"][-1]["running"] == 1


def test_status_aliases_fold_into_rendered_buckets(tmp_path, monkeypatch, now):
    db_path = tmp_path / "state.db"

    async def seed():
        await _seed_session(db_path, status="completed_empty", ended_at=now)
        await _seed_session(db_path, status="timed_out", ended_at=now)
        await _seed_session(db_path, status="aborted", ended_at=now)

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    data = client.get("/api/stats/activity").json()

    last = data["buckets"][-1]
    # completed_empty is terminal but unsuccessful (ADR-0064) -- it folds
    # into "failed" alongside timed_out, never into "completed".
    assert last["completed"] == 0
    assert last["failed"] == 2
    assert last["cancelled"] == 1
    assert data["total"] == 3
    # denom = 0 completed + 2 failed + 1 cancelled
    assert data["completion_rate"] == pytest.approx(0.0)


def test_null_and_unknown_statuses_count_in_total_only(tmp_path, monkeypatch, now):
    db_path = tmp_path / "state.db"

    async def seed():
        await _seed_session(db_path, status="completed", ended_at=now)
        await _seed_session(db_path, status="running", ended_at=now)
        await _seed_session(db_path, status="cancelled", ended_at=now)

    import asyncio

    asyncio.run(seed())

    # create_session enforces the ADR-0057 vocabulary, but legacy rows can hold
    # NULL or retired statuses — inject those directly to exercise the fold.
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET status = NULL WHERE id IN "
            "(SELECT id FROM sessions WHERE status = 'running')"
        )
        conn.execute(
            "UPDATE sessions SET status = 'paused' WHERE id IN "
            "(SELECT id FROM sessions WHERE status = 'cancelled')"
        )
        conn.commit()

    client = _make_client(monkeypatch, db_path)
    data = client.get("/api/stats/activity").json()

    assert data["total"] == 3
    # NULL/unknown reach neither a bucket nor the completion-rate denominator
    last = data["buckets"][-1]
    assert last["completed"] == 1
    assert last["failed"] == 0
    assert last["cancelled"] == 0
    assert last["running"] == 0
    assert data["completion_rate"] == pytest.approx(1.0)


def test_oldest_bucket_boundary_is_included(tmp_path, monkeypatch, now):
    db_path = tmp_path / "state.db"
    oldest_start = (int(now) // 3600) * 3600 - 23 * 3600

    async def seed():
        await _seed_session(db_path, status="failed", ended_at=float(oldest_start))

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    data = client.get("/api/stats/activity?window=24h").json()

    assert data["total"] == 1
    assert data["buckets"][0]["t"] == oldest_start
    assert data["buckets"][0]["failed"] == 1


def test_row_one_second_before_the_24h_window_is_excluded(tmp_path, monkeypatch, now):
    db_path = tmp_path / "state.db"
    oldest_start = (int(now) // 3600) * 3600 - 23 * 3600

    async def seed():
        await _seed_session(db_path, status="completed", ended_at=float(oldest_start - 1))

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    data = client.get("/api/stats/activity?window=24h").json()

    assert data["total"] == 0
    assert data["completion_rate"] is None
    assert all(b["completed"] == 0 for b in data["buckets"])


def test_end_time_anchors_the_bucket_when_both_timestamps_are_set(tmp_path, monkeypatch, now):
    db_path = tmp_path / "state.db"

    async def seed():
        await _seed_session(
            db_path,
            status="completed",
            created_at=now - 5 * 3600,
            started_at=now - 7200,
            ended_at=now,
        )

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    data = client.get("/api/stats/activity?window=24h").json()

    # Three timestamps in three different buckets, so a bucket anchored on the
    # start or the creation time shows up as a count in the wrong one.
    assert data["buckets"][-1]["completed"] == 1
    assert data["buckets"][-3]["completed"] == 0
    assert data["buckets"][-6]["completed"] == 0


def test_route_buckets_from_the_clock_the_now_fixture_pins(tmp_path, monkeypatch):
    # The `now` fixture holds only while the route reads the time through the
    # stats module's `time` attribute. An instant two days back shows which
    # clock the route actually read.
    import types

    import lionagi.studio.services.stats as stats_mod

    pinned = float((int(time.time()) // 3600) * 3600 - 2 * 86400 + 1800)
    monkeypatch.setattr(stats_mod, "time", types.SimpleNamespace(time=lambda: pinned))

    client = _make_client(monkeypatch, tmp_path / "state.db")
    data = client.get("/api/stats/activity?window=24h").json()

    assert data["buckets"][-1]["t"] == int(pinned) // 3600 * 3600


def test_running_session_with_only_created_at_anchors_to_creation(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    # The store stamps created_at from its own clock, which the `now` fixture
    # does not pin, so this test keeps clear of an hour rollover instead.
    now_wall = time.time()
    if now_wall % 3600 > 3570:
        time.sleep(3600 - now_wall % 3600 + 1)

    async def seed():
        await _seed_session(db_path, status="running")

    import asyncio

    asyncio.run(seed())

    client = _make_client(monkeypatch, db_path)
    data = client.get("/api/stats/activity").json()

    assert data["total"] == 1
    assert data["buckets"][-1]["running"] == 1


def test_missing_db_file_is_not_created_by_read(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/activity")
    assert r.status_code == 200
    assert not db_path.exists()


def test_bucket_list_is_dense_and_oldest_first(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(monkeypatch, db_path)

    r = client.get("/api/stats/activity?window=24h")
    data = r.json()
    ts = [b["t"] for b in data["buckets"]]
    assert len(ts) == 24
    assert ts == sorted(ts)
    assert all(ts[i + 1] - ts[i] == 3600 for i in range(len(ts) - 1))
