# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Automatic shutdowns are recorded as automatic, with their cause.

The store defaults an unexplained disable to the operator-request code, which
is the right default for a route a person called and the wrong one for the
scheduler. Left alone it files every automated shutdown in the audit ledger as
something a person asked for, which is the single claim the ledger must not
invent. These tests pin both halves: the scheduler's blanket default never says
"request", and the paths that know why they are stopping say which reason.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from lionagi.state.reasons import ScheduleReasons


def _minimal_schedule(**overrides) -> dict:
    base = {
        "id": "sched-001",
        "name": "test-sched",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
    }
    base.update(overrides)
    return base


def _make_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.update_schedule = AsyncMock()
    svc.count_schedule_runs = AsyncMock(return_value=0)
    svc.sum_schedule_spend = AsyncMock(return_value={"cost_usd": 0.0, "tokens": 0})
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    svc.update_status = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_scheduler_disable_without_a_stated_cause_is_not_filed_as_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unclassified scheduler disable lands as automatic.

    This is the catch-all arm. It says less than the specific codes below, but
    a reader can still tell it apart from a person having asked, which is the
    distinction that was being lost.
    """
    from lionagi.studio.services import scheduler_state

    captured: dict[str, object] = {}

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def update_schedule(self, schedule_id, **fields):  # noqa: ANN001 - test double
            captured.update(fields)
            captured["schedule_id"] = schedule_id

    monkeypatch.setattr(scheduler_state, "StateDB", _FakeDB)

    svc = scheduler_state._DBSchedulerStateService()  # noqa: SLF001
    await svc.update_schedule("sched-001", enabled=0)

    assert captured["lifecycle_reason_code"] == ScheduleReasons.DISABLED_AUTOMATIC
    assert captured["lifecycle_reason_code"] != ScheduleReasons.DISABLED_REQUEST


@pytest.mark.asyncio
async def test_scheduler_disable_keeps_a_cause_the_caller_already_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must not overwrite a caller that stated its reason.

    Without this, the specific codes the engine passes below would be
    flattened back to the catch-all and the cause lost a second way.
    """
    from lionagi.studio.services import scheduler_state

    captured: dict[str, object] = {}

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def update_schedule(self, schedule_id, **fields):  # noqa: ANN001 - test double
            captured.update(fields)

    monkeypatch.setattr(scheduler_state, "StateDB", _FakeDB)

    svc = scheduler_state._DBSchedulerStateService()  # noqa: SLF001
    await svc.update_schedule(
        "sched-001",
        enabled=0,
        lifecycle_reason_code=ScheduleReasons.DISABLED_BUDGET_EXHAUSTED,
    )

    assert captured["lifecycle_reason_code"] == ScheduleReasons.DISABLED_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_max_runs_exhaustion_records_the_max_runs_code() -> None:
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    svc.count_schedule_runs = AsyncMock(return_value=5)
    engine = SchedulerEngine(svc=svc)

    await engine._check_max_runs(_minimal_schedule(max_runs=5), chain_depth=0)  # noqa: SLF001

    svc.update_schedule.assert_awaited_once()
    kwargs = svc.update_schedule.await_args.kwargs
    assert kwargs["enabled"] == 0
    assert kwargs["lifecycle_reason_code"] == ScheduleReasons.DISABLED_MAX_RUNS


@pytest.mark.asyncio
async def test_budget_exhaustion_records_the_budget_code() -> None:
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)

    await engine._disable_for_budget_exhausted(  # noqa: SLF001
        _minimal_schedule(budget_usd=1.0), now=0.0
    )

    svc.update_schedule.assert_awaited_once()
    kwargs = svc.update_schedule.await_args.kwargs
    assert kwargs["enabled"] == 0
    assert kwargs["lifecycle_reason_code"] == ScheduleReasons.DISABLED_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_missed_fire_recovery_refused_by_max_runs_records_the_max_runs_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missed-fire recovery path disables for the same reason as the rest.

    Of the four places the scheduler shuts a schedule down, this is the one no
    other test reaches, so it is the one that would silently keep filing its
    shutdowns as an operator request.
    """
    import time

    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)

    async def _refuse():
        return False, None

    monkeypatch.setattr(engine, "_reserve_max_runs_budget", lambda schedule: _refuse())

    schedule = _minimal_schedule(
        trigger_type="at", cron_expr=None, max_runs=1, next_fire_at=time.time() - 60
    )
    await engine._recover_missed_fire_run_once(schedule, time.time())  # noqa: SLF001

    svc.update_schedule.assert_awaited_once()
    kwargs = svc.update_schedule.await_args.kwargs
    assert kwargs["enabled"] == 0
    assert kwargs["lifecycle_reason_code"] == ScheduleReasons.DISABLED_MAX_RUNS


def test_the_automatic_codes_are_distinct_from_each_other_and_from_a_request() -> None:
    """Three separate codes, or the ledger cannot answer why it stopped.

    Collapsing any pair would still read as "not an operator request" while
    silently discarding the cause, so distinctness is the property to hold,
    not merely being different from the request code.
    """
    codes = {
        ScheduleReasons.DISABLED_AUTOMATIC,
        ScheduleReasons.DISABLED_MAX_RUNS,
        ScheduleReasons.DISABLED_BUDGET_EXHAUSTED,
        ScheduleReasons.DISABLED_REQUEST,
    }
    assert len(codes) == 4
