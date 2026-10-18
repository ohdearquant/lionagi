# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Skipped occurrences explain their persisted reason on every run reader."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event

from lionagi.cli import machine_schedule
from lionagi.state.db import StateDB
from lionagi.studio import cli as schedule_cli
from lionagi.studio.services import run_view, schedules
from lionagi.studio.services.scheduler_state import _DBSchedulerStateService, create_skipped_run

_CAPACITY_REASON = "schedule.deferred.capacity"
_CAPACITY_SUMMARY = (
    "Schedule fire deferred: global concurrent-fire cap reached; will retry next tick."
)
_SKIP_REASONS = (
    (_CAPACITY_REASON, _CAPACITY_SUMMARY),
    (
        "schedule.skipped.overlap",
        "Schedule fire skipped because overlap_policy=skip and a prior run is still active.",
    ),
    (
        "schedule.skipped.missed_fire",
        "Schedule fire skipped because the scheduled time passed while the server was down "
        "or the tick was delayed (missed_fire_policy=skip).",
    ),
    (
        "schedule.budget.exhausted",
        "Schedule fire refused and the schedule disabled because its "
        "configured spend budget is exhausted.",
    ),
)


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


async def _seed_skipped_run(reason_code: str, reason_summary: str) -> None:
    schedule = {
        "id": "nightly",
        "name": "nightly",
        "trigger_type": "interval",
        "interval_sec": 60,
        "action_kind": "command",
    }
    async with StateDB() as db:
        await db.create_schedule(schedule)
    await create_skipped_run(
        _DBSchedulerStateService(),
        run_id="deferred",
        schedule=schedule,
        trigger_context={"private": "event payload"},
        now=100.0,
        reason_code=reason_code,
        reason_summary=reason_summary,
        metadata={"deferral_count": 1},
    )


def _assert_reason(view: dict, code: str, summary: str) -> None:
    assert view["reason_code"] == code
    assert view["reason_summary"] == summary
    assert view["outcome"] == {
        "code": "skipped",
        "summary": summary,
        "source": "occurrence",
        "summary_reported": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason_code", "reason_summary"), _SKIP_REASONS)
async def test_skipped_reason_survives_service_and_wire_views(
    isolated_db: Path, reason_code: str, reason_summary: str
) -> None:
    await _seed_skipped_run(reason_code, reason_summary)

    async with StateDB() as db:
        raw = await db.get_schedule_run("deferred")
        assert raw is not None
        assert not raw.get("error_detail")
        direct = await run_view.get_run_view(db, "deferred")
    service_list = await schedules.list_schedule_run_views("nightly")
    service_detail = await schedules.get_schedule_run("deferred")
    service_status = await schedules.get_schedule_status("nightly")
    wire_list = await schedules.list_schedule_runs_route("nightly", status=None, limit=20, offset=0)
    wire_detail = await schedules.get_schedule_run_route("deferred")
    wire_status = await schedules.get_schedule_status_route("nightly")

    assert service_status is not None
    for view in (
        direct,
        service_list[0],
        service_detail,
        service_status["latest_run"],
        wire_list["runs"][0],
        wire_detail,
        wire_status["latest_run"],
    ):
        assert view is not None
        _assert_reason(view, reason_code, reason_summary)
    assert "trigger_context" not in wire_list["runs"][0]
    assert "trigger_context" not in wire_detail


@pytest.mark.asyncio
async def test_latest_recorded_skip_reason_wins(isolated_db: Path) -> None:
    await _seed_skipped_run(*_SKIP_REASONS[1])
    async with StateDB() as db:
        await db.update_status(
            "schedule_run",
            "deferred",
            new_status="skipped",
            reason_code=_CAPACITY_REASON,
            reason_summary=_CAPACITY_SUMMARY,
        )
        await db.execute(
            "UPDATE status_transitions SET created_at = ? "
            "WHERE entity_type = 'schedule_run' AND entity_id = ?",
            (1000.0, "deferred"),
        )
        await db.execute(
            "UPDATE status_transitions SET created_at = ? "
            "WHERE entity_type = 'schedule_run' AND entity_id = ? AND reason_code = ?",
            (2000.0, "deferred", _CAPACITY_REASON),
        )
        view = await run_view.get_run_view(db, "deferred")
    assert view is not None
    _assert_reason(view, _CAPACITY_REASON, _CAPACITY_SUMMARY)


@pytest.mark.asyncio
async def test_recorded_reason_text_keeps_list_disclosure_boundary(isolated_db: Path) -> None:
    summary = "Connection refused: private endpoint token=secret-value"
    await _seed_skipped_run(_CAPACITY_REASON, summary)

    service = await schedules.get_schedule_run("deferred")
    assert service is not None
    _assert_reason(service, _CAPACITY_REASON, summary)
    listed = await schedules.list_schedule_runs_route("nightly", status=None, limit=20, offset=0)
    status = await schedules.get_schedule_status_route("nightly")
    for view in (listed["runs"][0], status["latest_run"]):
        assert view["reason_code"] == _CAPACITY_REASON
        assert view["reason_summary"] == "network"
        assert view["outcome"]["summary"] == "network"
        assert "secret-value" not in json.dumps(view)
    detail = await schedules.get_schedule_run_route("deferred")
    _assert_reason(detail, _CAPACITY_REASON, summary)


@pytest.mark.asyncio
async def test_legacy_skipped_run_without_reason_keeps_fallback(isolated_db: Path) -> None:
    await _seed_skipped_run(_CAPACITY_REASON, _CAPACITY_SUMMARY)
    async with StateDB() as db:
        await db.execute(
            "DELETE FROM status_transitions WHERE entity_type = 'schedule_run' AND entity_id = ?",
            ("deferred",),
        )
        view = await run_view.get_run_view(db, "deferred")
    assert view is not None
    assert view["reason_code"] is None
    assert view["reason_summary"] is None
    assert view["outcome"]["summary"] == "skipped"
    assert view["outcome"]["summary_reported"] is False


@pytest.mark.asyncio
async def test_reading_legacy_store_does_not_create_status_ledger(isolated_db: Path) -> None:
    await _seed_skipped_run(_CAPACITY_REASON, _CAPACITY_SUMMARY)
    with sqlite3.connect(isolated_db) as connection:
        connection.execute("DROP TABLE status_transitions")

    async with StateDB(isolated_db, readonly=True) as db:
        view = await run_view.get_run_view(db, "deferred")

    assert view is not None
    assert view["reason_code"] is None
    assert view["reason_summary"] is None
    assert view["outcome"]["summary"] == "skipped"
    with sqlite3.connect(isolated_db) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'status_transitions'"
            ).fetchall()
            == []
        )


