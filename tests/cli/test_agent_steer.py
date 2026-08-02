# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Agent-leg steer: enqueue gate, turn-end drain, terminal tombstone, status.

A `message` control queued against a running agent session lands as a warm
continuation turn when the in-flight operate() returns. pause/resume have no
seam inside a single turn and are refused at enqueue. A steer the run never
consumed is finalized rejected at teardown, and the status surface renders a
pending control on a terminal run as never-landed regardless.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.agent import _drain_pending_steers, _tombstone_pending_steers
from lionagi.cli.orchestrate._control import run_ctl_msg, run_ctl_pause, run_ctl_resume
from lionagi.cli.status import EXIT_UNKNOWN
from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_agent_session(
    db: StateDB,
    *,
    status: str = "running",
    run_id: str | None = "20260801T000000-testrun",
    cc_session_id: str | None = None,
) -> str:
    """A native `li agent` session carries a run_id (the runner stamps one);
    a mirrored Claude Code / Codex session is also invocation_kind='agent'
    but has no run_id and no runner to drain its controls."""
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    row = {
        "id": sid,
        "progression_id": pid,
        "status": status,
        "invocation_kind": "agent",
        "started_at": time.time(),
    }
    if run_id is not None:
        row["run_id"] = run_id
    if cc_session_id is not None:
        row["cc_session_id"] = cc_session_id
    await db.create_session(row)
    return sid


class _RecordingBranch:
    """Fake branch: records operate() calls; optionally enqueues a follow-up
    steer during the first continuation to exercise the drain's second pass."""

    def __init__(self, db: StateDB | None = None, session_id: str | None = None):
        self.calls: list[dict] = []
        self._db = db
        self._session_id = session_id
        self._enqueue_once = db is not None

    async def operate(self, *, instruction: str, **kwargs):
        self.calls.append({"instruction": instruction, **kwargs})
        if self._enqueue_once:
            self._enqueue_once = False
            await self._db.insert_session_control(
                session_id=self._session_id,
                verb="message",
                payload={"text": "second steer"},
            )
        return f"turn-{len(self.calls)}"


# ── enqueue gate ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_msg_enqueues_for_running_agent_session(temp_db_path, capsys):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
    rc = run_ctl_msg(argparse.Namespace(id=sid, text="redirect"))
    assert rc == 0
    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert [row["verb"] for row in pending] == ["message"]
    assert pending[0]["payload"] == {"text": "redirect"}


@pytest.mark.anyio
@pytest.mark.parametrize("runner", [run_ctl_pause, run_ctl_resume])
async def test_pause_resume_refused_for_agent_kind(temp_db_path, caplog, runner):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
    with caplog.at_level("ERROR"):
        rc = runner(argparse.Namespace(id=sid))
    assert rc == EXIT_UNKNOWN
    assert "seam" in caplog.text
    async with StateDB() as db:
        assert await db.list_pending_session_controls(sid) == []


@pytest.mark.anyio
async def test_msg_refused_for_mirrored_agent_session(temp_db_path, caplog):
    """A mirrored Claude Code session is agent-kind and can read as running,
    but no lionagi runner owns it, so a steer could never be delivered."""
    async with StateDB() as db:
        sid = await _make_agent_session(db, run_id=None, cc_session_id=uuid.uuid4().hex)
    with caplog.at_level("ERROR"):
        rc = run_ctl_msg(argparse.Namespace(id=sid, text="redirect"))
    assert rc == EXIT_UNKNOWN
    assert "mirrored/imported" in caplog.text
    async with StateDB() as db:
        assert await db.list_pending_session_controls(sid) == []


@pytest.mark.anyio
async def test_msg_refused_for_terminal_agent_session(temp_db_path, capsys):
    async with StateDB() as db:
        sid = await _make_agent_session(db, status="completed")
    rc = run_ctl_msg(argparse.Namespace(id=sid, text="too late"))
    assert rc == EXIT_UNKNOWN
    async with StateDB() as db:
        assert await db.list_pending_session_controls(sid) == []


# ── run-id addressing (the operator's actual handle for an agent leg) ──────


@pytest.mark.anyio
async def test_msg_enqueues_by_run_id(temp_db_path):
    """The run id is what `li agent` prints back to the operator — it must
    resolve to the session it was stamped on, not just the session id."""
    run_id = "20260801T010101-steerrun"
    async with StateDB() as db:
        sid = await _make_agent_session(db, run_id=run_id)
    rc = run_ctl_msg(argparse.Namespace(id=run_id, text="redirect by run id"))
    assert rc == 0
    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert [row["verb"] for row in pending] == ["message"]
    assert pending[0]["payload"] == {"text": "redirect by run id"}


