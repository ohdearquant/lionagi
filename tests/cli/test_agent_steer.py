# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Agent-leg steer: enqueue gate, turn-end drain, terminal tombstone, status.

A `message` control queued against a running agent session lands as a warm
continuation turn when the in-flight operate() returns. pause/resume have no
seam inside a single turn and are refused at enqueue. A steer no consumer ever
claimed is finalized rejected at teardown, and the status surface renders it as
never-landed regardless. A steer a consumer did claim is a different state and
stays standing: it names its owner and the time the claim was taken, because
nothing after the fact can say whether that consumer delivered the message.
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


async def _terminalize(db: StateDB, sid: str) -> None:
    """Take a session through its real terminal transition.

    Written as the transition rather than a status poke because the writer's
    admission condition and the teardown sweep both read the same column, and a
    test that set it some other way would not exercise the ordering they rely on.
    """
    await db.update_status("session", sid, new_status="completed", reason_code="run.completed.ok")


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
    """Queued while the run was live, terminalized, then swept.

    The terminalize step is not scene-setting. The sweep runs after the run's
    terminal transition, and refuses to touch a session that has not made it,
    so a call site that swept first would leave this row pending and fail here.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "never lands"}
        )
        await _terminalize(db, sid)
        await _tombstone_pending_steers({"db": db, "session_id": sid})
        assert await db.list_pending_session_controls(sid) == []
        row = await db.get_session_control(cid)
        assert row["result"].startswith("rejected:")
        assert "li agent -r" in row["result"]


@pytest.mark.anyio
async def test_tombstone_declines_to_sweep_a_session_that_is_still_running(temp_db_path, caplog):
    """The sweep's precondition, asserted rather than assumed.

    Rejecting a control on a live session destroys a steer whose consumer has
    not had its turn yet. The sweep is only safe because the terminal
    transition it follows is what stops new controls being admitted, so it
    refuses when that transition has not happened and says why.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "still deliverable"}
        )
        with caplog.at_level("ERROR"):
            await _tombstone_pending_steers({"db": db, "session_id": sid})

        row = await db.get_session_control(cid)
        assert row["result"] is None, "a steer on a live run was tombstoned"
        assert row["applied_at"] is None
        assert "not terminal" in caplog.text


@pytest.mark.anyio
async def test_tombstone_failure_logs_and_does_not_raise(temp_db_path, caplog):
    class _BrokenDB:
        async def get_session(self, _sid):
            # Terminal, so the sweep's precondition passes and the failure
            # below is what this test is actually about.
            return {"id": _sid, "status": "completed"}

        async def list_pending_session_controls(self, _sid):
            raise RuntimeError("db gone")

    with caplog.at_level("ERROR"):
        await _tombstone_pending_steers({"db": _BrokenDB(), "session_id": "s1"})
    assert "tombstone write failed" in caplog.text
    assert "db gone" in caplog.text


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
        assert await db.mark_session_control_applying(cid) == "applying"
        assert await db.mark_session_control_applying(cid) is None, (
            "a second consumer claimed a control that was already claimed"
        )


@pytest.mark.anyio
async def test_the_claim_comes_back_so_the_claimant_can_guard_its_own_finalize(temp_db_path):
    """The winner is handed the exact string it wrote. Rebuilding that string at
    the call site is how a guard stops matching the row it is supposed to guard,
    so the only copy lives in the claimer."""
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "hi"}
        )
        claim = await db.mark_session_control_applying(cid, owner="leg-a")
        assert claim == "applying:leg-a"
        # The returned value is usable as-is: it matches the stored row, so a
        # finalize carrying it lands.
        assert (await db.get_session_control(cid))["result"] == claim
        assert await db.finalize_session_control(cid, result="applied", expect_claim=claim)
        assert (await db.get_session_control(cid))["result"] == "applied"


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


# ── claim ownership: a claimed row belongs to its claimant ───────────────────


