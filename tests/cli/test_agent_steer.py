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
import contextlib
import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.agent import _drain_pending_steers, _tombstone_pending_steers
from lionagi.cli.orchestrate._control import (
    _enqueue_control_inner,
    _runner_drains_controls,
    run_ctl_msg,
    run_ctl_pause,
    run_ctl_resume,
)
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
    drains_controls: bool = True,
) -> str:
    """A native `li agent` session carries a run_id (the runner stamps one)
    and declares that it drains operator controls; a mirrored Claude Code /
    Codex session is also invocation_kind='agent' but has neither. Other
    embedded runners persist through the same path and do stamp a run_id, so
    the declaration is what separates them: `drains_controls=False` here is a
    session whose runner never reads the controls queued against it."""
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    row = {
        "id": sid,
        "progression_id": pid,
        "status": status,
        "invocation_kind": "agent",
        "started_at": time.time(),
        "node_metadata": {"drains_controls": drains_controls},
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
async def test_msg_refused_when_the_runner_does_not_drain_controls(temp_db_path, caplog):
    """An embedded runner persists through the same path as `li agent`: same
    invocation kind, same running status, and a run_id of its own. It has no
    turn-end drain, so a steer queued against it is delivered by nobody and
    closed by nobody. Owning a run and consuming controls are different
    properties, and only the second one answers this question."""
    async with StateDB() as db:
        sid = await _make_agent_session(
            db, run_id="20260801T060606-embedded", drains_controls=False
        )
    with caplog.at_level("ERROR"):
        rc = run_ctl_msg(argparse.Namespace(id=sid, text="redirect"))
    assert rc == EXIT_UNKNOWN
    assert "does not consume operator controls" in caplog.text
    async with StateDB() as db:
        assert await db.list_pending_session_controls(sid) == []


@pytest.mark.anyio
async def test_msg_refused_when_the_session_never_declared_a_drain(temp_db_path, caplog):
    """Absence is a refusal, not a pass. A session row written before the
    declaration existed, or by a runner that forgot to make one, says nothing
    about whether anything will read its controls — and admitting on silence
    is how the queue fills with rows nobody closes.

    What this body asserts is narrow: an agent row with a run_id and no
    declaration is refused and leaves no pending control. That is the whole
    check. It does not start a CLI runner and does not establish that a real
    pre-declaration leg would have drained, so do not read it as measuring the
    cost of the refusal.

    The cost is real and it is recorded next to the predicate rather than here,
    because it is a property of the design and not of this row: a leg whose
    session predates the declaration does have a drain, and it is refused
    anyway. Accepted, not overlooked."""
    async with StateDB() as db:
        sid = uuid.uuid4().hex[:12]
        pid = uuid.uuid4().hex
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid,
                "status": "running",
                "invocation_kind": "agent",
                "started_at": time.time(),
                "run_id": "20260801T070707-undeclared",
            }
        )
    with caplog.at_level("ERROR"):
        rc = run_ctl_msg(argparse.Namespace(id=sid, text="redirect"))
    assert rc == EXIT_UNKNOWN
    assert "does not consume operator controls" in caplog.text
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


