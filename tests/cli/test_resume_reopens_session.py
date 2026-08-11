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

import json
import os
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
    emitted: list = []
    DEFAULT_TERMINAL_CALLBACKS.register(
        "test-observer", lambda env: emitted.append(env), kinds=["session"]
    )
    try:
        async with StateDB() as db:
            sid = await _finished_session(db)
            # The first leg's own close emits. Asserting on the accumulated list
            # would then pass with the reopen removed, which is the vacuous
            # version of this test: only what happens AFTER this point is
            # evidence about the reopen.
            emitted.clear()

            await _reopen_session_for_resume(db, sid, await db.get_session(sid))

            await db.update_status(
                "session",
                sid,
                new_status="completed",
                reason_code=RunReasons.COMPLETED_OK,
                source="executor",
                actor=sid,
            )

        assert [(e.entity.id, e.previous_status, e.terminal_status) for e in emitted] == [
            (sid, "running", "completed")
        ]
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
async def test_reopening_takes_over_the_liveness_markers(temp_db_path):
    """Liveness is judged from the process markers on the row. A terminal
    session is never checked for liveness, so markers left by the leg that
    already exited were harmless; a running one is checked, so keeping them
    would describe this live leg by a dead process and let the phantom reaper
    take a working session to failed."""
    async with StateDB() as db:
        sid = await _running_session(db)
        await db.update_session(sid, node_metadata=json.dumps({"pid": 999999, "keep": "me"}))
        await db.update_status(
            "session",
            sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            source="executor",
            actor=sid,
        )

        await _reopen_session_for_resume(db, sid, await db.get_session(sid))

        meta = (await db.get_session(sid))["node_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)

    assert meta["pid"] == os.getpid()
    assert meta["keep"] == "me"  # unrelated metadata is merged, not replaced


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["null", "legacy", '"a string"', "[1, 2]", "{not json"])
async def test_metadata_that_is_not_an_object_does_not_stop_the_resume(temp_db_path, stored):
    """Values that are not objects do occur in this column; a JSON `null` is
    the ordinary one, from a session that never recorded metadata, and it reads
    back as nothing at all. The shapes that raise are text that is not JSON and
    a JSON scalar or list, which raise in different places: the first when it is
    read, the second when it is merged.
    Every other reader of it ignores what it cannot read as an object; a resume
    that raised here would lose the leg all of its state persistence, which is a
    far worse outcome than dropping a value nothing else consults."""
    async with StateDB() as db:
        sid = await _finished_session(db)
        await db.execute("UPDATE sessions SET node_metadata = ? WHERE id = ?", (stored, sid))

        assert await _reopen_session_for_resume(db, sid, await db.get_session(sid)) is True

        meta = (await db.get_session(sid))["node_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)

    assert meta["pid"] == os.getpid()


@pytest.mark.asyncio
async def test_the_status_and_the_markers_move_together(temp_db_path):
    """Splitting them leaves a window where the row reads running while still
    carrying the previous leg's markers. The sweeps select on status and then
    ask these markers whether the row is alive, so a row that is running for
    even an instant with a dead process recorded is a row they can cancel."""
    async with StateDB() as db:
        sid = await _running_session(db)
        await db.update_session(sid, node_metadata=json.dumps({"pid": 999999}))
        await db.update_status(
            "session",
            sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            source="executor",
            actor=sid,
        )

        async def _no_second_write(*args, **kwargs):
            raise AssertionError("markers were written outside the status transaction")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db, "update_session", _no_second_write)
            assert await _reopen_session_for_resume(db, sid, await db.get_session(sid)) is True

        row = await db.get_session(sid)
        meta = row["node_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)

    assert row["status"] == "running"
    assert meta["pid"] == os.getpid()


@pytest.mark.asyncio
async def test_a_lost_reopen_race_does_not_take_over_another_leg_s_markers(temp_db_path):
    """On a lost race the row belongs to a different leg. Stamping our markers
    on it would make that leg's liveness answer for our process, which is the
    same defect one owner over."""
    async with StateDB() as db:
        sid = await _running_session(db)
        await db.update_session(sid, node_metadata=json.dumps({"pid": 999999}))
        stale = await db.get_session(sid)  # snapshot taken while terminal...
        stale = {**stale, "status": "completed"}  # ...as this leg believed it to be

        # The row is actually running (another leg owns it), so the guarded
        # reopen finds nothing to move.
        assert await _reopen_session_for_resume(db, sid, stale) is False

        meta = (await db.get_session(sid))["node_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)

    assert meta["pid"] == 999999


@pytest.mark.asyncio
async def test_a_missing_session_row_is_not_an_error(temp_db_path):
    """get_session returns None for a branch whose session was pruned. The
    resume should carry on rather than fail on its own bookkeeping."""
    async with StateDB() as db:
        assert await _reopen_session_for_resume(db, "gone", None) is False


@pytest.mark.asyncio
async def test_a_session_deleted_while_the_resume_waited_is_not_an_error(temp_db_path):
    """The row can be read as terminal and be gone by the time it is written.

    Maintenance removes old terminal sessions and holds each candidate for the
    length of its transaction, so a resume that arrives just before one starts
    is released only after it commits. Nothing is left to reopen, and treating
    that as a failure would stop the leg over bookkeeping it does not need.
    """
    async with StateDB() as db:
        sid = await _finished_session(db)
        snapshot = await db.get_session(sid)
        await db.execute("DELETE FROM sessions WHERE id = ?", (sid,))

        assert await _reopen_session_for_resume(db, sid, snapshot) is False


@pytest.mark.asyncio
async def test_a_resumed_leg_links_its_session_to_its_own_invocation(temp_db_path):
    """A resumed leg reopens the branch's existing session row instead of
    inserting a new one, so create_session's ON CONFLICT DO NOTHING never
    runs for it and the resume's invocation_id was silently dropped. The
    invocation that actually drove the resume must still be able to find the
    session it drove.
    """
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist, teardown_agent_persist

    branch = Branch(name="resumed")
    async with StateDB() as db:
        await db.create_invocation({"id": "first-inv", "skill": "agent", "started_at": 1.0})
        await db.create_invocation({"id": "resume-inv", "skill": "resume:agent", "started_at": 2.0})

        first = await setup_agent_persist(
            branch, agent_name="implementer", invocation_id="first-inv"
        )
        assert first is not None
        await teardown_agent_persist(first, status="completed")

        second = await setup_agent_persist(
            branch, agent_name="implementer", invocation_id="resume-inv"
        )
        assert second is not None
        assert second["session_id"] == first["session_id"], "resume must reuse the same session"

        resumed_invocation = await db.get_invocation("resume-inv")
        assert resumed_invocation["session_count"] == 1
        sessions = await db.list_sessions_for_invocation("resume-inv")
        assert [s["id"] for s in sessions] == [second["session_id"]]

        # The invocation the session left behind must stop claiming it, or
        # the lifecycle reaper's session_count == 0 check never fires for it.
        first_invocation = await db.get_invocation("first-inv")
        assert first_invocation["session_count"] == 0
        assert await db.list_sessions_for_invocation("first-inv") == []


@pytest.mark.asyncio
async def test_a_resume_whose_session_is_pruned_mid_setup_still_records_itself(
    temp_db_path, monkeypatch
):
    """Persistence reads the branch and its session as two separate reads.

    A maintenance pass can commit between them, so the branch is read as
    present and its session comes back missing. Reading the rest of the
    resume out of the branch's copy of a row that no longer exists takes the
    whole run down to no persistence at all, which is a much larger loss than
    the session that went away. The leg records itself under a new session
    instead, the same way a branch nobody has seen before does.
    """
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist, teardown_agent_persist

    branch = Branch(name="resumed")
    async with StateDB() as db:
        first = await setup_agent_persist(branch, agent_name="implementer")
        assert first is not None
        await teardown_agent_persist(first, status="completed")
        pruned_id = first["session_id"]

    real_get_session = StateDB.get_session

    async def get_session_after_a_prune(self, session_id):
        row = await real_get_session(self, session_id)
        if row is not None and session_id == pruned_id:
            # The maintenance pass commits here, between the two reads.
            await self.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return None
        return row

    # Scoped to this patch alone: undoing every patch would also put back the
    # database path this test is redirected away from.
    with monkeypatch.context() as mp:
        mp.setattr(StateDB, "get_session", get_session_after_a_prune)
        second = await setup_agent_persist(branch, agent_name="implementer")

    assert second is not None, "the resumed leg was left with no persistence at all"
    assert second["session_id"] != pruned_id

    async with StateDB() as db:
        assert await db.get_session(second["session_id"]) is not None
