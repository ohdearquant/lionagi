# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Studio show-level staleness reaper."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

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


async def _seed_show(
    db_path: Path,
    show_dir: Path,
    *,
    show_id: str | None = None,
    status: str = "active",
    updated_at: float | None = None,
) -> str:
    sid = show_id or str(uuid.uuid4())
    async with StateDB(db_path) as db:
        await db.create_show(
            {
                "id": sid,
                "topic": f"topic-{sid[:8]}",
                "show_dir": str(show_dir),
                "status": status,
            }
        )
        if updated_at is not None:
            await db.execute(
                "UPDATE shows SET updated_at = ? WHERE id = ?",
                (updated_at, sid),
            )
    return sid


async def _seed_play_row(
    db_path: Path,
    show_id: str,
    *,
    play_id: str | None = None,
    status: str = "merged",
    session_id: str | None = None,
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
                "started_at": now,
            }
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


async def _get_show(db_path: Path, show_id: str) -> dict | None:
    async with StateDB(db_path) as db:
        return await db.get_show(show_id)


async def _count_transitions(db_path: Path, entity_id: str) -> int:
    async with StateDB(db_path) as db:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
        return row["n"] if row else 0


def _write_play_meta(show_dir: Path, name: str, *, status: str) -> None:
    play_dir = show_dir / name
    play_dir.mkdir(parents=True, exist_ok=True)
    (play_dir / "_meta.json").write_text(json.dumps({"status": status}))


_STALE = time.time() - 100 * 3600  # 100h ago, well past any reasonable stale_hours


# ── all-merged-on-disk reap ───────────────────────────────────────────────────


def test_reap_stale_shows_all_merged_reaped(tmp_path, monkeypatch):
    """A stale non-terminal show whose every on-disk play reached 'merged' is
    reaped to 'completed' via the same rule import_shows() uses at creation."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "my-topic"
    _write_play_meta(show_dir, "play-one", status="merged")
    _write_play_meta(show_dir, "play-two", status="merged")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 1

    show = run_async(_get_show(db_path, show_id))
    assert show is not None
    assert show["status"] == "completed"
    assert show["status_reason_code"] == "show.completed.all_plays_merged"
    assert run_async(_count_transitions(db_path, show_id)) >= 1


# ── abort marker reap ─────────────────────────────────────────────────────────


def test_reap_stale_shows_abort_marker_reaped(tmp_path, monkeypatch):
    """A stale non-terminal show with an on-disk _ABORT marker is reaped to
    'aborted', regardless of play state."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "aborted-topic"
    show_dir.mkdir(parents=True)
    (show_dir / "_ABORT").write_text("")
    _write_play_meta(show_dir, "play-one", status="running")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 1

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "aborted"
    assert show["status_reason_code"] == "show.aborted.operator"


# ── still-in-flight-on-disk: nothing to reap ──────────────────────────────────


def test_reap_stale_shows_skips_still_in_flight_on_disk(tmp_path, monkeypatch):
    """A stale show whose on-disk plays are not all merged and carries no abort
    marker or passing verdict is left alone — genuinely still in flight."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "in-flight-topic"
    _write_play_meta(show_dir, "play-one", status="merged")
    _write_play_meta(show_dir, "play-two", status="running")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 0

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "active"


# ── liveness-first ─────────────────────────────────────────────────────────────


def test_reap_stale_shows_skips_live_child_session(tmp_path, monkeypatch):
    """A show with a child play whose session process is still alive is never
    reaped, even when the on-disk snapshot looks fully merged and ancient."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "live-topic"
    _write_play_meta(show_dir, "play-one", status="merged")
    _monkey_db(monkeypatch, db_path)

    session_id = run_async(_seed_session(db_path, node_metadata={"pid": 1}))
    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))
    run_async(_seed_play_row(db_path, show_id, status="merged", session_id=session_id))

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: True)

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 0

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "active"


