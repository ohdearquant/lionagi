# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Automatic definition changes retain their cause and actual process provenance."""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from lionagi.state.db import NO_CURSOR_CLAIM, StateDB


async def _events(db, schedule_id="audited"):
    rows = await db.fetch_all(
        "SELECT * FROM status_transitions WHERE entity_type='schedule' "
        "AND entity_id=:id ORDER BY created_at, id",
        {"id": schedule_id},
    )
    for row in rows:
        if isinstance(row["metadata"], str):
            row["metadata"] = json.loads(row["metadata"])
    return rows


@asynccontextmanager
async def _backed_engine(tmp_path, monkeypatch, *, budget=False, runs=2):
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    async with StateDB(tmp_path / "state.db") as db:
        await db.create_schedule(
            {
                "id": "audited",
                "name": "Observed definition",
                "enabled": 1,
                "trigger_type": "interval",
                "interval_sec": 60,
                "next_fire_at": 1000.0,
                "action_kind": "agent",
                "action_cwd": str(tmp_path / "execution"),
                "action_project": "execution-project",
                "max_runs": None if budget else 2,
                "budget_usd": 3.0 if budget else None,
            }
        )
        for index in range(runs):
            await db.create_schedule_run(
                {
                    "id": f"completed-{index}",
                    "schedule_id": "audited",
                    "status": "completed",
                    "trigger_context": {"scheduled": True},
                    "action_kind": "agent",
                    "action_args": [],
                    "fired_at": float(index + 1),
                    "chain_depth": 0,
                }
            )
        svc = _DBSchedulerStateService()

        @asynccontextmanager
        async def opened_db():
            yield db

        monkeypatch.setattr(svc, "_db_context", opened_db)
        engine = SchedulerEngine(svc=svc)
        monkeypatch.setattr(engine, "_tracked_fire", Mock())
        yield db, svc, engine, await db.get_schedule("audited")


def _observed_cwd(tmp_path, monkeypatch, failure):
    from lionagi.studio.scheduler import engine as module

    process_cwd = tmp_path / "scheduler-process"
    process_cwd.mkdir()
    expected = str(process_cwd.resolve())
    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(tmp_path / "configured-execution"))
    if failure is not None:

        def unavailable(_cls):
            raise failure("working directory cannot be resolved")

        monkeypatch.setattr(module.Path, "cwd", classmethod(unavailable))
    return expected


@pytest.mark.parametrize(
    ("path", "cause"),
    [
        ("recovery", "missed_fire_max_runs"),
        ("prefire", "scheduled_fire_max_runs"),
        ("budget", "budget_exhausted"),
        ("completion", "terminal_count_max_runs"),
    ],
)
@pytest.mark.parametrize("cwd_failure", [None, OSError, RuntimeError])
async def test_each_automatic_disable_records_its_cause_and_actual_cwd(
    tmp_path, monkeypatch, path, cause, cwd_failure
):
    async with _backed_engine(tmp_path, monkeypatch, budget=path == "budget") as (
        db,
        svc,
        engine,
        schedule,
    ):
        before = await _events(db)
        process_cwd = _observed_cwd(tmp_path, monkeypatch, cwd_failure)
        if path == "budget":
            monkeypatch.setattr(
                svc,
                "sum_schedule_spend",
                AsyncMock(return_value={"cost_usd": 3.0, "tokens": 9, "unreported_sessions": 0}),
            )
        if path == "recovery":
            await engine._recover_missed_fire_run_once(schedule, now=1000.0)
        elif path == "completion":
            await engine._check_max_runs(schedule, chain_depth=0)
        else:
            await engine._maybe_fire(schedule, now=1000.0)

        assert (await db.get_schedule("audited"))["enabled"] == 0
        history = await _events(db)
        assert history[:-1] == before
        assert len(history) == len(before) + 1
        event = history[-1]
        assert event["previous_status"] == "enabled"
        assert event["status"] == "disabled"
        budget = path == "budget"
        assert event["actor"] == ("scheduler:budget" if budget else "scheduler:max-runs")
        assert event["source"] == "scheduler"
        assert event["reason_code"] == (
            "schedule_definition.disabled.budget_exhausted"
            if budget
            else "schedule_definition.disabled.max_runs"
        )
        assert event["reason_summary"].strip()
        metadata = event["metadata"]
        assert metadata["schedule_name"] == "Observed definition"
        assert metadata["disable_cause"] == cause
        assert metadata["provenance"] == {
            "origin": "scheduler",
            "submitting_cwd": process_cwd if cwd_failure is None else None,
            "actor_status": "available",
            "cwd_status": "available" if cwd_failure is None else "unavailable",
            "assurance": "system",
            "failure_category": None if cwd_failure is None else "cwd_unavailable",
        }
        if budget:
            assert metadata["budget_usd"] == 3.0
            assert metadata["cost_usd"] == 3.0
            occurrence = await db.fetch_all(
                "SELECT reason_code FROM status_transitions WHERE entity_type='schedule_run'"
            )
            codes = [row["reason_code"] for row in occurrence]
            assert codes.count("schedule.budget.exhausted") == 1
            assert not any(code.startswith("schedule_definition.") for code in codes)
        else:
            assert metadata["max_runs"] == 2
            if path == "completion":
                assert metadata["terminal_runs"] == 2
            else:
                assert "terminal_runs" not in metadata
        engine._tracked_fire.assert_not_called()
        assert not engine._fire_tasks


