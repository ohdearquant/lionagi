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


def test_reap_stale_invocations_reaches_oldest_row_past_first_thousand(tmp_path, monkeypatch):
    """A stale invocation cannot hide behind 1,000 newer running rows.

    ``list_invocations`` sorts newest-first and the lifecycle pass historically
    capped that list at 1,000. A busy daemon could therefore leave its oldest
    crashed invocation running forever as long as at least 1,000 newer rows
    remained active.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    now = time.time()
    stale_id = "stale-behind-first-page"
    rows = [
        {
            "id": f"recent-{index:04d}",
            "skill": "test:skill",
            "started_at": now - 60,
            "status": "running",
            "session_count": 1,
            "created_at": now,
            "updated_at": now,
        }
        for index in range(1000)
    ]
    rows.append(
        {
            "id": stale_id,
            "skill": "test:skill",
            "started_at": now - 8000,
            "status": "running",
            "session_count": 1,
            "created_at": now - 8000,
            "updated_at": now - 8000,
        }
    )

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            async with db.transaction() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO invocations "
                        "(id, skill, started_at, status, session_count, created_at, updated_at) "
                        "VALUES (:id, :skill, :started_at, :status, :session_count, "
                        ":created_at, :updated_at)"
                    ),
                    rows,
                )

    run_async(_seed())

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200))

    assert count == 1
    stale = run_async(_get_invocation(db_path, stale_id))
    assert stale is not None
    assert stale["status"] == "timed_out"


def test_invocation_reaper_keyset_uses_bounded_composite_index_plan(tmp_path):
    """A later keyset page must seek into running rows, not rescan them.

    This captures the exact statement emitted by StateDB and explains that
    statement on disk. The top-level invocation search must use the composite
    ``(status, started_at, id)`` index with a row-value range and must not
    allocate a temporary B-tree for its ORDER BY. A correlated schedule-run
    subquery has its own plan nodes and is deliberately outside this assertion.
    """
    db_path = tmp_path / "state.db"

    async def _capture_query() -> tuple[str, tuple[object, ...]]:
        async with StateDB(db_path) as db:
            await db.create_invocation(
                {
                    "id": "reaper-plan-row",
                    "skill": "test:skill",
                    "started_at": 2.0,
                    "status": "running",
                    "session_count": 1,
                }
            )
            captured: list[tuple[str, tuple[object, ...]]] = []

            def _capture(_conn, _cursor, statement, parameters, _context, _executemany):
                if "FROM invocations inv" in statement and "inv.status = 'running'" in statement:
                    captured.append((statement, tuple(parameters)))

            event.listen(db._engine.sync_engine, "before_cursor_execute", _capture)
            try:
                await db.list_running_invocations_for_reaping(
                    after_started_at=1.0,
                    after_id="cursor-id",
                    limit=500,
                )
            finally:
                event.remove(db._engine.sync_engine, "before_cursor_execute", _capture)
        assert len(captured) == 1
        return captured[0]

    statement, parameters = run_async(_capture_query())
    with sqlite3.connect(db_path) as conn:
        plan = list(conn.execute(f"EXPLAIN QUERY PLAN {statement}", parameters))

    top_level = [row[3] for row in plan if row[1] == 0]
    invocation_search = next(detail for detail in top_level if "inv" in detail.lower())
    compact_search = invocation_search.replace(" ", "").lower()

    assert "idx_invocations_reaper" in invocation_search
    assert "(started_at,id)>(?,?)" in compact_search
    assert "scan inv" not in invocation_search.lower()
    assert not any("USE TEMP B-TREE" in detail for detail in top_level), plan


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


def test_reap_stale_invocations_deadline_lost_cas_does_not_stamp_ended_at(tmp_path, monkeypatch):
    """A lost CAS race on the deadline path must not leave ended_at stamped
    while status is still "running" — the same "status and ended_at
    disagree" defect as issue #2844, on the invocation deadline path.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    old_started = time.time() - 8000  # well past default 7200s deadline
    iid = run_async(_seed_invocation(db_path, started_at=old_started, session_count=1))

    async def _lost_race(*_a, **_k):
        return False

    monkeypatch.setattr(StateDB, "update_status", _lost_race)

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200))
    assert count == 0

    inv = run_async(_get_invocation(db_path, iid))
    assert inv is not None
    assert inv["status"] == "running"
    assert inv["ended_at"] is None


