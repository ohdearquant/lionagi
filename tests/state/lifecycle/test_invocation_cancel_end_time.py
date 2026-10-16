# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Invocation cancellation fills missing end times without rewriting recorded ones."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import ActorRecord, OverrideRecord, ReasonRecord, TransitionCommand
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService
from lionagi.state.reasons import RunReasons

pytestmark = pytest.mark.asyncio


async def _invocation(db, *, ended_at=200.0):
    await db.create_invocation(
        {"id": "run", "skill": "example", "status": "running", "started_at": 100.0}
    )
    await db.update_invocation("run", ended_at=ended_at)


def _command(*, status="cancelled", patch=None, override=None):
    return TransitionCommand(
        entity_type="invocation",
        entity_id="run",
        to_status=status,
        reason=ReasonRecord(code=RunReasons.CANCELLED_MANUAL_KILL),
        actor=ActorRecord(type="admin", id="user"),
        expected_statuses=frozenset({"running"}),
        patch={"ended_at": 300.0} if patch is None else patch,
        override=override,
    )


async def test_invocation_cancel_preserves_end_time_written_after_transition_read(monkeypatch):
    async with StateDB(":memory:") as db:
        await _invocation(db, ended_at=None)
        original_write = SQLAlchemyLifecycleService._write

        async def write_after_end_time(self, conn, table, command, **kwargs):
            await conn.execute(
                text("UPDATE invocations SET ended_at = 200.0 WHERE id = :id"),
                {"id": command.entity_id},
            )
            return await original_write(self, conn, table, command, **kwargs)

        monkeypatch.setattr(SQLAlchemyLifecycleService, "_write", write_after_end_time)
        outcome = await SQLAlchemyLifecycleService(db).transition(_command())
        row = await db.get_invocation("run")
        assert outcome.result == "applied"
        assert row["status"] == "cancelled"
        assert row["ended_at"] == 200.0
        assert row["status_reason_code"] == RunReasons.CANCELLED_MANUAL_KILL
        assert await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? "
            "AND previous_status IS NOT NULL",
            ("run",),
        ) == [{"status": "cancelled"}]


@pytest.mark.parametrize(
    "status", ["running", "completed", "completed_empty", "failed", "timed_out", "aborted"]
)
async def test_other_invocation_statuses_keep_explicit_end_time_write(status):
    async with StateDB(":memory:") as db:
        await _invocation(db)
        outcome = await SQLAlchemyLifecycleService(db).transition(_command(status=status))
        assert outcome.result == "applied"
        row = await db.get_invocation("run")
        assert row["status"] == status
        assert row["ended_at"] == 300.0


@pytest.mark.parametrize("ended_at", [0.0, 200.0])
async def test_invocation_cancel_explicit_override_can_replace_end_time(ended_at):
    async with StateDB(":memory:") as db:
        await _invocation(db, ended_at=ended_at)
        outcome = await SQLAlchemyLifecycleService(db).transition(
            _command(override=OverrideRecord(actor="operator", justification="repair end time"))
        )
        assert outcome.result == "applied"
        assert (await db.get_invocation("run"))["ended_at"] == 300.0


@pytest.mark.parametrize("ended_at", [None, 200.0])
async def test_invocation_cancel_without_end_time_patch_keeps_stored_value(ended_at):
    async with StateDB(":memory:") as db:
        await _invocation(db, ended_at=ended_at)
        outcome = await SQLAlchemyLifecycleService(db).transition(_command(patch={}))
        assert outcome.result == "applied"
        assert (await db.get_invocation("run"))["ended_at"] == ended_at


@pytest.mark.parametrize("ended_at", [0.0, 200.0])
async def test_invocation_cancel_null_patch_keeps_recorded_end_time(ended_at):
    async with StateDB(":memory:") as db:
        await _invocation(db, ended_at=ended_at)
        outcome = await SQLAlchemyLifecycleService(db).transition(
            _command(patch={"ended_at": None})
        )
        assert outcome.result == "applied"
        assert (await db.get_invocation("run"))["ended_at"] == ended_at


@pytest.mark.parametrize("entity_type", ["session", "schedule_run", "play"])
async def test_other_entities_keep_explicit_end_time_write(entity_type, tmp_path):
    async with StateDB(":memory:") as db:
        status = "cancelled"
        reason_code = RunReasons.CANCELLED_MANUAL_KILL
        if entity_type == "session":
            await db.create_progression("progression")
            await db.create_session(
                {"id": "run", "progression_id": "progression", "status": "running"}
            )
            table = "sessions"
        elif entity_type == "schedule_run":
            await db.create_schedule(
                {
                    "id": "schedule",
                    "name": "example",
                    "trigger_type": "interval",
                    "interval_sec": 60,
                    "action_kind": "agent",
                }
            )
            await db.create_schedule_run(
                {
                    "id": "run",
                    "schedule_id": "schedule",
                    "trigger_context": {},
                    "action_kind": "agent",
                    "action_args": [],
                    "status": "running",
                    "fired_at": 100.0,
                }
            )
            table = "schedule_runs"
        else:
            await db.create_show(
                {
                    "id": "show",
                    "topic": "example",
                    "show_dir": str(tmp_path),
                    "status": "active",
                }
            )
            await db.create_play(
                {"id": "run", "show_id": "show", "name": "example", "status": "running"}
            )
            table = "plays"
            status = "blocked"
            reason_code = "play.blocked.dep_failed"
        await db.execute(f"UPDATE {table} SET ended_at = 200.0 WHERE id = ?", ("run",))

        outcome = await SQLAlchemyLifecycleService(db).transition(
            TransitionCommand(
                entity_type=entity_type,
                entity_id="run",
                to_status=status,
                reason=ReasonRecord(code=reason_code),
                actor=ActorRecord(type="admin", id="user"),
                patch={"ended_at": 300.0},
            )
        )
        assert outcome.result == "applied"
        row = await db.fetch_one(f"SELECT status, ended_at FROM {table} WHERE id = ?", ("run",))
        assert row == {"status": status, "ended_at": 300.0}
