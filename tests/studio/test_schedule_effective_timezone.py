# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The scheduler records which zone it resolved each cron schedule in.

The failure these guard against is not a wrong zone -- it is a zone nobody
could name after the fact. A resolution that quietly produced UTC moved every
cron schedule in the process by the host's offset and left no trace on any
row, so "firing an hour early" and "correct" looked identical in storage.

So these assert on the *record*, not on the value: an unresolvable
configuration must leave a row that says the zone was not resolvable, and a
resolvable one must leave a row naming the zone that was actually used. The
two UTCs -- one requested, one fallen back to -- must not read the same.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests._scheduler_claims import fire_with_claim

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("croniter", reason="studio extra not installed")


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp file DB.

    Only the one binding is patched, because only one is read. ``StateDB``
    resolves the default at call time from its own module, and the schedules
    service no longer decides anything from a path: it asks the configured
    store. Patching a name the service does not read would pin nothing while
    looking like it pinned something.
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _pin_scheduler_tz(monkeypatch: pytest.MonkeyPatch, name: str, source: str) -> None:
    """Pin the process-wide resolution the scheduler reads.

    Both attributes are set together because they are two halves of one
    answer: pinning only the name would leave the source describing how some
    other name was arrived at, which is exactly the confusion under test.
    """
    import lionagi.studio.config as studio_config

    monkeypatch.setattr(studio_config, "SCHEDULER_TZ", name)
    monkeypatch.setattr(studio_config, "SCHEDULER_TZ_SOURCE", source)
    monkeypatch.setattr(studio_config, "SCHEDULER_TZ_SOURCE_DETAIL", "test")


async def _create_cron_schedule(name: str, **extra) -> str:
    from lionagi.studio.services.schedules import create_schedule

    created = await create_schedule(
        {
            "name": name,
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
            **extra,
        }
    )
    return created["id"]


async def _stamp_and_read(schedule_id: str) -> dict:
    """Run the scheduler's startup stamping pass and read the row back."""
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import SchedulerEngine

    await SchedulerEngine()._stamp_effective_timezones()
    async with StateDB() as db:
        return await db.get_schedule(schedule_id)


@pytest.mark.asyncio
async def test_unloadable_zone_is_recorded_as_unresolvable(temp_db_path, monkeypatch):
    """A configuration naming a zone this host cannot load must produce a row
    that says so -- not merely a row that happens to say UTC."""
    from lionagi.studio.config import TZ_SOURCE_UTC_UNLOADABLE_NAME

    _pin_scheduler_tz(monkeypatch, "Not/A_Real_Zone", "env:LIONAGI_SCHEDULER_TZ")
    sid = await _create_cron_schedule("tz-unloadable")

    row = await _stamp_and_read(sid)

    assert row["effective_timezone_source"] == TZ_SOURCE_UTC_UNLOADABLE_NAME
    assert row["effective_timezone"] == "UTC"


@pytest.mark.asyncio
async def test_resolvable_zone_is_recorded_by_name(temp_db_path, monkeypatch):
    """A resolvable configuration must name the zone that was actually used,
    and attribute it to where it came from."""
    _pin_scheduler_tz(monkeypatch, "America/New_York", "env:LIONAGI_SCHEDULER_TZ")
    sid = await _create_cron_schedule("tz-resolvable")

    row = await _stamp_and_read(sid)

    assert row["effective_timezone"] == "America/New_York"
    assert row["effective_timezone_source"] == "env:LIONAGI_SCHEDULER_TZ"


