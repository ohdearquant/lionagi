# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ``li state`` maintenance subcommands: ``stats``,
``checkpoint``, ``vacuum``, ``prune``, and the ``ls --limit / --status``
filter/pagination logic.

These commands ship in commit ``d1269eebd`` as the operational tools
that turn a growing ``state.db`` from a leak into a manageable artifact.
Every test points the default DB at a temp file and seeds rows
directly via ``StateDB`` so there are no real CLI / API dependencies.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.state import (
    _checkpoint,
    _doctor,
    _format_bytes,
    _list_sessions,
    _print_stats,
    _prune,
    _vacuum,
)
from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp file DB. ``li state`` commands open StateDB() without
    arguments — they read DEFAULT_DB_PATH, so we patch that.
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_session(
    db: StateDB,
    *,
    name: str | None = None,
    status: str | None = None,
    updated_at: float | None = None,
) -> str:
    sid = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "name": name,
            "status": status,
            "started_at": time.time(),
        }
    )
    if updated_at is not None:
        await db.db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (updated_at, sid),
        )
        await db.db.commit()
    return sid


async def _seed_session_with_messages(
    db: StateDB,
    *,
    n_messages: int = 3,
    status: str = "completed",
    updated_at: float | None = None,
) -> tuple[str, list[str]]:
    """Create a session + branch + N messages threaded through both
    branch and session progressions. Returns (session_id, msg_ids).
    """
    sid = str(uuid.uuid4())
    bid = str(uuid.uuid4())
    spid = str(uuid.uuid4())
    bpid = str(uuid.uuid4())
    await db.create_progression(spid)
    await db.create_progression(bpid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": spid,
            "status": status,
            "started_at": time.time(),
        }
    )
    await db.create_branch(
        {
            "id": bid,
            "session_id": sid,
            "progression_id": bpid,
        }
    )
    msg_ids = []
    for i in range(n_messages):
        mid = str(uuid.uuid4())
        await db.insert_message(
            {
                "id": mid,
                "created_at": time.time(),
                "node_metadata": {},
                "content": {"text": f"msg-{i}"},
                "role": "user",
                "sender": "u",
                "recipient": "x",
                "channel": "test",
            }
        )
        await db.append_to_progression(bpid, mid)
        await db.append_to_progression(spid, mid)
        msg_ids.append(mid)
    if updated_at is not None:
        await db.db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (updated_at, sid),
        )
        await db.db.commit()
    return sid, msg_ids


# ── _format_bytes ─────────────────────────────────────────────────────────────


def test_format_bytes_handles_each_unit():
    assert _format_bytes(0).endswith("B")
    assert "KiB" in _format_bytes(2 * 1024)
    assert "MiB" in _format_bytes(2 * 1024 * 1024)
    assert "GiB" in _format_bytes(2 * 1024 * 1024 * 1024)
    assert "TiB" in _format_bytes(2 * 1024**4)


# ── _list_sessions (li state ls) ──────────────────────────────────────────────


async def test_ls_prints_empty_message_when_no_sessions(
    temp_db_path: Path,
    capsys: pytest.CaptureFixture,
):
    # Ensure the DB exists so the function doesn't bail before the
    # "(no sessions in state.db)" message.
    async with StateDB():
        pass
    await _list_sessions(limit=50, status=None)
    out = capsys.readouterr().out
    assert "(no sessions in state.db)" in out


async def test_ls_lists_seeded_sessions(
    temp_db_path: Path,
    capsys: pytest.CaptureFixture,
):
    async with StateDB() as db:
        sid = await _seed_session(
            db, name="foo", status="running", updated_at=time.time()
        )

    await _list_sessions(limit=50, status=None)
    out = capsys.readouterr().out
    assert sid in out
    assert "foo" in out
    assert "running" in out


async def test_ls_limit_caps_results(
    temp_db_path: Path,
    capsys: pytest.CaptureFixture,
):
    async with StateDB() as db:
        for i in range(5):
            await _seed_session(
                db, name=f"s{i}", status="completed", updated_at=time.time() - i
            )

    await _list_sessions(limit=2, status=None)
    out = capsys.readouterr().out
    # Three of the five names should NOT appear when limit=2.
    appearing = sum(1 for i in range(5) if f"s{i}" in out)
    assert appearing == 2


