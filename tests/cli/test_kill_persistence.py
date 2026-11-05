# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Cancellation timestamps are recorded atomically without revising terminal history."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import lionagi.cli._kill_service as kill_service
import lionagi.cli.kill as kill_mod
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons


async def _create_invocation(db: StateDB, *, status: str = "running", ended_at=None):
    await db.create_invocation(
        {"id": "invocation", "skill": "example", "status": status, "started_at": 100.0}
    )
    if ended_at is not None:
        await db.execute(
            "UPDATE invocations SET ended_at = ? WHERE id = ?", (ended_at, "invocation")
        )


async def _cancel(db: StateDB, reason_code: str = RunReasons.CANCELLED_MANUAL_KILL):
    await kill_mod._persist_cancel(
        db,
        "invocation",
        "invocation",
        reason_code=reason_code,
        reason_summary="Cancellation observed",
        evidence={"signal": "already_dead"},
    )


@pytest.mark.parametrize(
    "reason_code", [RunReasons.CANCELLED_MANUAL_KILL, RunReasons.CANCELLED_STALE_AUTO]
)
async def test_invocation_cancel_records_observation_time_atomically(
    tmp_path, monkeypatch, reason_code
):
    patched_time = SimpleNamespace(time=lambda: 300.0)
    monkeypatch.setattr(kill_mod, "time", patched_time)
    monkeypatch.setattr(kill_service, "time", patched_time)
    async with StateDB(tmp_path / "state.db") as db:
        await _create_invocation(db)
        # A reader must never observe the cancellation without its end time.
        # This also rejects an implementation that patches the time afterward.
        await db.execute(
            "CREATE TRIGGER cancellation_requires_end BEFORE UPDATE OF status ON invocations "
            "WHEN NEW.status = 'cancelled' AND NEW.ended_at IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'cancellation requires an end time'); END"
        )

        await _cancel(db, reason_code)

        row = await db.get_invocation("invocation")
        assert row["status"] == "cancelled"
        assert row["started_at"] == 100.0
        assert row["ended_at"] == 300.0
        transitions = await db.fetch_all(
            "SELECT status, reason_code FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running'",
            ("invocation",),
        )
        assert transitions == [{"status": "cancelled", "reason_code": reason_code}]


@pytest.mark.parametrize("status", ["completed", "cancelled"])
@pytest.mark.parametrize("ended_at", [None, 200.0])
async def test_invocation_cancel_pre_check_skips_non_running_rows(
    tmp_path, monkeypatch, status, ended_at
):
    """The pre-check refuses a row that is already terminal, so the guarded
    write below it is never reached and the row is untouched."""
    patched_time = SimpleNamespace(time=lambda: 300.0)
    monkeypatch.setattr(kill_mod, "time", patched_time)
    monkeypatch.setattr(kill_service, "time", patched_time)
    async with StateDB(tmp_path / "state.db") as db:
        await _create_invocation(db, status=status, ended_at=ended_at)
        before = await db.get_invocation("invocation")
        await _cancel(db)
        assert await db.get_invocation("invocation") == before


@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_invocation_cancel_preserves_concurrent_terminal_transition(
    tmp_path, monkeypatch, status
):
    patched_time = SimpleNamespace(time=lambda: 300.0)
    monkeypatch.setattr(kill_mod, "time", patched_time)
    monkeypatch.setattr(kill_service, "time", patched_time)
    async with StateDB(tmp_path / "state.db") as db:
        await _create_invocation(db)
        original_update = db.update_status
        winner_reason = (
            RunReasons.COMPLETED_OK if status == "completed" else RunReasons.CANCELLED_SYSTEM
        )

        async def race_update(entity_type, entity_id, **kwargs):
            await original_update(
                entity_type,
                entity_id,
                new_status=status,
                reason_code=winner_reason,
                extra_fields={"ended_at": 200.0},
            )
            return await original_update(entity_type, entity_id, **kwargs)

        monkeypatch.setattr(db, "update_status", race_update)
        await _cancel(db)

        row = await db.get_invocation("invocation")
        assert row["status"] == status
        assert row["ended_at"] == 200.0
        assert row["status_reason_code"] == winner_reason
        transitions = await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? "
            "AND previous_status IS NOT NULL",
            ("invocation",),
        )
        assert transitions == [{"status": status}]


@pytest.mark.parametrize("ended_at", [0.0, 200.0])
async def test_invocation_cancel_preserves_existing_running_end_time(
    tmp_path, monkeypatch, ended_at
):
    """Regression control for the end-time rule, not for the running guard: a
    plain write would overwrite the stored value with the observation time."""
    patched_time = SimpleNamespace(time=lambda: 300.0)
    monkeypatch.setattr(kill_mod, "time", patched_time)
    monkeypatch.setattr(kill_service, "time", patched_time)
    async with StateDB(tmp_path / "state.db") as db:
        await _create_invocation(db, ended_at=ended_at)
        await _cancel(db)
        row = await db.get_invocation("invocation")
        assert row["status"] == "cancelled"
        assert row["ended_at"] == ended_at


async def test_invocation_cancel_preserves_concurrent_running_end_time(tmp_path, monkeypatch):
    """Regression control for the end-time rule against a write that lands
    before the transition's own read; the running guard does not reach this
    case, because the row is still ``running`` when the read happens."""
    patched_time = SimpleNamespace(time=lambda: 300.0)
    monkeypatch.setattr(kill_mod, "time", patched_time)
    monkeypatch.setattr(kill_service, "time", patched_time)
    async with StateDB(tmp_path / "state.db") as db:
        await _create_invocation(db)
        original_update = db.update_status

        async def race_update(entity_type, entity_id, **kwargs):
            await db.update_invocation(entity_id, ended_at=200.0)
            return await original_update(entity_type, entity_id, **kwargs)

        monkeypatch.setattr(db, "update_status", race_update)
        await _cancel(db)
        row = await db.get_invocation("invocation")
        assert row["status"] == "cancelled"
        assert row["ended_at"] == 200.0
        assert await db.fetch_all(
            "SELECT status FROM status_transitions WHERE entity_id = ? AND previous_status IS NOT NULL",
            ("invocation",),
        ) == [{"status": "cancelled"}]