def test_reap_stale_invocations_zero_session_lost_cas_does_not_stamp_ended_at(
    tmp_path, monkeypatch
):
    """A lost CAS race on the zero-session path must not leave ended_at
    stamped while status is still "running" — the same "status and ended_at
    disagree" defect as issue #2844, on the invocation zero-session path.
    """
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

    async def _lost_race(*_a, **_k):
        return False

    monkeypatch.setattr(StateDB, "update_status", _lost_race)

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 0

    inv = run_async(_get_invocation(db_path, iid))
    assert inv is not None
    assert inv["status"] == "running"
    assert inv["ended_at"] is None


def test_reap_stale_invocations_deadline_write_is_atomic_with_ended_at(tmp_path, monkeypatch):
    """The winning transition must stamp ended_at in the SAME write as the
    status change, not depend on a follow-up update_invocation() call that
    could independently fail and leave status="timed_out" with ended_at=None
    while `reaped` still counts the row (the "winning CAS can still lose
    ended_at" defect). Proven by making update_invocation() raise: the
    reaper must never call it at all for this transition.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    old_started = time.time() - 8000
    iid = run_async(_seed_invocation(db_path, started_at=old_started, session_count=1))

    async def _boom(*_a, **_k):
        raise RuntimeError("update_invocation must not be called by this reaper")

    monkeypatch.setattr(StateDB, "update_invocation", _boom)

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200))
    assert count == 1

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "timed_out"
    assert inv["ended_at"] is not None


def test_reap_stale_invocations_zero_session_write_is_atomic_with_ended_at(tmp_path, monkeypatch):
    """Same atomicity requirement as the deadline path, for the zero-session
    branch."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_updated = time.time() - 600
    iid = run_async(
        _seed_invocation(
            db_path,
            started_at=time.time() - 120,
            updated_at=stale_updated,
            session_count=0,
        )
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("update_invocation must not be called by this reaper")

    monkeypatch.setattr(StateDB, "update_invocation", _boom)

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 1

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "timed_out"
    assert inv["ended_at"] is not None


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

    # Patch the reaper projection to inject action_kind into the returned rows
    # (the invocations table currently has no action_kind column; the per-kind
    # lookup is tested here at the reaper level via its projected page).

    _original_list = state_db_mod.StateDB.list_running_invocations_for_reaping

    async def _patched_list(self, **kwargs):
        rows = await _original_list(self, **kwargs)
        for row in rows:
            if row["id"] == agent_iid:
                row["action_kind"] = "agent"
            elif row["id"] == flow_iid:
                row["action_kind"] = "flow"
        return rows

    monkeypatch.setattr(state_db_mod.StateDB, "list_running_invocations_for_reaping", _patched_list)

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200))
    assert count == 1, "exactly the agent invocation should be reaped"

    agent_inv = run_async(_get_invocation(db_path, agent_iid))
    flow_inv = run_async(_get_invocation(db_path, flow_iid))

    assert agent_inv["status"] == "timed_out", "agent kind exceeded its 1800 s deadline"
    assert flow_inv["status"] == "running", "flow kind within global 7200 s deadline"
    assert run_async(_count_transitions(db_path, agent_iid)) >= 1
    assert run_async(_count_reaping_transitions(db_path, flow_iid)) == 0


# ── null-status session detector ─────────────────────────────────────────────


