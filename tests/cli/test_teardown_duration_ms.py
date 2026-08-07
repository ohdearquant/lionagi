# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: duration_ms was left NULL on every terminal session row
regardless of outcome -- loudest on a zero-turn timeout, where the row could
not even say how long nothing happened for. _teardown_common must derive
duration_ms from (ended_at - started_at) on the session row it is closing
out, covering the case where no message was ever appended (no progression
row exists yet)."""

from __future__ import annotations

import time

import pytest

from lionagi.cli._runs import _teardown_common, find_incomplete_session_for_run
from lionagi.state.db import StateDB


@pytest.fixture
async def db(tmp_path):
    database = StateDB(tmp_path / "state.db")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def test_teardown_common_populates_duration_ms_from_started_at(db, monkeypatch):
    sid = "sess-duration-1"
    started_at = 1_700_000_000.0
    await db.create_progression("prog-duration-1")
    await db.create_session(
        {
            "id": sid,
            "progression_id": "prog-duration-1",
            "status": "running",
            "started_at": started_at,
        }
    )

    monkeypatch.setattr(time, "time", lambda: started_at + 12.5)
    await _teardown_common(
        db,
        session_id=sid,
        session_prog_id="prog-duration-1",
        status="timed_out",
        exception=None,
        artifacts_path=None,
        artifact_contract=None,
    )

    row = await db.get_session(sid)
    assert row["duration_ms"] == pytest.approx(12_500.0)


async def test_teardown_common_populates_duration_ms_on_zero_turn_timeout(db, monkeypatch):
    """The exact #2495 shape: a session that timed out with no message ever
    appended -- the progression row exists (created alongside the session)
    but its collection is empty, so num_turns/input_tokens stay 0 and
    duration_ms was previously the only field that could have said where the
    time went, yet was itself always NULL."""
    sid = "sess-duration-zero-turn"
    started_at = 1_700_000_000.0
    await db.create_progression("prog-zero-turn")
    await db.create_session(
        {
            "id": sid,
            "progression_id": "prog-zero-turn",
            "status": "running",
            "started_at": started_at,
        }
    )

    monkeypatch.setattr(time, "time", lambda: started_at + 300.0)
    await _teardown_common(
        db,
        session_id=sid,
        session_prog_id="prog-zero-turn",
        status="timed_out",
        exception=None,
        artifacts_path=None,
        artifact_contract=None,
    )

    row = await db.get_session(sid)
    assert row["duration_ms"] == pytest.approx(300_000.0)
    assert row["first_msg_id"] is None


async def test_find_incomplete_session_for_run_populates_duration_ms(tmp_path, monkeypatch):
    """setup-recovery path: a session row committed by setup_agent_persist()
    before it raised is still 'running' forever unless recovered here -- the
    recovery write must also carry a duration, not just a terminal status."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    started_at = time.time() - 4.0
    async with StateDB(db_path) as db:
        prog_id = "prog-recover-1"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": "sess-recover-1",
                "run_id": "run-recover-1",
                "progression_id": prog_id,
                "status": "running",
                "started_at": started_at,
            }
        )

    row = await find_incomplete_session_for_run("run-recover-1")
    assert row is not None
    assert row["status"] == "failed"
    assert row["ended_at"] is not None
    assert row["duration_ms"] == pytest.approx((row["ended_at"] - started_at) * 1000)


async def test_engine_maybe_update_db_populates_session_duration_ms(db):
    """`li engine run` links an engine_runs row to a mirrored sessions row and
    terminalizes both through _maybe_update_db(); the sessions row must carry
    a duration the same as every other terminal write, and started_at must be
    set at creation for there to be anything to subtract from."""
    from lionagi.cli.engine import _maybe_update_db

    started_at = 1_700_000_000.0
    await db.insert_engine_run(
        run_id="engine-run-1",
        kind="research",
        spec_json={},
        started_at=started_at,
    )
    prog_id = "prog-engine-1"
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": "engine-session-1",
            "created_at": started_at,
            "started_at": started_at,
            "progression_id": prog_id,
            "name": "engine:research",
            "status": "running",
        }
    )

    await _maybe_update_db(
        db,
        "engine-run-1",
        "completed",
        ended_at=started_at + 42.0,
        signal_session_id="engine-session-1",
    )

    row = await db.get_session("engine-session-1")
    assert row["status"] == "completed"
    assert row["ended_at"] is not None
    assert row["duration_ms"] == pytest.approx((row["ended_at"] - started_at) * 1000)
