# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Adversarial edge-case tests for studio lifecycle reaper mechanisms."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB

from ._helpers import run_async

# ── shared DB helpers ─────────────────────────────────────────────────────────


def _monkey_db(monkeypatch, db_path: Path) -> None:
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
    status: str | None = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    artifacts_path: str | None = None,
) -> str:
    sid = str(uuid.uuid4())
    now = time.time()
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid,
                "name": "adv-test-session",
                "status": status,
                "started_at": started_at or now,
            }
        )
        updates: dict = {}
        if updated_at is not None:
            updates["updated_at"] = updated_at
        if artifacts_path is not None:
            updates["artifacts_path"] = artifacts_path
        if status is None:
            await db.db.execute("UPDATE sessions SET status = NULL WHERE id = ?", (sid,))
            await db.db.commit()
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [sid]
            await db.db.execute(
                f"UPDATE sessions SET {sets} WHERE id = ?",  # noqa: S608
                vals,
            )
            await db.db.commit()
    return sid


async def _seed_invocation(
    db_path: Path,
    *,
    status: str = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    session_count: int = 0,
) -> str:
    iid = uuid.uuid4().hex[:12]
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_invocation(
            {
                "id": iid,
                "skill": "adv:test",
                "started_at": started_at or now,
                "status": status,
                "session_count": session_count,
            }
        )
        if updated_at is not None:
            await db.db.execute(
                "UPDATE invocations SET updated_at = ? WHERE id = ?", (updated_at, iid)
            )
            await db.db.commit()
    return iid


async def _get_session_status(db_path: Path, sid: str) -> str | None:
    async with StateDB(db_path) as db:
        row = await db.get_session(sid)
    return row["status"] if row else None


async def _get_inv_status(db_path: Path, iid: str) -> str | None:
    async with StateDB(db_path) as db:
        row = await db.get_invocation(iid)
    return row["status"] if row else None


async def _count_transitions(db_path: Path, entity_id: str) -> int:
    async with StateDB(db_path) as db:
        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
        row = await cur.fetchone()
        return row["n"] if row else 0


# ── adversarial: invocation deadline false-positive guards ───────────────────


def test_1170_inv_with_live_sessions_not_reaped_by_zero_session_path(tmp_path, monkeypatch):
    """Invocation with session_count > 0 is NOT reaped even when updated_at is very old."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    # recent start (within deadline), old updated_at, but session_count=3
    iid = run_async(
        _seed_invocation(
            db_path,
            started_at=time.time() - 120,
            updated_at=time.time() - 9000,  # way past 300s grace
            session_count=3,
        )
    )

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 0
    assert run_async(_get_inv_status(db_path, iid)) == "running"


def test_1170_already_terminal_invocation_not_reaped(tmp_path, monkeypatch):
    """An invocation already in timed_out is not re-reaped (query guards status='running')."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    iid = run_async(
        _seed_invocation(
            db_path,
            status="timed_out",
            started_at=time.time() - 9000,
            session_count=0,
        )
    )

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 0
    # Still timed_out — not double-written
    assert run_async(_get_inv_status(db_path, iid)) == "timed_out"
    assert run_async(_count_transitions(db_path, iid)) == 0


def test_1170_neutralize_condition_row_stays_running(tmp_path, monkeypatch):
    """With a huge deadline, old invocations are NOT reaped (reaper fires only on deadline/grace)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    iid = run_async(
        _seed_invocation(
            db_path,
            started_at=time.time() - 500,
            session_count=0,
        )
    )

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    # Deadline so large it can never fire; grace also very large
    count = run_async(
        reap_stale_invocations(deadline_seconds=999_999, zero_session_grace_seconds=999_999)
    )
    assert count == 0
    assert run_async(_get_inv_status(db_path, iid)) == "running"


# ── adversarial: null-status session double-write guard ──────────────────────


def test_1171_terminal_session_never_overwritten(tmp_path, monkeypatch):
    """Completed sessions are invisible to the null-status reaper (WHERE status IS NULL)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_session(db_path, status="completed"))

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "_live_process_matches", lambda _s, _a: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions())
    assert count == 0
    assert run_async(_get_session_status(db_path, sid)) == "completed"
    assert run_async(_count_transitions(db_path, sid)) == 0


def test_1171_idempotent_double_call_no_double_write(tmp_path, monkeypatch):
    """Calling reap_null_status_sessions twice produces exactly one transition."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_session(db_path, status=None))

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "_live_process_matches", lambda _s, _a: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count1 = run_async(reap_null_status_sessions())
    assert count1 == 1
    assert run_async(_get_session_status(db_path, sid)) == "failed"

    # Second call — row is no longer NULL so it should be invisible
    count2 = run_async(reap_null_status_sessions())
    assert count2 == 0
    # Exactly one transition written
    assert run_async(_count_transitions(db_path, sid)) == 1


def test_1171_neutralize_condition_null_session_stays_null(tmp_path, monkeypatch):
    """When live-process check is mocked True, null-status session is NOT reaped.

    Confirms the reaper fires ONLY because process is dead, not for any other reason.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_session(db_path, status=None))

    import lionagi.studio.services.lifecycle as lc_mod

    # Process appears alive → reaper must not touch it
    monkeypatch.setattr(lc_mod, "_live_process_matches", lambda _s, _a: True)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions())
    assert count == 0
    assert run_async(_get_session_status(db_path, sid)) is None


