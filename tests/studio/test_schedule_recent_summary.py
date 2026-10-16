# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Recent schedule slices are bounded, batched, and distinguish failure from empty."""

import sqlite3
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionagi.studio.services import schedules


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


async def create_schedule(db: StateDB, sid: str, **fields) -> None:
    await db.create_schedule(
        {
            "id": sid,
            "name": sid,
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
            **fields,
        }
    )


async def create_run(db: StateDB, sid: str, rid: str, **fields) -> None:
    await db.create_schedule_run(
        {
            "id": rid,
            "schedule_id": sid,
            "action_kind": "agent",
            "status": "failed",
            "fired_at": 100,
            "error_detail": "Permission denied: private path",
            "action_args": ["private prompt"],
            "trigger_context": {"secret": "payload"},
            **fields,
        }
    )


async def list_rows(**filters) -> list[dict]:
    response = await schedules.list_schedules_route(
        enabled=filters.get("enabled"),
        trigger_type=None,
        project=filters.get("project"),
        include_recent_runs=True,
    )
    return response["schedules"]


@pytest.mark.asyncio
async def test_recent_summary_is_opt_in_and_default_does_no_summary_work(isolated_db, monkeypatch):
    async with StateDB() as db:
        await create_schedule(db, "nightly")

    async def forbidden(*args, **kwargs):
        pytest.fail("Default list consumers must not compute recent histories")

    monkeypatch.setattr(schedules, "_recent_run_summaries", forbidden)
    rows = await schedules.list_schedules()
    assert len(rows) == 1
    assert "recent_runs_summary" not in rows[0]
    response = await schedules.list_schedules_route(enabled=None, trigger_type=None, project=None)
    assert "recent_runs_summary" not in response["schedules"][0]


@pytest.mark.asyncio
async def test_recent_reads_limit_index_work_and_fetch_only_displayed_error_payloads(
    isolated_db, monkeypatch, record_property
):
    async with StateDB() as db:
        await create_schedule(db, "nightly")
    # Distinct timestamps let the existing schedule/time index stop at the
    # requested prefix; equal-timestamp groups may still need an id sort.
    with sqlite3.connect(isolated_db) as connection:
        connection.executemany(
            "INSERT INTO schedule_runs (id, schedule_id, action_kind, status, fired_at, error_detail,"
            " trigger_context, action_args, created_at)"
            " VALUES (?, 'nightly', 'agent', 'failed', ?, ?, '{}', '{}', 0)",
            [
                (f"run-{index:04}", index, "Permission denied " + "x" * 2048)
                for index in range(2000)
            ],
        )

    payloads = []
    steps = 0
    statements = []
    plan = []

    def progress():
        nonlocal steps
        steps += 1

    def error_payload(value):
        payloads.append(value)
        return value

    original = StateDB.fetch_all

    async def observed_read(db, query, params=None):
        if "schedule_runs" not in query:
            return await original(db, query, params)
        statements.append(query)
        with sqlite3.connect(isolated_db) as connection:
            connection.row_factory = sqlite3.Row
            connection.create_function("observe_error", 1, error_payload)
            columns = ", ".join(schedules._RUN_SUMMARY_FIELDS)
            connection.execute(
                f"CREATE TEMP VIEW observed_runs AS SELECT {columns},"
                " observe_error(error_detail) AS error_detail FROM schedule_runs"
            )
            observed_query = query.replace("schedule_runs", "observed_runs")
            plan.extend(
                row["detail"]
                for row in connection.execute("EXPLAIN QUERY PLAN " + observed_query, params)
            )
            connection.set_progress_handler(progress, 1)
            return [dict(row) for row in connection.execute(observed_query, params)]

    monkeypatch.setattr(StateDB, "fetch_all", observed_read)
    async with StateDB() as db:
        summary = (await schedules._recent_run_summaries(db, ["nightly"]))["nightly"]
    assert summary["status"] == "available"
    assert summary["has_next"] is True
    assert len(summary["runs"]) == 25
    assert len(statements) == 1
    record_property("sqlite_vm_steps", steps)
    record_property("error_payload_reads", len(payloads))
    record_property("query_plan", " | ".join(plan))
    assert any("idx_sched_runs_schedule" in step for step in plan)
    assert len(payloads) == 25
    assert steps < 5000, f"Expected an indexed prefix read, observed {steps} SQLite VM steps"


