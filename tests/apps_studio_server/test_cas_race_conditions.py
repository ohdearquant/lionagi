# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Race-condition tests for update_status CAS guard and orphan-cleanup in
prune_old_data.

Two independent protections cover the terminal-overwrite race:
  1. _unguarded_*: calls update_status WITHOUT expected_statuses. The write path
     protects terminal states unconditionally — an attempt to move an entity out
     of a terminal status without an explicit override is rejected loudly
     (TransitionRejectedError) and the terminal status is preserved.
  2. _guarded_*: calls update_status WITH expected_statuses. A compare-and-set
     miss (current status not in the expected set) is a benign skip — it returns
     False and preserves the winner status, without raising.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB, TransitionRejectedError  # noqa: E402

from ._helpers import run_async  # noqa: E402

# ── DB helpers ────────────────────────────────────────────────────────────────


def _patch_db(monkeypatch, db_path: Path) -> None:
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.lifecycle as lifecycle_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(lifecycle_mod, "DEFAULT_DB_PATH", db_path)


async def _make_session(db_path: Path, *, status: str | None = "running") -> str:
    sid = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid,
                "name": f"race-test-{sid[:6]}",
                "status": status,
                "started_at": now,
            }
        )
        if status is None:
            await db.execute("UPDATE sessions SET status = NULL WHERE id = ?", (sid,))
    return sid


async def _make_invocation(db_path: Path, *, status: str = "running") -> str:
    iid = uuid.uuid4().hex[:12]
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_invocation(
            {
                "id": iid,
                "skill": "race:test",
                "started_at": now,
                "status": status,
                "session_count": 1,
            }
        )
    return iid


async def _get_status(db_path: Path, entity_type: str, entity_id: str) -> str | None:
    table = "sessions" if entity_type == "session" else "invocations"
    async with StateDB(db_path) as db:
        row = await db.fetch_one(
            f"SELECT status FROM {table} WHERE id = ?",  # noqa: S608
            (entity_id,),
        )
    return row["status"] if row else None


async def _flip_status(db_path: Path, entity_type: str, entity_id: str, new_status: str) -> None:
    """Simulate a concurrent process writing a terminal status directly."""
    table = "sessions" if entity_type == "session" else "invocations"
    async with StateDB(db_path) as db:
        await db.execute(
            f"UPDATE {table} SET status = ?, updated_at = ? WHERE id = ?",  # noqa: S608
            (new_status, time.time(), entity_id),
        )


# ── FAIL-before: phantom session race ─────────────────────────────────────────


def test_session_race_unguarded_write_rejected_by_floor(tmp_path, monkeypatch):
    """An unguarded write out of a terminal session status is rejected loudly.

    Terminal protection does not depend on the caller remembering to pass
    expected_statuses: the write path itself refuses to move a session out of a
    terminal status without an explicit override, so the reaper race cannot
    corrupt a 'completed' session into 'failed'.
    """
    from lionagi.state.reasons import SessionReasons

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    sid = run_async(_make_session(db_path, status="running"))

    # Simulate: process completes the session between the reaper's list-query
    # and the reaper's update_status call.
    run_async(_flip_status(db_path, "session", sid, "completed"))
    assert run_async(_get_status(db_path, "session", sid)) == "completed"

    async def _unguarded(db_path: Path, sid: str) -> bool:
        async with StateDB(db_path) as db:
            return await db.update_status(
                "session",
                sid,
                new_status="failed",
                reason_code=SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
                reason_summary="phantom_reaped",
                source="system",
                actor="test_unguarded",
                # No expected_statuses and no override — the floor rejects this.
            )

    with pytest.raises(TransitionRejectedError):
        run_async(_unguarded(db_path, sid))
    # Terminal status preserved; the overwrite never landed.
    assert run_async(_get_status(db_path, "session", sid)) == "completed"


# ── PASS-after: phantom session race ──────────────────────────────────────────