@pytest.mark.anyio
async def test_msg_enqueues_by_run_id_prefix(temp_db_path):
    run_id = "20260801T020202-steerrun"
    async with StateDB() as db:
        sid = await _make_agent_session(db, run_id=run_id)
    rc = run_ctl_msg(argparse.Namespace(id=run_id[:12], text="prefix redirect"))
    assert rc == 0
    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert [row["verb"] for row in pending] == ["message"]


@pytest.mark.anyio
async def test_msg_by_run_id_picks_the_most_recently_updated_session(temp_db_path):
    """`run_id` carries no uniqueness constraint — `get_sessions_for_run`
    already documents that one run can persist more than one session. The
    fallback must not pick whichever session happens to sort first; it must
    pick the live one."""
    run_id = "20260801T030303-steerrun"
    async with StateDB() as db:
        stale_sid = await _make_agent_session(db, run_id=run_id)
        await db.update_session(stale_sid, status="timed_out")
        await asyncio.sleep(0.01)
        live_sid = await _make_agent_session(db, run_id=run_id)
        await db.update_session(live_sid, status="running")
    rc = run_ctl_msg(argparse.Namespace(id=run_id, text="redirect the live leg"))
    assert rc == 0
    async with StateDB() as db:
        assert len(await db.list_pending_session_controls(live_sid)) == 1
        assert await db.list_pending_session_controls(stale_sid) == []


@pytest.mark.anyio
async def test_msg_by_unmatched_run_id_fails_cleanly(temp_db_path, caplog):
    """An id that resolves nowhere — not a session, invocation, play, branch,
    or run — must fail with a clean refusal, not raise or silently pick."""
    async with StateDB() as db:
        await _make_agent_session(db, run_id="20260801T040404-steerrun")
    with caplog.at_level("ERROR"):
        rc = run_ctl_msg(argparse.Namespace(id="20260801T999999-nomatch", text="hello"))
    assert rc == EXIT_UNKNOWN
    assert "no session/invocation/play found" in caplog.text


@pytest.mark.anyio
async def test_msg_by_ambiguous_run_id_prefix_raises(temp_db_path, caplog):
    """Two distinct run ids sharing a prefix must refuse, not silently pick
    one — the same guarantee `fetch_unique_row` gives every other id kind."""
    async with StateDB() as db:
        await _make_agent_session(db, run_id="20260801T050505-runA")
        await _make_agent_session(db, run_id="20260801T050505-runB")
    with caplog.at_level("ERROR"):
        rc = run_ctl_msg(argparse.Namespace(id="20260801T050505-run", text="hello"))
    assert rc == EXIT_UNKNOWN
    assert "ambiguous id prefix" in caplog.text


@pytest.mark.anyio
async def test_msg_by_full_session_id_unaffected_by_run_id_fallback(temp_db_path):
    """Control: resolving by the full session id (the pre-existing path) must
    keep working unchanged — this passes on both sides of the run-id fix."""
    async with StateDB() as db:
        sid = await _make_agent_session(db)
    rc = run_ctl_msg(argparse.Namespace(id=sid, text="unchanged path"))
    assert rc == 0
    async with StateDB() as db:
        assert len(await db.list_pending_session_controls(sid)) == 1


# ── turn-end drain ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_drain_consumes_pending_steer_as_continuation(temp_db_path):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "batch mode now"}
        )
        branch = _RecordingBranch()
        res = await _drain_pending_steers(
            {"db": db, "session_id": sid},
            branch,
            operate_kwargs={"stream_persist": True},
            deadline=None,
        )
        assert res == "turn-1"
        assert len(branch.calls) == 1
        assert "batch mode now" in branch.calls[0]["instruction"]
        assert "[OPERATOR STEER]" in branch.calls[0]["instruction"]
        # No override claim: a banner asserting authority reads as injection.
        assert "supersede" not in branch.calls[0]["instruction"].lower()
        assert branch.calls[0]["stream_persist"] is True
        pending = await db.list_pending_session_controls(sid)
        assert pending == []


@pytest.mark.anyio
async def test_drain_joins_multiple_steers_into_one_turn(temp_db_path):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        await db.insert_session_control(session_id=sid, verb="message", payload={"text": "first"})
        await db.insert_session_control(session_id=sid, verb="message", payload={"text": "second"})
        branch = _RecordingBranch()
        await _drain_pending_steers(
            {"db": db, "session_id": sid}, branch, operate_kwargs={}, deadline=None
        )
        assert len(branch.calls) == 1
        instruction = branch.calls[0]["instruction"]
        assert instruction.index("first") < instruction.index("second")