@pytest.mark.anyio
async def test_a_teardown_does_not_reject_a_steer_another_leg_is_mid_apply(temp_db_path):
    """Two legs on one session, held at the boundary the claim protocol exists for.

    Leg A claims the row and parks inside operate(). Leg B finishes and runs the
    same teardown sweep A's own run will run. A sweep that finalized every
    pending row would write `rejected` onto a message A is at that moment
    delivering, and A's later finalize would overwrite it with `applied` -- two
    contradictory terminal records for one delivery, the first of which an
    operator may well read and act on by resending.

    Both assertions discriminate. Without the claim-owner narrowing the row
    reads `rejected` while A is still inside operate(); without the claim token
    on the finalize a foreign write could still close it.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "deploy now"}
        )

        calls: list[str] = []
        a_is_mid_apply = asyncio.Event()
        b_has_torn_down = asyncio.Event()

        class _ParkingBranch:
            async def operate(self, *, instruction: str, **kwargs):
                calls.append(instruction)
                a_is_mid_apply.set()
                await b_has_torn_down.wait()
                return "turn-1"

        observed_mid_apply: dict = {}

        async def leg_b() -> None:
            try:
                await a_is_mid_apply.wait()
                # B's own run is over; its teardown terminalizes the shared
                # session and then sweeps, which is the production order.
                await _terminalize(db, sid)
                await _tombstone_pending_steers({"db": db, "session_id": sid})
                observed_mid_apply.update(await db.get_session_control(cid))
            finally:
                # Released unconditionally. A failed observation must surface as
                # a failed assertion below, not as A parked forever waiting for
                # an event a raising task never set.
                b_has_torn_down.set()

        b = asyncio.create_task(leg_b())
        await _drain_pending_steers(
            {"db": db, "session_id": sid},
            _ParkingBranch(),
            operate_kwargs={},
            deadline=None,
            owner="leg-a",
        )
        await asyncio.wait_for(b, timeout=10)

        assert observed_mid_apply.get("result") == "applying:leg-a", (
            "another leg's live claim was overwritten with "
            f"{observed_mid_apply.get('result')!r} while its consumer was inside operate()"
        )
        assert observed_mid_apply.get("applied_at") is None
        assert len(calls) == 1, f"the steer was delivered {len(calls)} times, not once"
        row = await db.get_session_control(cid)
        assert row["result"] == "applied", (
            "the leg that performed the delivery could not record its own outcome"
        )
        assert row["applied_at"] is not None


@pytest.mark.anyio
async def test_a_claim_carries_its_owner_and_the_time_it_was_taken(temp_db_path):
    """The claim is what an operator reads off a wedged queue, so it has to say
    who holds the row and since when. A bare 'applying' answers neither."""
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "hold here"}
        )
        before = time.time()
        assert await db.mark_session_control_applying(cid, owner="20260802T120000-abc123")

        row = await db.get_session_control(cid)
        assert row["result"] == "applying:20260802T120000-abc123"
        assert row["claimed_at"] is not None and row["claimed_at"] >= before
        assert row["applied_at"] is None, "a claim is not an outcome"


@pytest.mark.anyio
async def test_a_finalize_cannot_close_a_claim_it_does_not_hold(temp_db_path):
    """The token is checked at the write, not trusted from the caller.

    Without it, any consumer holding the control id can stamp a terminal result
    on work another consumer performed, which is how a delivered message ends up
    on record as rejected.
    """
    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "mine"}
        )
        assert await db.mark_session_control_applying(cid, owner="leg-a")

        wrote = await db.finalize_session_control(
            cid, result="applied", expect_claim="applying:leg-b"
        )
        assert wrote is False
        row = await db.get_session_control(cid)
        assert row["result"] == "applying:leg-a"
        assert row["applied_at"] is None

        assert await db.finalize_session_control(
            cid, result="applied", expect_claim="applying:leg-a"
        )
        assert (await db.get_session_control(cid))["result"] == "applied"


@pytest.mark.anyio
async def test_a_claim_whose_leg_died_stays_visible_rather_than_being_resolved(temp_db_path):
    """The wedge is the honest state and it is left standing deliberately.

    Nothing here can tell a leg that died before delivering from one that died
    after, so `rejected` would assert an undelivered message and `applied` would
    assert a delivered one. The row keeps its claim, and the status surface
    names the owner and the age instead of rendering it as never-landed.
    """
    from lionagi.cli.status import _build_view

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "did this go out?"}
        )
        await db.mark_session_control_applying(cid, owner="20260802T120000-deadleg")
        await _terminalize(db, sid)
        await _tombstone_pending_steers({"db": db, "session_id": sid})

        row = await db.get_session_control(cid)
        assert row["result"] == "applying:20260802T120000-deadleg"
        assert row["applied_at"] is None

        view = await _build_view(
            db, command="ctl", entity_type="session", row=await db.get_session(sid)
        )
        assert view["terminal"] is True
        (ctl,) = view["pending_controls"]
        assert ctl["never_landed"] is False, (
            "a claimed row was rendered as never-landed, which asserts a "
            "non-delivery nobody established"
        )
        assert ctl["result"] == "applying:20260802T120000-deadleg"
        assert ctl["claimed_at"] is not None


# ── enqueue against a run that is terminalizing ──────────────────────────────


@pytest.mark.anyio
async def test_an_enqueue_that_loses_the_race_to_terminalization_is_refused(temp_db_path, caplog):
    """The caller-side status read and the insert are two statements.

    Here the read returns a running session and the run terminalizes before the
    insert, which is the interleaving that used to leave a control queued
    against a run with no consumer left. The insert carries the condition
    itself, so it writes nothing and the caller refuses.
    """
    import lionagi.cli.orchestrate._control as ctl_mod

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        await _terminalize(db, sid)
        stale = await db.get_session(sid)

    async def _stale_resolve(_db, _entity_id):
        # What the caller saw a moment before the transition landed.
        return {**stale, "status": "running"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ctl_mod, "_resolve_session", _stale_resolve)
        with caplog.at_level("ERROR"):
            rc = run_ctl_msg(argparse.Namespace(id=sid, text="too late"))

    assert rc == EXIT_UNKNOWN
    assert "terminal status" in caplog.text
    async with StateDB() as db:
        assert await db.list_pending_session_controls(sid) == [], (
            "a control was queued against a run that had already stopped"
        )


@pytest.mark.asyncio
async def test_the_runner_sweeps_after_it_terminalizes_not_before(
    temp_db_path, tmp_path, monkeypatch
):
    """The teardown ordering, asserted at the call site rather than assumed.

    The sweep and the run's terminal transition both happen in `_run_agent`'s
    finally block, and which one goes first decides whether a control admitted
    at the last moment has anywhere to land: the writer admits one only while
    the session reads running, so sweeping first leaves a window between the
    sweep's read and the transition in which a control can be accepted and then
    never consumed by anyone.

    Wired with a real database and a real terminal transition, so the assertion
    is on the stored row. If the sweep were moved back ahead of the transition
    the session would still read running when it ran, the sweep would decline on
    its own precondition, and this control would still be pending.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli.agent import _run_agent
    from lionagi.service.manager import iModelManager

    db = StateDB()
    await db.__aenter__()
    try:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "queued at the wire"}
        )

        async def fake_operate(self, instruction=None, **kw):
            return "done"

        async def fake_setup(*a, **kw):
            return {"db": db, "session_id": sid}

        async def fake_teardown(ctx, *, status="completed", **kw):
            # What the real teardown does that matters here: the session's
            # terminal transition.
            await _terminalize(db, sid)
            return status

        async def no_drain(*a, **kw):
            # The drain is a separate concern and would consume the row; this
            # test is about what happens to a row the drain did not take.
            return None

        monkeypatch.setattr(Branch, "operate", fake_operate)
        monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
        monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
        monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
        monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
        monkeypatch.setattr(agent_mod, "_drain_pending_steers", no_drain)
        monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
        monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
        monkeypatch.setattr(
            agent_mod,
            "_provenance",
            SimpleNamespace(
                resolve_model_spec=lambda p, m: f"{p}/{m}",
                agent_definition_hash=lambda n: "abc",
            ),
        )
        monkeypatch.setattr(
            agent_mod,
            "allocate_run",
            lambda: SimpleNamespace(
                run_id="20260802T000000-orderrun",
                artifact_root=tmp_path / "artifacts",
                stream_dir=tmp_path / "stream",
                branches_dir=tmp_path / "branches",
            ),
        )

        await _run_agent("claude", "do the thing")

        row = await db.get_session_control(cid)
        assert row["result"] is not None, (
            "the control was left pending, so the sweep ran while the session "
            "still read running and declined on its own precondition"
        )
        assert row["result"].startswith("rejected:")
        assert row["applied_at"] is not None
    finally:
        await db.__aexit__(None, None, None)


