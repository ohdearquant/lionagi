# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Definition history is bounded detail data and survives removal of the definition."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

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


async def test_schedule_routes_refuse_unsupported_persisted_kinds(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    body = {
        "name": "daily",
        "trigger_type": "interval",
        "interval_sec": 60,
        "action_kind": "agent",
        "action_agent": "research",
    }
    for target in ("research", "unknownengine"):
        with pytest.raises(HTTPException, match="Invalid schedule action_kind 'engine'") as bad:
            await schedules.create_schedule_route(
                schedules.CreateScheduleRequest(
                    **(body | {"action_kind": "engine", "action_agent": target})
                )
            )
        assert bad.value.status_code == 400

    result = await schedules.create_schedule_route(schedules.CreateScheduleRequest(**body))
    with pytest.raises(HTTPException) as duplicate:
        await schedules.create_schedule_route(schedules.CreateScheduleRequest(**body))
    assert duplicate.value.status_code == 409

    with pytest.raises(HTTPException, match="Invalid schedule action_kind 'engine'") as bad:
        await schedules.update_schedule_route(
            result["id"], schedules.UpdateScheduleRequest(action_kind="engine", name="changed")
        )
    assert bad.value.status_code == 400
    async with StateDB(path) as db:
        row = await db.get_schedule(result["id"])
        assert row is not None
        assert (row["name"], row["action_kind"], row["action_agent"]) == (
            "daily",
            "agent",
            "research",
        )


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_schedule_routes_report_an_action_kind_check_as_bad_input(
    tmp_path, monkeypatch, operation
):
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    original = sqlite3.IntegrityError("CHECK constraint failed: ck_schedules_action_kind")

    async def refuse(*_args, **_kwargs):
        raise IntegrityError(None, (), original)

    monkeypatch.setattr(StateDB, "create_schedule", refuse)
    monkeypatch.setattr(StateDB, "update_schedule", refuse)

    async def existing(*_args):
        return {"action_kind": "agent", "trigger_type": "interval", "interval_sec": 60}

    monkeypatch.setattr(StateDB, "get_schedule", existing)
    with pytest.raises(HTTPException, match="Invalid schedule action_kind") as bad:
        if operation == "create":
            await schedules.create_schedule_route(
                schedules.CreateScheduleRequest(
                    name="daily", trigger_type="interval", interval_sec=60, action_kind="agent"
                )
            )
        else:
            await schedules.update_schedule_route(
                "daily", schedules.UpdateScheduleRequest(action_kind="play")
            )
    assert bad.value.status_code == 400


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


@pytest.mark.parametrize("reason", [None, "planned maintenance"])
async def test_a_disable_records_no_provenance_it_did_not_observe(tmp_path, monkeypatch, reason):
    """The request carries a reason at most and nothing about its sender, so a reason
    must not turn the edge into one attributed to an operator."""
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

    assert await schedules.disable_schedule("definition", reason=reason) is True

    async with StateDB(path, readonly=True) as db:
        events = (await db.get_schedule_history("definition"))["events"]
    disabled = next(event for event in events if event["status"] == "disabled")
    assert disabled["source"] == "unavailable"
    assert disabled["actor"] is None
    provenance = disabled["metadata"]["provenance"]
    assert provenance["origin"] == "unavailable"
    assert provenance["assurance"] == "unavailable"


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


@pytest.mark.parametrize("entrypoint", ["service", "route", "no-reason"])
async def test_repeated_disable_cannot_claim_to_record_a_new_reason(
    tmp_path, monkeypatch, entrypoint
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
    await schedules.disable_schedule("definition", reason="original explanation")
    async with StateDB(path, readonly=True) as db:
        before = await db.get_schedule_history("definition")
    if entrypoint == "service":
        with pytest.raises(ValueError, match="already disabled.*reason.*not recorded"):
            await schedules.disable_schedule("definition", reason="later explanation")
    elif entrypoint == "route":
        with pytest.raises(HTTPException) as caught:
            await schedules.disable_schedule_route(
                "definition", schedules.DisableScheduleRequest(reason="later explanation")
            )
        assert caught.value.status_code == 400
        assert "reason was not recorded" in caught.value.detail
    else:
        assert await schedules.disable_schedule("definition") is True
    async with StateDB(path, readonly=True) as db:
        assert (await db.get_schedule("definition"))["enabled"] == 0
        assert await db.get_schedule_history("definition") == before


async def test_concurrent_reasoned_disables_report_only_the_recorded_winner(tmp_path, monkeypatch):
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
    reasons = ("first explanation", "second explanation")
    results = await asyncio.gather(
        *(schedules.disable_schedule("definition", reason=reason) for reason in reasons),
        return_exceptions=True,
    )
    assert results.count(True) == 1
    failure = next(result for result in results if result is not True)
    assert isinstance(failure, ValueError)
    assert "reason was not recorded" in str(failure)
    async with StateDB(path, readonly=True) as db:
        history = (await db.get_schedule_history("definition"))["events"]
    assert len(history) == 2  # Creation plus the one state edge.
    disabled = next(event for event in history if event["status"] == "disabled")
    assert disabled["reason_summary"] == reasons[results.index(True)]


def test_the_request_body_takes_the_reasons_the_service_takes():
    """The body strips before it measures, as the service and the CLI do, so a reason the
    service would record is not refused over HTTP for its surrounding whitespace."""
    padded = "  " + "x" * 500 + "\n"
    assert schedules.DisableScheduleRequest(reason=padded).reason == "x" * 500
    assert schedules.DisableScheduleRequest().reason is None
    for refused in ("", "   ", "x" * 501, 5):
        with pytest.raises(ValidationError):
            schedules.DisableScheduleRequest(reason=refused)


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