@pytest.mark.anyio
async def test_drain_catches_steer_enqueued_during_continuation(temp_db_path):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        await db.insert_session_control(session_id=sid, verb="message", payload={"text": "first"})
        branch = _RecordingBranch(db=db, session_id=sid)
        res = await _drain_pending_steers(
            {"db": db, "session_id": sid}, branch, operate_kwargs={}, deadline=None
        )
        assert len(branch.calls) == 2
        assert "second steer" in branch.calls[1]["instruction"]
        assert res == "turn-2"
        assert await db.list_pending_session_controls(sid) == []


@pytest.mark.anyio
async def test_drain_noop_without_pending_steers(temp_db_path):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        branch = _RecordingBranch()
        res = await _drain_pending_steers(
            {"db": db, "session_id": sid}, branch, operate_kwargs={}, deadline=None
        )
        assert res is None
        assert branch.calls == []


@pytest.mark.anyio
async def test_drain_stops_past_deadline_without_consuming(temp_db_path):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        await db.insert_session_control(session_id=sid, verb="message", payload={"text": "late"})
        branch = _RecordingBranch()
        res = await _drain_pending_steers(
            {"db": db, "session_id": sid},
            branch,
            operate_kwargs={},
            deadline=time.monotonic() - 1.0,
        )
        assert res is None
        assert branch.calls == []
        # Not consumed: the row stays pending for the teardown tombstone.
        assert len(await db.list_pending_session_controls(sid)) == 1


@pytest.mark.anyio
async def test_drain_without_live_session_is_noop(temp_db_path):
    branch = _RecordingBranch()
    assert await _drain_pending_steers(None, branch, operate_kwargs={}, deadline=None) is None
    assert await _drain_pending_steers({}, branch, operate_kwargs={}, deadline=None) is None
    assert branch.calls == []


# ── terminal tombstone ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_tombstone_rejects_never_consumed_steer(temp_db_path):
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "never lands"}
        )
        await _tombstone_pending_steers({"db": db, "session_id": sid})
        assert await db.list_pending_session_controls(sid) == []
        row = await db.get_session_control(cid)
        assert row["result"].startswith("rejected:")
        assert "li agent -r" in row["result"]


@pytest.mark.anyio
async def test_tombstone_failure_logs_and_does_not_raise(temp_db_path, caplog):
    class _BrokenDB:
        async def list_pending_session_controls(self, _sid):
            raise RuntimeError("db gone")

    with caplog.at_level("ERROR"):
        await _tombstone_pending_steers({"db": _BrokenDB(), "session_id": "s1"})
    assert "tombstone write failed" in caplog.text


async def test_drain_says_so_when_a_persisted_session_arrives_without_a_db(caplog):
    """setup_agent_persist always supplies both a session id and the database
    handle to read it with. If only one arrives, nothing can be drained -- but
    returning quietly would make that indistinguishable from "no steers were
    queued", which is the answer a caller would act on. The failure path names
    itself instead.
    """
    with caplog.at_level("ERROR"):
        result = await _drain_pending_steers(
            {"session_id": "s1"},
            None,
            operate_kwargs={},
            deadline=None,
        )

    assert result is None
    assert "no database handle" in caplog.text


async def test_drain_returns_quietly_when_there_is_no_persistence_at_all(caplog):
    """The control for the above: no session id either means the leg simply is
    not persisted, which is ordinary and must not log an error. This passes
    both before and after the missing-handle guard, so it distinguishes "not
    persisted" from "persisted but unreadable" rather than testing the guard.
    """
    with caplog.at_level("ERROR"):
        result = await _drain_pending_steers({}, None, operate_kwargs={}, deadline=None)

    assert result is None
    assert "no database handle" not in caplog.text


# ── at-most-once and the deadline boundary ───────────────────────────────────


@pytest.mark.anyio
async def test_claiming_a_control_twice_only_succeeds_once(temp_db_path):
    """The claim is a compare-and-set, so it is the thing that makes the drain
    at-most-once rather than the order the callers happen to run in."""
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "once"}
        )
        assert await db.mark_session_control_applying(cid) is True
        assert await db.mark_session_control_applying(cid) is False, (
            "a second consumer claimed a control that was already claimed"
        )