def test_session_race_guarded_preserves_terminal(tmp_path, monkeypatch):
    """WITH the CAS guard (expected_statuses={"running"}), 'completed' is preserved.

    This is the PASS-after demonstration: the fix blocks the overwrite because
    'completed' is not in expected_statuses={"running"}.
    """
    from lionagi.state.reasons import SessionReasons

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    sid = run_async(_make_session(db_path, status="running"))

    # Simulate: session completed between reaper's list-query and update_status.
    run_async(_flip_status(db_path, "session", sid, "completed"))
    assert run_async(_get_status(db_path, "session", sid)) == "completed"

    async def _guarded(db_path: Path, sid: str) -> bool:
        async with StateDB(db_path) as db:
            return await db.update_status(
                "session",
                sid,
                new_status="failed",
                reason_code=SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
                reason_summary="phantom_reaped",
                source="system",
                actor="test_guarded",
                expected_statuses={"running"},  # The fix
            )

    result = run_async(_guarded(db_path, sid))
    # CAS guard blocked the transition → returns False.
    assert result is False
    # Status preserved as 'completed'; not overwritten with 'failed'.
    assert run_async(_get_status(db_path, "session", sid)) == "completed"


# ── FAIL-before: invocation race ──────────────────────────────────────────────


def test_invocation_race_unguarded_write_rejected_by_floor(tmp_path, monkeypatch):
    """An unguarded write out of a terminal invocation status is rejected loudly.

    The deadline reaper cannot corrupt a 'completed' invocation into 'timed_out':
    the write path refuses to leave a terminal status without an explicit
    override, independent of expected_statuses.
    """
    from lionagi.state.reasons import RunReasons

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    iid = run_async(_make_invocation(db_path, status="running"))

    # Invocation completes normally between reaper list and update.
    run_async(_flip_status(db_path, "invocation", iid, "completed"))
    assert run_async(_get_status(db_path, "invocation", iid)) == "completed"

    async def _unguarded(db_path: Path, iid: str) -> bool:
        async with StateDB(db_path) as db:
            return await db.update_status(
                "invocation",
                iid,
                new_status="timed_out",
                reason_code=RunReasons.TIMED_OUT_DEADLINE,
                reason_summary="invocation_deadline_exceeded",
                source="system",
                actor="test_unguarded",
                # No expected_statuses and no override — the floor rejects this.
            )

    with pytest.raises(TransitionRejectedError):
        run_async(_unguarded(db_path, iid))
    # Terminal status preserved.
    assert run_async(_get_status(db_path, "invocation", iid)) == "completed"


# ── PASS-after: invocation race ───────────────────────────────────────────────


def test_invocation_race_guarded_preserves_terminal(tmp_path, monkeypatch):
    """WITH the CAS guard (expected_statuses={"running"}), 'completed' is preserved.

    PASS-after for the invocation deadline reaper race.
    """
    from lionagi.state.reasons import RunReasons

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    iid = run_async(_make_invocation(db_path, status="running"))

    # Invocation completes normally between reaper list and update.
    run_async(_flip_status(db_path, "invocation", iid, "completed"))
    assert run_async(_get_status(db_path, "invocation", iid)) == "completed"

    async def _guarded(db_path: Path, iid: str) -> bool:
        async with StateDB(db_path) as db:
            return await db.update_status(
                "invocation",
                iid,
                new_status="timed_out",
                reason_code=RunReasons.TIMED_OUT_DEADLINE,
                reason_summary="invocation_deadline_exceeded",
                source="system",
                actor="test_guarded",
                expected_statuses={"running"},  # The fix
            )

    result = run_async(_guarded(db_path, iid))
    assert result is False  # CAS guard fired → skipped
    # Status preserved as 'completed'.
    assert run_async(_get_status(db_path, "invocation", iid)) == "completed"


# ── null-status session CAS: expected_statuses={None} ─────────────────────────


def test_null_status_reaper_cas_blocks_nonnull(tmp_path, monkeypatch):
    """Null-status reaper must not touch a session that already has a status.

    The candidate query selects status IS NULL; by the time update_status runs,
    the session may have received a status.  The CAS guard with expected_statuses={None}
    must block the overwrite.
    """
    from lionagi.state.reasons import RunReasons

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    # Session starts with NULL status (crash scenario)
    sid = run_async(_make_session(db_path, status=None))
    assert run_async(_get_status(db_path, "session", sid)) is None

    # Between query and update: process wrote its own terminal status.
    run_async(_flip_status(db_path, "session", sid, "completed"))
    assert run_async(_get_status(db_path, "session", sid)) == "completed"

    async def _guarded(db_path: Path, sid: str) -> bool:
        async with StateDB(db_path) as db:
            return await db.update_status(
                "session",
                sid,
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary="process_exited_without_status",
                source="system",
                actor="test_null_cas",
                expected_statuses={None},  # Only transition from NULL
            )

    result = run_async(_guarded(db_path, sid))
    assert result is False  # 'completed' not in {None} → blocked
    assert run_async(_get_status(db_path, "session", sid)) == "completed"