def test_reap_null_status_sessions_dead_process(tmp_path, monkeypatch):
    """Null-status session with dead process, stale past the grace, is reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_time = time.time() - 7200  # past default 1h grace
    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            started_at=stale_time,
            updated_at=stale_time,
        )
    )

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess is not None
    assert sess["status"] == "failed"
    assert sess["ended_at"] is not None
    assert run_async(_count_transitions(db_path, sid)) >= 1


def test_reap_null_status_sessions_populates_duration_ms(tmp_path, monkeypatch):
    """The reaper pre-sets ended_at via update_session, then transitions status
    via update_status in a second call -- duration_ms must still land, derived
    from the same started_at/ended_at pair, not just ended_at alone."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_time = time.time() - 7200
    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            started_at=stale_time,
            updated_at=stale_time,
        )
    )

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "failed"
    assert sess["duration_ms"] == pytest.approx((sess["ended_at"] - stale_time) * 1000)


def test_reap_null_status_sessions_skips_live_process(tmp_path, monkeypatch):
    """Null-status session with live process is not transitioned."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_session(db_path, status=None))

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: True)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
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

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "completed"


def test_reap_null_status_sessions_live_recorded_pid_not_reaped(tmp_path, monkeypatch):
    """A fresh null-status session with a LIVE recorded node_metadata.pid is not reaped.

    Load-bearing regression check: the old id-only liveness check
    (``_live_process_matches``) never saw ``node_metadata``, so it fell
    through to ``None`` (unknown) -> reaped with no staleness grace. This
    fails on that old code and passes once liveness honors the recorded pid.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            node_metadata={"pid": os.getpid()},
        )
    )

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] is None


def test_reap_null_status_sessions_stale_dead_recorded_pid_reaped(tmp_path, monkeypatch):
    """A stale null-status session with a DEAD recorded pid is still reaped (cleanup preserved)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    proc = subprocess.Popen(["/bin/sleep", "0"])  # noqa: S603
    proc.wait()
    dead_pid = proc.pid

    stale_time = time.time() - 7200  # past the 1h grace
    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            started_at=stale_time,
            updated_at=stale_time,
            node_metadata={"pid": dead_pid},
        )
    )

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess is not None
    assert sess["status"] == "failed"
    assert run_async(_count_transitions(db_path, sid)) >= 1


def test_reap_null_status_sessions_stale_live_recorded_pid_not_reaped(tmp_path, monkeypatch):
    """A stale null-status session with a LIVE recorded pid is not reaped.

    Isolates the recorded-pid path from the staleness grace: the row is old
    enough that the grace no longer protects it, so only honoring the live
    ``node_metadata.pid`` keeps it alive. If liveness ever stops reading
    ``node_metadata``, this stale row reaps and the test fails.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_time = time.time() - 7200  # past the 1h grace
    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            started_at=stale_time,
            updated_at=stale_time,
            node_metadata={"pid": os.getpid()},
        )
    )

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] is None


def test_reap_null_status_sessions_fresh_unknown_liveness_not_reaped(tmp_path, monkeypatch):
    """A fresh null-status session with unknown liveness is skipped within the grace period.

    No recorded pid, no artifacts/pidfile, session id not in the ps snapshot ->
    liveness is unknown (None). A recent updated_at keeps it inside the
    staleness grace, so it must not be reaped yet.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            started_at=time.time() - 5,
            updated_at=time.time() - 5,
        )
    )

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] is None


def test_reap_null_status_sessions_lost_cas_does_not_stamp_ended_at(tmp_path, monkeypatch):
    """A lost CAS race on the status write must not leave ended_at stamped
    with status still NULL — that mismatch is exactly the "status and
    ended_at disagree" defect: a reader trusting either field alone is wrong.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_time = time.time() - 7200  # past default 1h grace
    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            started_at=stale_time,
            updated_at=stale_time,
        )
    )

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    async def _lost_race(*_a, **_k):
        return False

    monkeypatch.setattr(StateDB, "update_status", _lost_race)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess is not None
    assert sess["status"] is None
    assert sess["ended_at"] is None