def test_reap_stale_shows_reaps_dead_child_session(tmp_path, monkeypatch):
    """A show whose linked child session is confirmed dead (not merely absent)
    is reaped once stale and the on-disk plays are all merged."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "dead-session-topic"
    _write_play_meta(show_dir, "play-one", status="merged")
    _monkey_db(monkeypatch, db_path)

    session_id = run_async(_seed_session(db_path))
    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))
    run_async(_seed_play_row(db_path, show_id, status="merged", session_id=session_id))

    import lionagi.studio.services.lifecycle as lc_mod

    monkeypatch.setattr(lc_mod, "process_liveness", lambda *_a, **_k: False)

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 1

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "completed"


def test_reap_stale_shows_skips_live_unlinked_play_pid(tmp_path, monkeypatch):
    """A child process may be live before its play row is linked to a session."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "live-unlinked-topic"
    play_id = str(uuid.uuid4())
    play_name = f"play-{play_id[:8]}"
    _write_play_meta(show_dir, play_name, status="running")
    (show_dir / play_name / ".pid").write_text(str(os.getpid()))
    (show_dir / "_ABORT").write_text("")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))
    run_async(_seed_play_row(db_path, show_id, play_id=play_id, session_id=None))

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 0

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "active"


def test_reap_stale_shows_reaps_dead_unlinked_play_pid(tmp_path, monkeypatch):
    """A dead unlinked play PID does not block an otherwise reapable show."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "dead-unlinked-topic"
    play_id = str(uuid.uuid4())
    play_name = f"play-{play_id[:8]}"
    _write_play_meta(show_dir, play_name, status="running")
    proc = subprocess.Popen(["/bin/sleep", "0"])  # noqa: S603
    proc.wait()
    (show_dir / play_name / ".pid").write_text(str(proc.pid))
    (show_dir / "_ABORT").write_text("")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))
    run_async(_seed_play_row(db_path, show_id, play_id=play_id, session_id=None))

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 1

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "aborted"


# ── already-terminal: never reaped ────────────────────────────────────────────


def test_reap_stale_shows_skips_already_terminal(tmp_path, monkeypatch):
    """A show already in a terminal status is left untouched, regardless of
    on-disk state — it is outside the reapable status set entirely."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "terminal-topic"
    _write_play_meta(show_dir, "play-one", status="merged")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, status="completed", updated_at=_STALE))

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 0

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "completed"


# ── staleness grace ────────────────────────────────────────────────────────────


def test_reap_stale_shows_skips_fresh(tmp_path, monkeypatch):
    """A fresh (recently updated) show is not reaped even if fully merged on
    disk — the staleness window gives it the benefit of the doubt."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "fresh-topic"
    _write_play_meta(show_dir, "play-one", status="merged")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, updated_at=time.time()))

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 0

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "active"


# ── CAS / version guard ───────────────────────────────────────────────────────


def test_reap_stale_shows_version_guard_skips_claimed_row(tmp_path, monkeypatch):
    """A stale, reapable show that gets re-touched (re-imported/re-touched
    updated_at) after the reaper's fresh re-read validated it, but before the
    guarded write lands, must not be clobbered. The expected_updated_at
    optimistic-lock guard pins the transition to the exact row version
    validated, so the bumped updated_at defeats the write."""
    db_path = tmp_path / "state.db"
    show_dir = tmp_path / "shows" / "claimed-topic"
    _write_play_meta(show_dir, "play-one", status="merged")
    _monkey_db(monkeypatch, db_path)

    show_id = run_async(_seed_show(db_path, show_dir, updated_at=_STALE))

    import lionagi.state.db as state_db_mod

    original_update_status = state_db_mod.StateDB.update_status
    claimed = {"done": False}

    async def _claim_then_update(self, entity_type, entity_id, **kwargs):
        if entity_type == "show" and entity_id == show_id and not claimed["done"]:
            claimed["done"] = True
            await self.execute(
                "UPDATE shows SET updated_at = ? WHERE id = ?",
                (time.time(), show_id),
            )
        return await original_update_status(self, entity_type, entity_id, **kwargs)

    monkeypatch.setattr(state_db_mod.StateDB, "update_status", _claim_then_update)

    from lionagi.studio.services.lifecycle import reap_stale_shows

    count = run_async(reap_stale_shows(stale_hours=6.0))
    assert count == 0

    show = run_async(_get_show(db_path, show_id))
    assert show["status"] == "active"


# ── wiring ───────────────────────────────────────────────────────────────────────


def test_run_startup_reconciliation_includes_stale_shows_key(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    from lionagi.studio.services.lifecycle import run_startup_reconciliation

    results = run_async(run_startup_reconciliation())
    assert "stale_shows" in results
    assert isinstance(results["stale_shows"], int)


def test_run_periodic_reapers_includes_stale_shows_key(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    from lionagi.studio.services.lifecycle import run_periodic_reapers

    results = run_async(run_periodic_reapers())
    assert "stale_shows" in results
    assert isinstance(results["stale_shows"], int)