@pytest.mark.anyio
async def test_a_refused_finalize_after_delivery_is_reported_not_swallowed(temp_db_path, caplog):
    """Somebody resolves the row while the continuation turn is running.

    The guard is doing its job: the other writer's outcome stands and the drain
    does not overwrite it. But the message was already handed to the branch
    before that happened, so the row now records an outcome that disagrees with
    what the run did. That disagreement is the whole reason to look at the
    record, and it is invisible if the refusal returns quietly, since the drain
    hands its caller the continuation's result either way.
    """

    class _ResolvingBranch:
        """Resolves the control by hand during the continuation turn, which is
        the exact window the claim guard exists to cover."""

        def __init__(self, db, control_id):
            self._db = db
            self._control_id = control_id
            self.calls: list[str] = []

        async def operate(self, *, instruction: str, **kwargs):
            self.calls.append(instruction)
            await self._db.finalize_session_control(
                self._control_id, result="abandoned:ops@example.com"
            )
            return "turn-1"

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "still relevant?"}
        )

        branch = _ResolvingBranch(db, cid)
        res = await _drain_pending_steers(
            {"db": db, "session_id": sid}, branch, operate_kwargs={}, deadline=None
        )

        # Delivery happened. The drain cannot take it back and does not pretend
        # otherwise: the continuation's result is still what it returns.
        assert len(branch.calls) == 1, "the operator message was never delivered"
        assert res == "turn-1"

        # The other writer's outcome stands. This is the guard working.
        stored = (await db.get_session_control(cid))["result"]
        assert stored == "abandoned:ops@example.com", (
            f"the drain overwrote a resolution it did not own: {stored!r}"
        )

    # And the disagreement reached somebody. Without this the caller has no way
    # to tell a landed finalize from a refused one.
    reported = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(cid in m for m in reported), f"the refused finalize named no control: {reported!r}"
    assert any("delivered" in m for m in reported), (
        f"the report does not say the message went out anyway: {reported!r}"
    )


@pytest.mark.asyncio
async def test_the_sweep_survives_the_teardown_closing_the_handle_it_was_given(
    temp_db_path, tmp_path, monkeypatch
):
    """The real teardown closes the run's database handle in its own `finally`,
    and the sweep is called after it, deliberately, because the terminal
    transition has to land before pending rows can be judged.

    So the sweep cannot use the handle it was set up with: it is already
    closed. Nothing about that fails loudly. The sweep's own must-not-raise
    catch swallows it, which leaves the entire tombstone path dead on every run
    with one log line as the only symptom, and the row it exists to close
    pending forever.

    The sibling ordering test above wires a teardown double that does NOT close
    the handle, so it passes either way. That divergence between the double and
    the real teardown is what hid this.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli.agent import _run_agent
    from lionagi.service.manager import iModelManager

    db = StateDB()
    await db.__aenter__()
    closed = False
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
            # Both halves of what the real teardown does to this path: the
            # terminal transition, and then closing the handle on the way out.
            nonlocal closed
            await _terminalize(db, sid)
            await db.close()
            closed = True
            return status

        async def no_drain(*a, **kw):
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
                run_id="20260802T000000-closedrun",
                artifact_root=tmp_path / "artifacts",
                stream_dir=tmp_path / "stream",
                branches_dir=tmp_path / "branches",
            ),
        )

        await _run_agent("claude", "do the thing")

        assert closed, "the teardown double did not close the handle, so this proves nothing"

        async with StateDB() as check:
            row = await check.get_session_control(cid)
        assert row["result"] is not None, (
            "the control was left pending: the sweep ran against the handle the "
            "teardown had already closed"
        )
        assert row["result"].startswith("rejected:")
        assert row["applied_at"] is not None
    finally:
        if not closed:
            await db.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_the_cli_runner_declares_its_drain_so_its_own_runs_stay_steerable(
    temp_db_path, tmp_path, monkeypatch
):
    """The writer now refuses any agent session that has not declared a drain,
    and the runner that has one has to say so or it locks itself out.

    Wired through the real `setup_agent_persist` and the real admission
    predicate rather than a recorded keyword, because the keyword is not what
    matters: what matters is that a control aimed at a live `li agent` run is
    still accepted. The enqueue runs inside the turn, which is when an operator
    would actually issue it and the only point at which the session reads
    running.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli.agent import _run_agent
    from lionagi.service.manager import iModelManager

    run_id = "20260802T000000-steerablerun"
    admitted: list[tuple[str, int]] = []

    turns: list[str] = []

    async def fake_operate(self, instruction=None, **kw):
        turns.append(str(instruction))
        # Enqueued once, on the first turn only. The real drain runs after this
        # returns, so a second enqueue here would feed itself forever.
        if len(turns) == 1:
            admitted.append(
                await _enqueue_control_inner(
                    entity_id=run_id, verb="message", payload={"text": "hi"}
                )
            )
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
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
            run_id=run_id,
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )
    # The real allocator sets the process-wide run pointer as a side effect, and
    # that pointer is what stamps run_id onto the session row. The stand-in above
    # writes nothing outside tmp_path, so the pointer is set here instead; without
    # it the persisted session carries no run_id and would be refused as a
    # mirrored session, which is a property of the stand-in rather than of the
    # runner under test.
    monkeypatch.setattr(runs_mod, "active_run_id", lambda: run_id)

    await _run_agent("claude", "do the thing")

    assert admitted, "the turn never ran, so nothing was admitted or refused"
    message, exit_code = admitted[0]
    assert exit_code == 0, f"a live `li agent` run refused its own steer: {message}"
    assert "queued message" in message

    # The declaration is a claim about this runner, so the test has to check the
    # claim and not just that it was written down. The real drain runs here: the
    # steer came back as a second turn carrying its text, and the control row it
    # came from is closed rather than left pending.
    assert len(turns) == 2, f"the steer did not come back as a continuation turn: {turns}"
    assert "hi" in turns[1]
    async with StateDB() as db:
        sid = (await db.get_sessions_for_run(run_id))[0]["id"]
        assert await db.list_pending_session_controls(sid) == []


