# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resuming a branch puts its session back into execution.

A session's closing transition only announces itself when the status actually
changes. A resume adopts a session an earlier leg already took terminal, so
writing that same terminal status at the end is not a change: the leg finishes
silently, its completion notice never arrives, and anything waiting on that
notice cannot tell the leg apart from one still running.

The reopen is also the only sanctioned exit from a terminal status, which the
transition service refuses without an override. That refusal is silent from the
caller's side and produces exactly the symptom the reopen exists to remove, so
it is pinned here separately from the notice it enables.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from lionagi.cli._runs import _reopen_session_for_resume
from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB
from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS
from lionagi.state.reasons import RunReasons


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


# The teardown's own status-to-reason mapping, so a session closed as timed_out
# in a test carries the reason a real one would.
_REASON_FOR = {
    "completed": RunReasons.COMPLETED_OK,
    "completed_empty": RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
    "failed": RunReasons.FAILED_EXCEPTION,
    "timed_out": RunReasons.TIMED_OUT_DEADLINE,
    "aborted": RunReasons.CANCELLED_SIGINT,
    "cancelled": RunReasons.CANCELLED_SYSTEM,
}


async def _running_session(db: StateDB) -> str:
    sid = uuid.uuid4().hex[:12]
    prog = str(uuid.uuid4())
    await db.create_progression(prog)
    await db.create_session(
        {
            "id": sid,
            "name": "agent",
            "invocation_kind": "agent",
            "progression_id": prog,
            "status": "running",
            "started_at": 1000.0,
        }
    )
    return sid


async def _finished_session(db: StateDB, *, status: str = "completed") -> str:
    """A session an earlier leg already closed."""
    sid = await _running_session(db)
    await db.update_status(
        "session",
        sid,
        new_status=status,
        reason_code=_REASON_FOR[status],
        source="executor",
        actor=sid,
        extra_fields={"ended_at": 2000.0},
    )
    return sid


@pytest.mark.asyncio
async def test_a_terminal_session_is_reopened_rather_than_refused(temp_db_path):
    """The session policy declares one edge, running to terminal, and refuses
    any exit from terminal without an override. Without one this write is
    rejected and the resume proceeds on a session still marked finished."""
    async with StateDB() as db:
        sid = await _finished_session(db)

        applied = await _reopen_session_for_resume(db, sid, await db.get_session(sid))

        assert applied is True
        assert (await db.get_session(sid))["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(SESSION_TERMINAL_STATUSES))
async def test_every_terminal_status_can_be_reopened(temp_db_path, status):
    """Not just the happy one. A leg resumed after a timeout or an abort has
    the same claim on announcing itself as one resumed after a clean finish."""
    async with StateDB() as db:
        sid = await _finished_session(db, status=status)

        assert await _reopen_session_for_resume(db, sid, await db.get_session(sid)) is True


@pytest.mark.asyncio
async def test_the_close_after_a_reopen_is_a_real_change(temp_db_path):
    """The payoff. Closing a session that was never reopened writes the same
    status it already held, which is not a change, so no terminal event is
    emitted and nothing downstream hears the leg finish."""
    emitted: list[str] = []
    DEFAULT_TERMINAL_CALLBACKS.register(
        "test-observer", lambda env: emitted.append(env.entity.id), kinds=["session"]
    )
    try:
        async with StateDB() as db:
            sid = await _finished_session(db)
            await _reopen_session_for_resume(db, sid, await db.get_session(sid))

            await db.update_status(
                "session",
                sid,
                new_status="completed",
                reason_code=RunReasons.COMPLETED_OK,
                source="executor",
                actor=sid,
            )

        assert sid in emitted
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister("test-observer")


@pytest.mark.asyncio
async def test_a_running_session_is_left_alone(temp_db_path):
    """A resume racing a live leg on the same branch. The row already describes
    the session correctly, and reopening it would be a write with nothing to
    say."""
    async with StateDB() as db:
        sid = await _running_session(db)
        before = await db.get_session(sid)

        assert await _reopen_session_for_resume(db, sid, before) is False
        assert (await db.get_session(sid))["updated_at"] == before["updated_at"]


@pytest.mark.asyncio
async def test_reopening_clears_the_end_time_and_keeps_the_start(temp_db_path):
    """A session cannot both have finished and be executing. Leaving a stale
    end time on a running session is the same defect one column over, and the
    start time belongs to the session rather than to whichever leg is running."""
    async with StateDB() as db:
        sid = await _finished_session(db)

        await _reopen_session_for_resume(db, sid, await db.get_session(sid))

        row = await db.get_session(sid)
        assert row["ended_at"] is None
        assert row["started_at"] == 1000.0


@pytest.mark.asyncio
async def test_a_reopen_leaves_a_record_of_what_did_it(temp_db_path):
    """Reopening is the system's one exception to terminal finality, so it is
    written down rather than passed off as an ordinary status write."""
    async with StateDB() as db:
        sid = await _finished_session(db)

        await _reopen_session_for_resume(db, sid, await db.get_session(sid))

        rows = await db.fetch_all(
            "SELECT action, details FROM admin_events WHERE target_id = ?", (sid,)
        )

    assert any(r["action"] == "status_transition_override" for r in rows)


@pytest.mark.asyncio
async def test_a_missing_session_row_is_not_an_error(temp_db_path):
    """get_session returns None for a branch whose session was pruned. The
    resume should carry on rather than fail on its own bookkeeping."""
    async with StateDB() as db:
        assert await _reopen_session_for_resume(db, "gone", None) is False
