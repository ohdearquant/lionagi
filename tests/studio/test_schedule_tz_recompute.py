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
async def test_create_invalid_cron_expr_rejected(temp_db_path):
    """An invalid cron_expr at create time is a clean ValueError and nothing
    is committed — bad data can't enter the DB at the POST boundary either."""
    from lionagi.studio.services.schedules import create_schedule, get_schedule_by_name

    with pytest.raises(ValueError, match="Invalid cron expression"):
        await create_schedule(
            {
                "name": "create-invalid-cron-test",
                "trigger_type": "cron",
                "cron_expr": "not a cron expression",
                "action_kind": "agent",
                "action_prompt": "ping",
            }
        )

    assert await get_schedule_by_name("create-invalid-cron-test") is None


@pytest.mark.asyncio
async def test_patch_recompute_retry_recovers_transient_failure(temp_db_path, caplog, monkeypatch):
    """A recompute that fails once then succeeds still lands a fresh
    next_fire_at — the guarded retry absorbs transient DB contention."""
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import scheduler
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-recompute-retry-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]
    async with StateDB() as db:
        await db.update_schedule(sid, next_fire_at=100.0)

    real_recompute = scheduler.recompute_next_fire
    calls = {"n": 0}

    async def _flaky(effective):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db busy")
        await real_recompute(effective)

    monkeypatch.setattr(scheduler, "recompute_next_fire", _flaky)

    with caplog.at_level(logging.WARNING):
        ok = await update_schedule(sid, {"cron_expr": "50 1 * * *"})
    assert ok is True
    assert calls["n"] == 2

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["next_fire_at"] != 100.0
    assert row["next_fire_at"] > time.time()


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
    async with StateDB() as db:
        await db.update_schedule(sid, next_fire_at=100.0)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db busy")

    monkeypatch.setattr(scheduler, "recompute_next_fire", _boom)

    with caplog.at_level(logging.WARNING):
        ok = await update_schedule(sid, {"cron_expr": "50 1 * * *"})
    assert ok is True

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["cron_expr"] == "50 1 * * *"  # the field update still committed
    # Documented degradation: both recompute attempts failed, so the stale
    # next_fire_at is untouched until the daemon-startup recompute heals it.
    assert row["next_fire_at"] == 100.0
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


@pytest.mark.asyncio
async def test_create_cron_empty_string_expr_rejected(temp_db_path):
    """A cron-triggered create with an empty cron_expr must not commit — the
    falsy early-return in the validator used to let this through, producing a
    schedule whose next_fire_at is never set."""
    from lionagi.studio.services.schedules import create_schedule, get_schedule_by_name

    with pytest.raises(ValueError, match="cron_expr is required"):
        await create_schedule(
            {
                "name": "create-empty-cron-test",
                "trigger_type": "cron",
                "cron_expr": "",
                "action_kind": "agent",
                "action_prompt": "ping",
            }
        )

    assert await get_schedule_by_name("create-empty-cron-test") is None


@pytest.mark.asyncio
async def test_create_cron_none_expr_rejected(temp_db_path):
    """Same as above but cron_expr omitted entirely (None)."""
    from lionagi.studio.services.schedules import create_schedule, get_schedule_by_name

    with pytest.raises(ValueError, match="cron_expr is required"):
        await create_schedule(
            {
                "name": "create-none-cron-test",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_prompt": "ping",
            }
        )

    assert await get_schedule_by_name("create-none-cron-test") is None


@pytest.mark.asyncio
async def test_patch_flip_to_cron_without_expr_rejected(temp_db_path):
    """A PATCH that flips trigger_type to 'cron' while cron_expr is absent (and
    was never set) on the effective row must also be rejected — the
    touches_trigger gate fires on trigger_type alone."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-flip-to-cron-test",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    with pytest.raises(ValueError, match="cron_expr is required"):
        await update_schedule(sid, {"trigger_type": "cron"})

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["trigger_type"] == "interval"  # untouched — rejected before commit


@pytest.mark.asyncio
async def test_patch_blank_expr_on_cron_schedule_rejected(temp_db_path):
    """A PATCH that blanks cron_expr on an existing cron schedule is rejected —
    the touches_trigger gate fires on cron_expr alone."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-blank-cron-expr-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    with pytest.raises(ValueError, match="cron_expr is required"):
        await update_schedule(sid, {"cron_expr": ""})

    async with StateDB() as db:
        row = await db.get_schedule(sid)
    assert row["cron_expr"] == "0 18 * * *"  # untouched — rejected before commit


