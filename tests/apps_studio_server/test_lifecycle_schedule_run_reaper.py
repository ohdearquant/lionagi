# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the schedule_runs staleness reaper.

A schedule_run row has no process-liveness signal to check against (the
"process" is the scheduler daemon itself, and its own restart is what
triggers reaping), so this reaper is a pure wall-clock deadline against the
row's own updated_at/fired_at, guarded by the same optimistic-lock
(expected_updated_at) pattern reap_stale_plays uses.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB

from ._helpers import run_async


def _monkey_db(monkeypatch, db_path: Path) -> None:
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.lifecycle as lifecycle_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(lifecycle_mod, "DEFAULT_DB_PATH", db_path)


async def _seed_schedule(db_path: Path, *, schedule_id: str | None = None) -> str:
    sid = schedule_id or str(uuid.uuid4())
    async with StateDB(db_path) as db:
        await db.create_schedule(
            {
                "id": sid,
                "name": f"sched-{sid[:8]}",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "agent",
                "action_model": "gpt-4.1-mini",
                "action_prompt": "ping",
            }
        )
    return sid


async def _seed_schedule_run(
    db_path: Path,
    schedule_id: str,
    *,
    run_id: str | None = None,
    status: str = "running",
    fired_at: float | None = None,
    updated_at: float | None = None,
) -> str:
    rid = run_id or str(uuid.uuid4())
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_schedule_run(
            {
                "id": rid,
                "schedule_id": schedule_id,
                "trigger_context": {"source": "cron"},
                "action_kind": "agent",
                "action_args": {"prompt": "ping"},
                "status": status,
                "fired_at": fired_at or now,
            }
        )
        if updated_at is not None:
            await db.execute(
                "UPDATE schedule_runs SET updated_at = ? WHERE id = ?",
                (updated_at, rid),
            )
    return rid


async def _get_schedule_run(db_path: Path, run_id: str) -> dict | None:
    async with StateDB(db_path) as db:
        return await db.get_schedule_run(run_id)


_STALE = time.time() - 100 * 3600  # 100h ago, well past any reasonable stale_hours


def test_reap_stale_schedule_runs_transitions_stuck_running_row(tmp_path, monkeypatch):
    """(d) A schedule_run stuck at status='running' with an old updated_at
    is transitioned to 'timed_out' -- the terminal status a crashed-mid-fire
    occurrence should land in once the daemon restarts and the reaper scans."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_schedule(db_path))
    run_id = run_async(_seed_schedule_run(db_path, sid, status="running", updated_at=_STALE))

    from lionagi.studio.services.lifecycle import reap_stale_schedule_runs

    count = run_async(reap_stale_schedule_runs(stale_hours=6.0))
    assert count == 1

    run = run_async(_get_schedule_run(db_path, run_id))
    assert run is not None
    assert run["status"] == "timed_out"
    assert run["status_reason_code"] == "run.timed_out.deadline"


def test_reap_stale_schedule_runs_skips_fresh_running_row(tmp_path, monkeypatch):
    """A running row updated recently (well inside the stale window) is left
    alone -- it may still be legitimately in flight."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_schedule(db_path))
    run_id = run_async(_seed_schedule_run(db_path, sid, status="running", updated_at=time.time()))

    from lionagi.studio.services.lifecycle import reap_stale_schedule_runs

    count = run_async(reap_stale_schedule_runs(stale_hours=6.0))
    assert count == 0

    run = run_async(_get_schedule_run(db_path, run_id))
    assert run["status"] == "running"


def test_reap_stale_schedule_runs_skips_terminal_status(tmp_path, monkeypatch):
    """A row already in a terminal status is outside the reapable set and
    left untouched, even if stale by wall clock."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_schedule(db_path))
    run_id = run_async(_seed_schedule_run(db_path, sid, status="completed", updated_at=_STALE))

    from lionagi.studio.services.lifecycle import reap_stale_schedule_runs

    count = run_async(reap_stale_schedule_runs(stale_hours=6.0))
    assert count == 0

    run = run_async(_get_schedule_run(db_path, run_id))
    assert run["status"] == "completed"


def test_reap_stale_schedule_runs_falls_back_to_fired_at_when_never_touched(tmp_path, monkeypatch):
    """A row that was inserted and never subsequently touched has
    updated_at=NULL (create_schedule_run's INSERT does not set it) -- the
    reaper must fall back to fired_at rather than treating a NULL
    updated_at as "just touched, not stale"."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_schedule(db_path))
    run_id = run_async(_seed_schedule_run(db_path, sid, status="running", fired_at=_STALE))

    run_before = run_async(_get_schedule_run(db_path, run_id))
    assert run_before["updated_at"] is None

    from lionagi.studio.services.lifecycle import reap_stale_schedule_runs

    count = run_async(reap_stale_schedule_runs(stale_hours=6.0))
    assert count == 1

    run = run_async(_get_schedule_run(db_path, run_id))
    assert run["status"] == "timed_out"


def test_reap_stale_schedule_runs_version_guard_skips_row_touched_between_scan_and_write(
    tmp_path, monkeypatch
):
    """A row that is legitimately touched (e.g. its terminal write lands)
    between the reaper's scan and its guarded write must not be
    clobbered -- update_status()'s expected_updated_at optimistic-lock
    guard sees the row's updated_at no longer matches the snapshot the
    reaper scanned and skips the transition, mirroring
    reap_stale_plays_cas_guard_skips_concurrently_transitioned_row."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    sid = run_async(_seed_schedule(db_path))
    run_id = run_async(_seed_schedule_run(db_path, sid, status="running", updated_at=_STALE))

    import lionagi.state.db as state_db_mod

    original_update_status = state_db_mod.StateDB.update_status
    flipped = {"done": False}

    async def _flip_then_call(self, entity_type, entity_id, **kwargs):
        if entity_type == "schedule_run" and entity_id == run_id and not flipped["done"]:
            flipped["done"] = True
            # Simulate a concurrent legitimate write landing first (e.g.
            # the run's own terminal update_schedule_run() call) -- bumps
            # updated_at so the reaper's stale snapshot is now out of date.
            await self.execute(
                "UPDATE schedule_runs SET status = 'completed', updated_at = ? WHERE id = ?",
                (time.time(), entity_id),
            )
        return await original_update_status(self, entity_type, entity_id, **kwargs)

    monkeypatch.setattr(state_db_mod.StateDB, "update_status", _flip_then_call)

    from lionagi.studio.services.lifecycle import reap_stale_schedule_runs

    count = run_async(reap_stale_schedule_runs(stale_hours=6.0))
    assert count == 0

    run = run_async(_get_schedule_run(db_path, run_id))
    assert run["status"] == "completed"


def test_run_startup_reconciliation_includes_stale_schedule_runs_key(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    from lionagi.studio.services.lifecycle import run_startup_reconciliation

    results = run_async(run_startup_reconciliation())
    assert "stale_schedule_runs" in results
    assert isinstance(results["stale_schedule_runs"], int)


def test_run_periodic_reapers_includes_stale_schedule_runs_key(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    from lionagi.studio.services.lifecycle import run_periodic_reapers

    results = run_async(run_periodic_reapers())
    assert "stale_schedule_runs" in results
    assert isinstance(results["stale_schedule_runs"], int)
