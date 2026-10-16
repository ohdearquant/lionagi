# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Late terminal writers preserve the first recorded outcome and end time."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.cli.invoke import _end_invocation
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.studio.services import launches


@pytest.mark.parametrize("writer", ["invoke", "launch_exit", "launch_cancel"])
@pytest.mark.parametrize("winner", ["completed", "cancelled"])
async def test_terminal_writers_preserve_concurrent_terminal_outcome(
    tmp_path, monkeypatch, writer, winner
):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_invocation(
            {
                "id": "run",
                "skill": "example",
                "started_at": 100.0,
                "status": "running",
                "node_metadata": {"original": True},
            }
        )
    original_update = StateDB.update_status
    intercepted = False
    winner_reason = (
        RunReasons.COMPLETED_OK if winner == "completed" else RunReasons.CANCELLED_MANUAL_KILL
    )

    async def racing_update(db, entity_type, entity_id, **kwargs):
        nonlocal intercepted
        assert not intercepted
        intercepted = True
        await original_update(
            db,
            entity_type,
            entity_id,
            new_status=winner,
            reason_code=winner_reason,
            reason_summary="First outcome",
            extra_fields={"ended_at": 200.0},
        )
        return await original_update(db, entity_type, entity_id, **kwargs)

    monkeypatch.setattr(StateDB, "update_status", racing_update)

    async def spawn(*args, **kwargs):
        if writer == "launch_cancel":
            raise asyncio.CancelledError
        return (0, "")

    monkeypatch.setattr("lionagi.studio.scheduler.subprocess.spawn_and_wait", spawn)
    if writer == "invoke":
        result = await _end_invocation("run", status=winner, metadata={"late": True})
        assert result["status"] == winner
    elif writer == "launch_cancel":
        with pytest.raises(asyncio.CancelledError):
            await launches._spawn_detached(["unused"], "run", tmp_path=None)
    else:
        await launches._spawn_detached(["unused"], "run", tmp_path=None)
    assert intercepted
    async with StateDB(path) as db:
        row = await db.get_invocation("run")
        assert row["status"] == winner
        assert row["ended_at"] == 200.0
        assert row["status_reason_code"] == winner_reason
        assert row["status_reason_summary"] == "First outcome"
        assert row["node_metadata"] == {"original": True}
        assert await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? AND previous_status IS NOT NULL",
            ("run",),
        ) == [{"status": winner}]


@pytest.mark.parametrize("writer", ["invoke", "launch_exit", "launch_cancel"])
async def test_terminal_writers_finish_running_invocation(tmp_path, monkeypatch, writer):
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    async with StateDB(path) as db:
        await db.create_invocation(
            {"id": "run", "skill": "example", "started_at": 100.0, "status": "running"}
        )

    async def spawn(*args, **kwargs):
        if writer == "launch_cancel":
            raise asyncio.CancelledError
        return (0, "")

    monkeypatch.setattr("lionagi.studio.scheduler.subprocess.spawn_and_wait", spawn)
    if writer == "invoke":
        await _end_invocation("run", status="completed", metadata={"finished": True})
    elif writer == "launch_cancel":
        with pytest.raises(asyncio.CancelledError):
            await launches._spawn_detached(["unused"], "run", tmp_path=None)
    else:
        await launches._spawn_detached(["unused"], "run", tmp_path=None)
    async with StateDB(path) as db:
        row = await db.get_invocation("run")
        assert row["status"] == ("cancelled" if writer == "launch_cancel" else "completed")
        assert row["ended_at"] >= row["started_at"]