def test_null_status_reaper_cas_allows_null(tmp_path, monkeypatch):
    """Null-status reaper succeeds when status is still NULL (happy path)."""
    from lionagi.state.reasons import RunReasons

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    sid = run_async(_make_session(db_path, status=None))
    assert run_async(_get_status(db_path, "session", sid)) is None

    async def _guarded(db_path: Path, sid: str) -> bool:
        async with StateDB(db_path) as db:
            return await db.update_status(
                "session",
                sid,
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary="process_exited_without_status",
                source="system",
                actor="test_null_cas_ok",
                expected_statuses={None},
            )

    result = run_async(_guarded(db_path, sid))
    assert result is True  # NULL is in {None} → transition succeeds
    assert run_async(_get_status(db_path, "session", sid)) == "failed"


# ── orphan cleanup after prune ───────────────────────────────────────────────


def test_prune_cleans_orphaned_messages_and_progressions(tmp_path, monkeypatch):
    """prune_old_data() leaves zero orphaned progressions and messages.

    Scenario: one old terminal session (will be pruned), one recent session
    (must be preserved).  Each session has a progression with two messages.
    After pruning, only the recent session's progression and messages remain.
    """
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"

    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    old_ts = time.time() - 40 * 86400
    recent_ts = time.time() - 1 * 86400

    async def _seed() -> tuple[str, str, list[str], list[str]]:
        """Create two sessions, each with a progression and two messages.

        Returns (old_sid, recent_sid, old_msg_ids, recent_msg_ids).
        """
        async with StateDB(db_path) as db:
            # ── old session (will be pruned) ──────────────────────────────
            old_pid = str(uuid.uuid4())
            old_sid = str(uuid.uuid4())
            await db.create_progression(old_pid)
            await db.create_session(
                {
                    "id": old_sid,
                    "progression_id": old_pid,
                    "name": "old-session",
                    "status": "completed",
                    "started_at": old_ts,
                }
            )
            # Create two messages and record them in the progression's collection.
            old_msg1 = str(uuid.uuid4())
            old_msg2 = str(uuid.uuid4())
            for mid in (old_msg1, old_msg2):
                await db.execute(
                    "INSERT INTO messages (id, content, created_at, role, lion_class)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (mid, '{"content":"hi"}', old_ts, "user", 2),
                )
            import json

            old_collection = json.dumps([old_msg1, old_msg2])
            await db.execute(
                "UPDATE progressions SET collection = ? WHERE id = ?",
                (old_collection, old_pid),
            )

            # ── recent session (must survive) ─────────────────────────────
            recent_pid = str(uuid.uuid4())
            recent_sid = str(uuid.uuid4())
            await db.create_progression(recent_pid)
            await db.create_session(
                {
                    "id": recent_sid,
                    "progression_id": recent_pid,
                    "name": "recent-session",
                    "status": "completed",
                    "started_at": recent_ts,
                }
            )
            recent_msg1 = str(uuid.uuid4())
            recent_msg2 = str(uuid.uuid4())
            for mid in (recent_msg1, recent_msg2):
                await db.execute(
                    "INSERT INTO messages (id, content, created_at, role, lion_class)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (mid, '{"content":"hello"}', recent_ts, "assistant", 3),
                )
            recent_collection = json.dumps([recent_msg1, recent_msg2])
            await db.execute(
                "UPDATE progressions SET collection = ? WHERE id = ?",
                (recent_collection, recent_pid),
            )

        return old_sid, recent_sid, [old_msg1, old_msg2], [recent_msg1, recent_msg2]

    old_sid, recent_sid, old_msgs, recent_msgs = run_async(_seed())

    async def _count(table: str) -> int:
        async with StateDB(db_path) as db:
            row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
            return row["n"]

    # Before prune: 2 progressions, 4 messages.
    assert run_async(_count("progressions")) == 2
    assert run_async(_count("messages")) == 4

    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] == 1  # Only old session pruned.

    # After prune: 1 progression, 2 messages (recent session only).
    assert run_async(_count("progressions")) == 1, "Orphaned progression not cleaned"
    assert run_async(_count("messages")) == 2, "Orphaned messages not cleaned"

    # Confirm the survivor is the recent session's progression/messages.
    async def _get_prog_ids() -> set[str]:
        async with StateDB(db_path) as db:
            rows = await db.fetch_all("SELECT id FROM progressions")
            return {r["id"] for r in rows}

    async def _get_msg_ids() -> set[str]:
        async with StateDB(db_path) as db:
            rows = await db.fetch_all("SELECT id FROM messages")
            return {r["id"] for r in rows}

    remaining_progs = run_async(_get_prog_ids())
    remaining_msgs = run_async(_get_msg_ids())

    # Old entries gone.
    for mid in old_msgs:
        assert mid not in remaining_msgs, f"Old message {mid} should be pruned"

    # Recent entries survive.
    for mid in recent_msgs:
        assert mid in remaining_msgs, f"Recent message {mid} should survive"