async def test_ls_status_filter(
    temp_db_path: Path,
    capsys: pytest.CaptureFixture,
):
    async with StateDB() as db:
        await _seed_session(
            db, name="finished", status="completed", updated_at=time.time()
        )
        await _seed_session(db, name="open", status="running", updated_at=time.time())

    await _list_sessions(limit=50, status="completed")
    out = capsys.readouterr().out
    assert "finished" in out
    assert "open" not in out


# ── _print_stats (li state stats) ─────────────────────────────────────────────


async def test_stats_reports_no_db_message_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """When state.db does not yet exist, stats prints a helpful hint
    instead of crashing.
    """
    db_path = tmp_path / "never_created.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    await _print_stats()
    out = capsys.readouterr().out
    assert "no state.db yet" in out


async def test_stats_reports_row_counts_and_pragmas(
    temp_db_path: Path,
    capsys: pytest.CaptureFixture,
):
    async with StateDB() as db:
        await _seed_session_with_messages(db, n_messages=2)
        await _seed_session_with_messages(db, n_messages=1, status="running")

    await _print_stats()
    out = capsys.readouterr().out
    # Path + sizes
    assert "state.db path:" in out
    assert "state.db size:" in out
    assert "state.db-wal:" in out
    # Row counts
    assert "Row counts:" in out
    assert "messages" in out
    assert "sessions" in out
    assert "branches" in out
    # Status distribution
    assert "Sessions by status:" in out
    # PRAGMAs
    assert "PRAGMAs:" in out
    assert "journal_mode" in out
    assert "wal_autocheckpoint" in out
    assert "busy_timeout" in out


# ── _checkpoint (li state checkpoint) ─────────────────────────────────────────


async def test_checkpoint_returns_summary_string(temp_db_path: Path):
    # Seed something so the WAL has frames to checkpoint.
    async with StateDB() as db:
        await _seed_session_with_messages(db, n_messages=2)

    result = await _checkpoint("PASSIVE")
    assert "busy=" in result
    assert "log_pages=" in result
    assert "checkpointed=" in result


@pytest.mark.parametrize("mode", ["PASSIVE", "FULL", "RESTART", "TRUNCATE"])
async def test_checkpoint_each_mode_runs(temp_db_path: Path, mode: str):
    async with StateDB() as db:
        await _seed_session_with_messages(db, n_messages=1)

    result = await _checkpoint(mode)
    # All four modes must return the three-field summary.
    assert "busy=" in result
    assert "log_pages=" in result


# ── _vacuum (li state vacuum) ─────────────────────────────────────────────────


async def test_vacuum_runs_without_error(temp_db_path: Path):
    async with StateDB() as db:
        await _seed_session_with_messages(db, n_messages=3)

    # MUST NOT raise — VACUUM holds an exclusive lock for the duration.
    await _vacuum()

    # Verify the DB is still usable after VACUUM.
    async with StateDB() as db:
        cur = await db.db.execute("SELECT COUNT(*) AS n FROM sessions")
        n = (await cur.fetchone())["n"]
    assert n == 1


# ── _prune (li state prune) ───────────────────────────────────────────────────


async def test_prune_dry_run_does_not_delete(temp_db_path: Path):
    """Dry-run returns counts but leaves rows in place."""
    now = time.time()
    old_ts = now - (60 * 86400)  # 60 days ago
    async with StateDB() as db:
        old_sid, _ = await _seed_session_with_messages(
            db,
            n_messages=2,
            updated_at=old_ts,
        )
        new_sid, _ = await _seed_session_with_messages(
            db,
            n_messages=1,
            updated_at=now,
        )

    result = await _prune(keep_days=30, keep_n=1, dry_run=True)
    assert result["sessions"] >= 1
    assert result["messages"] == 0  # dry-run never previews messages

    async with StateDB() as db:
        assert (await db.get_session(old_sid)) is not None
        assert (await db.get_session(new_sid)) is not None