@pytest.mark.asyncio
async def test_tied_summary_and_detail_choose_the_same_first_page(isolated_db):
    async with StateDB() as db:
        await create_schedule(db, "nightly")
        for index in reversed(range(30)):
            await create_run(db, "nightly", f"run-{index:02}")
    summary = (await list_rows())[0]["recent_runs_summary"]
    history = await schedules.list_schedule_run_views("nightly", limit=25)
    assert [row["id"] for row in summary["runs"]] == [row["id"] for row in history]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 25, 26, 40])
async def test_recent_slice_order_bound_completeness_and_projection(isolated_db, count):
    async with StateDB() as db:
        await create_schedule(db, "nightly")
        for index in range(count):
            await create_run(db, "nightly", f"run-{index:02}", chain_depth=index % 2)

    summary = (await list_rows())[0]["recent_runs_summary"]
    assert summary["version"] == 1
    assert summary["status"] == "available"
    assert summary["limit"] == 25
    assert summary["has_next"] is (count > 25)
    assert [r["id"] for r in summary["runs"]] == [
        f"run-{index:02}" for index in range(count - 1, max(-1, count - 26), -1)
    ]
    for row in summary["runs"]:
        assert set(row) == set(schedules._RUN_SUMMARY_FIELDS) | {"error_class"}
        assert row["error_class"] == "permission"
        assert row["status"] == "failed"
        assert row["chain_depth"] == int(row["id"].split("-")[1]) % 2
    assert "private" not in str(summary)


@pytest.mark.asyncio
async def test_100_schedules_use_three_batched_reads_and_no_history_reads(isolated_db, monkeypatch):
    async with StateDB() as db:
        for index in range(100):
            sid = f"schedule-{index}"
            iid = f"invocation-{index}"
            await create_schedule(db, sid)
            await db.create_invocation(
                {"id": iid, "skill": "agent", "status": "completed", "started_at": 100}
            )
            await create_run(db, sid, f"run-{index}", invocation_id=iid)

    calls = []
    original = StateDB.fetch_all

    async def fetch_all(db, query, params=None):
        result = await original(db, query, params)
        calls.append((query, len(result)))
        return result

    async def forbidden(*args, **kwargs):
        pytest.fail("The list must not issue per-schedule or per-run history queries")

    monkeypatch.setattr(StateDB, "fetch_all", fetch_all)
    monkeypatch.setattr(StateDB, "list_schedule_runs", forbidden)
    monkeypatch.setattr(StateDB, "get_invocation", forbidden)
    monkeypatch.setattr(StateDB, "list_sessions_for_invocation", forbidden)
    rows = await list_rows()
    assert len(rows) == 100
    assert len(calls) == 3
    assert calls[0][1] == 100
    assert "ORDER BY fired_at DESC, id DESC LIMIT ?" in calls[0][0]
    assert all(len(row["recent_runs_summary"]["runs"]) == 1 for row in rows)