# ── operator resolution of a wedged claim ────────────────────────────────────


@pytest.mark.anyio
async def test_an_operator_can_close_a_wedged_claim_and_the_claim_survives_in_the_record(
    temp_db_path, capsys
):
    """The verb the design requires a human to have.

    A claimed row on a terminal run is deliberately not resolved by anything
    automatic, which only makes it a degraded state rather than an abandoned one
    if something in the product can end it. This is that something, and what it
    writes has to keep the claim: the value of leaving the row standing is the
    record of who held the message and what a human then decided about it.
    """
    from lionagi.cli.orchestrate._control import run_ctl_resolve

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "did this go out?"}
        )
        await db.mark_session_control_applying(cid, owner="20260802T120000-deadleg")
        await _terminalize(db, sid)
        await _tombstone_pending_steers({"db": db, "session_id": sid})

    rc = run_ctl_resolve(argparse.Namespace(control_id=cid, outcome="abandoned"))
    assert rc == 0

    async with StateDB() as db:
        row = await db.get_session_control(cid)
        assert row["applied_at"] is not None, "the row is still pending after being resolved"
        assert row["result"].startswith("abandoned:")
        assert "20260802T120000-deadleg" in row["result"], (
            "the claim it replaced was not preserved, so the record no longer says "
            "who held the message"
        )
        assert await db.list_pending_session_controls(sid) == []