async def test_prune_deletes_old_sessions_and_cascades_branches(
    temp_db_path: Path,
):
    """The real prune deletes old sessions and cascade-drops branches.

    Message sweep semantics: messages are dropped only when NO
    progression anywhere references them. Today the deleted session's
    progression row is NOT FK-cascaded (sessions.progression_id has no
    ON DELETE CASCADE), so its messages remain referenced via the
    orphaned progression — and the sweep is effectively a no-op in
    this scenario. Documented as the current behavior; if a future
    cascade or progression-sweep is added, this assertion will catch
    the change.
    """
    now = time.time()
    old_ts = now - (60 * 86400)
    async with StateDB() as db:
        old_sid, old_msgs = await _seed_session_with_messages(
            db,
            n_messages=3,
            updated_at=old_ts,
        )
        new_sid, new_msgs = await _seed_session_with_messages(
            db,
            n_messages=2,
            updated_at=now,
        )

    result = await _prune(keep_days=30, keep_n=1, dry_run=False)
    assert result["sessions"] == 1
    # branches were cascaded from the deleted session.
    assert result["branches"] == 1
    # Current behavior: orphan progression keeps msgs alive, sweep no-ops.
    assert result["messages"] == 0

    async with StateDB() as db:
        assert (await db.get_session(old_sid)) is None
        assert (await db.get_session(new_sid)) is not None
        # The branch row for the old session is gone (FK cascade).
        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?",
            (old_sid,),
        )
        assert (await cur.fetchone())["n"] == 0
        # Surviving session's branches are intact.
        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?",
            (new_sid,),
        )
        assert (await cur.fetchone())["n"] == 1


async def test_prune_keeps_n_most_recent_even_when_old(temp_db_path: Path):
    """``--keep-n`` always preserves the N most-recent sessions, even
    if they're older than ``--keep-days``.
    """
    now = time.time()
    old_ts = now - (60 * 86400)
    async with StateDB() as db:
        # All three sessions are OLD.
        s1 = await _seed_session(
            db, name="oldest", status="completed", updated_at=old_ts - 100
        )
        s2 = await _seed_session(
            db, name="middle", status="completed", updated_at=old_ts - 50
        )
        s3 = await _seed_session(
            db, name="newest_old", status="completed", updated_at=old_ts
        )

    # keep_n=2: must preserve the 2 most recent (s2, s3).
    result = await _prune(keep_days=30, keep_n=2, dry_run=False)
    assert result["sessions"] == 1

    async with StateDB() as db:
        assert (await db.get_session(s1)) is None
        assert (await db.get_session(s2)) is not None
        assert (await db.get_session(s3)) is not None


async def test_prune_with_nothing_to_delete_returns_zero(temp_db_path: Path):
    """If no sessions match the prune criteria, the result is all zeros
    and no rows are touched.
    """
    now = time.time()
    async with StateDB() as db:
        sid = await _seed_session(db, name="recent", status="completed", updated_at=now)

    result = await _prune(keep_days=30, keep_n=10, dry_run=False)
    assert result == {"sessions": 0, "branches": 0, "messages": 0}

    async with StateDB() as db:
        assert (await db.get_session(sid)) is not None


# ── _doctor (li state doctor) — R5-A MED-2 ───────────────────────────────────


async def test_doctor_dry_run_does_not_modify_status(temp_db_path: Path):
    """``_doctor --dry-run`` reports which sessions WOULD be swept but
    leaves status='running' untouched.
    """
    now = time.time()
    old = now - (48 * 3600)
    async with StateDB() as db:
        stale = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (old, stale),
        )
        await db.db.commit()
        recent = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (now, recent),
        )
        await db.db.commit()

    result = await _doctor(stale_hours=24, dry_run=True)
    assert result["running"] == 2
    assert result["swept"] == 1
    assert result["skipped"] == 1

    async with StateDB() as db:
        s_stale = await db.get_session(stale)
        s_recent = await db.get_session(recent)
    # Both still 'running' — dry run did nothing.
    assert s_stale["status"] == "running"
    assert s_recent["status"] == "running"