@pytest.mark.asyncio
async def test_a_resumed_session_takes_the_declaration_of_the_leg_running_it_now(
    temp_db_path, monkeypatch
):
    """The declaration is written when a session is created, and a resume does
    not create one — it adopts a row another leg wrote.

    So a session started before this existed, or started by a runner without a
    drain, would keep saying so while a leg that does drain is the one actually
    executing, and every steer aimed at that live leg would be refused. The
    declaration describes whoever is running now, which means the resuming
    caller's value replaces the adopted one.

    It has to replace it in the *same* write that installs this leg's process
    markers. The two legs get different markers here for that reason: a
    declaration written separately afterwards would carry the row as it was read
    before the reopen, restoring the exited leg's pid and pid_create_time over
    the live leg's. The stale-session doctor reads exactly those two fields to
    decide whether a row belongs to a process that is gone.
    """
    import lionagi.cli._runs as runs_mod
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist

    monkeypatch.setattr(runs_mod, "active_run_id", lambda: "20260802T000000-resumedrun")

    exited_leg = {"pid": 101, "pid_create_time": 1.0}
    live_leg = {"pid": 202, "pid_create_time": 2.0}
    monkeypatch.setattr(runs_mod, "current_pid_markers", lambda: dict(exited_leg), raising=False)
    monkeypatch.setattr("lionagi.cli.kill.current_pid_markers", lambda: dict(exited_leg))

    branch = Branch(name="resumed")
    first = await setup_agent_persist(branch, agent_name="claude", share_db=False)
    assert first is not None, "the first persist failed, so there is nothing to resume into"
    session_id = first["session_id"]
    await first["db"].close()

    async with StateDB() as db:
        await _terminalize(db, session_id)
        row = await db.get_session(session_id)
        assert not _runner_drains_controls(row)
        assert row["node_metadata"]["pid"] == 101, "the first leg's markers did not land"

    # A resume rebuilds the branch from its snapshot rather than reusing the
    # object, which is why the second call is a resume at all: same branch id,
    # new instance, and not still owned by the first session.
    resumed_branch = Branch.from_dict(branch.to_dict())
    assert str(resumed_branch.id) == str(branch.id)

    monkeypatch.setattr("lionagi.cli.kill.current_pid_markers", lambda: dict(live_leg))

    second = await setup_agent_persist(
        resumed_branch, agent_name="claude", share_db=False, drains_controls=True
    )
    assert second is not None
    assert second["session_id"] == session_id, "this did not resume, so it proves nothing"
    await second["db"].close()

    async with StateDB() as db:
        row = await db.get_session(session_id)
        assert _runner_drains_controls(row)
        # And the row the resume adopted is admissible again, which is the point.
        assert row["status"] == "running"
        # The live leg's identity survived the declaration. If these read 101,
        # the declaration was written over a pre-reopen copy of the row and the
        # doctor would be judging this live leg by a dead process's pid.
        assert row["node_metadata"]["pid"] == 202
        assert row["node_metadata"]["pid_create_time"] == 2.0
    message, exit_code = await _enqueue_control_inner(
        entity_id=session_id, verb="message", payload={"text": "steer the resumed leg"}
    )
    assert exit_code == 0, message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared_by_the_first_leg", "declared_by_the_resumer", "admits"),
    [
        # The row outlives the leg that wrote it. If that leg declared a drain
        # and then died without terminalizing, the row still says so, and a
        # control is admitted for a drain that is gone. This is what the status
        # column costs: a row reading running is the only evidence available,
        # and a dead leg leaves exactly that evidence behind. Unchanged by the
        # capability predicate — the previous rule admitted this row too, on the
        # run_id every agent leg stamps — so it is pinned here as a known gap
        # rather than claimed as solved.
        (True, False, True),
        # The other direction costs availability instead of safety: the row
        # keeps the first leg's False and refuses controls the resuming leg
        # would have drained. This one IS a change; the previous rule admitted
        # it. Refusing is the side to be wrong on.
        (False, True, False),
    ],
    ids=["KNOWN-GAP-stale-true-still-admits", "GUARANTEE-stale-false-refuses"],
)
async def test_a_resume_that_does_not_reopen_keeps_the_row_declaration_one_gap_one_guarantee(
    temp_db_path, monkeypatch, declared_by_the_first_leg, declared_by_the_resumer, admits
):
    """A resume only rewrites the declaration when it reopens a terminal row.

    The two parameters are NOT both guarantees, and the ids say which is which.
    KNOWN-GAP pins behaviour that is wrong and not fixed here: a control admitted
    for a drain that is gone. It is in the suite so it cannot be lost, not
    because it is correct. GUARANTEE pins behaviour this change is responsible
    for. Read the first as coverage of the defect and you have it backwards.

    Adopting a row that still reads running leaves it alone, deliberately: a
    write here is a read-modify-write against a row a live leg may be updating,
    which is how an exited leg's process markers were restored over a live one's
    once already. Both consequences are pinned so neither can be lost to a
    comment.
    """
    import lionagi.cli._runs as runs_mod
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist

    monkeypatch.setattr(runs_mod, "active_run_id", lambda: "20260802T000000-noreopen")

    branch = Branch(name="not-reopened")
    first = await setup_agent_persist(
        branch,
        agent_name="claude",
        share_db=False,
        drains_controls=declared_by_the_first_leg,
    )
    assert first is not None
    session_id = first["session_id"]
    await first["db"].close()

    # Deliberately NOT terminalized: the row still reads running, which is both
    # what a live leg looks like and what a leg that died leaves behind.
    async with StateDB() as db:
        assert (await db.get_session(session_id))["status"] == "running"

    second = await setup_agent_persist(
        Branch.from_dict(branch.to_dict()),
        agent_name="claude",
        share_db=False,
        drains_controls=declared_by_the_resumer,
    )
    assert second is not None
    assert second["session_id"] == session_id, "this did not adopt the row, so it proves nothing"
    await second["db"].close()

    async with StateDB() as db:
        row = await db.get_session(session_id)
    assert _runner_drains_controls(row) is declared_by_the_first_leg, (
        "the row was rewritten; this path is supposed to leave it alone"
    )

    _message, exit_code = await _enqueue_control_inner(
        entity_id=session_id, verb="message", payload={"text": "steer"}
    )
    assert (exit_code == 0) is admits, _message