@pytest.mark.asyncio
async def test_requested_utc_and_fallback_utc_are_distinguishable(temp_db_path, monkeypatch):
    """The whole point of storing the source: three configurations that all
    interpret cron in UTC must leave three different records."""
    from lionagi.studio.config import (
        TZ_SOURCE_SCHEDULER_ENV,
        TZ_SOURCE_UTC_FALLBACK,
        TZ_SOURCE_UTC_UNLOADABLE_NAME,
    )

    _pin_scheduler_tz(monkeypatch, "UTC", TZ_SOURCE_SCHEDULER_ENV)
    asked_for = await _stamp_and_read(await _create_cron_schedule("tz-utc-asked-for"))

    _pin_scheduler_tz(monkeypatch, "UTC", TZ_SOURCE_UTC_FALLBACK)
    fell_back = await _stamp_and_read(await _create_cron_schedule("tz-utc-fell-back"))

    _pin_scheduler_tz(monkeypatch, "Not/A_Real_Zone", TZ_SOURCE_SCHEDULER_ENV)
    unloadable = await _stamp_and_read(await _create_cron_schedule("tz-utc-unloadable"))

    names = {
        asked_for["effective_timezone"],
        fell_back["effective_timezone"],
        unloadable["effective_timezone"],
    }
    assert names == {"UTC"}, "premise: all three interpret cron in UTC"

    assert asked_for["effective_timezone_source"] == TZ_SOURCE_SCHEDULER_ENV
    assert fell_back["effective_timezone_source"] == TZ_SOURCE_UTC_FALLBACK
    assert unloadable["effective_timezone_source"] == TZ_SOURCE_UTC_UNLOADABLE_NAME


@pytest.mark.asyncio
async def test_declared_zone_on_the_row_wins_and_is_attributed_to_the_row(
    temp_db_path, monkeypatch
):
    """A row carrying its own zone resolves in that zone, and the record says
    the zone came from the row -- not from the process default it overrode."""
    from lionagi.studio.config import TZ_SOURCE_SCHEDULE_DECLARED

    _pin_scheduler_tz(monkeypatch, "UTC", "env:LIONAGI_SCHEDULER_TZ")
    sid = await _create_cron_schedule("tz-declared", resolved_timezone="Europe/Berlin")

    row = await _stamp_and_read(sid)

    assert row["effective_timezone"] == "Europe/Berlin"
    assert row["effective_timezone_source"] == TZ_SOURCE_SCHEDULE_DECLARED


@pytest.mark.asyncio
async def test_a_rearm_by_the_tick_loop_stamps_the_row_too(temp_db_path, monkeypatch):
    """Startup is not the only resolve point. A schedule armed by the tick
    loop must carry the stamp as well, or a row created between restarts
    stays unattributed until the daemon happens to bounce."""
    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import SchedulerEngine

    _pin_scheduler_tz(monkeypatch, "America/New_York", "env:LIONAGI_SCHEDULER_TZ")
    sid = await _create_cron_schedule("tz-armed-by-tick")

    async with StateDB() as db:
        await db.update_schedule(sid, next_fire_at=None)
        schedule = await db.get_schedule(sid)

    engine = SchedulerEngine()
    await engine.recompute_next_fire(schedule)

    async with StateDB() as db:
        row = await db.get_schedule(sid)

    assert row["effective_timezone"] == "America/New_York"
    assert row["effective_timezone_source"] == "env:LIONAGI_SCHEDULER_TZ"


@pytest.mark.asyncio
async def test_an_actual_fire_stamps_the_row_too(temp_db_path, monkeypatch):
    """Re-arming and firing are different writes, and both compute a next
    fire time.

    A schedule that never restarts and never has its next fire recomputed by
    the tick loop still passes through here on every occurrence, so a stamp
    missing from this path leaves exactly the schedules that run most often
    as the ones nobody can attribute. Asserted through a real fire against
    the store rather than on the arguments to a mock, because the question is
    what ends up on the row.
    """
    from unittest.mock import AsyncMock, patch

    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import SchedulerEngine

    _pin_scheduler_tz(monkeypatch, "America/New_York", "env:LIONAGI_SCHEDULER_TZ")
    sid = await _create_cron_schedule("tz-fired")

    async with StateDB() as db:
        schedule = await db.get_schedule(sid)

    engine = SchedulerEngine()
    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(0, "")),
        ),
    ):
        await fire_with_claim(engine, schedule, "run-tz-001", trigger_context={"scheduled": True})

    async with StateDB() as db:
        row = await db.get_schedule(sid)

    assert row["last_fired_at"] is not None, "the fire path did not run"
    assert row["effective_timezone"] == "America/New_York"
    assert row["effective_timezone_source"] == "env:LIONAGI_SCHEDULER_TZ"


