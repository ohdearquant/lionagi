# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for studio self-healing lifecycle reapers."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB

from ._helpers import run_async

# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _monkey_db(monkeypatch, db_path: Path) -> None:
    """Point all relevant modules at a temp DB path."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.lifecycle as lifecycle_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(lifecycle_mod, "DEFAULT_DB_PATH", db_path)


async def _seed_session(
    db_path: Path,
    *,
    session_id: str | None = None,
    status: str | None = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    artifacts_path: str | None = None,
    agent_name: str | None = None,
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


# ── invocation deadline reaper ────────────────────────────────────────────────


def test_reap_stale_invocations_deadline(tmp_path, monkeypatch):
    """Invocation started past deadline is transitioned to timed_out."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    old_started = time.time() - 8000  # well past default 7200s deadline
    iid = run_async(_seed_invocation(db_path, started_at=old_started, session_count=1))

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200))
    assert count == 1

    inv = run_async(_get_invocation(db_path, iid))
    assert inv is not None
    assert inv["status"] == "timed_out"
    assert inv["ended_at"] is not None
    assert run_async(_count_transitions(db_path, iid)) >= 1


def test_reap_stale_invocations_skips_recent(tmp_path, monkeypatch):
    """Invocation started recently is not reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    iid = run_async(_seed_invocation(db_path, started_at=time.time() - 60, session_count=1))

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200))
    assert count == 0

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "running"


def test_reap_stale_invocations_zero_session_grace(tmp_path, monkeypatch):
    """Running invocation with 0 sessions past grace period is reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_updated = time.time() - 600  # 10 min ago, past 5 min grace
    iid = run_async(
        _seed_invocation(
            db_path,
            started_at=time.time() - 120,
            updated_at=stale_updated,
            session_count=0,
        )
    )

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 1

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "timed_out"
    assert run_async(_count_transitions(db_path, iid)) >= 1


def test_reap_stale_invocations_zero_session_within_grace(tmp_path, monkeypatch):
    """Running invocation with 0 sessions still within grace is not reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    iid = run_async(
        _seed_invocation(
            db_path,
            started_at=time.time() - 30,
            updated_at=time.time() - 30,
            session_count=0,
        )
    )

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 0

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "running"


# ── per-action-kind deadline override ────────────────────────────────────────


def test_deadline_for_kind_uses_env_var(monkeypatch):
    """_deadline_for_kind returns the env-var value for a matching kind."""
    monkeypatch.setenv("LIONAGI_STUDIO_INVOCATION_DEADLINE_AGENT_SECONDS", "1800")

    from lionagi.studio.services.lifecycle import _deadline_for_kind

    assert _deadline_for_kind("agent", 7200) == 1800
    assert _deadline_for_kind("AGENT", 7200) == 1800  # case-insensitive key
    assert _deadline_for_kind("flow", 7200) == 7200  # no override for flow
    assert _deadline_for_kind(None, 7200) == 7200  # None always uses global


def test_reap_stale_invocations_per_kind_override(tmp_path, monkeypatch):
    """Per-kind env override reaps only the matching kind at the shorter cutoff (agent 1800s vs flow 7200s)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)
    monkeypatch.setenv("LIONAGI_STUDIO_INVOCATION_DEADLINE_AGENT_SECONDS", "1800")

    started = time.time() - 3000  # 3000s ago: past 1800s but within 7200s

    agent_iid = run_async(_seed_invocation(db_path, started_at=started, session_count=1))
    flow_iid = run_async(_seed_invocation(db_path, started_at=started, session_count=1))

    # Patch list_invocations to inject action_kind into the returned rows
    # (the invocations table currently has no action_kind column; the per-kind
    # lookup is tested here at the reaper level via the list_invocations result).
    import lionagi.state.db as state_db_mod

    _original_list = state_db_mod.StateDB.list_invocations

    async def _patched_list(self, *, skill=None, status=None, limit=100, offset=0):
        rows = await _original_list(self, skill=skill, status=status, limit=limit, offset=offset)
        for row in rows:
            if row["id"] == agent_iid:
                row["action_kind"] = "agent"
            elif row["id"] == flow_iid:
                row["action_kind"] = "flow"
        return rows

    monkeypatch.setattr(state_db_mod.StateDB, "list_invocations", _patched_list)

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200))
    assert count == 1, "exactly the agent invocation should be reaped"

    agent_inv = run_async(_get_invocation(db_path, agent_iid))
    flow_inv = run_async(_get_invocation(db_path, flow_iid))

    assert agent_inv["status"] == "timed_out", "agent kind exceeded its 1800 s deadline"
    assert flow_inv["status"] == "running", "flow kind within global 7200 s deadline"
    assert run_async(_count_transitions(db_path, agent_iid)) >= 1
    assert run_async(_count_transitions(db_path, flow_iid)) == 0


