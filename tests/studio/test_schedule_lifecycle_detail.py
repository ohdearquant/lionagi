# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Definition history is bounded detail data and survives removal of the definition."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException

from lionagi.state.db import StateDB
from lionagi.studio.services import schedules


@pytest.mark.parametrize("history_kind", ["recorded", "legacy", "unavailable"])
async def test_detail_projects_latest_definition_event_separately_from_occurrences(
    tmp_path, monkeypatch, history_kind
):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_schedule(
            {
                "id": "definition",
                "name": "Definition",
                "enabled": 1,
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
            }
        )
        await db.update_schedule("definition", enabled=0)
    if history_kind != "recorded":
        with sqlite3.connect(path) as conn:
            if history_kind == "legacy":
                conn.execute("DELETE FROM status_transitions WHERE entity_type='schedule'")
            else:
                conn.execute("DROP TABLE status_transitions")
    result = await schedules.get_schedule_route("definition")
    lifecycle = result["lifecycle"]
    assert lifecycle["available"] == (history_kind != "unavailable")
    assert result["recent_runs"] == []
    assert "lifecycle" not in schedules._schedule_summary(result)
    if history_kind == "recorded":
        assert lifecycle["latest"]["status"] == "disabled"
        assert lifecycle["latest"]["reason_code"] == "schedule_definition.disabled.unspecified"
        assert lifecycle["latest"]["actor"] is None
    else:
        assert lifecycle["latest"] is None
    if history_kind == "unavailable":
        with sqlite3.connect(path) as conn:
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='status_transitions'"
                ).fetchall()
                == []
            )


async def test_deleted_detail_is_404_but_definition_history_remains(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_schedule(
            {
                "id": "definition",
                "name": "Definition",
                "enabled": 1,
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
            }
        )
        await db.delete_schedule("definition")
    with pytest.raises(HTTPException) as caught:
        await schedules.get_schedule_route("definition")
    assert caught.value.status_code == 404
    async with StateDB(path, readonly=True) as db:
        result = await db.get_schedule_history("definition", limit=1)
        assert result["available"]
        assert result["events"][0]["status"] == "deleted"


async def test_disable_persists_the_reason_in_definition_history(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_schedule(
            {
                "id": "definition",
                "name": "Definition",
                "enabled": 1,
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
            }
        )

    assert await schedules.disable_schedule("definition", reason="planned maintenance") is True

    result = await schedules.get_schedule_route("definition")
    latest = result["lifecycle"]["latest"]
    assert latest["status"] == "disabled"
    assert latest["reason_code"] == "schedule_definition.disabled.manual"
    assert latest["reason_summary"] == "planned maintenance"


async def test_disable_without_a_reason_records_an_unspecified_disable(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_schedule(
            {
                "id": "definition",
                "name": "Definition",
                "enabled": 1,
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
            }
        )

    assert await schedules.disable_schedule("definition") is True

    latest = (await schedules.get_schedule_route("definition"))["lifecycle"]["latest"]
    assert latest["status"] == "disabled"
    assert latest["reason_code"] == "schedule_definition.disabled.unspecified"


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
async def test_disable_refuses_an_unusable_reason_before_writing(tmp_path, monkeypatch, reason):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_schedule(
            {
                "id": "definition",
                "name": "Definition",
                "enabled": 1,
                "trigger_type": "interval",
                "interval_sec": 60,
                "action_kind": "agent",
            }
        )

    with pytest.raises(ValueError, match="reason"):
        await schedules.disable_schedule("definition", reason=reason)

    async with StateDB(path) as db:
        assert (await db.get_schedule("definition"))["enabled"]
