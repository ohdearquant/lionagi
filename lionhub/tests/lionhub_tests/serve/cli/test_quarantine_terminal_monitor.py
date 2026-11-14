"""Resolved quarantines retain their history without appearing unreadable."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from lionagi.state.db import StateDB
from lionhub.serve.cli import kill as kill_cli
from lionhub.serve.cli import monitor

from .quarantine_helpers import (
    GENERATION,
    fetch_row,
    isolated_store,  # noqa: F401
    seed,
    sweep,
)


async def _resolve_quarantine(kind, monkeypatch):
    entity_id = await seed(kind)
    for _ in range(3):
        await sweep(monkeypatch, ["unverifiable"])
    monkeypatch.setattr(kill_cli, "check_pid_identity", lambda *_args, **_kwargs: "unverifiable")
    assert await kill_cli._resolve_quarantine(entity_id, target="failed", reason="reviewed") == 0
    row = await fetch_row(kind, entity_id)
    assert row["status"] == "failed"
    assert row["node_metadata"]["unverifiable_quarantined"] is True
    return entity_id


async def _add_backing_play(entity_id):
    async with StateDB() as db:
        await db.create_show({"id": "show", "topic": "test", "show_dir": "/tmp/test"})
        await db.create_play(
            {
                "id": "play",
                "show_id": "show",
                "name": "play",
                "status": "running",
                "session_id": entity_id,
                "started_at": time.time(),
            }
        )


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("surface", ["detail", "since"])
async def test_resolved_marker_is_retained_without_unreadable_monitor_warning(
    kind, surface, monkeypatch
):
    monkeypatch.setattr(monitor, "_subagent_status", AsyncMock(return_value={}))
    entity_id = await _resolve_quarantine(kind, monkeypatch)
    since = time.time() - 3600

    detail = await monitor._run_detail(entity_id)
    table = await monitor._run_table(since=since, entity_type=kind, project=None)
    async with StateDB() as db:
        entities = await monitor._gather_entities(db, since=since, entity_type=kind, project=None)
    selected = [
        row for entity_kind, row in entities if entity_kind == kind and row["id"] == entity_id
    ]

    assert len(selected) == 1
    assert (
        monitor._parse_json_field(selected[0]["node_metadata"])["unverifiable_quarantined"] is True
    )
    assert entity_id[:16] in table
    assert entity_id in detail
    assert "quarantine record unreadable" not in (detail if surface == "detail" else table)
    assert "identity unverified, quarantined" not in table


async def test_resolved_play_backed_session_does_not_force_an_extra_row(monkeypatch):
    monkeypatch.setattr(monitor, "_subagent_status", AsyncMock(return_value={}))
    entity_id = await _resolve_quarantine("session", monkeypatch)
    await _add_backing_play(entity_id)
    since = time.time() - 3600
    async with StateDB() as db:
        entities = await monitor._gather_entities(db, since=since, entity_type=None, project=None)
    assert any(kind == "play" and row["id"] == "play" for kind, row in entities)
    assert not any(kind == "session" and row["id"] == entity_id for kind, row in entities)
    table = await monitor._run_table(since=since, entity_type=None, project=None)
    assert "quarantine record unreadable" not in table


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_malformed_running_marker_still_warns_and_is_visible(kind, monkeypatch):
    monkeypatch.setattr(monitor, "_subagent_status", AsyncMock(return_value={}))
    generation = GENERATION if kind == "session" else None
    metadata = {
        "pid": 54321,
        "pid_create_time": 100.0,
        "unverifiable_identity": json.dumps([54321, 100.0, generation], separators=(",", ":")),
        "unverifiable_count": 1,
        "unverifiable_epoch": 0,
        "unverifiable_quarantined": True,
    }
    entity_id = await seed(kind, metadata=metadata)
    if kind == "session":
        await _add_backing_play(entity_id)
    async with StateDB() as db:
        entities = await monitor._gather_entities(db, since=None, entity_type=None, project=None)
    assert any(entity_kind == kind and row["id"] == entity_id for entity_kind, row in entities)
    table = await monitor._run_table(since=None, entity_type=None, project=None)
    assert f"quarantine record unreadable: {kind} {entity_id[:16]}" in table
