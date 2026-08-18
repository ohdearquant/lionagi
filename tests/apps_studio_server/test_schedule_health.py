# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the schedule health verdict
(healthy/failing/overdue/never-fired/no-evidence/disabled).

Health is derived from cadence + recorded schedule_runs rows and the threshold
evaluation watermark, never from next_fire_at -- these tests plant fixture
rows for each state and assert the verdict lands where it should, including
the shapes next_fire_at cannot represent: a schedule that has never recorded a
row, one whose recorded rows never became a real execution
(skipped/queued/pending), and one that keeps skipping instead of running.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services.schedules import (  # noqa: E402
    compute_schedule_health,
    create_schedule,
    get_schedule,
    list_schedules,
    update_schedule,
)


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_schedule(**overrides) -> str:
    spec = {
        "name": f"health-test-{uuid.uuid4().hex[:8]}",
        "trigger_type": "interval",
        "interval_sec": 300,
        "action_kind": "agent",
        "action_prompt": "ping",
    }
    spec.update(overrides)
    created = await create_schedule(spec)
    return created["id"]


async def _seed_run(
    schedule_id: str,
    *,
    status: str,
    fired_at: float,
    chain_depth: int = 0,
) -> None:
    async with StateDB() as db:
        await db.create_schedule_run(
            {
                "id": str(uuid.uuid4()),
                "schedule_id": schedule_id,
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": {},
                "status": status,
                "chain_depth": chain_depth,
                "fired_at": fired_at,
            }
        )


async def _list_row(sid: str) -> dict:
    rows = await list_schedules()
    return next(r for r in rows if r["id"] == sid)


async def test_never_fired_enabled_schedule_with_zero_rows(temp_db_path):
    sid = await _make_schedule()
    row = await _list_row(sid)
    assert row["health_state"] == "never-fired"
    assert row["health_last_outcome"] is None
    assert row["health_last_outcome_at"] is None
    assert row["health_since"] is not None

    detail = await get_schedule(sid)
    assert detail["health_state"] == "never-fired"


async def test_no_evidence_when_watermark_survives_pruned_run_history(temp_db_path):
    """schedules.last_fired_at is a retained per-schedule column, written by
    the normal occurrence paths, that survives schedule_runs retention
    pruning even after every run row for a schedule is gone. Zero recorded
    rows must not read as never-fired when that watermark says the schedule
    executed before its history was pruned."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    async with StateDB() as db:
        await db.update_schedule(sid, last_fired_at=now - 500_000)

    row = await _list_row(sid)
    assert row["health_state"] == "no-evidence"

    detail = await get_schedule(sid)
    assert detail["health_state"] == "no-evidence"


async def test_never_fired_still_stands_with_no_watermark_and_no_rows(temp_db_path):
    """Inverse of the pruned-history case above: with neither a recorded row
    nor a surviving last_fired_at watermark, never-fired remains the honest
    verdict -- both signals must agree nothing was recorded."""
    sid = await _make_schedule()
    row = await _list_row(sid)
    assert row["health_state"] == "never-fired"
    assert row["last_fired_at"] is None


def test_compute_schedule_health_last_fired_at_watermark_beats_never_fired():
    """Pins the pure-function contract: never-fired requires BOTH zero
    recorded rows AND a null last_fired_at watermark."""
    now = time.time()
    row = {
        "enabled": 1,
        "trigger_type": "interval",
        "interval_sec": 300,
        "created_at": now - 1000,
        "last_fired_at": now - 500_000,
    }
    evidence = {
        "last_recorded_run_at": None,
        "last_executed_run_at": None,
        "last_executed_status": None,
    }

    with_watermark = compute_schedule_health(row, evidence, now=now)
    assert with_watermark["health_state"] == "no-evidence"

    without_watermark = compute_schedule_health({**row, "last_fired_at": None}, evidence, now=now)
    assert without_watermark["health_state"] == "never-fired"


async def test_no_evidence_when_only_skipped_rows_recorded(temp_db_path):
    """Skipped rows are recorded evidence but not executed evidence -- a
    skip-only history is a distinct, honest "cannot tell" state, not the
    stronger "never-fired" claim (EMPTY != CLEAN)."""
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status="skipped", fired_at=now - 20)
    await _seed_run(sid, status="skipped", fired_at=now - 10)

    row = await _list_row(sid)
    assert row["health_state"] == "no-evidence"
    assert row["health_last_outcome"] is None


@pytest.mark.parametrize("status", ["queued", "waiting_dependency", "retry_wait"])
async def test_no_evidence_for_each_pending_lifecycle_status(temp_db_path, status):
    """A queued/waiting/retry-wait row has not executed yet -- it must not
    read as healthy with that pending status standing in for an outcome."""
    sid = await _make_schedule()
    now = time.time()
    await _seed_run(sid, status=status, fired_at=now - 5)

    row = await _list_row(sid)
    assert row["health_state"] == "no-evidence"
    assert row["health_last_outcome"] is None


async def test_running_execution_counts_as_evidence(temp_db_path):
    """Unlike the pending lifecycle states, 'running' has actually started
    and counts as execution evidence."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="running", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "healthy"
    assert row["health_last_outcome"] == "running"