# ── adversarial: phantom detection reuse + no regression ─────────────────────


def test_1172_phantom_reaper_driven_by_list_phantom_sessions(tmp_path, monkeypatch):
    """Mocking list_phantom_sessions to [] suppresses reaping even for legitimate phantoms."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_dir")
    sid = run_async(
        _seed_session(
            db_path,
            status="running",
            started_at=time.time() - 9000,
            updated_at=time.time() - 9000,
            artifacts_path=missing_dir,
        )
    )

    import lionagi.studio.services.lifecycle as lc_mod

    # Neutralise detection: list_phantom_sessions returns nothing
    async def _no_phantoms(**_kw):
        return []

    monkeypatch.setattr(lc_mod.admin_svc, "list_phantom_sessions", _no_phantoms)

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 0
    assert run_async(_get_session_status(db_path, sid)) == "running"


def test_1172_admin_prune_phantom_delegates_no_delete(tmp_path, monkeypatch):
    """prune_phantom_sessions() transitions the row, not deletes it (regression guard)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost2")
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

    from lionagi.studio.services.admin import prune_phantom_sessions

    count = run_async(prune_phantom_sessions(stale_hours=1.0))
    assert count == 1

    # Row PRESERVED (not deleted), status transitioned
    status = run_async(_get_session_status(db_path, sid))
    assert status == "failed"
    assert run_async(_count_transitions(db_path, sid)) >= 1


def test_1172_phantom_reaper_skips_already_terminal_even_if_detected(tmp_path, monkeypatch):
    """Phantom reaper skips sessions already in a terminal status (guards on current_status == 'running')."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_session(db_path, status="failed"))

    import lionagi.studio.services.lifecycle as lc_mod

    # Force list_phantom_sessions to return this already-failed session
    async def _fake_list(**_kw):
        return [{"session_id": sid, "reason": "missing_artifacts"}]

    monkeypatch.setattr(lc_mod.admin_svc, "list_phantom_sessions", _fake_list)

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 0

    # Status unchanged, no transition written
    assert run_async(_get_session_status(db_path, sid)) == "failed"
    assert run_async(_count_transitions(db_path, sid)) == 0


# ── adversarial: prune FK integrity and data preservation ────────────────────


def test_1173_prune_does_not_touch_running_old_sessions(tmp_path, monkeypatch):
    """Prune never removes a running session; status filter gates, not wall-clock age."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    ancient = time.time() - 365 * 86400  # 1 year old

    async def seed():
        async with StateDB(db_path) as db:
            pid = str(uuid.uuid4())
            sid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": pid,
                    "name": "ancient-running",
                    "status": "running",
                    "started_at": ancient,
                }
            )
            return sid

    sid = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result["sessions_pruned"] == 0
    assert run_async(_get_session_status(db_path, sid)) == "running"


def test_1173_prune_status_transitions_cleanup(tmp_path, monkeypatch):
    """Prune removes status_transitions for pruned sessions (no orphan audit rows)."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            pid = str(uuid.uuid4())
            sid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": pid,
                    "name": "old-completed",
                    "status": "completed",
                    "started_at": old_ts,
                }
            )
            # Insert a fake status_transition for this session
            trans_id = str(uuid.uuid4())
            await db.db.execute(
                "INSERT INTO status_transitions"
                " (id, entity_type, entity_id, status, reason_code, source, actor, created_at)"
                " VALUES (?, 'session', ?, 'completed', 'test.completed', 'test', 'test', ?)",
                (trans_id, sid, old_ts),
            )
            await db.db.commit()
        return sid, trans_id

    sid, trans_id = run_async(seed())

    run_async(maint.prune_old_data(keep_days=30, actor="test"))

    # Session gone
    assert run_async(_get_session_status(db_path, sid)) is None

    # Transition row also cleaned up
    async def check_trans():
        async with StateDB(db_path) as db:
            cur = await db.db.execute("SELECT id FROM status_transitions WHERE id = ?", (trans_id,))
            return await cur.fetchone()

    assert run_async(check_trans()) is None


def test_1173_prune_preserves_recent_terminal_sessions(tmp_path, monkeypatch):
    """Prune leaves recently-completed sessions intact (cutoff guard)."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    recent_ts = time.time() - 5 * 86400  # 5 days ago, within 30-day keep window

    async def seed():
        async with StateDB(db_path) as db:
            pid = str(uuid.uuid4())
            sid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": pid,
                    "name": "recent-completed",
                    "status": "completed",
                    "started_at": recent_ts,
                }
            )
            return sid

    sid = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result["sessions_pruned"] == 0
    assert run_async(_get_session_status(db_path, sid)) == "completed"