@pytest.mark.asyncio
async def test_run_list_loads_reason_events_in_one_batch(isolated_db: Path) -> None:
    await _seed_skipped_run(_CAPACITY_REASON, _CAPACITY_SUMMARY)
    async with StateDB() as db:
        for index in range(3):
            await db.create_schedule_run(
                {
                    "id": f"skip-{index}",
                    "schedule_id": "nightly",
                    "trigger_context": {},
                    "action_kind": "command",
                    "action_args": [],
                    "status": "skipped",
                    "fired_at": 100.0 + index,
                }
            )
            await db.update_status(
                "schedule_run",
                f"skip-{index}",
                new_status="skipped",
                reason_code=_CAPACITY_REASON,
                reason_summary=_CAPACITY_SUMMARY,
            )
        reads = []

        def record_query(conn, cursor, statement, parameters, context, executemany):
            if "FROM status_transitions" in statement:
                reads.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", record_query)
        try:
            views = await run_view.list_run_views(db, "nightly")
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", record_query)

    assert len(reads) == 1
    assert len(views) == 4
    for view in views:
        _assert_reason(view, _CAPACITY_REASON, _CAPACITY_SUMMARY)


@pytest.mark.asyncio
async def test_reason_batch_respects_sqlite_parameter_bound(isolated_db: Path) -> None:
    await _seed_skipped_run(_CAPACITY_REASON, _CAPACITY_SUMMARY)
    runs = [{"id": f"missing-{index}", "status": "skipped"} for index in range(500)]
    runs.append({"id": "deferred", "status": "skipped"})
    parameter_counts = []

    def record_query(conn, cursor, statement, parameters, context, executemany):
        if "FROM status_transitions" in statement:
            parameter_counts.append(len(parameters))

    async with StateDB() as db:
        event.listen(db._engine.sync_engine, "before_cursor_execute", record_query)
        try:
            enriched = await run_view._with_skip_reasons(db, runs)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", record_query)

    assert parameter_counts == [500, 1]
    assert enriched[:-1] == runs[:-1]
    assert enriched[-1]["reason_code"] == _CAPACITY_REASON
    assert enriched[-1]["reason_summary"] == _CAPACITY_SUMMARY


@pytest.mark.asyncio
@pytest.mark.parametrize("subcommand", ("runs", "run"))
async def test_capacity_reason_is_untruncated_in_human_and_json_cli(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    subcommand: str,
) -> None:
    await _seed_skipped_run(_CAPACITY_REASON, _CAPACITY_SUMMARY)
    result = (
        await schedules.list_schedule_runs_route("nightly", status=None, limit=20, offset=0)
        if subcommand == "runs"
        else await schedules.get_schedule_run_route("deferred")
    )
    monkeypatch.setattr(schedule_cli, "_api", lambda path: result)
    args = argparse.Namespace(id="nightly" if subcommand == "runs" else "deferred", limit=20)
    command = schedule_cli._cmd_runs if subcommand == "runs" else schedule_cli._cmd_run

    assert command(args) == 0
    human = capsys.readouterr().out
    assert _CAPACITY_REASON in human
    assert _CAPACITY_SUMMARY in human

    args.as_json = True
    assert command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    view = payload["runs"][0] if subcommand == "runs" else payload
    _assert_reason(view, _CAPACITY_REASON, _CAPACITY_SUMMARY)


@pytest.mark.asyncio
async def test_capacity_reason_passes_through_machine_runs(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_skipped_run(_CAPACITY_REASON, _CAPACITY_SUMMARY)
    result = await schedules.list_schedule_runs_route("nightly", status=None, limit=20, offset=0)
    monkeypatch.setattr(machine_schedule, "_studio", lambda path: result)

    payload = machine_schedule.dispatch_schedule(["runs", "nightly"])

    _assert_reason(payload["runs"][0], _CAPACITY_REASON, _CAPACITY_SUMMARY)