# ── newborn-orphan regression ─────────────────────────────────────────────────


def _seed_unguarded_global_delete(db_path: Path) -> tuple[str, str]:
    """Simulate the old global-delete code path for FAIL-before demonstration.

    Returns (orphan_prog_id, orphan_msg_id) — both committed without a
    referencing session, simulating _persist.py mid-startup state.
    """

    async def _seed() -> tuple[str, str]:
        orphan_prog_id = str(uuid.uuid4())
        orphan_msg_id = str(uuid.uuid4())
        async with StateDB(db_path) as db:
            # Progression committed (matches create_progression commit order).
            await db.create_progression(orphan_prog_id)
            # Message committed (matches insert_message commit order).
            await db.execute(
                "INSERT INTO messages (id, content, created_at, role, lion_class)"
                " VALUES (?, ?, ?, ?, ?)",
                (orphan_msg_id, '{"content":"newborn"}', time.time(), "user", 2),
            )
            # Append to collection to simulate mid-startup hook write.
            import json

            await db.execute(
                "UPDATE progressions SET collection = ? WHERE id = ?",
                (json.dumps([orphan_msg_id]), orphan_prog_id),
            )
        # No create_session() call — simulates the gap between progression
        # commit and session commit in setup_agent_persist().
        return orphan_prog_id, orphan_msg_id

    return run_async(_seed())


async def _run_global_delete(db_path: Path) -> None:
    """Reproduce the old (buggy) global-delete SQL without the scope fix."""
    async with StateDB(db_path) as db:
        async with db.transaction() as conn:
            await conn.execute(
                text(
                    "DELETE FROM progressions WHERE id NOT IN ("
                    "  SELECT progression_id FROM sessions"
                    "  UNION"
                    "  SELECT progression_id FROM branches"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "DELETE FROM messages WHERE id NOT IN ("
                    "  SELECT value FROM progressions, json_each(progressions.collection)"
                    ")"
                )
            )


