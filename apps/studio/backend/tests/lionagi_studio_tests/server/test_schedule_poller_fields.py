# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The schedule routes serve a github_poll schedule's own health gauges.

``last_healthy_poll_at`` is stamped on every healthy poll including an empty
one, and ``poller_consecutive_401`` counts the authentication-failure streak.
On a quiet repo no event arrives and no run row is written, so these two are
the only fields that separate a poller still reading the repo from one that
stopped -- a caller projected away from them cannot tell those apart, and the
absence reads as health.

The projection is a privacy boundary in both directions, so each test pairs
the fields it requires with a field that must stay unserved: the authored
prompt, which the record view widens to and the list view does not, and
``github_cursor``, which is poll bookkeeping and is served on neither.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionhub.state.db as state_db_mod  # noqa: E402
from lionhub.serve.daemon.services.schedules import create_schedule  # noqa: E402
from lionhub.state.db import StateDB  # noqa: E402

HEALTHY_AT = 1_700_000_000.0


def _patch_db(monkeypatch, db_path: Path) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client() -> TestClient:
    from lionagi_studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


async def _seed_poller(
    *,
    polled: bool,
    healthy_at: float = HEALTHY_AT,
    stagnation: int = 0,
    merged_limit: int | None = None,
    last_capped_limit: int | None = None,
    enabled: bool = True,
) -> str:
    created = await create_schedule(
        {
            "name": f"poller-fields-{uuid.uuid4().hex[:8]}",
            "trigger_type": "github_poll",
            "github_repo": "acme/widgets",
            "github_filter": (
                {"event": "pr_merged", "merged_page_limit": merged_limit}
                if merged_limit is not None
                else None
            ),
            "poll_interval_sec": 300,
            "action_kind": "agent",
            "action_prompt": "review the new commits",
            "enabled": enabled,
        }
    )
    if polled:
        async with StateDB() as db:
            await db.update_schedule(
                created["id"],
                last_healthy_poll_at=healthy_at,
                poller_consecutive_401=3,
                github_cursor="sha-deadbeef",
            )
            for _ in range(stagnation):
                assert await db.record_github_poll_stagnation(
                    created["id"], reset=False, page_limit=last_capped_limit
                )
    return created["id"]


