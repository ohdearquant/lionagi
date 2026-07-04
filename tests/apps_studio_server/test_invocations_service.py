# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Round-trip tests: status_reason_code/status_reason_summary flow through
both serializer paths in lionagi.studio.services.invocations."""

from __future__ import annotations

import time
import uuid

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

import lionagi.state.db as state_db_mod  # noqa: E402
import lionagi.studio.services.invocations as invocations_mod  # noqa: E402
from lionagi.state.db import StateDB  # noqa: E402
from lionagi.state.reasons import RunReasons  # noqa: E402


async def _create_invocation(db: StateDB, *, status: str = "running") -> str:
    inv_id = uuid.uuid4().hex[:12]
    now = time.time()
    await db.execute(
        "INSERT INTO invocations (id, skill, status, created_at, started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (inv_id, "test:skill", status, now, now, now),
    )
    return inv_id


async def test_get_invocation_returns_reason_fields_when_set(tmp_path, monkeypatch):
    """get_invocation serializer includes status_reason_code and status_reason_summary
    with their exact DB values when the invocation has been transitioned to a terminal
    status with a reason."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_id = await _create_invocation(db)
        await db.update_invocation(
            inv_id,
            status="failed",
            ended_at=time.time(),
            reason_code=RunReasons.FAILED_EXCEPTION,
            reason_summary="RuntimeError: boom",
            evidence_refs=[{"kind": "session", "id": "s-1"}],
        )

    result = await invocations_mod.get_invocation(inv_id)

    assert result is not None
    assert result["status_reason_code"] == RunReasons.FAILED_EXCEPTION
    assert result["status_reason_summary"] == "RuntimeError: boom"
    assert result["status_evidence_refs"] == [{"kind": "session", "id": "s-1"}]


async def test_get_invocation_returns_none_reason_fields_when_unset(tmp_path, monkeypatch):
    """get_invocation serializer returns None for both reason fields when the
    invocation has never been transitioned (columns are NULL)."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_id = await _create_invocation(db)

    result = await invocations_mod.get_invocation(inv_id)

    assert result is not None
    assert result["status_reason_code"] is None
    assert result["status_reason_summary"] is None
    assert result["status_evidence_refs"] is None


async def _create_failed_schedule_run(
    db: StateDB, *, invocation_id: str, exit_code: int, error_detail: str
) -> str:
    """Insert a minimal schedule (schedule_runs.schedule_id is FK-enforced)
    plus a schedule_runs row linked to *invocation_id* — mirrors what
    SchedulerEngine._fire_inner writes on a failed run (ADR-0027)."""
    sched_id = uuid.uuid4().hex[:12]
    now = time.time()
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"test-sched-{sched_id}",
            "trigger_type": "cron",
            "cron_expr": "0 * * * *",
            "action_kind": "agent",
        }
    )
    run_id = uuid.uuid4().hex[:12]
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "invocation_id": invocation_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "failed",
            "fired_at": now,
            "ended_at": now,
            "error_detail": error_detail,
        }
    )
    await db.update_schedule_run(run_id, exit_code=exit_code)
    return run_id


async def test_get_invocation_surfaces_schedule_run_failure_fields(tmp_path, monkeypatch):
    """get_invocation exposes the linked schedule_run's exit_code and
    error_detail so the UI can show WHY a scheduled run failed."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_id = await _create_invocation(db, status="failed")
        await _create_failed_schedule_run(
            db,
            invocation_id=inv_id,
            exit_code=2,
            error_detail="Cannot spawn scheduled action: unable to resolve an "
            "absolute path to the `li` executable",
        )

    result = await invocations_mod.get_invocation(inv_id)

    assert result is not None
    assert result["schedule_run_exit_code"] == 2
    assert "unable to resolve" in result["schedule_run_error_detail"]


async def test_get_invocation_schedule_run_fields_none_for_interactive_invocation(
    tmp_path, monkeypatch
):
    """An invocation with no linked schedule_run (an interactive/API-driven
    run, not a scheduled one) reports None for both fields, not an error."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_id = await _create_invocation(db)

    result = await invocations_mod.get_invocation(inv_id)

    assert result is not None
    assert result["schedule_run_exit_code"] is None
    assert result["schedule_run_error_detail"] is None


async def test_list_invocations_includes_schedule_run_failure_fields(tmp_path, monkeypatch):
    """list_invocations surfaces the same schedule_run fields cheaply via the
    existing JOIN, for both a failed-scheduled and a plain invocation."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_a = await _create_invocation(db, status="failed")
        await _create_failed_schedule_run(db, invocation_id=inv_a, exit_code=1, error_detail="boom")
        inv_b = await _create_invocation(db)

    rows = await invocations_mod.list_invocations()
    by_id = {r["id"]: r for r in rows}

    assert by_id[inv_a]["schedule_run_exit_code"] == 1
    assert by_id[inv_a]["schedule_run_error_detail"] == "boom"
    assert by_id[inv_b]["schedule_run_exit_code"] is None
    assert by_id[inv_b]["schedule_run_error_detail"] is None


async def test_list_invocations_includes_reason_fields(tmp_path, monkeypatch):
    """list_invocations serializer includes status_reason_code and
    status_reason_summary with exact values for both the set and null cases."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_a = await _create_invocation(db)
        await db.update_invocation(
            inv_a,
            status="failed",
            ended_at=time.time(),
            reason_code=RunReasons.FAILED_EXCEPTION,
            reason_summary="RuntimeError: boom",
            evidence_refs=[{"kind": "session", "id": "s-a"}],
        )
        inv_b = await _create_invocation(db)
        # Leave inv_b as running — reason columns stay NULL.

    rows = await invocations_mod.list_invocations()

    by_id = {r["id"]: r for r in rows}

    assert inv_a in by_id, "row A must appear in list"
    assert by_id[inv_a]["status_reason_code"] == RunReasons.FAILED_EXCEPTION
    assert by_id[inv_a]["status_reason_summary"] == "RuntimeError: boom"
    assert by_id[inv_a]["status_evidence_refs"] == [{"kind": "session", "id": "s-a"}]

    assert inv_b in by_id, "row B must appear in list"
    assert by_id[inv_b]["status_reason_code"] is None
    assert by_id[inv_b]["status_reason_summary"] is None
    assert by_id[inv_b]["status_evidence_refs"] is None
