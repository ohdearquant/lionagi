# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for two schedule-runs API defects:

- the ``/schedules/{id}/runs`` route's server-side default page size must
  stay 50 (a prior change silently cut it to 20) while still honoring an
  explicit ``limit=``.
- ``get_schedule_run`` must derive its merged response (legacy fields +
  RunView-reconciled fields) from a single read of the ``schedule_runs``
  row, not two independent reads that can straddle a concurrent write.
"""

from __future__ import annotations

import inspect
import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionagi.studio.services.schedules import get_schedule_run, list_schedule_runs_route


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("lionagi.studio.services.schedules.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_schedule_with_runs(db: StateDB, count: int) -> str:
    sched_id = uuid.uuid4().hex[:12]
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"sched-{sched_id}",
            "trigger_type": "cron",
            "cron_expr": "0 2 * * *",
            "action_kind": "agent",
            "next_fire_at": time.time() + 3600,
        }
    )
    for i in range(count):
        await db.create_schedule_run(
            {
                "id": uuid.uuid4().hex[:12],
                "schedule_id": sched_id,
                "invocation_id": None,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": "completed",
                "exit_code": 0,
                "fired_at": time.time() + i,
            }
        )
    return sched_id


async def _seed_run_with_error_detail(db: StateDB) -> tuple[str, str]:
    sched_id = uuid.uuid4().hex[:12]
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"sched-{sched_id}",
            "trigger_type": "cron",
            "cron_expr": "0 2 * * *",
            "action_kind": "agent",
            "next_fire_at": time.time() + 3600,
        }
    )
    run_id = uuid.uuid4().hex[:12]
    fired_at = time.time()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "invocation_id": None,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "running",
            "exit_code": None,
            "error_detail": None,
            "fired_at": fired_at,
        }
    )
    return sched_id, run_id


# ---------------------------------------------------------------------------
# Issue #2292 — route default limit
# ---------------------------------------------------------------------------


def test_list_schedule_runs_route_default_limit_is_50() -> None:
    """The route's own Query() default is the server-side contract — a CLI
    default of 20 must never leak into the route signature again."""
    sig = inspect.signature(list_schedule_runs_route)
    limit_param = sig.parameters["limit"]
    assert limit_param.default.default == 50
    le_constraints = [m.le for m in limit_param.default.metadata if hasattr(m, "le")]
    assert le_constraints == [200]


@pytest.mark.asyncio
async def test_list_schedule_runs_route_default_returns_up_to_50(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        sched_id = await _seed_schedule_with_runs(db, 60)

    result = await list_schedule_runs_route(sched_id, status=None, limit=50, offset=0)

    assert result["limit"] == 50
    assert len(result["runs"]) == 50
    assert result["has_next"] is True


@pytest.mark.asyncio
async def test_list_schedule_runs_route_explicit_limit_passthrough(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        sched_id = await _seed_schedule_with_runs(db, 10)

    result = await list_schedule_runs_route(sched_id, status=None, limit=5, offset=0)

    assert result["limit"] == 5
    assert len(result["runs"]) == 5
    assert result["has_next"] is True


# ---------------------------------------------------------------------------
# Issue #2293 — double read of the schedule_runs row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_schedule_run_reads_row_exactly_once(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_schedule_run`` must read ``schedule_runs`` exactly once. A second,
    independent read (the pre-fix shape) can straddle a concurrent writer and
    produce a response where some fields reflect the old row and others the
    new one."""
    async with StateDB() as db:
        _sched_id, run_id = await _seed_run_with_error_detail(db)

    original = StateDB.get_schedule_run
    reads: list[dict] = []

    async def counting_get_schedule_run(self, rid):
        row = await original(self, rid)
        if row is not None:
            reads.append(row)
            if len(reads) >= 2:
                # Simulate a concurrent writer landing between the two reads
                # a pre-fix implementation would perform: status flips to
                # failed and error_detail is newly populated.
                row = {
                    **row,
                    "status": "failed",
                    "error_detail": "MUTATED-BETWEEN-READS",
                }
        return row

    monkeypatch.setattr(StateDB, "get_schedule_run", counting_get_schedule_run)

    view = await get_schedule_run(run_id)

    # Discrimination proof: the pre-fix implementation performs a second,
    # independent read via run_view.get_run_view(db, run_id) — this asserts
    # that seam is gone.
    assert len(reads) == 1

    assert view is not None
    # Every field in the response must come from the single row that was
    # actually read — not a mutated one that never happened.
    assert view["status"] == reads[0]["status"] == "running"
    assert view["error_detail"] == reads[0]["error_detail"] is None
