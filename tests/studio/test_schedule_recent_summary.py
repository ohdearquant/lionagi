# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Recent schedule slices are bounded, batched, and distinguish failure from empty."""

import json
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
async def test_tied_boundary_keys_are_cut_by_id_and_the_extra_key_has_no_payload(isolated_db):
    """A tie group that straddles the displayed 25 is cut by id DESC, and the
    one key past the limit is read without its occurrence payload."""
    async with StateDB() as db:
        await create_schedule(db, "nightly")
        # Twenty-four runs newer than the tie group, then six runs sharing one
        # older fired_at. Only the two greatest ids of that group are among the
        # newest 26 keys, and only the greater of those two is displayed. The
        # group is inserted in descending id order: insertion order and id order
        # disagree, so a statement that drops the id tie-break cannot pass by
        # returning the rows in the order they were written.
        for index in range(24):
            await create_run(db, "nightly", f"run-{index:02}", fired_at=101 + index)
        for suffix in "fedcba":
            await create_run(db, "nightly", f"tie-{suffix}", fired_at=100)
        grouped, _, _ = await schedules._read_run_batch(db, ["nightly"])
        summary = (await list_rows())[0]["recent_runs_summary"]

    keys = [row["id"] for row in grouped["nightly"]]
    assert len(keys) == 26
    assert keys[-2:] == ["tie-f", "tie-e"]
    assert keys[24] == "tie-f"
    # The 26th key is read for its boundary position only; every occurrence
    # column past the limit stays empty, so its payload is never loaded.
    assert grouped["nightly"][25]["id"] == "tie-e"
    assert grouped["nightly"][25]["status"] is None
    assert grouped["nightly"][25]["error_detail"] is None

    assert summary["has_next"] is True
    assert [row["id"] for row in summary["runs"]][-2:] == ["run-00", "tie-f"]
    assert len(summary["runs"]) == 25


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
        calls.append((query, params, len(result)))
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
    # The three reads are set-based and issued in a fixed order: the bounded key
    # read, then the invocation evidence, then the session evidence. Assert the
    # calls and their order through what each one reads, not through the text of
    # one statement: the bound limit and the row each read returns are the facts
    # a per-row rewrite would change.
    key_params, key_rows = calls[0][1], calls[0][2]
    assert key_rows == 100
    assert json.loads(key_params[0]) == [row["id"] for row in rows]
    assert key_params[1] == schedules._RECENT_RUN_LIMIT + 1
    assert key_params[2] == schedules._RECENT_RUN_LIMIT
    # The second set read resolves the 100 invocations; the third resolves their
    # sessions, and this fixture links none, so it returns no rows while still
    # being issued (three reads, not a read per row).
    assert calls[1][2] == 100
    assert calls[2][1][0] == calls[1][1][0]
    assert calls[2][2] == 0
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
async def test_store_wide_read_failure_is_unavailable_for_every_slice(isolated_db, monkeypatch):
    async with StateDB() as db:
        for sid in ["a", "b", "c", "d", "e", "f", "g"]:
            await create_schedule(db, sid)
        await db.create_invocation(
            {"id": "inv", "skill": "agent", "status": "failed", "started_at": 100}
        )
        await create_run(db, "g", "run", invocation_id="inv")

    reads = 0

    async def fetch_all(db, query, params=None):
        nonlocal reads
        reads += 1
        raise OSError("Database read unavailable")

    async def fetch_one(db, query, params=None):
        nonlocal reads
        reads += 1
        raise OSError("Database read unavailable")

    monkeypatch.setattr(StateDB, "fetch_all", fetch_all)
    monkeypatch.setattr(StateDB, "fetch_one", fetch_one)
    rows = await list_rows()
    assert len(rows) == 7
    # A store-wide failure -- the batch read and the liveness read alike --
    # leaves every slice unavailable, never an available empty slice that would
    # read as "no recent runs".
    assert all(
        row["recent_runs_summary"]
        == {"version": 1, "status": "unavailable", "limit": 25, "has_next": None, "runs": []}
        for row in rows
    )
    # The batch read and one liveness read; the store is unavailable, so the
    # remaining definitions are marked without a read each: two reads, not one
    # per listed definition.
    assert reads == 2