@pytest.mark.anyio
async def test_resolve_refuses_a_row_no_consumer_claimed(temp_db_path, caplog):
    """Refusing here is what keeps the verb from standing in for the teardown
    sweep. An unclaimed pending row has a truthful automatic outcome, and a
    hand-written one would replace a fact with an opinion."""
    from lionagi.cli.orchestrate._control import run_ctl_resolve

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "never claimed"}
        )

    with caplog.at_level("ERROR"):
        rc = run_ctl_resolve(argparse.Namespace(control_id=cid, outcome="applied"))
    assert rc == EXIT_UNKNOWN
    assert "not a claimed row" in caplog.text

    async with StateDB() as db:
        row = await db.get_session_control(cid)
        assert row["result"] is None
        assert row["applied_at"] is None


@pytest.mark.anyio
async def test_resolve_refuses_a_row_its_consumer_already_finalized(temp_db_path, caplog):
    """The consumer's own record outranks a later hand-written one: it was
    written by the only party that knew."""
    from lionagi.cli.orchestrate._control import run_ctl_resolve

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "delivered"}
        )
        await db.mark_session_control_applying(cid, owner="leg-a")
        await db.finalize_session_control(cid, result="applied", expect_claim="applying:leg-a")

    with caplog.at_level("ERROR"):
        rc = run_ctl_resolve(argparse.Namespace(control_id=cid, outcome="abandoned"))
    assert rc == EXIT_UNKNOWN

    async with StateDB() as db:
        assert (await db.get_session_control(cid))["result"] == "applied"


@pytest.mark.anyio
async def test_the_terminal_header_does_not_say_never_landed_over_a_claimed_row(temp_db_path):
    """The header speaks for every row beneath it.

    Saying "never landed" above a row that says "outcome unknown" tells the
    operator a message was not delivered where the protocol says delivery is
    unknowable, and a reader who believes the header resends. The per-row text
    was already right; the section title was the assertion nobody had checked.
    """
    from lionagi.cli.status import _build_view, _render_human

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "unknown"}
        )
        await db.mark_session_control_applying(cid, owner="leg-a")
        await _terminalize(db, sid)

        view = await _build_view(
            db, command="ctl", entity_type="session", row=await db.get_session(sid)
        )
        rendered = _render_human(view)

    assert "never landed" not in rendered, (
        "the section header asserted a non-delivery over a row whose outcome is unknown"
    )
    assert "outcome unknown" in rendered
    assert "claimed by leg-a" in rendered


