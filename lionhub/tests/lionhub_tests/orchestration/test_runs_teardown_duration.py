# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Run teardown and incomplete-session duration persistence."

from __future__ import annotations

import time

import pytest

from lionagi.state.db import StateDB
from lionhub.orchestration.runs import _teardown_common, find_incomplete_session_for_run
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._runs_teardown_duration_test_support import db as db


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


async def test_ordinary_success_teardown_persists_measured_end(db, monkeypatch):
    """Pin the ordinary successful completion path that repaired the live defect."""
    sid = "sess-success-end"
    started_at = 1_700_000_000.0
    await db.create_progression("prog-success-end")
    await db.create_session(
        {
            "id": sid,
            "progression_id": "prog-success-end",
            "status": "running",
            "started_at": started_at,
        }
    )

    monkeypatch.setattr(time, "time", lambda: started_at + 7.25)
    final_status = await _teardown_common(
        db,
        session_id=sid,
        session_prog_id="prog-success-end",
        status="completed",
        exception=None,
        artifacts_path=None,
        artifact_contract=None,
    )

    row = await db.get_session(sid)
    assert final_status == "completed"
    assert row["status"] == "completed"
    assert row["ended_at"] == started_at + 7.25
    assert row["ended_at_is_approximate"] is False
    assert row["duration_ms"] == pytest.approx(7_250.0)


async def test_teardown_common_populates_duration_ms_on_zero_turn_timeout(db, monkeypatch):
    """A session that timed out with no message ever appended: the progression
    row exists (created alongside the session) but its collection is empty,
    so num_turns/input_tokens stay 0 and duration_ms was previously the only
    field that could have said where the time went, yet was itself always
    NULL."""
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