def test_newborn_orphan_global_delete_destroys_progression(tmp_path, monkeypatch):
    """FAIL-before: the old global-delete deletes a newborn progression with no session yet.

    This reproduces the race: _persist.py commits progression before session.
    The old NOT IN (SELECT ... FROM sessions UNION ...) query returns nothing for
    sessions/branches table (session row doesn't exist yet), so the progression
    is spuriously deleted.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    # Seed an old prunable session so the prune has something to do.
    old_ts = time.time() - 40 * 86400

    async def _seed_old_session() -> None:
        async with StateDB(db_path) as db:
            old_pid = str(uuid.uuid4())
            old_sid = str(uuid.uuid4())
            await db.create_progression(old_pid)
            await db.create_session(
                {
                    "id": old_sid,
                    "progression_id": old_pid,
                    "name": "old-prunable",
                    "status": "completed",
                    "started_at": old_ts,
                }
            )

    run_async(_seed_old_session())

    # Seed the newborn — progression+message committed, session not yet.
    orphan_prog_id, orphan_msg_id = _seed_unguarded_global_delete(db_path)

    # Verify they exist before the buggy delete.
    async def _exists_prog(pid: str) -> bool:
        async with StateDB(db_path) as db:
            row = await db.fetch_one("SELECT id FROM progressions WHERE id = ?", (pid,))
            return row is not None

    async def _exists_msg(mid: str) -> bool:
        async with StateDB(db_path) as db:
            row = await db.fetch_one("SELECT id FROM messages WHERE id = ?", (mid,))
            return row is not None

    assert run_async(_exists_prog(orphan_prog_id)), "Newborn progression must exist before delete"
    assert run_async(_exists_msg(orphan_msg_id)), "Newborn message must exist before delete"

    # Run the old global-delete code path.
    run_async(_run_global_delete(db_path))

    # FAIL-before: global delete wipes the newborn progression and message.
    assert not run_async(_exists_prog(orphan_prog_id)), (
        "FAIL-before confirmed: global-delete wiped the newborn progression"
    )
    assert not run_async(_exists_msg(orphan_msg_id)), (
        "FAIL-before confirmed: global-delete wiped the newborn message"
    )


def test_newborn_orphan_scoped_delete_preserves_progression(tmp_path, monkeypatch):
    """PASS-after: the scoped delete (fix) never touches a newborn progression.

    prune_old_data() only deletes progressions/messages that were captured
    from the pruned sessions' lineage before the DELETE.  A newborn progression
    with no referencing session is not in that candidate set — it survives.
    """
    import lionagi.state.db as state_db_mod
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    old_ts = time.time() - 40 * 86400

    async def _seed_old_session() -> None:
        async with StateDB(db_path) as db:
            old_pid = str(uuid.uuid4())
            old_sid = str(uuid.uuid4())
            await db.create_progression(old_pid)
            await db.create_session(
                {
                    "id": old_sid,
                    "progression_id": old_pid,
                    "name": "old-prunable-2",
                    "status": "completed",
                    "started_at": old_ts,
                }
            )

    run_async(_seed_old_session())

    # Newborn: progression+message committed, no session row yet.
    orphan_prog_id, orphan_msg_id = _seed_unguarded_global_delete(db_path)

    async def _exists_prog(pid: str) -> bool:
        async with StateDB(db_path) as db:
            row = await db.fetch_one("SELECT id FROM progressions WHERE id = ?", (pid,))
            return row is not None

    async def _exists_msg(mid: str) -> bool:
        async with StateDB(db_path) as db:
            row = await db.fetch_one("SELECT id FROM messages WHERE id = ?", (mid,))
            return row is not None

    # Run the fixed prune_old_data (scoped delete).
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] >= 1  # Old session was pruned.

    # PASS-after: newborn progression and message survive.
    assert run_async(_exists_prog(orphan_prog_id)), (
        "PASS-after: scoped delete must not touch newborn progression"
    )
    assert run_async(_exists_msg(orphan_msg_id)), (
        "PASS-after: scoped delete must not touch newborn message"
    )


# ── NULL-trap test ────────────────────────────────────────────────────────────


def test_null_in_collection_does_not_stall_message_cleanup(tmp_path, monkeypatch):
    """A progression collection containing JSON null doesn't block message cleanup.

    The NOT IN (SELECT value FROM progressions, json_each(collection) WHERE value IS NOT NULL)
    guard ensures that a null entry in the collection array doesn't propagate
    a NULL into the NOT IN set and silently suppress all message deletions.
    """
    import json

    import lionagi.state.db as state_db_mod
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    old_ts = time.time() - 40 * 86400

    async def _seed() -> tuple[str, str, str]:
        """Create an old session whose progression collection has a JSON null entry.

        Returns (session_id, prog_id, real_msg_id).
        """
        async with StateDB(db_path) as db:
            pid = str(uuid.uuid4())
            sid = str(uuid.uuid4())
            real_msg_id = str(uuid.uuid4())

            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": pid,
                    "name": "null-trap-session",
                    "status": "completed",
                    "started_at": old_ts,
                }
            )
            # Insert a real message.
            await db.execute(
                "INSERT INTO messages (id, content, created_at, role, lion_class)"
                " VALUES (?, ?, ?, ?, ?)",
                (real_msg_id, '{"content":"msg"}', old_ts, "user", 2),
            )
            # Collection contains both a valid id AND a JSON null.
            bad_collection = json.dumps([real_msg_id, None])
            await db.execute(
                "UPDATE progressions SET collection = ? WHERE id = ?",
                (bad_collection, pid),
            )
        return sid, pid, real_msg_id

    sid, pid, real_msg_id = run_async(_seed())

    async def _count_msgs() -> int:
        async with StateDB(db_path) as db:
            row = await db.fetch_one("SELECT COUNT(*) AS n FROM messages")
            return row["n"]

    assert run_async(_count_msgs()) == 1  # One message before prune.

    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] == 1

    # The real message must be cleaned up (not stalled by the null entry).
    assert run_async(_count_msgs()) == 0, (
        "NULL in collection must not stall message cleanup via NOT IN NULL trap"
    )


# ── shared-message protection ─────────────────────────────────────────────────
#
# Scenario: message M appears in pruned session P's progression collection AND
# is also referenced by a surviving session's first_msg_id / last_msg_id FK
# column (or a surviving branch's system_msg_id).
#
# Old code (collection-only survivor check): M would be spuriously deleted
# because the NOT IN subquery only scanned progressions.collection, not the
# direct FK columns.
#
# Fixed code: survivor subquery UNIONs first_msg_id, last_msg_id (sessions)
# and system_msg_id (branches), so M is retained.


async def _seed_shared_message_scenario(db_path: Path) -> tuple[str, str, str, str]:
    """Seed the shared-message scenario.

    Creates:
    - pruned_session (old, terminal) whose progression contains shared_msg and unshared_msg
    - surviving_branch (recent) whose system_msg_id = shared_msg

    The surviving branch belongs to a surviving session that is NOT being pruned.
    shared_msg is in the pruned progression's collection AND referenced by
    surviving_branch.system_msg_id.  unshared_msg is only in the pruned progression.

    Note: first_msg_id / last_msg_id on sessions have a real FK REFERENCES messages(id),
    so SQLite with foreign_keys=ON prevents deletion while the FK is live.  To demonstrate
    the bug plainly we use system_msg_id on a branch of a DIFFERENT (surviving) session,
    which also has the FK — but the FAIL-before test disables foreign_keys first, showing
    what the old collection-only check would have done without the constraint.

    Returns (pruned_sid, surviving_sid, shared_msg_id, unshared_msg_id).
    """
    import json

    old_ts = time.time() - 40 * 86400
    recent_ts = time.time() - 1 * 86400

    async with StateDB(db_path) as db:
        # ── shared message ─────────────────────────────────────────────────
        shared_msg_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO messages (id, content, created_at, role, lion_class)"
            " VALUES (?, ?, ?, ?, ?)",
            (shared_msg_id, '{"content":"shared"}', old_ts, "user", 2),
        )

        # ── unshared message (only in pruned progression) ──────────────────
        unshared_msg_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO messages (id, content, created_at, role, lion_class)"
            " VALUES (?, ?, ?, ?, ?)",
            (unshared_msg_id, '{"content":"unshared"}', old_ts, "user", 2),
        )

        # ── pruned session ─────────────────────────────────────────────────
        pruned_pid = str(uuid.uuid4())
        pruned_sid = str(uuid.uuid4())
        await db.create_progression(pruned_pid)
        await db.create_session(
            {
                "id": pruned_sid,
                "progression_id": pruned_pid,
                "name": "pruned-session",
                "status": "completed",
                "started_at": old_ts,
            }
        )
        # Both shared and unshared messages in the pruned progression.
        await db.execute(
            "UPDATE progressions SET collection = ? WHERE id = ?",
            (json.dumps([shared_msg_id, unshared_msg_id]), pruned_pid),
        )

        # ── surviving session + branch — system_msg_id = shared_msg ───────
        surviving_pid = str(uuid.uuid4())
        surviving_sid = str(uuid.uuid4())
        await db.create_progression(surviving_pid)
        await db.create_session(
            {
                "id": surviving_sid,
                "progression_id": surviving_pid,
                "name": "surviving-session",
                "status": "running",
                "started_at": recent_ts,
            }
        )
        branch_pid = str(uuid.uuid4())
        branch_id = str(uuid.uuid4())
        await db.create_progression(branch_pid)
        await db.execute(
            "INSERT INTO branches"
            " (id, session_id, progression_id, system_msg_id, created_at, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (branch_id, surviving_sid, branch_pid, shared_msg_id, recent_ts, recent_ts),
        )

    return pruned_sid, surviving_sid, shared_msg_id, unshared_msg_id


def test_shared_message_collection_only_check_deletes_it(tmp_path, monkeypatch):
    """FAIL-before: collection-only survivor check spuriously deletes shared message.

    Simulates the old code path with foreign_keys=OFF to expose the bug:
    NOT IN only checks progressions.collection.  After the pruned session's
    progression is deleted, shared_msg_id no longer appears in any collection
    — so it is deleted even though surviving_branch.system_msg_id holds it.

    foreign_keys=OFF is used here to bypass the FK constraint that would
    ordinarily prevent the delete; this is the exact failure mode in any
    runtime that doesn't enforce FKs (e.g. a migration path, or prior to
    the PRAGMA being applied) or if the message is held by a table without
    a FK constraint (hypothetical future table).
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    _, _, shared_msg_id, unshared_msg_id = run_async(_seed_shared_message_scenario(db_path))

    async def _old_cleanup_fk_off(db_path: Path, candidate_msg_ids: list[str]) -> None:
        """Reproduce the old collection-only NOT IN check with FK enforcement off."""
        async with StateDB(db_path) as db:
            # PRAGMA foreign_keys is a no-op inside a transaction, so drive the
            # raw sqlite connection directly (autocommit) to toggle enforcement —
            # exposing the logic bug rather than the FK constraint.
            async with db._engine.connect() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                # Disable FK enforcement to expose the logic bug, not the constraint.
                await driver.execute("PRAGMA foreign_keys = OFF")
                # Delete the pruned session (and orphan its progression).
                await driver.execute("DELETE FROM sessions WHERE status = 'completed'")
                await driver.execute(
                    "DELETE FROM progressions WHERE id NOT IN ("
                    "  SELECT progression_id FROM sessions"
                    "  UNION SELECT progression_id FROM branches"
                    ")"
                )
                # Old cleanup: only checks collection, not system_msg_id.
                if candidate_msg_ids:
                    ph = ", ".join("?" * len(candidate_msg_ids))
                    await driver.execute(
                        f"DELETE FROM messages WHERE id IN ({ph})"  # noqa: S608
                        " AND id NOT IN ("
                        "  SELECT value FROM progressions, json_each(progressions.collection)"
                        "  WHERE value IS NOT NULL"
                        ")",
                        candidate_msg_ids,
                    )
                await driver.commit()
                await driver.execute("PRAGMA foreign_keys = ON")

    run_async(_old_cleanup_fk_off(db_path, [shared_msg_id, unshared_msg_id]))

    async def _exists_msg(mid: str) -> bool:
        async with StateDB(db_path) as db:
            row = await db.fetch_one("SELECT id FROM messages WHERE id = ?", (mid,))
            return row is not None

    # FAIL-before: shared message is spuriously deleted because system_msg_id
    # was not in the survivor subquery.
    assert not run_async(_exists_msg(shared_msg_id)), (
        "FAIL-before confirmed: collection-only check deletes the shared message"
    )
    assert not run_async(_exists_msg(unshared_msg_id))