async def test_budget_annotation_failure_still_records_honest_definition_cause(
    tmp_path, monkeypatch
):
    async with _backed_engine(tmp_path, monkeypatch, budget=True) as (db, svc, engine, schedule):
        _observed_cwd(tmp_path, monkeypatch, None)
        monkeypatch.setattr(
            svc,
            "sum_schedule_spend",
            AsyncMock(
                side_effect=[
                    {"cost_usd": 3.0, "tokens": 0, "unreported_sessions": 0},
                    RuntimeError("rollup reread failed"),
                ]
            ),
        )
        await engine._maybe_fire(schedule, now=1000.0)
        assert (await db.get_schedule("audited"))["enabled"] == 0
        event = (await _events(db))[-1]
        assert event["reason_code"] == "schedule_definition.disabled.budget_exhausted"
        assert event["metadata"]["budget_usd"] == 3.0
        assert event["metadata"]["cost_usd"] is None
        assert event["metadata"]["unreported_sessions"] is None
        assert [run["status"] for run in await db.list_schedule_runs("audited")].count(
            "skipped"
        ) == 1


@pytest.mark.parametrize("guarded", [False, True])
async def test_service_forwards_audit_records_separately_from_guarded_fields(monkeypatch, guarded):
    from lionagi.state.lifecycle import ReasonRecord, ScheduleAuditContext
    from lionagi.state.reasons import ScheduleDefinitionReasons
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    context = ScheduleAuditContext(
        actor="scheduler:max-runs",
        submitting_cwd="/actual/scheduler",
        origin="scheduler",
        source="scheduler",
        assurance="system",
        actor_status="available",
        cwd_status="available",
        failure_category=None,
    )
    reason = ReasonRecord(
        ScheduleDefinitionReasons.DISABLED_MAX_RUNS,
        "The configured run limit was reached.",
        metadata={"max_runs": 2},
    )
    db = Mock()
    db.update_schedule = AsyncMock(return_value=guarded)

    @asynccontextmanager
    async def opened_db():
        yield db

    svc = _DBSchedulerStateService()
    monkeypatch.setattr(svc, "_db_context", opened_db)
    claim = 1000.0 if guarded else NO_CURSOR_CLAIM
    github = "event-1" if guarded else NO_CURSOR_CLAIM
    matched = await svc.update_schedule(
        "audited",
        guard_cursor_forward=guarded,
        expect_next_fire_at=claim,
        expect_github_cursor=github,
        audit_context=context,
        reason=reason,
        enabled=0,
    )
    assert matched is guarded
    db.update_schedule.assert_awaited_once_with(
        "audited",
        guard_cursor_forward=guarded,
        expect_next_fire_at=claim,
        expect_github_cursor=github,
        audit_context=context,
        reason=reason,
        enabled=0,
    )


@pytest.mark.parametrize("method", ["update_schedule", "create_schedule_run_and_advance"])
def test_audit_arguments_are_explicit_service_parameters(method):
    from lionagi.studio.services.scheduler_state import (
        SchedulerStateService,
        _DBSchedulerStateService,
    )

    for service in (SchedulerStateService, _DBSchedulerStateService):
        parameters = inspect.signature(getattr(service, method)).parameters
        for name in ("audit_context", "reason"):
            assert name in parameters
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert parameters[name].default is None


@pytest.mark.parametrize("matched", [False, True])
async def test_occurrence_service_forwards_definition_audit_and_both_claims(monkeypatch, matched):
    from lionagi.state.lifecycle import ReasonRecord, ScheduleAuditContext
    from lionagi.state.reasons import ScheduleDefinitionReasons
    from lionagi.studio.services.scheduler_state import _DBSchedulerStateService

    context = ScheduleAuditContext(
        actor="scheduler:max-runs",
        submitting_cwd="/actual/scheduler",
        origin="scheduler",
        source="scheduler",
        assurance="system",
        actor_status="available",
        cwd_status="available",
        failure_category=None,
    )
    reason = ReasonRecord(
        ScheduleDefinitionReasons.DISABLED_MAX_RUNS,
        "The configured run limit was reached.",
    )
    db = Mock()
    db.create_schedule_run_and_advance = AsyncMock(return_value=matched)

    @asynccontextmanager
    async def opened_db():
        yield db

    svc = _DBSchedulerStateService()
    monkeypatch.setattr(svc, "_db_context", opened_db)
    run = {"id": "occurrence", "schedule_id": "audited"}
    fields = {"enabled": 0, "next_fire_at": 2000.0}
    result = await svc.create_schedule_run_and_advance(
        run,
        schedule_id="audited",
        schedule_fields=fields,
        expect_next_fire_at=1000.0,
        expect_github_cursor="event-1",
        audit_context=context,
        reason=reason,
    )
    assert result is matched
    assert fields == {"enabled": 0, "next_fire_at": 2000.0}
    db.create_schedule_run_and_advance.assert_awaited_once_with(
        run,
        schedule_id="audited",
        schedule_fields=fields,
        expect_next_fire_at=1000.0,
        expect_github_cursor="event-1",
        audit_context=context,
        reason=reason,
    )


@pytest.mark.parametrize("control", ["under_limit", "chain_child", "cursor_only"])
async def test_non_disable_scheduler_work_adds_no_definition_event(tmp_path, monkeypatch, control):
    async with _backed_engine(tmp_path, monkeypatch, runs=1) as (db, svc, engine, schedule):
        before = await _events(db)
        if control == "cursor_only":
            assert await svc.update_schedule("audited", next_fire_at=2000.0)
        else:
            await engine._check_max_runs(schedule, chain_depth=1 if control == "chain_child" else 0)
        assert (await db.get_schedule("audited"))["enabled"] == 1
        assert await _events(db) == before