# ── null-status session detector ─────────────────────────────────────────────


def test_reap_null_status_sessions_dead_process(tmp_path, monkeypatch):
    """Null-status session with dead process is transitioned to failed."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_session(db_path, status=None, artifacts_path=None))

    # Patch _live_process_matches to report the process as dead.
    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "_live_process_matches", lambda _sid, _ap: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions())
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess is not None
    assert sess["status"] == "failed"
    assert sess["ended_at"] is not None
    assert run_async(_count_transitions(db_path, sid)) >= 1


def test_reap_null_status_sessions_skips_live_process(tmp_path, monkeypatch):
    """Null-status session with live process is not transitioned."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_session(db_path, status=None))

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "_live_process_matches", lambda _sid, _ap: True)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions())
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] is None


def test_reap_null_status_sessions_skips_terminal(tmp_path, monkeypatch):
    """Already-terminal sessions are never double-written."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    # Seed a 'completed' session — should be skipped (status IS NOT NULL).
    sid = run_async(_seed_session(db_path, status="completed"))

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "_live_process_matches", lambda _sid, _ap: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions())
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "completed"


# ── automatic phantom reaper ─────────────────────────────────────────────────


def test_reap_phantom_sessions_missing_artifacts(tmp_path, monkeypatch):
    """Running session with missing artifacts dir is transitioned to failed."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_artifacts")
    stale_time = time.time() - 7200  # old enough
    sid = run_async(
        _seed_session(
            db_path,
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            artifacts_path=missing_dir,
        )
    )

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess is not None
    assert sess["status"] == "failed"
    assert sess["ended_at"] is not None
    assert run_async(_count_transitions(db_path, sid)) >= 1

    # Reason summary should be phantom_reaped.
    async def _get_reason(db_path: Path, sid: str) -> str | None:
        async with StateDB(db_path) as db:
            row = await db.fetch_one(
                "SELECT status_reason_summary FROM sessions WHERE id = ?", (sid,)
            )
            return row["status_reason_summary"] if row else None

    reason = run_async(_get_reason(db_path, sid))
    assert reason == "phantom_reaped"


def test_reap_phantom_sessions_completes_mirrored_claude_session(tmp_path, monkeypatch):
    """A mirrored Claude session (agent_name='claude-code') is reaped to completed, not failed.

    It has no lionagi process, so the phantom model must not brand it failed/process_dead —
    an idle transcript is a normal completion. Guards the reaper's mirror-session branch.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_claude_artifacts")
    stale_time = time.time() - 7200
    sid = run_async(
        _seed_session(
            db_path,
            status="running",
            agent_name="claude-code",
            started_at=stale_time,
            updated_at=stale_time,
            artifacts_path=missing_dir,
        )
    )

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess is not None
    assert sess["status"] == "completed"  # NOT failed
    assert sess["ended_at"] is not None

    async def _get_reason(db_path: Path, sid: str) -> str | None:
        async with StateDB(db_path) as db:
            row = await db.fetch_one(
                "SELECT status_reason_summary FROM sessions WHERE id = ?", (sid,)
            )
            return row["status_reason_summary"] if row else None

    assert run_async(_get_reason(db_path, sid)) == "mirror_idle_reaped"


def test_reap_phantom_sessions_skips_already_terminal(tmp_path, monkeypatch):
    """Already-failed session is not double-written."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_artifacts2")
    stale_time = time.time() - 7200
    sid = run_async(
        _seed_session(
            db_path,
            status="failed",
            started_at=stale_time,
            updated_at=stale_time,
            artifacts_path=missing_dir,
        )
    )

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    # Even if list_phantom_sessions somehow listed it, reap_phantom_sessions
    # guards on status == 'running' before writing.
    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "failed"


def test_reap_phantom_sessions_skips_healthy_running(tmp_path, monkeypatch):
    """Running session with live artifacts is not reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    real_dir = tmp_path / "live_artifacts"
    real_dir.mkdir()
    sid = run_async(
        _seed_session(
            db_path,
            status="running",
            started_at=time.time() - 60,
            updated_at=time.time() - 10,
            artifacts_path=str(real_dir),
        )
    )

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "running"


# ── admin prune delegates to transition-based reaper ─────────────────────────


def test_admin_prune_all_phantom_transitions_not_deletes(tmp_path, monkeypatch):
    """POST /api/admin/prune with all_phantom=true now transitions, not deletes."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

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

    import lionagi.state.db as state_db_mod

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


# ── phantom_count in stats ───────────────────────────────────────────────────


def test_stats_includes_phantom_count(tmp_path, monkeypatch):
    """GET /api/stats includes phantom_count field."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "phantom_count" in body
    assert isinstance(body["phantom_count"], int)
