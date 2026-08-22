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
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod  # noqa: E402
from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services.schedules import create_schedule  # noqa: E402

HEALTHY_AT = 1_700_000_000.0


def _patch_db(monkeypatch, db_path: Path) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client() -> TestClient:
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


async def _seed_poller(*, polled: bool) -> str:
    created = await create_schedule(
        {
            "name": f"poller-fields-{uuid.uuid4().hex[:8]}",
            "trigger_type": "github_poll",
            "github_repo": "acme/widgets",
            "poll_interval_sec": 300,
            "action_kind": "agent",
            "action_prompt": "review the new commits",
        }
    )
    if polled:
        async with StateDB() as db:
            await db.update_schedule(
                created["id"],
                last_healthy_poll_at=HEALTHY_AT,
                poller_consecutive_401=3,
                github_cursor="sha-deadbeef",
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