@pytest.mark.asyncio
async def test_status_fields_and_reconciled_error_class_match_history(isolated_db):
    statuses = ["running", "completed", "failed", "skipped", "cancelled", "timed_out"]
    async with StateDB() as db:
        await create_schedule(db, "nightly")
        await db.create_invocation(
            {"id": "inv", "skill": "agent", "status": "failed", "started_at": 100}
        )
        await db.create_progression("progression")
        await db.create_session(
            {
                "id": "session",
                "progression_id": "progression",
                "invocation_id": "inv",
                "status": "failed",
                "status_reason_summary": "Connection refused: private host",
            }
        )
        await db.update_status(
            "session",
            "session",
            new_status="failed",
            expected_statuses={"failed"},
            reason_code="run.failed.exception",
            reason_summary="Connection refused: private host",
        )
        for index, status in enumerate(statuses):
            await create_run(
                db,
                "nightly",
                f"run-{index}",
                status=status,
                fired_at=100 + index,
                exit_code=index,
                ended_at=200 + index,
                invocation_id="inv",
            )

    summary = (await list_rows())[0]["recent_runs_summary"]
    history = await schedules.list_schedule_run_views("nightly", limit=25)
    assert summary["runs"] == [schedules._run_summary(row) for row in history]
    assert [row["status"] for row in summary["runs"]] == statuses[::-1]
    assert all(row["error_class"] == "network" for row in summary["runs"])
    assert "private" not in str(summary)
    async with StateDB() as db:
        await db.create_progression("active-progression")
        await db.create_session(
            {
                "id": "active-session",
                "progression_id": "active-progression",
                "invocation_id": "inv",
                "status": "running",
            }
        )
    # One active child prevents grading the invocation from its terminal sibling.
    summary = (await list_rows())[0]["recent_runs_summary"]
    history = await schedules.list_schedule_run_views("nightly", limit=25)
    assert summary["runs"] == [schedules._run_summary(row) for row in history]
    assert all(row["error_class"] == "permission" for row in summary["runs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_read", [1, 2, 3])
async def test_batch_failure_is_unavailable_for_every_slice(isolated_db, monkeypatch, failed_read):
    async with StateDB() as db:
        await create_schedule(db, "empty")
        await create_schedule(db, "ran")
        await db.create_invocation(
            {"id": "inv", "skill": "agent", "status": "failed", "started_at": 100}
        )
        await create_run(db, "ran", "run", invocation_id="inv")
    original = StateDB.fetch_all
    reads = 0

    async def fetch_all(db, query, params=None):
        nonlocal reads
        reads += 1
        if reads == failed_read:
            raise OSError("Database read unavailable")
        return await original(db, query, params)

    monkeypatch.setattr(StateDB, "fetch_all", fetch_all)
    rows = await list_rows()
    assert len(rows) == 2
    assert all(
        row["recent_runs_summary"]
        == {"version": 1, "status": "unavailable", "limit": 25, "has_next": None, "runs": []}
        for row in rows
    )


@pytest.mark.asyncio
async def test_serialization_failure_is_attributed_without_hiding_other_schedules(
    isolated_db, monkeypatch
):
    async with StateDB() as db:
        for sid in ["empty", "bad", "good"]:
            await create_schedule(db, sid)
        await create_run(db, "bad", "bad-run")
        await create_run(db, "good", "good-run")
    original = schedules._run_summary

    def project(row):
        result = original(row)
        if row["schedule_id"] == "bad":
            result["fired_at"] = object()
        return result

    monkeypatch.setattr(schedules, "_run_summary", project)
    rows = {row["id"]: row["recent_runs_summary"] for row in await list_rows()}
    assert rows["bad"]["status"] == "unavailable"
    assert rows["bad"]["has_next"] is None
    assert rows["empty"]["status"] == "available"
    assert rows["empty"]["runs"] == []
    assert rows["good"]["status"] == "available"
    assert len(rows["good"]["runs"]) == 1


@pytest.mark.asyncio
async def test_list_filters_and_detail_history_remain_independent(isolated_db):
    async with StateDB() as db:
        await create_schedule(db, "selected", project="project-one")
        await create_schedule(db, "other", project="project-two")
        for index in range(30):
            await create_run(db, "selected", f"run-{index:02}", fired_at=index)
    rows = await list_rows(project="project-one")
    assert [row["id"] for row in rows] == ["selected"]
    assert len(rows[0]["recent_runs_summary"]["runs"]) == 25
    detail = await schedules.get_schedule_route("selected")
    assert len(detail["recent_runs"]) == 10
    assert "recent_runs_summary" not in detail
    history = await schedules.list_schedule_run_views("selected", limit=7, offset=25)
    assert [row["id"] for row in history] == [f"run-{index:02}" for index in range(4, -1, -1)]