async def test_execution_survives_more_than_fifty_more_recent_skips(temp_db_path):
    """The bounded evidence query used to rank a fixed-size window before
    filtering to executed rows, so a real execution could be pushed out of
    that window by enough non-executing rows landing after it and read as
    never-fired. It must be found however far back it sits."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 30)
    for i in range(51):
        await _seed_run(sid, status="skipped", fired_at=now - 29 + i * 0.5)

    row = await _list_row(sid)
    assert row["health_state"] == "healthy"
    assert row["health_last_outcome"] == "completed"
    assert row["health_last_outcome_at"] == pytest.approx(now - 30, abs=2)


async def test_github_poll_cadence_falls_back_to_interval_sec_before_default(temp_db_path):
    """github_poll health must share the scheduler's own cadence fallback
    (poll_interval_sec or interval_sec or 300) instead of a shorter,
    independently retyped chain that skips interval_sec and jumps straight
    to the 300s default."""
    sid = await _make_schedule(
        trigger_type="github_poll",
        interval_sec=3600,
        poll_interval_sec=None,
        github_repo="acme/widgets",
    )
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 1000)

    row = await _list_row(sid)
    # 1000s since the last execution is well past the 300s default's overdue
    # threshold (900s) but well within the real 3600s cadence's.
    assert row["health_state"] == "healthy"


async def test_healthy_when_recent_execution_completed(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "healthy"
    assert row["health_last_outcome"] == "completed"
    assert row["health_last_outcome_at"] == pytest.approx(now - 30, abs=2)


async def test_failing_when_last_executed_outcome_failed(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 600)
    await _seed_run(sid, status="failed", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "failing"
    assert row["health_last_outcome"] == "failed"


async def test_failing_when_last_executed_outcome_timed_out(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="timed_out", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "failing"


async def test_failing_resets_to_healthy_after_a_newer_success(temp_db_path):
    """N=1 is the deliberate contract: only the single latest execution is
    evaluated, so a success immediately after failures resets straight back
    to healthy rather than waiting for a streak to clear."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 600)
    await _seed_run(sid, status="failed", fired_at=now - 300)
    await _seed_run(sid, status="completed", fired_at=now - 30)

    row = await _list_row(sid)
    assert row["health_state"] == "healthy"
    assert row["health_last_outcome"] == "completed"


def test_failing_threshold_contract_is_exactly_one():
    """Pins the explicit N=1 contract named in compute_schedule_health's
    docstring -- a demo-facing badge treats a single failed/timed_out run as
    worth a glance, not a placeholder to be raised into a streak threshold
    later."""
    from lionagi.studio.services.schedules import _HEALTH_FAILING_THRESHOLD

    assert _HEALTH_FAILING_THRESHOLD == 1