def test_list_serves_poller_health_gauges(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    sid = asyncio.run(_seed_poller(polled=True))

    resp = _make_client().get("/api/schedules/")

    assert resp.status_code == 200
    row = next(item for item in resp.json()["schedules"] if item["id"] == sid)
    assert row["last_healthy_poll_at"] == HEALTHY_AT
    assert row["poller_consecutive_401"] == 3
    assert "action_prompt" not in row
    assert "github_cursor" not in row


def test_record_serves_poller_health_gauges(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    sid = asyncio.run(_seed_poller(polled=True))

    resp = _make_client().get(f"/api/schedules/{sid}")

    assert resp.status_code == 200
    row = resp.json()
    assert row["last_healthy_poll_at"] == HEALTHY_AT
    assert row["poller_consecutive_401"] == 3
    assert "action_prompt" in row
    assert "github_cursor" not in row


def test_never_polled_serves_null_rather_than_omitting_the_field(tmp_path, monkeypatch):
    """A poller that has never had a healthy read must be distinguishable from
    a route that does not serve the gauge at all: the caller has to be able to
    fail closed on "never", and an omitted key reads the same as an omitted
    feature."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    sid = asyncio.run(_seed_poller(polled=False))

    resp = _make_client().get("/api/schedules/")

    row = next(item for item in resp.json()["schedules"] if item["id"] == sid)
    assert "last_healthy_poll_at" in row
    assert row["last_healthy_poll_at"] is None
    assert row["poller_consecutive_401"] == 0


@pytest.mark.parametrize("surface", ["list", "record"])
def test_stagnation_changes_generic_health_without_projecting_the_counter(
    tmp_path, monkeypatch, surface
):
    _patch_db(monkeypatch, tmp_path / "state.db")
    healthy_at = time.time()
    sid = asyncio.run(_seed_poller(polled=True, healthy_at=healthy_at, stagnation=3))
    response = _make_client().get(
        "/api/schedules/" if surface == "list" else f"/api/schedules/{sid}"
    )

    assert response.status_code == 200
    row = (
        next(item for item in response.json()["schedules"] if item["id"] == sid)
        if surface == "list"
        else response.json()
    )
    assert row["health_state"] == "overdue"
    assert row["last_healthy_poll_at"] == healthy_at
    assert row["poller_consecutive_401"] == 3
    assert row["health_last_outcome"] is None
    assert "health_effective_poll_interval_sec" not in row
    assert "poller_consecutive_cap_no_dispatch" not in row
    assert "github_cursor" not in row
    assert ("action_prompt" in row) == (surface == "record")


@pytest.mark.parametrize("surface", ["list", "record"])
@pytest.mark.parametrize(
    "stagnation,merged_limit,last_capped_limit,enabled,health,interval",
    [
        pytest.param(1, 5, 5, True, "healthy", 600, id="first-cap"),
        pytest.param(3, 5, 5, True, "overdue", 2400, id="stalled"),
        pytest.param(3, 10, 5, True, "overdue", None, id="raised-cap"),
        pytest.param(3, 5, None, True, "overdue", None, id="unknown-cap"),
        pytest.param(3, 5, 5, False, "disabled", None, id="disabled"),
    ],
)
def test_merged_poll_projects_active_backoff(
    tmp_path,
    monkeypatch,
    surface,
    stagnation,
    merged_limit,
    last_capped_limit,
    enabled,
    health,
    interval,
):
    _patch_db(monkeypatch, tmp_path / "state.db")
    healthy_at = time.time()
    sid = asyncio.run(
        _seed_poller(
            polled=True,
            healthy_at=healthy_at,
            stagnation=stagnation,
            merged_limit=merged_limit,
            last_capped_limit=last_capped_limit,
            enabled=enabled,
        )
    )
    response = _make_client().get(
        "/api/schedules/" if surface == "list" else f"/api/schedules/{sid}"
    )

    assert response.status_code == 200
    row = (
        next(item for item in response.json()["schedules"] if item["id"] == sid)
        if surface == "list"
        else response.json()
    )
    assert row["health_state"] == health
    assert row["poll_interval_sec"] == 300
    assert row["last_healthy_poll_at"] == healthy_at
    assert row["poller_consecutive_401"] == 3
    if health == "overdue":
        assert row["health_cause"] == "page_limit_reached"
        assert (
            f"The page limit was {last_capped_limit}; raise it and retry."
            if last_capped_limit is not None
            else "The last capped page limit is unknown; review the limit and retry."
        ) == row["health_recovery"]
    else:
        assert "health_cause" not in row
        assert "health_recovery" not in row
    if interval is None:
        assert "health_effective_poll_interval_sec" not in row
    else:
        assert row["health_effective_poll_interval_sec"] == interval
    assert "poller_consecutive_cap_no_dispatch" not in row
    assert "poller_last_capped_page_limit" not in row
    assert "poller_target_generation" not in row
    assert "github_cursor" not in row


@pytest.mark.parametrize(
    "stagnation,quiet_for,health",
    [
        # Authored 300 s: overdue past max(3 * 300, 300 + 300) = 900 s.
        pytest.param(0, 1000, "overdue", id="no-backoff-past-authored-threshold"),
        # Two capped scans back the poll off to 1200 s, so the threshold is
        # max(3 * 1200, 1200 + 300) = 3600 s and a 1000 s gap is on time.
        pytest.param(2, 1000, "healthy", id="backed-off-within-effective-threshold"),
        pytest.param(2, 3700, "overdue", id="backed-off-past-effective-threshold"),
    ],
)
def test_merged_poll_overdue_uses_backed_off_interval(
    tmp_path, monkeypatch, stagnation, quiet_for, health
):
    _patch_db(monkeypatch, tmp_path / "state.db")
    healthy_at = time.time() - quiet_for
    sid = asyncio.run(
        _seed_poller(
            polled=True,
            healthy_at=healthy_at,
            stagnation=stagnation,
            merged_limit=5,
            last_capped_limit=5,
        )
    )
    response = _make_client().get(f"/api/schedules/{sid}")

    assert response.status_code == 200
    row = response.json()
    assert row["health_state"] == health
    assert "health_cause" not in row
    if stagnation:
        assert row["health_effective_poll_interval_sec"] == 1200
    else:
        assert "health_effective_poll_interval_sec" not in row