async def test_doctor_sweeps_stale_running_sessions_to_aborted(
    temp_db_path: Path,
):
    """Sessions with started_at older than --stale-hours are reset to
    the configured status (default 'aborted'); fresh ones are left alone.
    """
    now = time.time()
    old = now - (48 * 3600)
    async with StateDB() as db:
        stale = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (old, stale),
        )
        await db.db.commit()
        recent = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (now, recent),
        )
        await db.db.commit()

    result = await _doctor(stale_hours=24, dry_run=False)
    assert result["swept"] == 1
    assert result["skipped"] == 1

    async with StateDB() as db:
        s_stale = await db.get_session(stale)
        s_recent = await db.get_session(recent)
    assert s_stale["status"] == "aborted"
    assert s_stale["ended_at"] is not None
    assert s_recent["status"] == "running"


async def test_doctor_handles_null_started_at_as_stale(temp_db_path: Path):
    """A 'running' row with NULL started_at is itself a corruption
    signal — doctor treats it as stale regardless of threshold.
    """
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = NULL WHERE id = ?", (sid,),
        )
        await db.db.commit()

    result = await _doctor(stale_hours=24, dry_run=False)
    assert result["swept"] == 1

    async with StateDB() as db:
        s = await db.get_session(sid)
    assert s["status"] == "aborted"


async def test_doctor_no_running_sessions_returns_zeros(temp_db_path: Path):
    async with StateDB() as db:
        await _seed_session(db, status="completed", updated_at=time.time())

    result = await _doctor(stale_hours=24, dry_run=False)
    assert result == {"running": 0, "swept": 0, "skipped": 0}


async def test_doctor_does_not_overwrite_session_that_completed_post_select(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """R6: ``_doctor`` previously selected victims, then updated by id
    only — a session that completed (teardown → status='completed')
    AFTER selection but BEFORE update was overwritten back to 'aborted'.

    The fix folds the ``status='running' AND stale`` predicate into the
    UPDATE itself so the conditional only fires when the row is STILL
    stale-running.

    We simulate the race by monkeypatching ``_doctor`` indirectly: we
    flip one row's status to 'completed' immediately after fetchall but
    before the UPDATE fires. The race-safety property is exposed
    cleanly by patching ``StateDB.update_session`` to inject the flip
    just before doctor's UPDATE runs.
    """
    from lionagi.state.db import StateDB as _SDB

    now = time.time()
    old = now - (48 * 3600)

    async with StateDB() as db:
        racy = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (old, racy),
        )
        truly_stale = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, truly_stale),
        )
        await db.db.commit()

    # Race injection: patch _doctor's underlying execute to flip `racy`
    # to 'completed' just before the UPDATE runs.
    class _RacyConn:
        def __init__(self, real):
            self._real = real
            self._fired = False

        async def execute(self, sql, params=None):
            # Detect doctor's UPDATE; flip racy first.
            if (
                not self._fired
                and "UPDATE sessions SET status" in sql
                and "WHERE status = 'running'" in sql
            ):
                self._fired = True
                await self._real.execute(
                    "UPDATE sessions SET status = 'completed', ended_at = ? "
                    "WHERE id = ?",
                    (now, racy),
                )
                await self._real.commit()
            return await self._real.execute(sql, params or ())

        async def commit(self):
            return await self._real.commit()

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_db_prop = _SDB.db

    def racy_db_getter(self):
        real = real_db_prop.fget(self)
        return _RacyConn(real)

    monkeypatch.setattr(_SDB, "db", property(racy_db_getter))

    result = await _doctor(stale_hours=24, dry_run=False)

    # The wrapper is no longer needed for the read-back.
    monkeypatch.setattr(_SDB, "db", real_db_prop)
    async with StateDB() as db:
        s_racy = await db.get_session(racy)
        s_truly = await db.get_session(truly_stale)

    # Only truly_stale was actually updated; the predicate excluded
    # racy because its status flipped to 'completed' mid-flight.
    assert s_racy["status"] == "completed"
    assert s_truly["status"] == "aborted"
    assert result["swept"] == 1


async def test_doctor_with_failed_new_status(temp_db_path: Path):
    """Operators can pick 'failed' instead of 'aborted'."""
    now = time.time()
    old = now - (48 * 3600)
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")
        await db.db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (old, sid),
        )
        await db.db.commit()

    await _doctor(stale_hours=24, dry_run=False, new_status="failed")

    async with StateDB() as db:
        s = await db.get_session(sid)
    assert s["status"] == "failed"
