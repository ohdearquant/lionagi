# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for studio self-healing lifecycle reapers."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event, text

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB

from ._helpers import run_async

# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _monkey_db(monkeypatch, db_path: Path) -> None:
    """Point all relevant modules at a temp DB path."""

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


async def _seed_session(
    db_path: Path,
    *,
    session_id: str | None = None,
    status: str | None = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    artifacts_path: str | None = None,
    agent_name: str | None = None,
    node_metadata: dict | None = None,
) -> str:
    sid = session_id or str(uuid.uuid4())
    now = time.time()
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid,
                "name": "test-session",
                "status": status,
                "started_at": started_at or now,
                "agent_name": agent_name,
                "node_metadata": node_metadata,
            }
        )
        updates: dict = {}
        if updated_at is not None:
            updates["updated_at"] = updated_at
        if artifacts_path is not None:
            updates["artifacts_path"] = artifacts_path
        if status is None:
            # Force null status via direct SQL — update_session validates non-null.
            await db.execute("UPDATE sessions SET status = NULL WHERE id = ?", (sid,))
            updates.pop("status", None)
        if updates:
            # updated_at / artifacts_path must go through direct SQL when status is NULL
            # because update_session touches updated_at internally.
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [sid]
            await db.execute(
                f"UPDATE sessions SET {sets} WHERE id = ?",  # noqa: S608
                vals,
            )
    return sid


async def _seed_invocation(
    db_path: Path,
    *,
    inv_id: str | None = None,
    status: str = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    session_count: int = 0,
) -> str:
    iid = inv_id or uuid.uuid4().hex[:12]
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_invocation(
            {
                "id": iid,
                "skill": "test:skill",
                "started_at": started_at or now,
                "status": status,
                "session_count": session_count,
            }
        )
        if updated_at is not None:
            await db.execute(
                "UPDATE invocations SET updated_at = ? WHERE id = ?", (updated_at, iid)
            )
    return iid


async def _get_session(db_path: Path, sid: str) -> dict | None:
    async with StateDB(db_path) as db:
        return await db.get_session(sid)


async def _get_invocation(db_path: Path, iid: str) -> dict | None:
    async with StateDB(db_path) as db:
        return await db.get_invocation(iid)


async def _count_transitions(db_path: Path, entity_id: str) -> int:
    async with StateDB(db_path) as db:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
        return row["n"] if row else 0


async def _count_reaping_transitions(db_path: Path, entity_id: str) -> int:
    # Count transitions out of a real prior status, excluding the synthetic
    # initial creation row (previous_status IS NULL). Use this for entities
    # created through the managed insert path (which now records a creation
    # row) when asserting that no reaping transition occurred.
    async with StateDB(db_path) as db:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions "
            "WHERE entity_id = ? AND previous_status IS NOT NULL",
            (entity_id,),
        )
        return row["n"] if row else 0


# ── invocation deadline reaper ────────────────────────────────────────────────


# ── per-action-kind deadline override ────────────────────────────────────────


# ── null-status session detector ─────────────────────────────────────────────


# ── automatic phantom reaper ─────────────────────────────────────────────────


# ── admin prune delegates to transition-based reaper ─────────────────────────


def test_admin_prune_all_phantom_transitions_not_deletes(tmp_path, monkeypatch):
    """POST /api/admin/prune with all_phantom=true now transitions, not deletes."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_arts")
    stale_time = time.time() - 7200
    sid = run_async(
        _seed_session(
            db_path,
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            artifacts_path=missing_dir,
        )
    )

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.post("/api/admin/prune", json={"all_phantom": True})
    assert r.status_code == 200
    assert r.json()["pruned"] == 1

    # Session row must still exist (not deleted) but status = 'failed'.
    sess = run_async(_get_session(db_path, sid))
    assert sess is not None, "session row should be preserved (not deleted)"
    assert sess["status"] == "failed"
    assert run_async(_count_transitions(db_path, sid)) >= 1


async def _list_admin_events(db_path, **kwargs):
    async with StateDB(db_path) as db:
        return await db.list_admin_events(**kwargs)


def test_admin_prune_all_phantom_writes_admin_event(tmp_path, monkeypatch):
    """A visible destructive action must leave an audit row, not just a count:
    the adjacent table renders 'No admin events recorded yet' when nothing was
    recorded, which is indistinguishable from 'nothing happened' unless this
    path actually writes one."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_arts_event")
    stale_time = time.time() - 7200
    sid = run_async(
        _seed_session(
            db_path,
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            artifacts_path=missing_dir,
        )
    )

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.post("/api/admin/prune", json={"all_phantom": True})
    assert r.status_code == 200
    assert r.json()["pruned"] == 1

    events = run_async(_list_admin_events(db_path, action="prune_phantoms"))
    assert len(events) == 1, "prune_phantoms must write exactly one admin_events row"
    details = events[0]["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["count"] == 1
    assert sid in details["session_ids"]

    # The row is also findable by filtering on the affected session id, even
    # though target_id is NULL on a batch event.
    by_session = run_async(_list_admin_events(db_path, target_id=sid))
    assert any(e["action"] == "prune_phantoms" for e in by_session)


# ── phantom_count in stats ───────────────────────────────────────────────────


def test_stats_includes_phantom_count(tmp_path, monkeypatch):
    """GET /api/stats includes phantom_count field."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "phantom_count" in body
    assert isinstance(body["phantom_count"], int)


# ── session-less action kinds ────────────────────────────────────────────────


async def _seed_scheduled_invocation(
    db_path: Path,
    *,
    action_kind: str,
    updated_at: float,
    started_at: float | None = None,
) -> str:
    """Seed a running invocation plus the schedule_run occurrence that fired
    it, so the reaper can see the action kind the way production does."""
    iid = await _seed_invocation(
        db_path,
        started_at=started_at or time.time() - 120,
        updated_at=updated_at,
        session_count=0,
    )
    async with StateDB(db_path) as db:
        await db.create_schedule_run(
            {
                "id": uuid.uuid4().hex[:12],
                "schedule_id": None,
                "invocation_id": iid,
                "trigger_context": {},
                "action_kind": action_kind,
                "action_args": [],
                "status": "running",
                "fired_at": time.time(),
            }
        )
    return iid


def test_list_invocations_surfaces_action_kind_from_occurrence(tmp_path, monkeypatch):
    """action_kind lives on schedule_runs, not invocations; the listing joins it
    through so per-kind reaper policy has something to read."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    iid = run_async(
        _seed_scheduled_invocation(db_path, action_kind="command", updated_at=time.time())
    )

    async def _list() -> list[dict]:
        async with StateDB(db_path) as db:
            return await db.list_invocations(status="running")

    rows = {r["id"]: r for r in run_async(_list())}
    assert rows[iid]["action_kind"] == "command"
