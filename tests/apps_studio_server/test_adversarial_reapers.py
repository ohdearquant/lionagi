# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Adversarial edge-case tests for studio lifecycle reaper mechanisms."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB  # noqa: E402

from ._helpers import run_async  # noqa: E402

# ── shared DB helpers ─────────────────────────────────────────────────────────


def _monkey_db(monkeypatch, db_path: Path) -> None:

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


async def _seed_session(
    db_path: Path,
    *,
    status: str | None = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    artifacts_path: str | None = None,
    node_metadata: dict | None = None,
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
                "node_metadata": node_metadata,
            }
        )
        updates: dict = {}
        if updated_at is not None:
            updates["updated_at"] = updated_at
        if artifacts_path is not None:
            updates["artifacts_path"] = artifacts_path
        if status is None:
            await db.execute("UPDATE sessions SET status = NULL WHERE id = ?", (sid,))
        if updates:
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
            await db.execute(
                "UPDATE invocations SET updated_at = ? WHERE id = ?", (updated_at, iid)
            )
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
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
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

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 0
    assert run_async(_get_session_status(db_path, sid)) == "completed"
    assert run_async(_count_transitions(db_path, sid)) == 0


def test_1171_idempotent_double_call_no_double_write(tmp_path, monkeypatch):
    """Calling reap_null_status_sessions twice produces exactly one transition."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_time = time.time() - 7200  # past the 1h grace
    sid = run_async(
        _seed_session(db_path, status=None, started_at=stale_time, updated_at=stale_time)
    )

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count1 = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count1 == 1
    assert run_async(_get_session_status(db_path, sid)) == "failed"

    # Second call — row is no longer NULL so it should be invisible
    count2 = run_async(reap_null_status_sessions(stale_hours=1.0))
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
    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: True)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
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

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

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
                    "updated_at": ancient,
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

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

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
                    "updated_at": old_ts,
                }
            )
            # Insert a fake status_transition for this session
            trans_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO status_transitions"
                " (id, entity_type, entity_id, status, reason_code, source, actor, created_at)"
                " VALUES (?, 'session', ?, 'completed', 'test.completed', 'test', 'test', ?)",
                (trans_id, sid, old_ts),
            )
        return sid, trans_id

    sid, trans_id = run_async(seed())

    run_async(maint.prune_old_data(keep_days=30, actor="test"))

    # Session gone
    assert run_async(_get_session_status(db_path, sid)) is None

    # Transition row also cleaned up
    async def check_trans():
        async with StateDB(db_path) as db:
            return await db.fetch_one("SELECT id FROM status_transitions WHERE id = ?", (trans_id,))

    assert run_async(check_trans()) is None


def test_1173_prune_preserves_recent_terminal_sessions(tmp_path, monkeypatch):
    """Prune leaves recently-completed sessions intact (cutoff guard)."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

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
                    "updated_at": recent_ts,
                }
            )
            return sid

    sid = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result["sessions_pruned"] == 0
    assert run_async(_get_session_status(db_path, sid)) == "completed"


# ── shared state store: rows this machine cannot see are not this machine's to judge ──


