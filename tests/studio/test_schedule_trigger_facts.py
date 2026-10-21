# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule run projections expose trigger facts without their source payload."""

import json

import pytest

from lionagi.state.db import StateDB
from lionagi.studio.services import schedules


@pytest.mark.parametrize(
    "project", [schedules._run_view, schedules._run_summary, schedules._run_record]
)
@pytest.mark.parametrize("encoded", [False, True], ids=["native-json", "sqlite-text"])
@pytest.mark.parametrize(
    "context,fields,expected",
    [
        pytest.param({"scheduled": True}, {}, (True, None), id="tick"),
        pytest.param({"manual": True}, {}, (False, None), id="manual"),
        pytest.param({"missed_recovery": True}, {}, (False, None), id="recovery"),
        pytest.param({"scheduled": True}, {"chain_depth": 1}, (False, None), id="chain-depth"),
        pytest.param(
            {"scheduled": True}, {"chain_parent_id": "parent"}, (False, None), id="chain-parent"
        ),
        pytest.param({"scheduled": 1}, {}, (False, None), id="integer-is-not-true"),
        pytest.param({"scheduled": "true"}, {}, (False, None), id="string-is-not-true"),
        pytest.param(
            {"missed_fire_at": 1700000000.125},
            {"reason_code": "schedule.skipped.missed_fire"},
            (False, 1700000000.125),
            id="missed-fraction",
        ),
        pytest.param(
            {"missed_fire_at": 1700000000.1234567},
            {"reason_code": "schedule.skipped.missed_fire"},
            (False, 1700000000.1234567),
            id="missed-seven-fraction-digits",
        ),
        pytest.param(
            {"missed_fire_at": 1700000000},
            {"reason_code": "schedule.skipped.missed_fire"},
            (False, 1700000000.0),
            id="missed-integer",
        ),
        pytest.param(
            {"missed_fire_at": 2**53},
            {"reason_code": "schedule.skipped.missed_fire"},
            (False, float(2**53)),
            id="missed-largest-exact-integer",
        ),
        pytest.param(
            {"missed_fire_at": 1700000000.125},
            {"reason_code": "schedule.skipped.overlap"},
            (False, None),
            id="wrong-reason",
        ),
        pytest.param(None, {}, (False, None), id="null"),
        pytest.param([], {}, (False, None), id="list"),
    ],
)
def test_trigger_facts_are_identical_for_native_and_text_context(
    project, encoded, context, fields, expected
):
    row = {"id": "run", "trigger_context": json.dumps(context) if encoded else context, **fields}
    result = project(row)
    assert (result["scheduled"], result["missed_fire_at"]) == expected
    assert type(result["scheduled"]) is bool
    if expected[1] is not None:
        assert type(result["missed_fire_at"]) is float
    assert "trigger_context" not in result


@pytest.mark.parametrize(
    "value",
    [
        True,
        "1700000000",
        float("nan"),
        float("inf"),
        -float("inf"),
        None,
        10**400,
        2**53 + 1,
        2**53 + 2,
        -(2**53 + 2),
        2**60,
    ],
)
@pytest.mark.parametrize("encoded", [False, True], ids=["native-json", "sqlite-text"])
def test_invalid_missed_fire_times_are_null_and_json_safe(value, encoded):
    context = {"missed_fire_at": value}
    row = {
        "reason_code": "schedule.skipped.missed_fire",
        "trigger_context": json.dumps(context) if encoded else context,
    }
    for project in (schedules._run_view, schedules._run_summary, schedules._run_record):
        result = project(row)
        assert result["scheduled"] is False
        assert result["missed_fire_at"] is None
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"trigger_context": "{"},
        {"trigger_context": '"text"'},
        # An integer past the interpreter's digit limit, and nesting past the parser's depth:
        # neither text is a context, and neither may fail the read.
        {"trigger_context": '{"n": 1' + "0" * 4999 + "}"},
        {"trigger_context": "[" * 100_000},
    ],
    ids=["absent", "truncated", "string", "digit-limit", "nesting"],
)
def test_absent_and_malformed_context_has_both_default_facts(row):
    for project in (schedules._run_view, schedules._run_summary, schedules._run_record):
        result = project(row)
        assert result["scheduled"] is False
        assert result["missed_fire_at"] is None