def test_shared_message_fk_survivor_check_preserves_it(tmp_path, monkeypatch):
    """PASS-after: full survivor check (collection + FK columns) preserves shared message.

    prune_old_data() UNIONs first_msg_id / last_msg_id (sessions) and
    system_msg_id (branches) in the NOT IN subquery.  shared_msg_id is held
    by surviving_branch.system_msg_id, so it survives even though the pruned
    progression's collection no longer exists.
    The unshared message (only in the pruned progression) is correctly deleted.
    """
    import lionagi.state.db as state_db_mod
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(maint, "DEFAULT_DB_PATH", db_path)

    _, _, shared_msg_id, unshared_msg_id = run_async(_seed_shared_message_scenario(db_path))

    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] == 1

    async def _exists_msg(mid: str) -> bool:
        async with StateDB(db_path) as db:
            row = await db.fetch_one("SELECT id FROM messages WHERE id = ?", (mid,))
            return row is not None

    # PASS-after: shared message survives because surviving_branch.system_msg_id holds it.
    assert run_async(_exists_msg(shared_msg_id)), (
        "PASS-after: message held by surviving branch.system_msg_id must not be deleted"
    )
    # Unshared message is correctly pruned (only referenced by the now-deleted progression).
    assert not run_async(_exists_msg(unshared_msg_id)), (
        "Unshared message (pruned lineage only) must be deleted"
    )