def test_a_stale_session_hosted_on_another_machine_is_not_reaped(tmp_path, monkeypatch):
    """A reaper reads this host's process table, so a remote row is unmeasurable here.

    The reapers keep a session alive only on a positive liveness reading and
    otherwise lean on a staleness grace to protect a merely quiet run. That
    grace answers a momentary blind spot. Being on another machine is a
    permanent one, so waiting supplies nothing and the row is reaped precisely
    because it is still running somewhere this daemon cannot see.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(admin_mod.socket, "gethostname", lambda: "this-host")

    stale_time = time.time() - 7200
    remote = run_async(
        _seed_session(
            db_path,
            status=None,
            started_at=stale_time,
            updated_at=stale_time,
            node_metadata={"pid": 4242, "pid_host": "some-other-host"},
        )
    )
    local = run_async(
        _seed_session(
            db_path,
            status=None,
            started_at=stale_time,
            updated_at=stale_time,
            node_metadata={"pid": 4243, "pid_host": "this-host"},
        )
    )

    # Not patched to a constant: the real function has to be the thing that
    # answers, or this test would pass against a liveness check that had
    # stopped consulting the row at all.
    assert lc_mod.process_liveness is admin_mod.process_liveness

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))

    assert run_async(_get_session_status(db_path, remote)) is None, (
        "another host's row must be left exactly as it was"
    )
    assert run_async(_count_transitions(db_path, remote)) == 0
    assert run_async(_get_session_status(db_path, local)) == "failed", (
        "the local row must still be reaped — otherwise this passes on a dead reaper"
    )
    assert count == 1


def test_phantom_classifier_returns_no_reason_for_another_machines_row(tmp_path, monkeypatch):
    """The classifier that feeds the phantom reaper, checked directly.

    Its verdict is what the reaper writes `failed` from, so the local row
    beside it fixes what the answer would otherwise have been.
    """
    import lionagi.studio.services.admin as admin_mod

    monkeypatch.setattr(admin_mod.socket, "gethostname", lambda: "this-host")

    now = time.time()
    stale = now - 7200

    def _row(meta: dict) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "updated_at": stale,
            "artifacts_path": None,
            "node_metadata": meta,
        }

    class _Row(dict):
        def keys(self):  # noqa: D102 — sqlite3.Row-shaped access used by the classifier
            return super().keys()

    remote = _Row(_row({"pid": 4242, "pid_host": "some-other-host"}))
    local = _Row(_row({"pid": 4242, "pid_host": "this-host"}))

    monkeypatch.setattr(admin_mod, "_pid_is_live", lambda pid: False)

    assert (
        admin_mod._classify_phantom(remote, now=now, stale_seconds=3600, ps_snapshot="") is None
    ), "another host's stale row must produce no phantom reason"
    assert (
        admin_mod._classify_phantom(local, now=now, stale_seconds=3600, ps_snapshot="")
        == "process_dead"
    ), "a local stale row with a dead pid must still classify — otherwise nothing is being tested"


def test_process_identity_is_foreign_reads_host_and_unknown_modes(monkeypatch):
    """Foreign means "not measurable here", which covers two distinct records."""
    import lionagi.studio.services.admin as admin_mod

    monkeypatch.setattr(admin_mod.socket, "gethostname", lambda: "this-host")
    foreign = admin_mod.process_identity_is_foreign

    assert foreign({"node_metadata": {"pid_host": "other-host"}}) is True
    assert foreign({"node_metadata": {"process_identity_mode": "external"}}) is True
    # A mode this code does know how to check is not foreign on its own.
    assert foreign({"node_metadata": {"process_identity_mode": "local"}}) is False
    assert foreign({"node_metadata": {"process_identity_mode": "in_process"}}) is False
    assert foreign({"node_metadata": {"pid_host": "this-host"}}) is False
    assert foreign({"node_metadata": {"pid": 1}}) is False
    assert foreign({"node_metadata": None}) is False
    assert foreign({}) is False
    # Stored as JSON text by some read paths; the same answer either way.
    assert foreign({"node_metadata": '{"pid_host": "other-host"}'}) is True
    assert foreign({"node_metadata": "not json at all"}) is False

    # A marker recorded with the wrong type is still a marker, and it still
    # names a mode this code cannot check. Read as an absent one it would put
    # the row back in reach of the reapers, which treat a non-True liveness as
    # death once the row goes stale.
    assert foreign({"node_metadata": {"process_identity_mode": 123}}) is True
    assert foreign({"node_metadata": {"process_identity_mode": {"kind": "remote"}}}) is True
    # Absence keeps its own meaning: an old row is judged by the host check.
    assert foreign({"node_metadata": {"process_identity_mode": None}}) is False
