# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Recorded-use aggregates follow the filters and snapshot, not the recent page."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from lionagi.state.db import StateDB
from lionagi.studio.services import invocations


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


async def insert(
    db, iid, *, skill="sample", plugin="package", status="completed", started_at=100, updated_at=100
):
    await db.create_invocation(
        {
            "id": iid,
            "skill": skill,
            "plugin": plugin,
            "status": status,
            "started_at": started_at,
            "updated_at": updated_at,
        }
    )


async def query(*, skill=None, plugin=None, status=None, limit=5, offset=0):
    # These tests exercise the usage aggregates, so they request them the way
    # the library usage panel does.
    return await invocations.list_invocations_route(
        skill=skill,
        plugin=plugin,
        status=status,
        limit=limit,
        offset=offset,
        include_usage=True,
    )


async def test_latest_start_and_totals_cover_more_than_200_records_beyond_recent_page(db_path):
    async with StateDB(db_path) as db:
        for index in range(240):
            # The most recently started row is the least recently updated one.
            await insert(
                db,
                f"run-{index}",
                started_at=100 + index,
                updated_at=1000 - index,
                status="completed" if index < 180 else "failed",
            )

    first = await query(skill="sample")
    assert first["total"] == 240
    assert first["completed_total"] == 180
    assert first["finished_total"] == 240
    assert first["latest_started_at"] == 339
    assert [row["started_at"] for row in first["invocations"]] == [100, 101, 102, 103, 104]
    assert first["has_next"] is True
    past_end = await query(skill="sample", offset=300)
    assert past_end["invocations"] == []
    assert past_end["total"] == 240
    assert past_end["latest_started_at"] == 339


@pytest.mark.parametrize(
    "filters,total,completed,latest",
    [
        ({}, 4, 3, 400),
        ({"skill": "sample"}, 3, 2, 300),
        ({"plugin": "package"}, 3, 2, 400),
        ({"skill": "sample", "plugin": "package"}, 2, 1, 200),
        ({"skill": "sample", "plugin": "package", "status": "failed"}, 1, 1, 200),
        ({"skill": "sample", "plugin": "package", "status": "completed"}, 1, 1, 100),
        ({"skill": "absent"}, 0, 0, None),
        ({"skill": "sample", "plugin": "package", "status": "running"}, 0, 1, None),
    ],
)
async def test_aggregate_has_same_filters_as_total_and_completed_count_keeps_its_semantics(
    db_path, filters, total, completed, latest
):
    async with StateDB(db_path) as db:
        await insert(db, "a", started_at=100)
        await insert(db, "b", started_at=200, status="failed")
        await insert(db, "c", started_at=300, plugin="different")
        await insert(db, "d", started_at=400, skill="different")
    result = await query(**filters)
    assert result["total"] == total
    assert result["completed_total"] == completed
    assert result["latest_started_at"] == latest


@pytest.mark.parametrize("exists", [False, True])
async def test_absent_or_empty_store_reports_null_latest(db_path, exists):
    if exists:
        async with StateDB(db_path):
            pass
    result = await query()
    assert result["invocations"] == []
    assert result["total"] == result["completed_total"] == result["finished_total"] == 0
    assert result["latest_started_at"] is None
    assert db_path.exists() is exists


async def test_epoch_zero_is_a_recorded_start_not_missing(db_path):
    async with StateDB(db_path) as db:
        await insert(db, "epoch", started_at=0)
    assert (await query())["latest_started_at"] == 0


async def test_latest_count_and_page_share_snapshot_during_concurrent_write(db_path, monkeypatch):
    async with StateDB(db_path) as db:
        await insert(db, "initial", started_at=100)
    async with StateDB(db_path) as writer:
        original = StateDB.list_invocations
        inserted = False

        async def interleave(db, **kwargs):
            nonlocal inserted
            rows = await original(db, **kwargs)
            if not inserted:
                inserted = True
                await insert(writer, "concurrent", started_at=500, updated_at=500)
            return rows

        monkeypatch.setattr(StateDB, "list_invocations", interleave)
        result = await query()
    assert inserted
    assert [row["id"] for row in result["invocations"]] == ["initial"]
    assert result["total"] == result["completed_total"] == result["finished_total"] == 1
    assert result["latest_started_at"] == 100
    assert (await query())["latest_started_at"] == 500