@pytest.mark.asyncio
async def test_a_row_that_never_declared_stays_undeclared_across_a_resume_that_does_not_reopen(
    temp_db_path, monkeypatch
):
    """KNOWN GAP: the refusal is not bounded by the life of the leg that predates it.

    A row written before the declaration existed carries no key at all. A resume
    that adopts such a row while it still reads running writes nothing, on
    purpose, so the key stays absent however many times it is adopted. The leg
    running it now has a real turn-end drain and declares True, and is refused
    anyway.

    This is pinned because the boundary is easy to describe as time-limited and
    it is not: nothing about the original leg ending clears the row, and only a
    resume that REOPENS a terminal row ever replaces the declaration. It is the
    third representation the adoption path names — absent is not a spelling of
    False, and this is the case that separates them.
    """
    import json as _json

    import lionagi.cli._runs as runs_mod
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist

    monkeypatch.setattr(runs_mod, "active_run_id", lambda: "20260802T000000-undeclared")

    branch = Branch(name="predates-the-field")
    first = await setup_agent_persist(
        branch, agent_name="claude", share_db=False, drains_controls=True
    )
    assert first is not None
    session_id = first["session_id"]
    await first["db"].close()

    # Turn it into a row that predates the field. Removing the key is the point:
    # writing False instead would test the case the parametrized test already
    # covers, and absent is the state this one exists to distinguish.
    async with StateDB() as db:
        meta = dict((await db.get_session(session_id)).get("node_metadata") or {})
        meta.pop("drains_controls", None)
        await db.update_session(session_id, node_metadata=_json.dumps(meta))
        checked = await db.get_session(session_id)
        assert checked["status"] == "running"
        assert "drains_controls" not in (checked["node_metadata"] or {}), (
            "the row still declares, so the rest of this test would prove nothing"
        )

    second = await setup_agent_persist(
        Branch.from_dict(branch.to_dict()),
        agent_name="claude",
        share_db=False,
        drains_controls=True,
    )
    assert second is not None
    assert second["session_id"] == session_id, "this did not adopt the row, so it proves nothing"
    await second["db"].close()

    async with StateDB() as db:
        row = await db.get_session(session_id)
    after = row["node_metadata"] or {}
    assert "drains_controls" not in after, (
        "the non-reopening path wrote a declaration; it is documented as writing nothing"
    )
    # The owner gate runs first and returns the same nonzero code, so without
    # this the whole test passes on a row that lost its run_id and never
    # reached the predicate under test. Verified by mutation: stripping run_id
    # at creation leaves every other assertion here satisfied.
    assert row["run_id"] == "20260802T000000-undeclared", (
        "the row lost its run_id, so a refusal below proves nothing about the declaration"
    )

    message, exit_code = await _enqueue_control_inner(
        entity_id=session_id, verb="message", payload={"text": "steer"}
    )
    assert exit_code != 0, (
        "a row carrying no declaration must refuse however capable the leg adopting it is"
    )
    assert "does not consume operator controls" in message, (
        f"refused by a different gate than the one under test: {message}"
    )
    async with StateDB() as db:
        assert await db.list_pending_session_controls(session_id) == []


