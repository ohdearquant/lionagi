# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Studio play-level staleness reaper."""

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


async def _seed_show(db_path: Path, *, show_id: str | None = None, status: str = "active") -> str:
    sid = show_id or str(uuid.uuid4())
    async with StateDB(db_path) as db:
        await db.create_show(
            {
                "id": sid,
                "topic": f"topic-{sid[:8]}",
                "show_dir": f"/tmp/show-{sid[:8]}",
                "status": status,
            }
        )
    return sid


async def _seed_play(
    db_path: Path,
    show_id: str,
    *,
    play_id: str | None = None,
    status: str = "running",
    session_id: str | None = None,
    updated_at: float | None = None,
    started_at: float | None = None,
) -> str:
    pid = play_id or str(uuid.uuid4())
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_play(
            {
                "id": pid,
                "show_id": show_id,
                "name": f"play-{pid[:8]}",
                "status": status,
                "session_id": session_id,
                "started_at": started_at or now,
            }
        )
        if updated_at is not None:
            await db.execute(
                "UPDATE plays SET updated_at = ? WHERE id = ?",
                (updated_at, pid),
            )
    return pid


async def _seed_session(
    db_path: Path,
    *,
    session_id: str | None = None,
    node_metadata: dict | None = None,
) -> str:
    sid = session_id or str(uuid.uuid4())
    async with StateDB(db_path) as db:
        prog_id = str(uuid.uuid4())
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog_id,
                "name": "test-session",
                "status": "running",
                "started_at": time.time(),
                "node_metadata": node_metadata,
            }
        )
    return sid


async def _get_play(db_path: Path, play_id: str) -> dict | None:
    async with StateDB(db_path) as db:
        return await db.get_play(play_id)


async def _count_transitions(db_path: Path, entity_id: str) -> int:
    async with StateDB(db_path) as db:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
        return row["n"] if row else 0


_STALE = time.time() - 100 * 3600  # 100h ago, well past any reasonable stale_hours


# ── orphan reap: the regression class the manual `li kill --stale` skips ────────


@pytest.mark.parametrize("status", ["running", "running_complete", "redoing", "prepared"])
def test_reap_stale_plays_orphan_reaped(tmp_path, monkeypatch, status):
    """Orphaned (session_id=NULL) play in a reapable status, stale, is reaped to blocked.

    Covers all four reapable statuses — including the three the manual `li kill
    --stale` path misses (only ever queried status='running').
    """
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status=status, session_id=None, updated_at=_STALE)
    )

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 1

    play = run_async(_get_play(db_path, play_id))
    assert play is not None
    assert play["status"] == "blocked"
    assert play["status_reason_code"] == "run.cancelled.stale_auto"
    assert play["ended_at"] is not None
    assert run_async(_count_transitions(db_path, play_id)) >= 1


# ── never-reap statuses ────────────────────────────────────────────────────────


def test_reap_stale_plays_never_reaps_gated(tmp_path, monkeypatch):
    """A stale orphaned play in 'gated' status is never reaped (legit long-lived pause)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status="gated", session_id=None, updated_at=_STALE)
    )

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 0

    play = run_async(_get_play(db_path, play_id))
    assert play["status"] == "gated"


def test_reap_stale_plays_never_reaps_pending(tmp_path, monkeypatch):
    """A stale orphaned play in 'pending' status is never reaped (may be waiting on depends_on)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status="pending", session_id=None, updated_at=_STALE)
    )

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 0

    play = run_async(_get_play(db_path, play_id))
    assert play["status"] == "pending"


def test_reap_stale_plays_skips_terminal(tmp_path, monkeypatch):
    """A terminal play ('merged') is left untouched — outside the reapable status set."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status="merged", session_id=None, updated_at=_STALE)
    )

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 0

    play = run_async(_get_play(db_path, play_id))
    assert play["status"] == "merged"


# ── liveness-first ──────────────────────────────────────────────────────────────


def test_reap_stale_plays_skips_live_child_session(tmp_path, monkeypatch):
    """A play with a live child session is never reaped, even with an ancient updated_at."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    session_id = run_async(_seed_session(db_path, node_metadata={"pid": 1}))
    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status="running", session_id=session_id, updated_at=_STALE)
    )

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: True)

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 0

    play = run_async(_get_play(db_path, play_id))
    assert play["status"] == "running"


def test_reap_stale_plays_reaps_dead_child_session(tmp_path, monkeypatch):
    """A play with a confirmed-dead (not merely absent) child session is reaped once stale."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    session_id = run_async(_seed_session(db_path))
    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status="running", session_id=session_id, updated_at=_STALE)
    )

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 1

    play = run_async(_get_play(db_path, play_id))
    assert play["status"] == "blocked"


# ── staleness grace ──────────────────────────────────────────────────────────────


def test_reap_stale_plays_skips_fresh(tmp_path, monkeypatch):
    """A fresh (recently updated) orphaned running play is not reaped."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status="running", session_id=None, updated_at=time.time())
    )

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 0

    play = run_async(_get_play(db_path, play_id))
    assert play["status"] == "running"


# ── wiring ───────────────────────────────────────────────────────────────────────


def test_run_startup_reconciliation_includes_stale_plays_key(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    from lionagi.studio.services.lifecycle import run_startup_reconciliation

    results = run_async(run_startup_reconciliation())
    assert "stale_plays" in results
    assert isinstance(results["stale_plays"], int)


def test_run_periodic_reapers_includes_stale_plays_key(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    from lionagi.studio.services.lifecycle import run_periodic_reapers

    results = run_async(run_periodic_reapers())
    assert "stale_plays" in results
    assert isinstance(results["stale_plays"], int)


# ── CAS guard ───────────────────────────────────────────────────────────────────


def test_reap_stale_plays_cas_guard_skips_concurrently_transitioned_row(tmp_path, monkeypatch):
    """A play that flips to a terminal status between the reaper's fetch and its
    write (simulated by mutating the row directly inside a patched
    ``StateDB.update_status``) is not double-counted — the CAS ``expected_statuses``
    guard sees the new status and skips the write."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path))
    play_id = run_async(
        _seed_play(db_path, show_id, status="running", session_id=None, updated_at=_STALE)
    )

    import lionagi.state.db as state_db_mod

    original_update_status = state_db_mod.StateDB.update_status
    flipped = {"done": False}

    async def _flip_then_call(self, entity_type, entity_id, **kwargs):
        if entity_type == "play" and entity_id == play_id and not flipped["done"]:
            flipped["done"] = True
            await self.execute("UPDATE plays SET status = 'merged' WHERE id = ?", (entity_id,))
        return await original_update_status(self, entity_type, entity_id, **kwargs)

    monkeypatch.setattr(state_db_mod.StateDB, "update_status", _flip_then_call)

    from lionagi.studio.services.lifecycle import reap_stale_plays

    count = run_async(reap_stale_plays(stale_hours=6.0))
    assert count == 0

    play = run_async(_get_play(db_path, play_id))
    assert play["status"] == "merged"