def test_reap_null_status_sessions_write_is_atomic_with_ended_at(tmp_path, monkeypatch):
    """The winning transition must stamp ended_at in the same write as the
    status change, not depend on a follow-up update_session() call. Proven
    by making update_session() raise: the reaper must never call it at all
    for this transition."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale_time = time.time() - 7200
    sid = run_async(
        _seed_session(
            db_path,
            status=None,
            artifacts_path=None,
            started_at=stale_time,
            updated_at=stale_time,
        )
    )

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    async def _boom(*_a, **_k):
        raise RuntimeError("update_session must not be called by this reaper")

    monkeypatch.setattr(StateDB, "update_session", _boom)

    from lionagi.studio.services.lifecycle import reap_null_status_sessions

    count = run_async(reap_null_status_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "failed"
    assert sess["ended_at"] is not None


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


def test_reap_phantom_sessions_populates_duration_ms(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_artifacts_duration")
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

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "failed"
    assert sess["duration_ms"] == pytest.approx((sess["ended_at"] - stale_time) * 1000)


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


def test_reap_phantom_sessions_lost_cas_does_not_stamp_ended_at(tmp_path, monkeypatch):
    """A lost CAS race on the status write must not leave ended_at stamped
    while status is still "running" — that mismatch is exactly the
    "status and ended_at disagree" defect from issue #2844.
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_artifacts_lost_race")
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

    async def _lost_race(*_a, **_k):
        return False

    monkeypatch.setattr(StateDB, "update_status", _lost_race)

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 0

    sess = run_async(_get_session(db_path, sid))
    assert sess is not None
    assert sess["status"] == "running"
    assert sess["ended_at"] is None


def test_reap_phantom_sessions_write_is_atomic_with_ended_at(tmp_path, monkeypatch):
    """The winning transition must stamp ended_at in the same write as the
    status change, not depend on a follow-up update_session() call that
    could independently fail and leave status="failed" with ended_at=None
    while `reaped` still counts the row. Proven by making update_session()
    raise: the reaper must never call it at all for this transition (covers
    both the generic-failed and mirror-idle-completed branches, which share
    the same extra_fields construction)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost_artifacts_atomic")
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

    async def _boom(*_a, **_k):
        raise RuntimeError("update_session must not be called by this reaper")

    monkeypatch.setattr(StateDB, "update_session", _boom)

    from lionagi.studio.services.lifecycle import reap_phantom_sessions

    count = run_async(reap_phantom_sessions(stale_hours=1.0))
    assert count == 1

    sess = run_async(_get_session(db_path, sid))
    assert sess["status"] == "failed"
    assert sess["ended_at"] is not None


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


def test_zero_session_reaper_skips_sessionless_kind(tmp_path, monkeypatch):
    """A 'command' run never opens a session, so a zero session count is its
    normal steady state and must not be read as a stuck launch."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale = time.time() - 600  # past the 300s grace
    iid = run_async(_seed_scheduled_invocation(db_path, action_kind="command", updated_at=stale))

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 0

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "running"
    assert run_async(_count_reaping_transitions(db_path, iid)) == 0


def test_zero_session_reaper_still_reaps_session_bearing_kind(tmp_path, monkeypatch):
    """The skip is scoped to session-less kinds — an 'agent' run that spawned
    nothing past the grace period is still reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    stale = time.time() - 600
    iid = run_async(_seed_scheduled_invocation(db_path, action_kind="agent", updated_at=stale))

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 1

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "timed_out"


def test_sessionless_kind_still_bound_by_wall_clock_deadline(tmp_path, monkeypatch):
    """Only the zero-session heuristic is off for session-less kinds; a
    'command' run past the wall-clock deadline is still reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    iid = run_async(
        _seed_scheduled_invocation(
            db_path,
            action_kind="command",
            started_at=time.time() - 8000,  # past the 7200s deadline
            updated_at=time.time(),
        )
    )

    from lionagi.studio.services.lifecycle import reap_stale_invocations

    count = run_async(reap_stale_invocations(deadline_seconds=7200, zero_session_grace_seconds=300))
    assert count == 1

    inv = run_async(_get_invocation(db_path, iid))
    assert inv["status"] == "timed_out"
