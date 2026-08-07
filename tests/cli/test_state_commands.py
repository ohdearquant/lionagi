# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``li state`` maintenance subcommands: stats, checkpoint, vacuum, prune, and ls --limit/--status."""

from __future__ import annotations

import contextlib
import json
import os
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
    """Per-test temp file DB: patches DEFAULT_DB_PATH so li state uses a throw-away file."""
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
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (updated_at, sid),
        )
    return sid


async def _seed_session_with_messages(
    db: StateDB,
    *,
    n_messages: int = 3,
    status: str = "completed",
    updated_at: float | None = None,
) -> tuple[str, list[str]]:
    """Seed a session + branch + N messages, threaded through both progressions. Returns (session_id, msg_ids)."""
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
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (updated_at, sid),
        )
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
        sid = await _seed_session(db, name="foo", status="running", updated_at=time.time())

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
            await _seed_session(db, name=f"s{i}", status="completed", updated_at=time.time() - i)

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
        await _seed_session(db, name="finished", status="completed", updated_at=time.time())
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
    """When state.db does not yet exist, stats prints a helpful hint instead of crashing."""
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
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM sessions")
        n = row["n"]
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

    async with StateDB() as db:
        assert (await db.get_session(old_sid)) is not None
        assert (await db.get_session(new_sid)) is not None
        # Nothing the dry run touched was kept: every row is still there.
        for table in ("sessions", "branches", "messages", "progressions"):
            row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
            assert row["n"] > 0, table
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM messages")
        assert row["n"] == 3


async def test_prune_deletes_old_sessions_and_cascades_branches(
    temp_db_path: Path,
):
    """Prune deletes old sessions, cascade-drops branches, and frees the messages they held."""
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
    assert result["messages"] == 3

    async with StateDB() as db:
        assert (await db.get_session(old_sid)) is None
        assert (await db.get_session(new_sid)) is not None
        # The branch row for the old session is gone (FK cascade).
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?",
            (old_sid,),
        )
        assert row["n"] == 0
        # Surviving session's branches are intact.
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?",
            (new_sid,),
        )
        assert row["n"] == 1


async def test_prune_keeps_n_most_recent_even_when_old(temp_db_path: Path):
    """``--keep-n`` preserves the N most-recent sessions even if they are older than ``--keep-days``."""
    now = time.time()
    old_ts = now - (60 * 86400)
    async with StateDB() as db:
        # All three sessions are OLD.
        s1 = await _seed_session(db, name="oldest", status="completed", updated_at=old_ts - 100)
        s2 = await _seed_session(db, name="middle", status="completed", updated_at=old_ts - 50)
        s3 = await _seed_session(db, name="newest_old", status="completed", updated_at=old_ts)

    # keep_n=2: must preserve the 2 most recent (s2, s3).
    result = await _prune(keep_days=30, keep_n=2, dry_run=False)
    assert result["sessions"] == 1

    async with StateDB() as db:
        assert (await db.get_session(s1)) is None
        assert (await db.get_session(s2)) is not None
        assert (await db.get_session(s3)) is not None


async def test_prune_with_nothing_to_delete_returns_zero(temp_db_path: Path):
    """If no sessions match the prune criteria, all counts are zero and no rows are touched."""
    now = time.time()
    async with StateDB() as db:
        sid = await _seed_session(db, name="recent", status="completed", updated_at=now)

    result = await _prune(keep_days=30, keep_n=10, dry_run=False)
    assert result == {"sessions": 0, "branches": 0, "messages": 0}

    async with StateDB() as db:
        assert (await db.get_session(sid)) is not None


async def test_prune_frees_the_message_rows_the_deleted_session_held(
    temp_db_path: Path,
):
    """Messages a pruned session held are removed along with the progressions that held them.

    Progressions carry no foreign key back to their session, so nothing removes
    them when their owner goes; the messages they list then stay reachable from
    a row nothing points at. Both are gone here.
    """
    now = time.time()
    async with StateDB() as db:
        old_sid, old_msgs = await _seed_session_with_messages(
            db,
            n_messages=4,
            updated_at=now - (60 * 86400),
        )
        new_sid, new_msgs = await _seed_session_with_messages(
            db,
            n_messages=2,
            updated_at=now,
        )

    result = await _prune(keep_days=30, keep_n=1, dry_run=False)
    assert result["messages"] == 4

    async with StateDB() as db:
        for mid in old_msgs:
            row = await db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE id = ?", (mid,))
            assert row["n"] == 0, f"message {mid} of the pruned session was left behind"
        # The surviving session keeps everything it holds.
        for mid in new_msgs:
            row = await db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE id = ?", (mid,))
            assert row["n"] == 1, f"message {mid} of a surviving session was deleted"
        # No progression is left pointing at nothing.
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM progressions WHERE id NOT IN ("
            "  SELECT progression_id FROM sessions"
            "  UNION"
            "  SELECT progression_id FROM branches"
            ")"
        )
        assert row["n"] == 0


