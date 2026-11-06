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
import pytest_asyncio

from lionagi.state.db import StateDB
from lionhub.orchestration.runs import _teardown_common, find_incomplete_session_for_run
from tests._runs_teardown_duration_test_support import db as db


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
