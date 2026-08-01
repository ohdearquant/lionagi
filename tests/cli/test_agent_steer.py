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
        assert "OPERATOR STEER" in branch.calls[0]["instruction"]
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