@pytest.mark.anyio
async def test_the_tombstone_cannot_reject_a_row_claimed_after_it_read_the_queue(temp_db_path):
    """The snapshot the sweep decides from is not the state it writes against.

    Reading the pending rows and then rejecting the ones that looked unclaimed
    is a check against a value that changes: another leg sitting at its own turn
    boundary can claim the row and hand the steer to the model inside that
    window. The unconditional write then records a delivered message as never
    delivered, and the claimant's own guarded finalize correctly refuses, so the
    false outcome is what survives. The guard has to travel with the write.
    """

    class _ClaimsDuringTheRead:
        """Real DB, except the pending-row read leaves a claim behind it.

        This is the interleave stated as a sequence rather than raced for: the
        claim lands after the sweep has its snapshot and before the sweep
        writes, which is the whole window.
        """

        def __init__(self, db, control_id: str) -> None:
            self._db = db
            self._control_id = control_id

        def __getattr__(self, name):
            return getattr(self._db, name)

        async def list_pending_session_controls(self, session_id):
            rows = await self._db.list_pending_session_controls(session_id)
            await self._db.mark_session_control_applying(self._control_id, owner="leg-a")
            return rows

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "go check the logs"}
        )
        await _terminalize(db, sid)

        wrapped = _ClaimsDuringTheRead(db, cid)
        await _tombstone_pending_steers({"db": wrapped, "session_id": sid})

        row = await db.get_session_control(cid)
        assert row["result"] == "applying:leg-a", (
            "the sweep overwrote a claim taken after its snapshot, so a steer the "
            f"claimant may already have delivered now reads as {row['result']!r}"
        )
        assert row["applied_at"] is None

        # The claimant can still report its own outcome, which is the point:
        # an overwrite would have made its guarded finalize a no-op forever.
        assert await db.finalize_session_control(
            cid, result="applied", expect_claim="applying:leg-a"
        )
        assert (await db.get_session_control(cid))["result"] == "applied"


@pytest.mark.anyio
async def test_a_hand_resolution_records_who_resolved_it(temp_db_path):
    """An operator action that records no operator is the wedge one level up.

    The row exists so a reader can find out who held the message and who then
    decided about it. A constant standing in for the second half leaves the
    record saying a human decided and not which one, which is the same dead end
    the verb was built to end.
    """
    from lionagi.cli.orchestrate._control import run_ctl_resolve

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "did this go out?"}
        )
        await db.mark_session_control_applying(cid, owner="20260802T120000-deadleg")
        await _terminalize(db, sid)

    rc = run_ctl_resolve(
        argparse.Namespace(control_id=cid, outcome="applied", actor="ops@example.com")
    )
    assert rc == 0

    async with StateDB() as db:
        stored = (await db.get_session_control(cid))["result"]
    assert "ops@example.com" in stored, f"the resolver is not in the record: {stored!r}"
    assert "applying:20260802T120000-deadleg" in stored, (
        f"the claim was not preserved verbatim: {stored!r}"
    )


@pytest.mark.anyio
async def test_a_hand_resolution_falls_back_to_the_os_account_not_a_placeholder(temp_db_path):
    """Without --by the record still names somebody real.

    Defaulting to a placeholder would make the identity optional in practice,
    since nothing prompts for it; the account running the command is already a
    real answer.
    """
    import getpass

    from lionagi.cli.orchestrate._control import run_ctl_resolve

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "no --by given"}
        )
        await db.mark_session_control_applying(cid, owner="leg-a")
        await _terminalize(db, sid)

    rc = run_ctl_resolve(argparse.Namespace(control_id=cid, outcome="abandoned", actor=None))
    assert rc == 0

    async with StateDB() as db:
        stored = (await db.get_session_control(cid))["result"]
    assert getpass.getuser() in stored, f"no real identity was recorded: {stored!r}"