@pytest.mark.asyncio
async def test_a_row_that_never_declared_is_repaired_by_a_resume_that_reopens_it(
    temp_db_path, monkeypatch
):
    """The repair path for a row written before the declaration existed.

    Its sibling above pins that adopting such a row while it still reads running
    leaves it undeclared and refusing. This pins the other end: once the row is
    terminal, a resume reopens it and the reopening write records the declaration
    of the leg running it now, so the session becomes steerable again.

    Absent is a third representation and this is where that matters. The terminal
    reopen is tested elsewhere starting from an explicit False; starting from no
    key at all is a different write, and without this the suite would still pass
    if a change repaired only rows that had once declared something.
    """
    import json as _json

    import lionagi.cli._runs as runs_mod
    from lionagi import Branch
    from lionagi.cli._runs import setup_agent_persist

    monkeypatch.setattr(runs_mod, "active_run_id", lambda: "20260802T000000-repaired")
    exited_leg = {"pid": 303, "pid_create_time": 3.0}
    live_leg = {"pid": 404, "pid_create_time": 4.0}
    monkeypatch.setattr(runs_mod, "current_pid_markers", lambda: dict(exited_leg), raising=False)
    monkeypatch.setattr("lionagi.cli.kill.current_pid_markers", lambda: dict(exited_leg))

    branch = Branch(name="predates-the-field-then-ends")
    first = await setup_agent_persist(
        branch, agent_name="claude", share_db=False, drains_controls=True
    )
    assert first is not None
    session_id = first["session_id"]
    await first["db"].close()

    # A row from before the field existed, then ended: no key at all, terminal.
    async with StateDB() as db:
        meta = dict((await db.get_session(session_id)).get("node_metadata") or {})
        meta.pop("drains_controls", None)
        await db.update_session(session_id, node_metadata=_json.dumps(meta))
        await _terminalize(db, session_id)
        row = await db.get_session(session_id)
        assert "drains_controls" not in (row["node_metadata"] or {}), (
            "the row still declares, so this does not start from the absent state"
        )
        assert row["status"] != "running", "not terminal, so the resume would not reopen"

    monkeypatch.setattr("lionagi.cli.kill.current_pid_markers", lambda: dict(live_leg))

    second = await setup_agent_persist(
        Branch.from_dict(branch.to_dict()),
        agent_name="claude",
        share_db=False,
        drains_controls=True,
    )
    assert second is not None
    assert second["session_id"] == session_id, "this did not resume, so it proves nothing"
    await second["db"].close()

    async with StateDB() as db:
        row = await db.get_session(session_id)
    assert _runner_drains_controls(row), (
        "the reopen did not record the resuming leg's declaration, so a row that "
        "predates the field can never become steerable again"
    )
    assert row["status"] == "running"
    # Same invariant the explicit-False reopen pins: the declaration rides the
    # transition, so the live leg's identity is not overwritten by the exited
    # leg's markers.
    assert row["node_metadata"]["pid"] == 404
    assert row["node_metadata"]["pid_create_time"] == 4.0

    message, exit_code = await _enqueue_control_inner(
        entity_id=session_id, verb="message", payload={"text": "steer the repaired leg"}
    )
    assert exit_code == 0, message


