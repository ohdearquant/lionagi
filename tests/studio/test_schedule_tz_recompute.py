# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Integration tests: the schedules service (PATCH, enable) recomputes
next_fire_at through the same SchedulerEngine.recompute_next_fire() code
path used at daemon startup, and never silently shifts a fire time."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("croniter", reason="studio extra not installed")

NY = ZoneInfo("America/New_York")


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp file DB, patched everywhere DEFAULT_DB_PATH is bound by
    plain import (state.db + the schedules service module both import the
    name directly, so both bindings need patching)."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("lionagi.studio.services.schedules.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def _pin_scheduler_tz(monkeypatch: pytest.MonkeyPatch):
    """Pin the configured cron timezone explicitly so these tests don't
    depend on the CI host's local timezone."""
    import lionagi.studio.config as studio_config

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", "America/New_York")


@pytest.mark.asyncio
async def test_patch_cron_expr_recomputes_next_fire_immediately(temp_db_path, caplog):
    """(c2) PATCH cron_expr must recompute next_fire_at under the new
    expression right away, not wait for the next fire."""
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-recompute-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    # Seed a stale next_fire_at as if it were computed under the old (wrong)
    # interpretation — an obviously-wrong value so the shift is unambiguous.
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        await db.update_schedule(sid, next_fire_at=100.0)

    with caplog.at_level(logging.INFO):
        ok = await update_schedule(sid, {"cron_expr": "50 1 * * *"})
    assert ok is True

    async with StateDB() as db:
        row = await db.get_schedule(sid)

    assert row["cron_expr"] == "50 1 * * *"
    assert row["next_fire_at"] != 100.0
    got_local = datetime.fromtimestamp(row["next_fire_at"], tz=NY)
    assert (got_local.hour, got_local.minute) == (1, 50)
    assert any("next_fire_at shifted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_patch_unrelated_field_does_not_log_shift(temp_db_path, caplog):
    """A PATCH that doesn't touch cron_expr/trigger fields recomputes to the
    *same* next_fire_at, so no shift is logged (requirement d)."""
    from lionagi.studio.scheduler.engine import scheduler
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-noop-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    from lionagi.state.db import StateDB

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    correct_next = scheduler._compute_next_fire(row, time.time())
    async with StateDB() as db:
        await db.update_schedule(sid, next_fire_at=correct_next)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        ok = await update_schedule(sid, {"description": "just a description change"})
    assert ok is True

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["next_fire_at"] == pytest.approx(correct_next)
    assert not any("next_fire_at shifted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_disable_enable_recomputes_stale_next_fire(temp_db_path, caplog):
    """(c3) disable -> enable recomputes next_fire_at; a stale past value
    never fires immediately on enable unless the fresh computation says so."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedules import (
        create_schedule,
        disable_schedule,
        enable_schedule,
    )

    created = await create_schedule(
        {
            "name": "enable-recompute-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    ok = await disable_schedule(sid)
    assert ok is True

    # Stale, long-past next_fire_at — the pre-fix bug would leave this as-is,
    # letting an immediate post-enable tick treat it as "due".
    stale_past = 1.0
    async with StateDB() as db:
        await db.update_schedule(sid, next_fire_at=stale_past)

    with caplog.at_level(logging.INFO):
        ok = await enable_schedule(sid)
    assert ok is True

    async with StateDB() as db:
        row = await db.get_schedule(sid)

    assert row["next_fire_at"] != stale_past
    assert row["next_fire_at"] > time.time()  # freshly computed, strictly future
    assert any("next_fire_at shifted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_patch_invalid_cron_expr_rejected_before_commit(temp_db_path):
    """An invalid cron_expr in a PATCH is a clean ValueError; the DB row is
    left untouched rather than committing bad data ahead of a recompute."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-invalid-cron-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    with pytest.raises(ValueError, match="Invalid cron expression"):
        await update_schedule(sid, {"cron_expr": "not a cron expression"})

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["cron_expr"] == "0 18 * * *"


@pytest.mark.asyncio
async def test_patch_recompute_failure_does_not_raise(temp_db_path, caplog, monkeypatch):
    """A recompute failure after a valid, already-committed PATCH degrades to
    a warning log instead of propagating out of update_schedule()."""
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import scheduler
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-recompute-fails-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    async def _boom(*args, **kwargs):
        raise RuntimeError("db busy")

    monkeypatch.setattr(scheduler, "recompute_next_fire", _boom)

    with caplog.at_level(logging.WARNING):
        ok = await update_schedule(sid, {"cron_expr": "50 1 * * *"})
    assert ok is True

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["cron_expr"] == "50 1 * * *"  # the field update still committed
    assert any("Failed to recompute next_fire_at" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_enable_recompute_failure_does_not_raise(temp_db_path, caplog, monkeypatch):
    """A recompute failure after a valid, already-committed enable degrades
    to a warning log instead of propagating out of enable_schedule()."""
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import scheduler
    from lionagi.studio.services.schedules import (
        create_schedule,
        disable_schedule,
        enable_schedule,
    )

    created = await create_schedule(
        {
            "name": "enable-recompute-fails-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]
    assert await disable_schedule(sid) is True

    async def _boom(*args, **kwargs):
        raise RuntimeError("db busy")

    monkeypatch.setattr(scheduler, "recompute_next_fire", _boom)

    with caplog.at_level(logging.WARNING):
        ok = await enable_schedule(sid)
    assert ok is True

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["enabled"] == 1  # the enable flag still committed
    assert any("Failed to recompute next_fire_at" in r.message for r in caplog.records)