async def test_prune_frees_direct_only_messages_of_deleted_session(temp_db_path: Path):
    """Direct message references are prune candidates even outside progressions."""
    now = time.time()
    old = now - (60 * 86400)
    async with StateDB() as db:
        victim_sid, _ = await _seed_session_with_messages(
            db,
            n_messages=0,
            updated_at=old,
        )
        keeper_sid, _ = await _seed_session_with_messages(
            db,
            n_messages=0,
            updated_at=now,
        )
        victim_branch = await db.fetch_one(
            "SELECT id FROM branches WHERE session_id = ?",
            (victim_sid,),
        )
        message_ids = [str(uuid.uuid4()) for _ in range(3)]
        for index, message_id in enumerate(message_ids):
            await db.insert_message(
                {
                    "id": message_id,
                    "created_at": now,
                    "node_metadata": {},
                    "content": {"text": f"direct-{index}"},
                    "role": "user",
                    "sender": "u",
                    "recipient": "x",
                    "channel": "test",
                }
            )
        await db.execute(
            "UPDATE sessions SET first_msg_id = ?, last_msg_id = ? WHERE id = ?",
            (message_ids[0], message_ids[1], victim_sid),
        )
        await db.execute(
            "UPDATE branches SET system_msg_id = ? WHERE id = ?",
            (message_ids[2], victim_branch["id"]),
        )

    result = await _prune(keep_days=30, keep_n=1, dry_run=False)

    assert result == {"sessions": 1, "branches": 1, "messages": 3}
    async with StateDB() as db:
        assert (await db.get_session(victim_sid)) is None
        assert (await db.get_session(keeper_sid)) is not None
        for message_id in message_ids:
            row = await db.fetch_one(
                "SELECT COUNT(*) AS n FROM messages WHERE id = ?",
                (message_id,),
            )
            assert row["n"] == 0


async def _seed_prune_fixture_with_shared_and_pinned_messages(db: StateDB) -> tuple[str, str, int]:
    """Seed one prunable session whose messages are only partly reclaimable.

    Of the victim's four messages, one is also listed in a surviving session's
    progression and one is named by a surviving session's ``first_msg_id``. Two
    are therefore freed. Every count that skips one of those two survivor
    references lands on 3 or 4 instead, so the fixture separates the real answer
    from the near misses.

    Returns ``(victim_session_id, survivor_session_id, expected_messages_freed)``.
    """
    now = time.time()
    victim_sid, victim_bid = str(uuid.uuid4()), str(uuid.uuid4())
    victim_spid, victim_bpid = str(uuid.uuid4()), str(uuid.uuid4())
    keeper_sid, keeper_bid = str(uuid.uuid4()), str(uuid.uuid4())
    keeper_spid, keeper_bpid = str(uuid.uuid4()), str(uuid.uuid4())

    for pid in (victim_spid, victim_bpid, keeper_spid, keeper_bpid):
        await db.create_progression(pid)
    for sid, pid in ((victim_sid, victim_spid), (keeper_sid, keeper_spid)):
        await db.create_session(
            {"id": sid, "progression_id": pid, "status": "completed", "started_at": now}
        )
    for bid, sid, pid in (
        (victim_bid, victim_sid, victim_bpid),
        (keeper_bid, keeper_sid, keeper_bpid),
    ):
        await db.create_branch({"id": bid, "session_id": sid, "progression_id": pid})

    victim_msgs = []
    for i in range(4):
        mid = str(uuid.uuid4())
        await db.insert_message(
            {
                "id": mid,
                "created_at": now,
                "node_metadata": {},
                "content": {"text": f"victim-{i}"},
                "role": "user",
                "sender": "u",
                "recipient": "x",
                "channel": "test",
            }
        )
        await db.append_to_progression(victim_bpid, mid)
        await db.append_to_progression(victim_spid, mid)
        victim_msgs.append(mid)

    # One of the victim's messages is quoted into a surviving progression, and
    # another is named directly by a surviving session.
    await db.append_to_progression(keeper_bpid, victim_msgs[0])
    await db.execute(
        "UPDATE sessions SET first_msg_id = ? WHERE id = ?",
        (victim_msgs[1], keeper_sid),
    )

    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (now - (60 * 86400), victim_sid),
    )
    await db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, keeper_sid))
    return victim_sid, keeper_sid, 2