@pytest.mark.anyio
async def test_drain_leaves_an_already_applying_row_untouched(temp_db_path):
    """A row stamped `applying` is a drain that stopped between the stamp and
    the apply. Re-running it would deliver the same operator message twice, so
    it is left alone, which is the rule the flow poller already follows.

    Two independent things produce the empty call list here: the claim refuses a
    row it has already stamped, and the drain stops at an `applying` row. Either
    alone passes this test, so the arm that separates them is the ordering case
    below, not this one.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "already going out"}
        )
        await db.mark_session_control_applying(cid)

        branch = _RecordingBranch()
        res = await _drain_pending_steers(
            {"db": db, "session_id": sid}, branch, operate_kwargs={}, deadline=None
        )

        assert branch.calls == [], "an in-flight steer was applied a second time"
        assert res is None
        # Still pending and still claimed: visible to the tombstone and to
        # status, not silently dropped.
        pending = await db.list_pending_session_controls(sid)
        assert [r["result"] for r in pending] == ["applying"]


@pytest.mark.anyio
async def test_drain_does_not_jump_a_stuck_row_to_apply_the_one_behind_it(temp_db_path):
    """A steer stuck mid-apply holds the queue rather than being stepped over.

    The claim alone is not enough here. It refuses the stuck row, but the drain
    would then walk on to the next one and deliver a later instruction while an
    earlier one is still in flight, which is the operator's messages arriving out
    of order. Stopping at the stuck row is what preserves the order, and this is
    the only arm that fails when that check is removed.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        stuck = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "first instruction"}
        )
        await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "second instruction"}
        )
        await db.mark_session_control_applying(stuck)

        branch = _RecordingBranch()
        await _drain_pending_steers(
            {"db": db, "session_id": sid}, branch, operate_kwargs={}, deadline=None
        )

        assert branch.calls == [], (
            "a later steer was delivered while an earlier one was still mid-apply"
        )


@pytest.mark.anyio
async def test_a_second_drain_does_not_reapply_a_steer_the_first_is_mid_apply(temp_db_path):
    """Two consumers on one session, held at the boundary that matters: the
    first has claimed the row and is inside `operate`, the second drains then.

    This is reachable when more than one resume leg attaches to a running
    session, since attaching retains the session rather than taking a
    single-consumer lease. Exactly one continuation may carry the message.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "deploy now"}
        )

        calls: list[str] = []
        first_is_mid_apply = asyncio.Event()
        release_first = asyncio.Event()

        class _ParkingBranch:
            async def operate(self, *, instruction: str, **kwargs):
                calls.append(instruction)
                first_is_mid_apply.set()
                await release_first.wait()
                return "turn-1"

        class _SecondBranch:
            async def operate(self, *, instruction: str, **kwargs):
                calls.append(instruction)
                return "turn-2"

        async def second_consumer() -> None:
            await first_is_mid_apply.wait()
            await _drain_pending_steers(
                {"db": db, "session_id": sid}, _SecondBranch(), operate_kwargs={}, deadline=None
            )
            release_first.set()

        second = asyncio.create_task(second_consumer())
        await _drain_pending_steers(
            {"db": db, "session_id": sid}, _ParkingBranch(), operate_kwargs={}, deadline=None
        )
        await asyncio.wait_for(second, timeout=10)

        assert len(calls) == 1, f"the steer was delivered {len(calls)} times, not once"


@pytest.mark.anyio
async def test_drain_does_not_start_a_continuation_after_the_deadline(temp_db_path):
    """The deadline is checked before the queue read, and the read is I/O that
    can cross it. A continuation started afterwards runs work the caller's
    timeout already forbade, and flooring its budget hands it a fresh second to
    do that work in.

    The discriminating assertion is the empty call list. Without the recheck the
    drain calls operate with `timeout=1.0` after the deadline has passed.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "too late"}
        )

        real_list = db.list_pending_session_controls

        async def slow_list(session_id):
            await asyncio.sleep(0.08)
            return await real_list(session_id)

        db.list_pending_session_controls = slow_list
        branch = _RecordingBranch()
        res = await _drain_pending_steers(
            {"db": db, "session_id": sid},
            branch,
            operate_kwargs={},
            deadline=time.monotonic() + 0.02,
        )

        assert branch.calls == [], "a continuation started after the run's deadline"
        assert res is None
        # Untouched, so the terminal tombstone reports it rather than a
        # half-claimed row nobody finalizes.
        pending = await real_list(sid)
        assert [r["result"] for r in pending] == [None]
