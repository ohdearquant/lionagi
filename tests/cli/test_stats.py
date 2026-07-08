# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li stats runs` — read-only aggregate reporting over state.db."""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.stats import (
    GROUP_BY_COLUMNS,
    _format_stats_table,
    _rows_for_json,
    _run_stats_runs,
    _validate_group_by,
    run_stats,
)
from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp file DB: patches DEFAULT_DB_PATH so `li stats` uses a throw-away file."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed_session(
    db: StateDB,
    *,
    status: str | None = "completed",
    project: str | None = "lionagi",
    invocation_kind: str | None = "agent",
    agent_name: str | None = None,
    model: str | None = "claude-sonnet",
    started_at: float | None = None,
    updated_at: float | None = None,
) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    now = time.time()
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": status,
            "project": project,
            "invocation_kind": invocation_kind,
            "agent_name": agent_name,
            "model": model,
            "started_at": started_at if started_at is not None else now,
            "updated_at": updated_at if updated_at is not None else now,
        }
    )
    return sid


def _stats_args(*, since: str = "7d", group_by: str = "project,kind", as_json: bool = False):
    return argparse.Namespace(
        stats_command="runs",
        since=since,
        group_by=group_by,
        json=as_json,
    )


# ── _validate_group_by ───────────────────────────────────────────────────────


def test_validate_group_by_accepts_all_known_keys():
    assert _validate_group_by("project,kind,agent,model,status") == [
        "project",
        "kind",
        "agent",
        "model",
        "status",
    ]


def test_validate_group_by_strips_whitespace():
    assert _validate_group_by(" project , kind ") == ["project", "kind"]


def test_validate_group_by_rejects_unknown_key():
    with pytest.raises(ValueError, match="Unknown --group-by key"):
        _validate_group_by("project,bogus")


def test_validate_group_by_error_lists_valid_keys():
    with pytest.raises(ValueError) as exc_info:
        _validate_group_by("bogus")
    msg = str(exc_info.value)
    for key in GROUP_BY_COLUMNS:
        assert key in msg


def test_validate_group_by_rejects_empty():
    with pytest.raises(ValueError, match="at least one key"):
        _validate_group_by("")


# ── aggregation + grouping ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregates_counts_and_grouping(temp_db_path: Path):
    now = time.time()
    async with StateDB() as db:
        await _seed_session(db, status="completed", project="lionagi", invocation_kind="agent")
        await _seed_session(db, status="completed", project="lionagi", invocation_kind="agent")
        await _seed_session(db, status="failed", project="lionagi", invocation_kind="agent")
        await _seed_session(db, status="running", project="lionagi", invocation_kind="play")
        await _seed_session(db, status="completed", project="khive", invocation_kind="agent")

        rows = await _run_stats_runs(since=now - 3600, group_by=["project", "kind"])

    by_group = {(r["project"], r["kind"]): r for r in rows}

    lionagi_agent = by_group[("lionagi", "agent")]
    assert lionagi_agent["run_count"] == 3
    assert lionagi_agent["completed"] == 2
    assert lionagi_agent["failed"] == 1

    lionagi_play = by_group[("lionagi", "play")]
    assert lionagi_play["run_count"] == 1
    assert lionagi_play["completed"] == 0
    assert lionagi_play["failed"] == 0

    khive_agent = by_group[("khive", "agent")]
    assert khive_agent["run_count"] == 1
    assert khive_agent["completed"] == 1
    assert khive_agent["failed"] == 0


@pytest.mark.asyncio
async def test_group_by_agent_and_model(temp_db_path: Path):
    now = time.time()
    async with StateDB() as db:
        await _seed_session(db, agent_name="reviewer", model="gpt-5.5")
        await _seed_session(db, agent_name="reviewer", model="gpt-5.5")
        await _seed_session(db, agent_name="implementer", model="claude-sonnet")

        rows = await _run_stats_runs(since=now - 3600, group_by=["agent", "model"])

    by_group = {(r["agent"], r["model"]): r for r in rows}
    assert by_group[("reviewer", "gpt-5.5")]["run_count"] == 2
    assert by_group[("implementer", "claude-sonnet")]["run_count"] == 1


@pytest.mark.asyncio
async def test_null_group_value_renders_none_in_table(temp_db_path: Path):
    now = time.time()
    async with StateDB() as db:
        await _seed_session(db, project=None, invocation_kind=None)
        rows = await _run_stats_runs(since=now - 3600, group_by=["project", "kind"])

    table = _format_stats_table(rows, ["project", "kind"])
    assert "(none)" in table