async def test_dry_run_message_count_is_the_count_the_real_prune_deletes(
    temp_db_path: Path,
):
    """The preview's message count is the real prune's, measured on the same store.

    The preview is the prune, rolled back, so the two cannot be derived
    separately. This runs both against the same fixture and pins the answer to
    the number of rows the store actually loses, which is what makes a
    re-introduced separate estimate visible rather than merely suspicious.
    """
    async with StateDB() as db:
        (
            victim_sid,
            keeper_sid,
            expected,
        ) = await _seed_prune_fixture_with_shared_and_pinned_messages(db)
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM messages")
        before = row["n"]

    preview = await _prune(keep_days=30, keep_n=1, dry_run=True)

    async with StateDB() as db:
        # The preview committed nothing, so the real prune below sees the same store.
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM messages")
        assert row["n"] == before
        assert (await db.get_session(victim_sid)) is not None

    real = await _prune(keep_days=30, keep_n=1, dry_run=False)

    async with StateDB() as db:
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM messages")
        observed = before - row["n"]

    assert preview["messages"] == real["messages"] == observed == expected
    assert preview["sessions"] == real["sessions"]
    assert preview["branches"] == real["branches"]

    async with StateDB() as db:
        assert (await db.get_session(victim_sid)) is None
        assert (await db.get_session(keeper_sid)) is not None


# ── _doctor (li state doctor) ────────────────────────────────────────────────


async def test_doctor_dry_run_does_not_modify_status(temp_db_path: Path):
    """``_doctor --dry-run`` reports which sessions WOULD be swept but leaves status='running' untouched."""
    now = time.time()
    old = now - (48 * 3600)
    async with StateDB() as db:
        stale = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, stale),
        )
        recent = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now, recent),
        )

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
    """Sessions with started_at older than --stale-hours are reset to 'aborted'; fresh ones are left alone."""
    now = time.time()
    old = now - (48 * 3600)
    async with StateDB() as db:
        stale = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, stale),
        )
        recent = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now, recent),
        )

    result = await _doctor(stale_hours=24, dry_run=False)
    assert result["swept"] == 1
    assert result["skipped"] == 1

    async with StateDB() as db:
        s_stale = await db.get_session(stale)
        s_recent = await db.get_session(recent)
    assert s_stale["status"] == "aborted"
    assert s_stale["ended_at"] is not None
    assert s_recent["status"] == "running"


async def test_doctor_sweep_populates_duration_ms(temp_db_path: Path):
    old = time.time() - (48 * 3600)
    async with StateDB() as db:
        stale = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, stale),
        )

    result = await _doctor(stale_hours=24, dry_run=False)
    assert result["swept"] == 1

    async with StateDB() as db:
        row = await db.get_session(stale)
    assert row["ended_at"] is not None
    assert row["duration_ms"] == pytest.approx((row["ended_at"] - old) * 1000)


async def test_doctor_leaves_an_old_session_whose_process_is_still_running(
    temp_db_path: Path,
):
    """Age answers how long since the session first started, which is how long
    the process has been running only for a session that ran once. A branch
    picked up again keeps its original start time while the process is new, so
    the command asks the process before calling the session stuck."""
    old = time.time() - (48 * 3600)
    async with StateDB() as db:
        alive = await _seed_session(db, status="running")
        dead = await _seed_session(db, status="running")
        for sid, pid in ((alive, os.getpid()), (dead, 999999)):
            await db.execute(
                "UPDATE sessions SET started_at = ?, node_metadata = ? WHERE id = ?",
                (old, json.dumps({"pid": pid}), sid),
            )

    result = await _doctor(stale_hours=24, dry_run=False)
    assert (result["swept"], result["skipped"]) == (1, 1)

    async with StateDB() as db:
        assert (await db.get_session(alive))["status"] == "running"
        assert (await db.get_session(dead))["status"] == "aborted"


async def test_doctor_sweeps_a_session_whose_pid_was_recycled(temp_db_path: Path):
    """A live PID is not proof on its own. The OS hands a dead session's number
    to an unrelated process eventually, and a sweep that stopped there would
    protect a genuinely stuck row for as long as that process lived."""
    old = time.time() - (48 * 3600)
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ?, node_metadata = ? WHERE id = ?",
            # This process is alive, but it started at a different moment than
            # the session recorded, so the number belongs to someone else now.
            (old, json.dumps({"pid": os.getpid(), "pid_create_time": 1.0}), sid),
        )

    assert (await _doctor(stale_hours=24, dry_run=False))["swept"] == 1

    async with StateDB() as db:
        assert (await db.get_session(sid))["status"] == "aborted"


