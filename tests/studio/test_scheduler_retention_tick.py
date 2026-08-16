# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The scheduler tick's retention pass.

The pass exists because nothing else calls ``prune_old_data`` on a schedule:
before this, every prune came from an admin route someone had to invoke, so an
installation nobody administered grew without bound.

What makes it more than an interval timer is where the interval is measured
from. Anchoring on process start would mean a daemon restarted more often than
the interval never prunes at all, and starting the counter at zero, the way the
reaper and checkpoint passes do, would put a prune in the middle of daemon
startup on every restart. It reads back when a prune last committed instead.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

DAY = 86400.0


def _engine():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    return SchedulerEngine(svc=AsyncMock())


def _patches(*, last_prune, interval=DAY):
    """Patch the three things `_maybe_prune` reads, and nothing else."""
    last = (
        AsyncMock(side_effect=last_prune)
        if isinstance(last_prune, Exception)
        else AsyncMock(return_value=last_prune)
    )
    prune = AsyncMock(return_value={"sessions_pruned": 0})
    return (
        patch("lionagi.studio.config.RETENTION_INTERVAL_SECONDS", int(interval)),
        patch("lionagi.studio.services.db_maintenance.get_last_prune_at", new=last),
        patch("lionagi.studio.services.db_maintenance.prune_old_data", new=prune),
        prune,
        last,
    )


async def _run(engine, *, last_prune, interval=DAY, now=None):
    p_interval, p_last, p_prune, prune, last = _patches(last_prune=last_prune, interval=interval)
    with p_interval, p_last, p_prune:
        await engine._maybe_prune(time.time() if now is None else now)
    return prune, last


@pytest.mark.asyncio
async def test_a_prune_older_than_the_interval_is_due():
    engine = _engine()
    prune, _ = await _run(engine, last_prune=time.time() - 2 * DAY)
    assert prune.await_count == 1
    assert prune.await_args.kwargs["actor"] == "scheduler_tick"


@pytest.mark.asyncio
async def test_a_prune_inside_the_interval_is_not_due():
    engine = _engine()
    prune, _ = await _run(engine, last_prune=time.time() - DAY / 2)
    assert prune.await_count == 0


@pytest.mark.asyncio
async def test_a_database_that_has_never_been_pruned_is_not_pruned_at_startup():
    """The property that keeps a first adoption out of daemon startup.

    An installation carrying a large backlog would otherwise prune during the
    first tick after the upgrade, which is both the least expected moment and
    the one competing with everything else startup is doing.
    """
    engine = _engine()
    start = time.time()
    prune, _ = await _run(engine, last_prune=None, now=start)
    assert prune.await_count == 0, "a never-pruned database must not prune on the first tick"
    assert engine._last_retention_run == start, "the clock has to start, or it never becomes due"


@pytest.mark.asyncio
async def test_a_database_that_has_never_been_pruned_is_pruned_one_interval_later():
    """The other half: not pruning at startup must not mean never pruning."""
    engine = _engine()
    start = time.time()
    prune, last = await _run(engine, last_prune=None, now=start)
    assert prune.await_count == 0

    prune, last = await _run(engine, last_prune=None, now=start + DAY + 1)
    assert prune.await_count == 1
    assert last.await_count == 0, "the anchor is resolved once, not re-read every tick"


@pytest.mark.asyncio
async def test_the_interval_is_measured_from_the_recorded_prune_not_from_startup():
    """A daemon restarted more often than the interval still reaches a pass.

    This is the case a process-local timer gets wrong: every restart would move
    the deadline forward, so a daemon bounced daily under a daily interval would
    never prune once.
    """
    engine = _engine()
    prune, _ = await _run(engine, last_prune=time.time() - 30 * DAY)
    assert prune.await_count == 1


@pytest.mark.asyncio
async def test_a_zero_interval_disables_the_pass_without_reading_anything():
    engine = _engine()
    prune, last = await _run(engine, last_prune=time.time() - 30 * DAY, interval=0)
    assert prune.await_count == 0
    assert last.await_count == 0, "a disabled pass must not cost a database read per tick"
    assert engine._last_retention_run is None


@pytest.mark.asyncio
async def test_a_failed_read_of_the_last_prune_retries_rather_than_anchoring():
    """A read failure is not an answer.

    Treating it as "never pruned" would anchor the schedule to the moment of the
    failure and silence the pass for the life of the process. Leaving it
    unresolved costs one more read on the next tick.
    """
    engine = _engine()
    prune, _ = await _run(engine, last_prune=RuntimeError("database is locked"))
    assert prune.await_count == 0
    assert engine._last_retention_run is None, "a failed read must not become the anchor"

    prune, _ = await _run(engine, last_prune=time.time() - 2 * DAY)
    assert prune.await_count == 1, "the next tick has to try again"


@pytest.mark.asyncio
async def test_a_failing_prune_is_not_retried_on_every_tick():
    """Matches the reaper and checkpoint passes: the stamp is unconditional."""
    engine = _engine()
    now = time.time()
    p_interval, p_last, _, _, last = _patches(last_prune=now - 2 * DAY)
    failing = AsyncMock(side_effect=RuntimeError("disk full"))
    with (
        p_interval,
        p_last,
        patch("lionagi.studio.services.db_maintenance.prune_old_data", new=failing),
    ):
        await engine._maybe_prune(now)
        await engine._maybe_prune(now + 60)
    assert failing.await_count == 1
    assert engine._last_retention_run == now


@pytest.mark.asyncio
async def test_the_pass_never_vacuums():
    """VACUUM holds an exclusive lock for as long as it takes to rewrite the
    file, so it stays on the admin route where a person picks the moment. A
    scheduled pass that quietly acquired it would stall every reader."""
    engine = _engine()
    p_interval, p_last, p_prune, prune, _ = _patches(last_prune=time.time() - 2 * DAY)
    vacuum = AsyncMock(return_value={"status": "ok"})
    with (
        p_interval,
        p_last,
        p_prune,
        patch("lionagi.studio.services.db_maintenance.vacuum_state_db", new=vacuum),
    ):
        await engine._maybe_prune(time.time())
    assert prune.await_count == 1, "control: the pass did run"
    assert vacuum.await_count == 0