@pytest.mark.asyncio
async def test_null_group_value_renders_null_in_json(temp_db_path: Path):
    now = time.time()
    async with StateDB() as db:
        await _seed_session(db, project=None, invocation_kind=None)
        rows = await _run_stats_runs(since=now - 3600, group_by=["project", "kind"])

    payload = _rows_for_json(rows, ["project", "kind"])
    assert payload[0]["project"] is None
    assert payload[0]["kind"] is None


# ── --since boundary ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_since_filter_boundary_is_inclusive(temp_db_path: Path):
    cutoff = time.time() - 1000
    async with StateDB() as db:
        await _seed_session(db, updated_at=cutoff)  # exactly at cutoff -> included
        await _seed_session(db, updated_at=cutoff - 1)  # just before -> excluded
        await _seed_session(db, updated_at=cutoff + 1)  # just after -> included

        rows = await _run_stats_runs(since=cutoff, group_by=["project"])

    total = sum(r["run_count"] for r in rows)
    assert total == 2


@pytest.mark.asyncio
async def test_since_filter_excludes_stale_runs(temp_db_path: Path):
    now = time.time()
    async with StateDB() as db:
        await _seed_session(db, updated_at=now - 30 * 86400)  # 30 days old
        await _seed_session(db, updated_at=now)

        rows = await _run_stats_runs(since=now - 7 * 86400, group_by=["project"])

    total = sum(r["run_count"] for r in rows)
    assert total == 1


# ── JSON field names (downstream ADR hardcodes these) ────────────────────────


@pytest.mark.asyncio
async def test_json_field_names_are_stable(temp_db_path: Path):
    now = time.time()
    async with StateDB() as db:
        await _seed_session(db)
        rows = await _run_stats_runs(since=now - 3600, group_by=["project", "kind"])

    payload = _rows_for_json(rows, ["project", "kind"])
    assert set(payload[0]) == {
        "project",
        "kind",
        "run_count",
        "completed",
        "failed",
        "first_at",
        "last_at",
    }
    assert isinstance(payload[0]["first_at"], str)
    assert isinstance(payload[0]["last_at"], str)


# ── empty-DB behavior ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_db_no_file_returns_no_rows(temp_db_path: Path):
    # temp_db_path patches DEFAULT_DB_PATH but never creates the file.
    rows = await _run_stats_runs(since=time.time() - 3600, group_by=["project"])
    assert rows == []


def test_format_stats_table_empty():
    assert _format_stats_table([], ["project"]) == "(no runs in this window)"


def test_rows_for_json_empty():
    assert _rows_for_json([], ["project"]) == []


# ── run_stats() CLI dispatch ──────────────────────────────────────────────────


def test_run_stats_unknown_group_by_key_exits_nonzero(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        rc = run_stats(_stats_args(group_by="project,bogus"))
    assert rc != 0
    assert "bogus" in caplog.text
    for key in GROUP_BY_COLUMNS:
        assert key in caplog.text


def test_run_stats_invalid_since_exits_nonzero(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        rc = run_stats(_stats_args(since="not-a-window"))
    assert rc != 0


def test_run_stats_json_mode_prints_json_array(temp_db_path: Path, capsys):
    async def _seed():
        async with StateDB() as db:
            await _seed_session(db, project="lionagi", invocation_kind="agent")

    import asyncio

    asyncio.run(_seed())

    rc = run_stats(_stats_args(as_json=True))
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, list)
    assert payload[0]["project"] == "lionagi"
    assert payload[0]["kind"] == "agent"
    assert payload[0]["run_count"] == 1


def test_run_stats_table_mode_prints_table(temp_db_path: Path, capsys):
    async def _seed():
        async with StateDB() as db:
            await _seed_session(db, project="lionagi", invocation_kind="agent")

    import asyncio

    asyncio.run(_seed())

    rc = run_stats(_stats_args())
    assert rc == 0
    captured = capsys.readouterr()
    assert "PROJECT" in captured.out
    assert "RUN_COUNT" in captured.out
    assert "lionagi" in captured.out


def test_run_stats_empty_db_table_mode(temp_db_path: Path, capsys):
    rc = run_stats(_stats_args())
    assert rc == 0
    captured = capsys.readouterr()
    assert "no runs" in captured.out