@pytest.mark.asyncio
async def test_a_fire_that_could_not_spawn_stamps_the_row_too(temp_db_path, monkeypatch):
    """The failure path computes a next fire time as well, so it resolves a
    zone as well, so it has to record which one.

    A schedule whose action cannot be built fires, fails and re-arms on every
    occurrence without ever reaching the normal path. Left unstamped it would
    be the one class of schedule that is both permanently broken and
    permanently unattributable, which is the combination least affordable
    when someone is trying to work out why it runs when it does.
    """
    from unittest.mock import patch

    from lionagi.state.db import StateDB
    from lionagi.studio.scheduler.engine import SchedulerEngine

    _pin_scheduler_tz(monkeypatch, "America/New_York", "env:LIONAGI_SCHEDULER_TZ")
    sid = await _create_cron_schedule("tz-unspawnable")

    async with StateDB() as db:
        schedule = await db.get_schedule(sid)

    engine = SchedulerEngine()
    with patch(
        "lionagi.studio.scheduler.subprocess.build_argv",
        side_effect=ValueError("action is not buildable"),
    ):
        await fire_with_claim(engine, schedule, "run-tz-002", trigger_context={"scheduled": True})

    async with StateDB() as db:
        row = await db.get_schedule(sid)

    assert row["last_fired_at"] is not None, "the failure path did not run"
    assert row["effective_timezone"] == "America/New_York"
    assert row["effective_timezone_source"] == "env:LIONAGI_SCHEDULER_TZ"


@pytest.mark.asyncio
async def test_non_cron_schedules_are_not_stamped(temp_db_path, monkeypatch):
    """An interval trigger computes its fire time from an offset, so no zone
    is in play. Stamping one would record a fact that was never used."""
    from lionagi.studio.services.schedules import create_schedule

    _pin_scheduler_tz(monkeypatch, "America/New_York", "env:LIONAGI_SCHEDULER_TZ")
    created = await create_schedule(
        {
            "name": "tz-interval",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )

    row = await _stamp_and_read(created["id"])

    assert row["effective_timezone"] is None
    assert row["effective_timezone_source"] is None


def test_startup_announces_the_resolved_zone(caplog, monkeypatch):
    """The zone is resolved at import and frozen for the process, so if
    startup does not say it, nothing does."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    _pin_scheduler_tz(monkeypatch, "America/New_York", "env:LIONAGI_SCHEDULER_TZ")

    with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
        SchedulerEngine()._log_scheduler_timezone()

    messages = [r.getMessage() for r in caplog.records]
    assert any("America/New_York" in m and "env:LIONAGI_SCHEDULER_TZ" in m for m in messages), (
        f"startup said nothing naming the zone and its source: {messages}"
    )


def test_startup_announces_a_utc_fallback_at_warning_level(caplog, monkeypatch):
    """A fallback moved every cron schedule in the process. It must not be
    logged at the same level as an ordinary, chosen configuration."""
    from lionagi.studio.config import TZ_SOURCE_UTC_FALLBACK
    from lionagi.studio.scheduler.engine import SchedulerEngine

    _pin_scheduler_tz(monkeypatch, "UTC", TZ_SOURCE_UTC_FALLBACK)

    with caplog.at_level(logging.INFO, logger="lionagi.studio.scheduler.engine"):
        SchedulerEngine()._log_scheduler_timezone()

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(TZ_SOURCE_UTC_FALLBACK in m for m in warnings), (
        f"a fleet-wide UTC fallback was not warned about: {warnings}"
    )