def test_trigger_payload_does_not_leak_from_any_projection_or_chain_child():
    parent = {
        "id": "parent",
        "trigger_context": {"scheduled": True, "event": {"token": "private-payload-value"}},
    }
    child = {**parent, "id": "child", "chain_depth": 1, "chain_parent_id": "parent"}
    for project in (schedules._run_view, schedules._run_summary, schedules._run_record):
        result = project(parent)
        assert result["scheduled"] is True
        assert result["missed_fire_at"] is None
        assert set(result) == {"id", "error_class", "scheduled", "missed_fire_at"}
    record = schedules._run_record({**parent, "chain_children": [child]})
    assert record["chain_children"][0]["scheduled"] is False
    assert record["chain_children"][0]["missed_fire_at"] is None
    for private in ("trigger_context", "event", "token", "private-payload-value"):
        assert private not in json.dumps(record)


@pytest.mark.parametrize("value", ["--payload-value", "Fix: x_y!"], ids=["flag", "charset"])
def test_a_refused_command_arg_render_does_not_carry_the_rendered_value(value):
    """The refusal is stored on the run as its error and served with it, so it names the
    template and never the trigger value it rendered to."""
    from lionagi.studio.scheduler.subprocess import _render_command_arg

    with pytest.raises(ValueError) as raised:
        _render_command_arg("{{title}}", {"title": value})
    assert "{{title}}" in str(raised.value)
    assert value not in str(raised.value)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


async def _seed_run(db, rid, context, **fields):
    await db.create_schedule_run(
        {
            "id": rid,
            "schedule_id": "nightly",
            "action_kind": "agent",
            "action_args": [],
            "status": "completed",
            "fired_at": 1700000000.0,
            "trigger_context": context,
            **fields,
        }
    )


async def test_database_rows_match_across_run_routes_and_recent_summaries(isolated_db):
    from lionagi.studio.scheduler.engine import SchedulerEngine

    async with StateDB() as db:
        await db.create_schedule(
            {
                "id": "nightly",
                "name": "nightly",
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
                "next_fire_at": 1700000000.125,
                "missed_fire_policy": "skip",
            }
        )
        await _seed_run(
            db, "tick", {"scheduled": True, "event": {"token": "private-payload-value"}}
        )
        await _seed_run(db, "manual", {"manual": True})
        await _seed_run(
            db,
            "child",
            {"scheduled": True, "chain_from": "tick"},
            chain_depth=1,
            chain_parent_id="tick",
        )
        schedule = await db.get_schedule("nightly")
    await SchedulerEngine()._record_missed_fire_skip(schedule, now=1700000100.0)

    views = await schedules.list_schedule_runs_route("nightly", limit=25, offset=0, status=None)
    detail = await schedules.get_schedule_route("nightly")
    listed = await schedules.list_schedules_route(
        enabled=None, trigger_type=None, project=None, include_recent_runs=True
    )
    slices = [
        views["runs"],
        detail["recent_runs"],
        listed["schedules"][0]["recent_runs_summary"]["runs"],
    ]
    assert all(len(rows) == 4 for rows in slices)
    expected = {"tick": (True, None), "manual": (False, None), "child": (False, None)}
    missed = next(row for row in views["runs"] if row["id"] not in expected)
    expected[missed["id"]] = (False, 1700000000.125)
    for rows in slices:
        assert {r["id"]: (r["scheduled"], r["missed_fire_at"]) for r in rows} == expected
        assert type(next(r for r in rows if r["id"] == missed["id"])["missed_fire_at"]) is float
        assert "private-payload-value" not in json.dumps(rows)
    for rid, facts in expected.items():
        record = await schedules.get_schedule_run_route(rid)
        assert (record["scheduled"], record["missed_fire_at"]) == facts
    parent = await schedules.get_schedule_run_route("tick")
    assert parent["chain_children"][0]["scheduled"] is False