@pytest.mark.asyncio
async def test_consecutive_definition_failures_do_not_hide_later_summaries(
    isolated_db, monkeypatch
):
    # Five failing definitions sit ahead of the healthy ones in list order
    # (updated_at DESC). A failure that belongs to a definition's own rows must
    # not mark the definitions listed after it unavailable.
    async with StateDB() as db:
        ids = ["bad-0", "bad-1", "bad-2", "bad-3", "bad-4", "ok-0", "ok-1"]
        for position, sid in enumerate(ids):
            await create_schedule(db, sid, updated_at=1000 - position)
        await create_run(db, "ok-0", "ok-0-run")
        await create_run(db, "ok-1", "ok-1-run")

    original = StateDB.fetch_all
    reads = 0

    async def fetch_all(db, query, params=None):
        nonlocal reads
        reads += 1
        if params and "bad" in str(params[0]):
            raise OSError("Database read unavailable for one definition")
        return await original(db, query, params)

    monkeypatch.setattr(StateDB, "fetch_all", fetch_all)
    listed = await list_rows()
    rows = {row["id"]: row["recent_runs_summary"] for row in listed}
    assert [row["id"] for row in listed] == ids
    # Every failing definition keeps its own unavailable attribution...
    for sid in ids[:5]:
        assert rows[sid]["status"] == "unavailable"
        assert rows[sid]["has_next"] is None
    # ...and the healthy definitions behind them are still read and served.
    for sid in ids[5:]:
        assert rows[sid]["status"] == "available"
        assert [run["id"] for run in rows[sid]["runs"]] == [f"{sid}-run"]
    # One batch read plus one read per definition: the run of failures does not
    # stop the reads that follow it.
    assert reads == 1 + len(ids)


@pytest.mark.asyncio
async def test_a_definition_specific_read_failure_leaves_other_summaries_available(
    isolated_db, monkeypatch
):
    async with StateDB() as db:
        for sid in ["bad", "empty", "good"]:
            await create_schedule(db, sid)
        await create_run(db, "bad", "bad-run")
        await create_run(db, "good", "good-run")

    original = StateDB.fetch_all

    async def fetch_all(db, query, params=None):
        # Fail only reads that name the "bad" definition, standing in for a
        # shared read that one definition's rows make fail. Every retry that
        # does not carry "bad" must still succeed.
        if params and "bad" in str(params[0]):
            raise OSError("Database read unavailable for one definition")
        return await original(db, query, params)

    monkeypatch.setattr(StateDB, "fetch_all", fetch_all)
    rows = {row["id"]: row["recent_runs_summary"] for row in await list_rows()}
    assert rows["bad"]["status"] == "unavailable"
    assert rows["bad"]["has_next"] is None
    assert rows["empty"]["status"] == "available"
    assert rows["empty"]["runs"] == []
    assert rows["good"]["status"] == "available"
    assert [run["id"] for run in rows["good"]["runs"]] == ["good-run"]


@pytest.mark.asyncio
async def test_a_statement_that_fails_for_every_definition_logs_once_per_page(
    isolated_db, monkeypatch, caplog
):
    """A shared read that fails for every definition while the store answers is
    retried per definition, and the retries are reported on one line per page
    rather than one traceback per definition per refresh."""
    ids = [f"def-{index}" for index in range(6)]
    async with StateDB() as db:
        for sid in ids:
            await create_schedule(db, sid)
            await create_run(db, sid, f"{sid}-run")

    reads = 0
    liveness = 0

    async def fetch_all(db, query, params=None):
        nonlocal reads
        reads += 1
        # Every read of the summary statement fails, whatever definitions it
        # names: a statement-level failure, not one definition's rows.
        raise OSError("Statement-level read failure")

    original_fetch_one = StateDB.fetch_one

    async def fetch_one(db, query, params=None):
        nonlocal liveness
        liveness += 1
        return await original_fetch_one(db, query, params)

    monkeypatch.setattr(StateDB, "fetch_all", fetch_all)
    monkeypatch.setattr(StateDB, "fetch_one", fetch_one)
    with caplog.at_level("ERROR", logger=schedules._log.name):
        rows = {row["id"]: row["recent_runs_summary"] for row in await list_rows()}

    # The liveness read answers, so every definition is retried and attributed
    # on its own: one batch read, one liveness read, one batch read per definition.
    assert reads == 1 + len(ids)
    assert liveness == 1
    assert all(rows[sid]["status"] == "unavailable" for sid in ids)
    naming_the_definitions = [
        record.getMessage()
        for record in caplog.records
        if any(sid in record.getMessage() for sid in ids)
    ]
    assert len(naming_the_definitions) == 1, naming_the_definitions
    assert all(sid in naming_the_definitions[0] for sid in ids)


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