async def test_doctor_handles_null_started_at_as_stale(temp_db_path: Path):
    """A 'running' row with NULL started_at is treated as stale regardless of the threshold."""
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = NULL WHERE id = ?",
            (sid,),
        )

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
    """A session that completes AFTER _doctor's SELECT but BEFORE its UPDATE must NOT be overwritten back to 'aborted'.
    The fix folds the stale predicate into the UPDATE itself so it only fires when the row is still stale-running.
    """
    from lionagi.state.db import StateDB as _SDB

    now = time.time()
    old = now - (48 * 3600)

    async with StateDB() as db:
        racy = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, racy),
        )
        truly_stale = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, truly_stale),
        )

    # Race injection: flip `racy` to 'completed' between the doctor's SELECT
    # and its CAS UPDATE. The doctor runs that UPDATE inside db._tx(), so wrap
    # _tx: on first entry (the doctor's write) flip racy first, then delegate.
    real_tx = _SDB._tx
    fired = {"done": False}

    @contextlib.asynccontextmanager
    async def racy_tx(self):
        if not fired["done"]:
            fired["done"] = True
            await self.execute(
                "UPDATE sessions SET status = 'completed', ended_at = ? WHERE id = ?",
                (now, racy),
            )
        async with real_tx(self) as conn:
            yield conn

    monkeypatch.setattr(_SDB, "_tx", racy_tx)

    result = await _doctor(stale_hours=24, dry_run=False)

    monkeypatch.setattr(_SDB, "_tx", real_tx)
    async with StateDB() as db:
        s_racy = await db.get_session(racy)
        s_truly = await db.get_session(truly_stale)

    # Only truly_stale was actually updated; the predicate excluded
    # racy because its status flipped to 'completed' mid-flight.
    assert s_racy["status"] == "completed"
    assert s_truly["status"] == "aborted"
    assert result["swept"] == 1


async def test_doctor_status_and_ended_at_are_atomic(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A crash immediately after the status-transitioning write commits must
    not leave a terminal-status row with ``ended_at`` still NULL — status and
    ``ended_at`` must land in the same transaction, not two sequential ones.
    """
    from lionagi.state.db import StateDB as _SDB

    now = time.time()
    old = now - (48 * 3600)
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, sid),
        )

    real_tx = _SDB._tx
    calls = {"n": 0}

    @contextlib.asynccontextmanager
    async def crash_after_first_tx(self):
        calls["n"] += 1
        async with real_tx(self) as conn:
            yield conn
        if calls["n"] == 1:
            raise RuntimeError("simulated crash right after the status write commits")

    monkeypatch.setattr(_SDB, "_tx", crash_after_first_tx)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await _doctor(stale_hours=24, dry_run=False)

    monkeypatch.setattr(_SDB, "_tx", real_tx)
    async with StateDB() as db:
        s = await db.get_session(sid)
    assert s["status"] == "aborted"
    assert s["ended_at"] is not None


async def test_doctor_with_failed_new_status(temp_db_path: Path):
    """Operators can pick 'failed' instead of 'aborted'."""
    now = time.time()
    old = now - (48 * 3600)
    async with StateDB() as db:
        sid = await _seed_session(db, status="running")
        await db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (old, sid),
        )

    await _doctor(stale_hours=24, dry_run=False, new_status="failed")

    async with StateDB() as db:
        s = await db.get_session(sid)
    assert s["status"] == "failed"


# ── _import_one_run: a row born terminal must carry a real duration_ms ────────


async def test_import_of_a_completed_run_derives_duration_ms(temp_db_path: Path, tmp_path: Path):
    """`li state import` inserts terminal rows directly via create_session(), a
    path that never reaches _transition() or the admin CAS. It must derive
    duration_ms itself rather than leaving the column null."""
    from lionagi.cli.state import _import_one_run

    run_dir = tmp_path / "runs" / "run-duration"
    run_dir.mkdir(parents=True)

    async with StateDB() as db:
        await _import_one_run(
            db,
            "run-duration",
            run_dir,
            {"kind": "agent", "status": "completed", "started_at": 100.0, "ended_at": 130.0},
        )
        session = await db.get_session("run-duration")

    assert session["status"] == "completed"
    assert session["started_at"] == 100.0
    assert session["ended_at"] == 130.0
    assert session["duration_ms"] == 30000.0


async def test_import_of_a_running_run_leaves_duration_ms_null(temp_db_path: Path, tmp_path: Path):
    """A non-terminal import must not have a duration computed for it."""
    from lionagi.cli.state import _import_one_run

    run_dir = tmp_path / "runs" / "run-open"
    run_dir.mkdir(parents=True)

    async with StateDB() as db:
        await _import_one_run(
            db,
            "run-open",
            run_dir,
            {"kind": "agent", "status": "running", "started_at": 100.0},
        )
        session = await db.get_session("run-open")

    assert session["status"] == "running"
    assert session["duration_ms"] is None