@pytest.mark.asyncio
async def test_create_cron_valid_expr_still_accepted(temp_db_path):
    """A valid cron_expr on a cron-triggered create still passes (regression
    guard for the required=True change)."""
    from lionagi.studio.services.schedules import create_schedule

    created = await create_schedule(
        {
            "name": "create-valid-cron-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    assert created["name"] == "create-valid-cron-test"


@pytest.mark.asyncio
async def test_create_non_cron_empty_expr_still_accepted(temp_db_path):
    """A non-cron trigger with an empty/absent cron_expr is unaffected — the
    required check only applies when trigger_type == 'cron'."""
    from lionagi.studio.services.schedules import create_schedule

    created = await create_schedule(
        {
            "name": "create-interval-empty-cron-test",
            "trigger_type": "interval",
            "interval_sec": 60,
            "cron_expr": "",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    assert created["name"] == "create-interval-empty-cron-test"


# ---------------------------------------------------------------------------
# PATCH exclude_unset semantics + enable/startup dead-cron guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_route_all_null_body_no_longer_404s(temp_db_path):
    """An UpdateScheduleRequest built from a body whose only field is
    explicitly null must reach update_schedule with that field present (not
    stripped to {}), so an existing schedule is never misreported as 404."""
    from lionagi.studio.services.schedules import (
        UpdateScheduleRequest,
        create_schedule,
        update_schedule_route,
    )

    created = await create_schedule(
        {
            "name": "patch-all-null-route-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
            "description": "will be cleared",
        }
    )
    sid = created["id"]

    body = UpdateScheduleRequest(description=None)
    result = await update_schedule_route(sid, body)
    assert result == {"ok": True}

    from lionagi.studio.services.schedules import get_schedule

    row = await get_schedule(sid)
    assert row["description"] is None


@pytest.mark.asyncio
async def test_patch_explicit_null_clears_nullable_field(temp_db_path):
    """Explicitly setting a nullable optional field to null via the service
    layer clears it in the DB."""
    from lionagi.studio.services.schedules import create_schedule, get_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-clear-nullable-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
            "action_model": "sonnet",
        }
    )
    sid = created["id"]

    ok = await update_schedule(sid, {"action_model": None})
    assert ok is True

    row = await get_schedule(sid)
    assert row["action_model"] is None


@pytest.mark.asyncio
async def test_patch_absent_field_untouched(temp_db_path):
    """A field never mentioned in the PATCH body (not present in `fields`)
    is left at its prior value — exercised at the pydantic layer via
    model_dump(exclude_unset=True)."""
    from lionagi.studio.services.schedules import (
        UpdateScheduleRequest,
        create_schedule,
        get_schedule,
    )

    created = await create_schedule(
        {
            "name": "patch-absent-field-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
            "action_model": "sonnet",
        }
    )
    sid = created["id"]

    # description never mentioned -> must not appear in the dumped fields.
    body = UpdateScheduleRequest(action_model="gpt-4")
    fields = body.model_dump(exclude_unset=True)
    assert "description" not in fields
    assert fields == {"action_model": "gpt-4"}

    from lionagi.studio.services.schedules import update_schedule

    ok = await update_schedule(sid, fields)
    assert ok is True
    row = await get_schedule(sid)
    assert row["action_model"] == "gpt-4"


@pytest.mark.asyncio
async def test_patch_reject_clearing_non_nullable_field(temp_db_path):
    """Explicitly nulling a NOT NULL column (name, trigger_type, action_kind,
    missed_fire_policy, overlap_policy) is rejected with a clear ValueError
    rather than reaching the DB constraint."""
    from lionagi.studio.services.schedules import create_schedule, get_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-reject-null-name-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    with pytest.raises(ValueError, match="cannot be cleared to null"):
        await update_schedule(sid, {"name": None})

    row = await get_schedule(sid)
    assert row["name"] == "patch-reject-null-name-test"  # untouched


@pytest.mark.asyncio
async def test_patch_empty_body_on_existing_schedule_is_noop_not_404(temp_db_path):
    """A PATCH body with zero fields set (`{}`) on a schedule that exists is
    a genuine no-op — it must not be misreported as 404."""
    from lionagi.studio.services.schedules import create_schedule, update_schedule

    created = await create_schedule(
        {
            "name": "patch-empty-body-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]

    ok = await update_schedule(sid, {})
    assert ok is True


@pytest.mark.asyncio
async def test_patch_unknown_schedule_still_404(temp_db_path):
    """A PATCH (empty or otherwise) against a schedule id that does not
    exist is still a genuine 404 — the no-op fix must not mask that."""
    from lionagi.studio.services.schedules import update_schedule

    ok = await update_schedule("no-such-schedule-id", {})
    assert ok is False

    ok = await update_schedule("no-such-schedule-id", {"description": "x"})
    assert ok is False


@pytest.mark.asyncio
async def test_enable_dead_cron_schedule_rejected(temp_db_path):
    """Enabling a cron schedule whose cron_expr has been blanked (a legacy
    dead row) is rejected with a 400-worthy ValueError instead of flipping
    enabled=1 on a schedule that can never fire."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedules import (
        create_schedule,
        disable_schedule,
        enable_schedule,
        get_schedule,
    )

    created = await create_schedule(
        {
            "name": "enable-dead-cron-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]
    assert await disable_schedule(sid) is True

    # Simulate a legacy row whose cron_expr was blanked out from under it
    # (write-time validation now prevents this for new rows, but existing
    # rows predating that fix can still be in this state).
    async with StateDB() as db:
        await db.update_schedule(sid, cron_expr="")

    with pytest.raises(ValueError, match="cron_expr is required"):
        await enable_schedule(sid)

    row = await get_schedule(sid)
    assert row["enabled"] == 0  # rejected before the flip committed


@pytest.mark.asyncio
async def test_enable_route_dead_cron_schedule_returns_400(temp_db_path):
    """The route wrapper turns the enable-dead-cron ValueError into a 400,
    mirroring create/update's ValueError -> 400 mapping."""
    from fastapi import HTTPException

    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedules import (
        create_schedule,
        disable_schedule,
        enable_schedule_route,
    )

    created = await create_schedule(
        {
            "name": "enable-route-dead-cron-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]
    assert await disable_schedule(sid) is True
    async with StateDB() as db:
        await db.update_schedule(sid, cron_expr="")

    with pytest.raises(HTTPException) as exc_info:
        await enable_schedule_route(sid)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_enable_healthy_cron_schedule_still_works(temp_db_path):
    """Regression guard: enabling a cron schedule with a valid cron_expr is
    unaffected by the dead-cron guard."""
    from lionagi.studio.services.schedules import (
        create_schedule,
        disable_schedule,
        enable_schedule,
        get_schedule,
    )

    created = await create_schedule(
        {
            "name": "enable-healthy-cron-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]
    assert await disable_schedule(sid) is True

    ok = await enable_schedule(sid)
    assert ok is True

    row = await get_schedule(sid)
    assert row["enabled"] == 1


@pytest.mark.asyncio
async def test_startup_recompute_warns_on_dead_cron_row(temp_db_path, caplog):
    """_recompute_armed_cron_schedules must log a warning naming the
    schedule id for an enabled cron row with an empty cron_expr, instead of
    silently skipping it via recompute_next_fire's falsy early-return."""
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import scheduler
    from lionagi.studio.services.schedules import create_schedule

    created = await create_schedule(
        {
            "name": "startup-dead-cron-test",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    sid = created["id"]
    async with StateDB() as db:
        await db.update_schedule(sid, cron_expr="")

    with caplog.at_level(logging.WARNING):
        await scheduler._recompute_armed_cron_schedules()

    matches = [
        r
        for r in caplog.records
        if "no cron_expr" in r.getMessage().lower() and sid in r.getMessage()
    ]
    assert matches, [r.getMessage() for r in caplog.records]