async def test_overdue_when_no_execution_within_expected_cadence(temp_db_path):
    """Enabled interval schedule, cadence known, but no
    execution evidence in a long time -- overdue regardless of a rosy
    next_fire_at."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 5000)

    row = await _list_row(sid)
    assert row["health_state"] == "overdue"


def test_threshold_health_uses_recent_evaluation_as_liveness():
    now = time.time()
    row = {
        "enabled": 1,
        "trigger_type": "interval",
        "interval_sec": 300,
        "threshold_config": {
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 60,
        },
        "created_at": now - 10_000,
        "last_fired_at": now - 5_000,
        "last_evaluated_at": now - 30,
    }
    evidence = {
        "last_recorded_run_at": now - 5_000,
        "last_executed_run_at": now - 5_000,
        "last_executed_status": "completed",
    }

    result = compute_schedule_health(row, evidence, now=now)

    assert result["health_state"] == "healthy"


def test_quiet_threshold_alert_with_recent_evaluation_is_healthy():
    now = time.time()
    row = {
        "enabled": 1,
        "trigger_type": "interval",
        "interval_sec": 300,
        "threshold_config": {
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 60,
        },
        "created_at": now - 10_000,
        "last_fired_at": None,
        "last_evaluated_at": now - 30,
    }
    evidence = {
        "last_recorded_run_at": None,
        "last_executed_run_at": None,
        "last_executed_status": None,
    }

    result = compute_schedule_health(row, evidence, now=now)

    assert result["health_state"] == "healthy"


def test_threshold_alert_with_stale_evaluation_is_overdue():
    now = time.time()
    row = {
        "enabled": 1,
        "trigger_type": "interval",
        "interval_sec": 300,
        "threshold_config": {
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 60,
        },
        "created_at": now - 10_000,
        "last_fired_at": None,
        "last_evaluated_at": now - 5_000,
    }
    evidence = {
        "last_recorded_run_at": None,
        "last_executed_run_at": None,
        "last_executed_status": None,
    }

    result = compute_schedule_health(row, evidence, now=now)

    assert result["health_state"] == "overdue"


def test_cron_threshold_alert_derives_cadence_without_an_execution():
    """A cron threshold alert that has only ever been evaluated is still overdue.

    Cron has no fixed period, so its expected gap is derived from two
    occurrences after a reference instant. A threshold alert that never
    breached has no execution to reference, and anchoring the derivation on
    the execution timestamp therefore hands it None: the gap comes back
    unknown, and a schedule whose evaluations stopped hours ago reports
    healthy forever. The evaluation watermark is the reference in that case.

    Interval schedules cannot cover this: their cadence resolves from
    interval_sec before the reference instant is ever read.
    """
    now = time.time()
    row = {
        "enabled": 1,
        "trigger_type": "cron",
        "cron_expr": "*/5 * * * *",  # 300s gap -> overdue past max(900, 600)
        "threshold_config": {
            "metric": "failed_sessions",
            "op": "gt",
            "value": 5,
            "window_minutes": 60,
        },
        "created_at": now - 10_000,
        "last_fired_at": None,
        "last_evaluated_at": now - 5_000,
    }
    evidence = {
        "last_recorded_run_at": None,
        "last_executed_run_at": None,
        "last_executed_status": None,
    }

    result = compute_schedule_health(row, evidence, now=now)

    assert result["health_state"] == "overdue"


async def test_overdue_outranks_a_pile_of_recent_skips(temp_db_path):
    """A schedule skipping every occurrence must not read as fresh just
    because rows keep being recorded -- only executed rows count as evidence."""
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 5000)
    for i in range(5):
        await _seed_run(sid, status="skipped", fired_at=now - (i * 60))

    row = await _list_row(sid)
    assert row["health_state"] == "overdue"
    assert row["health_last_outcome"] == "completed"


async def test_disabled_takes_precedence_over_any_run_history(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="failed", fired_at=now - 5000)
    ok = await update_schedule(sid, {"enabled": 0})
    assert ok

    row = await _list_row(sid)
    assert row["health_state"] == "disabled"

    detail = await get_schedule(sid)
    assert detail["health_state"] == "disabled"


async def test_stopped_cron_schedule_reports_overdue(temp_db_path):
    sid = await _make_schedule(trigger_type="cron", cron_expr="0 18 * * *", interval_sec=None)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 500_000)

    row = await _list_row(sid)
    assert row["health_state"] == "overdue"


async def test_cron_schedule_firing_on_cadence_reports_healthy(temp_db_path):
    sid = await _make_schedule(trigger_type="cron", cron_expr="*/5 * * * *", interval_sec=None)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 60)

    row = await _list_row(sid)
    assert row["health_state"] == "healthy"


async def test_chain_children_are_not_evidence(temp_db_path):
    sid = await _make_schedule(interval_sec=300)
    now = time.time()
    await _seed_run(sid, status="completed", fired_at=now - 5000)
    await _seed_run(sid, status="completed", fired_at=now - 10, chain_depth=1)

    row = await _list_row(sid)
    assert row["health_state"] == "overdue"


def test_compute_schedule_health_is_a_pure_function_of_row_and_evidence():
    now = time.time()
    row = {"enabled": 1, "trigger_type": "interval", "interval_sec": 300, "created_at": now - 1000}
    healthy = compute_schedule_health(
        row, {"last_executed_run_at": now - 30, "last_executed_status": "completed"}, now=now
    )
    assert healthy["health_state"] == "healthy"

    overdue = compute_schedule_health(
        row, {"last_executed_run_at": now - 5000, "last_executed_status": "completed"}, now=now
    )
    assert overdue["health_state"] == "overdue"

    never_fired = compute_schedule_health(
        row,
        {
            "last_recorded_run_at": None,
            "last_executed_run_at": None,
            "last_executed_status": None,
        },
        now=now,
    )
    assert never_fired["health_state"] == "never-fired"

    no_evidence = compute_schedule_health(
        row,
        {
            "last_recorded_run_at": now - 30,
            "last_executed_run_at": None,
            "last_executed_status": None,
        },
        now=now,
    )
    assert no_evidence["health_state"] == "no-evidence"