def test_machine_runs_preserves_trigger_facts_from_the_http_service(monkeypatch):
    from lionagi.cli import machine_schedule

    row = schedules._run_view({"id": "tick", "trigger_context": {"scheduled": True}})
    requests = []

    def studio(path):
        requests.append(path)
        return {"runs": [row], "limit": 25, "offset": 0, "has_next": False}

    monkeypatch.setattr(machine_schedule, "_studio", studio)
    result = machine_schedule._runs(["nightly", "--limit", "25"])
    assert requests == ["/nightly/runs?limit=25"]
    assert result["runs"][0]["scheduled"] is True
    assert result["runs"][0]["missed_fire_at"] is None


def _captured_trigger_context(tracked):
    tracked.assert_called_once()
    return tracked.call_args.kwargs["trigger_context"]


def _served_scheduled(context):
    row = {"id": "run", "trigger_context": context}
    return [
        project(row)["scheduled"]
        for project in (schedules._run_view, schedules._run_summary, schedules._run_record)
    ]


@pytest.mark.parametrize(
    "trigger",
    [
        {"trigger_type": "cron", "cron_expr": "0 * * * *"},
        {"trigger_type": "interval", "cron_expr": None, "interval_sec": 60},
        {"trigger_type": "at", "cron_expr": None},
    ],
    ids=["cron", "interval", "at"],
)
async def test_a_timer_fire_is_written_and_served_as_scheduled(trigger):
    from unittest.mock import patch

    from lionagi.studio.scheduler.engine import SchedulerEngine
    from tests.studio.test_scheduler_engine import _make_svc, _minimal_schedule

    engine = SchedulerEngine(svc=_make_svc())
    with patch.object(engine, "_tracked_fire") as tracked:
        await engine._maybe_fire(_minimal_schedule(next_fire_at=1000.0, **trigger), now=1000.0)
    assert _served_scheduled(_captured_trigger_context(tracked)) == [True, True, True]


async def test_a_manual_fire_is_written_and_served_as_not_scheduled():
    from unittest.mock import patch

    from lionagi.studio.scheduler.engine import SchedulerEngine
    from tests.studio.test_scheduler_engine import _make_svc, _minimal_schedule

    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(next_fire_at=1000.0)
    engine = SchedulerEngine(svc=svc)
    with patch.object(engine, "_tracked_fire") as tracked:
        await engine.fire_now("sched-001")
    context = _captured_trigger_context(tracked)
    assert context["manual"] is True
    assert _served_scheduled(context) == [False, False, False]


async def test_a_timer_fire_dispatched_again_after_a_restart_is_still_served_as_scheduled():
    from unittest.mock import patch

    from lionagi.studio.scheduler.engine import SchedulerEngine
    from tests.studio.test_scheduler_engine import _make_svc, _minimal_schedule

    svc = _make_svc()
    svc.get_schedule.return_value = _minimal_schedule(enabled=True)
    svc.list_undispatched_schedule_runs.return_value = [
        {
            "id": "orphan",
            "schedule_id": "sched-001",
            "chain_depth": 0,
            "trigger_context": {"scheduled": True, "fired_at": 1000.0},
        }
    ]
    engine = SchedulerEngine(svc=svc)
    with patch.object(engine, "_tracked_fire") as tracked:
        await engine._recover_undispatched_fires()
    assert tracked.call_args.kwargs["supersedes_run_id"] == "orphan"
    assert _served_scheduled(_captured_trigger_context(tracked)) == [True, True, True]