@pytest.mark.asyncio
async def test_a_run_still_finishes_when_the_sweep_cannot_get_a_connection(
    temp_db_path, tmp_path, monkeypatch, caplog
):
    """Acquiring the sweep's connection is bookkeeping, and bookkeeping does not
    get to fail a run that completed.

    The sweep swallows its own errors deliberately, but the connection is opened
    before the sweep is entered, so an open that raises would sail past that
    catch and out of the teardown block. StateDB re-raises out of __aenter__,
    and the reasons it does so are the ones the sweep already tolerates: another
    writer holding the lock, a busy timeout, a migration. The run must still end
    normally, and the row must stay visibly pending rather than being recorded
    as anything.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli.agent as agent_mod
    import lionagi.state.db as db_mod
    from lionagi import Branch
    from lionagi.cli.agent import _run_agent
    from lionagi.service.manager import iModelManager

    db = StateDB()
    await db.__aenter__()
    try:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "never swept"}
        )

        real_aenter = db_mod.StateDB.__aenter__
        opened_after_teardown = False

        async def refusing_aenter(self):
            # Only the sweep's own acquisition is refused; the run's setup
            # handle above was opened before this patch went in.
            if opened_after_teardown:
                raise RuntimeError("database is locked")
            return await real_aenter(self)

        async def fake_operate(self, instruction=None, **kw):
            return "done"

        async def fake_setup(*a, **kw):
            return {"db": db, "session_id": sid}

        async def fake_teardown(ctx, *, status="completed", **kw):
            nonlocal opened_after_teardown
            await _terminalize(db, sid)
            await db.close()
            opened_after_teardown = True
            return status

        async def no_drain(*a, **kw):
            return None

        monkeypatch.setattr(db_mod.StateDB, "__aenter__", refusing_aenter)
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
                run_id="20260802T000000-lockedrun",
                artifact_root=tmp_path / "artifacts",
                stream_dir=tmp_path / "stream",
                branches_dir=tmp_path / "branches",
            ),
        )

        with caplog.at_level("ERROR"):
            # The assertion is that this returns at all.
            await _run_agent("claude", "do the thing")

        assert opened_after_teardown, "the refusal never armed, so this proves nothing"
        assert "tombstone write failed" in caplog.text, (
            f"the refused acquisition was not reported: {caplog.text!r}"
        )
        assert "database is locked" in caplog.text, (
            f"the log does not say why it failed: {caplog.text!r}"
        )

        monkeypatch.setattr(db_mod.StateDB, "__aenter__", real_aenter)
        async with StateDB() as check:
            row = await check.get_session_control(cid)
        assert row["result"] is None, (
            f"a row was recorded by a sweep that never ran: {row['result']!r}"
        )
    finally:
        with contextlib.suppress(Exception):
            await db.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_the_resolver_refuses_when_the_claim_changed_under_it(temp_db_path):
    """Only the compare-and-set can answer this one.

    The resolver reads the current claim, decides, and then writes with
    `WHERE result = :prior`. Its other guard, that the row must still read
    `applying`, cannot refuse here: the row is claimed before and after, just by
    a different owner. So a resolver that kept the write and lost the WHERE
    clause would pass every other test in this file, including the
    already-finalized refusal, which is answered by the startswith check on a
    terminal row before the compare-and-set is ever consulted.

    The interleave is stated as a sequence rather than raced for. It has to
    happen on the resolver's own connection, because both statements live in one
    transaction and SQLite serialises writers, so an outside connection could
    not get between them.
    """
    import contextlib as _contextlib

    from sqlalchemy import text as _text

    class _ClaimChangesMidTransaction:
        """The real connection, except the claim is replaced after the read."""

        def __init__(self, conn, control_id: str, new_claim: str) -> None:
            self._conn = conn
            self._control_id = control_id
            self._new_claim = new_claim
            self._armed = True

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, statement, *args, **kwargs):
            result = await self._conn.execute(statement, *args, **kwargs)
            if self._armed and "SELECT result FROM session_controls" in str(statement):
                # Fires exactly once, between the resolver's read and its write.
                self._armed = False
                await self._conn.execute(
                    _text("UPDATE session_controls SET result = :r WHERE id = :id"),
                    {"r": self._new_claim, "id": self._control_id},
                )
            return result

    async with StateDB() as db:
        sid = await _make_agent_session(db)
        cid = await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "who has this?"}
        )
        await db.mark_session_control_applying(cid, owner="leg-a")

        real_tx = db._tx

        @_contextlib.asynccontextmanager
        async def intercepting_tx():
            async with real_tx() as conn:
                yield _ClaimChangesMidTransaction(conn, cid, "applying:leg-b")

        db._tx = intercepting_tx
        try:
            stored = await db.resolve_claimed_session_control(
                cid, outcome="abandoned", actor="ops@example.com"
            )
        finally:
            db._tx = real_tx

        assert stored is None, (
            f"the resolver returned a receipt for a write it did not land: {stored!r}"
        )

        # leg-b's claim survives untouched. Nothing recorded an outcome for a
        # message whose current holder was never consulted.
        row = await db.get_session_control(cid)
        assert row["result"] == "applying:leg-b", (
            f"the resolver overwrote a claim it never read: {row['result']!r}"
        )
        assert row["applied_at"] is None, "a refused resolution still stamped the row"