async def test_count_and_latest_share_one_bounded_aggregate_query(db_path, monkeypatch):
    async with StateDB(db_path) as db:
        await insert(db, "run")
    statements = []
    original = StateDB.open

    async def capture(db):
        await original(db)
        event.listen(
            db._engine.sync_engine,
            "before_cursor_execute",
            lambda conn, cursor, statement, parameters, context, executemany: statements.append(
                statement
            ),
        )

    monkeypatch.setattr(StateDB, "open", capture)
    assert (await query(skill="sample"))["latest_started_at"] == 100
    aggregates = [sql for sql in statements if "MAX(started_at)" in sql]
    assert len(aggregates) == 1
    assert "COUNT(*)" in aggregates[0]
    assert "LIMIT" not in aggregates[0]
    # Page, batched child sessions, count/latest, completed/finished counts.
    assert len([sql for sql in statements if sql.lstrip().startswith("SELECT")]) == 4


@pytest.mark.parametrize("status_filter", [None, "running", "failed"])
async def test_finished_count_excludes_running_and_keeps_completed_empty_separate(
    db_path, status_filter
):
    statuses = [
        "completed",
        "completed",
        "completed_empty",
        "failed",
        "timed_out",
        "cancelled",
        "aborted",
        "running",
        "running",
        "running",
    ]
    async with StateDB(db_path) as db:
        for index, status in enumerate(statuses):
            await insert(db, f"mixed-{index}", status=status)
        await insert(db, "other-skill", skill="sample-other")
        await insert(db, "other-plugin", plugin="package-other")
    result = await query(skill="sample", plugin="package", status=status_filter)
    assert result["completed_total"] == 2
    assert result["finished_total"] == 7
    assert result["total"] == (10 if status_filter is None else statuses.count(status_filter))


def _explode(name):
    def _fail(*args, **kwargs):
        raise AssertionError(f"{name} must not run for a polling-shaped request")

    return _fail


async def test_polling_request_skips_the_usage_aggregates(db_path, monkeypatch):
    """The fleet and live-board polls read only the recent rows, so a request
    that does not ask for the usage aggregates must not pay for them."""
    async with StateDB(db_path) as db:
        for index in range(3):
            await insert(db, f"poll-{index}", started_at=100 + index)

    monkeypatch.setattr(invocations, "_invocation_totals", _explode("_invocation_totals"))
    monkeypatch.setattr(
        invocations, "_invocation_completion_totals", _explode("_invocation_completion_totals")
    )

    result = await invocations.list_invocations_route(
        skill=None, plugin=None, status=None, limit=5, offset=0
    )

    assert [row["id"] for row in result["invocations"]] == ["poll-2", "poll-1", "poll-0"]
    # The always-present counts are the pre-existing filtered counts, unchanged.
    assert result["total"] == 3
    assert result["completed_total"] == 3
    # No aggregate was requested, so neither is reported as available.
    assert result["finished_total"] is None
    assert result["latest_started_at"] is None


async def test_usage_request_returns_the_aggregates(db_path, monkeypatch):
    """The library usage panel asks for the aggregates explicitly and still
    receives the count, latest start, and finished totals it renders."""
    async with StateDB(db_path) as db:
        await insert(db, "ok", started_at=100, status="completed")
        await insert(db, "empty", started_at=200, status="completed_empty")
        await insert(db, "bad", started_at=300, status="failed")

    result = await invocations.list_invocations_route(
        skill="sample", plugin="package", status=None, limit=5, offset=0, include_usage=True
    )

    assert result["total"] == 3
    assert result["completed_total"] == 1
    assert result["finished_total"] == 3
    assert result["latest_started_at"] == 300


async def test_latest_start_counts_a_fire_that_never_launched(db_path):
    """``latest_started_at`` is the maximum recorded start over every row, so an
    abandoned fire counts as the last use even though no process ever ran.

    The scheduler writes the invocation row at fire time and then abandons it as
    ``cancelled`` when its occurrence write loses the claim
    (``run.cancelled.stale_auto``, ``started_at`` kept). That reason code is also
    written by the stale sweep, which cancels rows that did launch, and the
    abandon's own distinguishing marks -- its ``status_transitions.actor`` and the
    ``{"kind": "schedule"}`` entry in ``status_evidence_refs`` -- are a JSON blob
    and a history column, not a declared identity key this status-scoped
    aggregate filters on. So the row is counted, pinned here.
    """
    async with StateDB(db_path) as db:
        await insert(db, "ran", started_at=100, status="completed")
        await insert(db, "never-launched", started_at=400)
        await db.update_status(
            "invocation",
            "never-launched",
            new_status="cancelled",
            reason_code="run.cancelled.stale_auto",
            reason_summary="Fire abandoned: another scheduler advanced the schedule first.",
            source="system",
            actor="scheduler_cursor_claim",
            evidence_refs=[{"kind": "schedule", "id": "sched"}],
            expected_statuses={"running"},
            extra_fields={"ended_at": 401},
        )

    assert (await query())["latest_started_at"] == 400
