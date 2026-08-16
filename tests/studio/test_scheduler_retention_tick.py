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
startup on every restart. It reads back when a prune last committed instead,
including prunes this process did not run.

The tick starts the pass and moves on. How much the sweep has to do is set by
however much eligible data has accumulated, and nothing bounds that, so a tick
that waited for it would stop delivering dispatches and evaluating schedules
for the duration.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

DAY = 86400.0


def _engine():
    from lionagi.studio.scheduler.engine import SchedulerEngine

    return SchedulerEngine(svc=AsyncMock())


def _patches(*, last_prune, interval=DAY, prune=None):
    """Patch the three things the pass reads, and nothing else."""
    last = (
        AsyncMock(side_effect=last_prune)
        if isinstance(last_prune, Exception)
        else AsyncMock(return_value=last_prune)
    )
    prune = prune if prune is not None else AsyncMock(return_value={"sessions_pruned": 0})
    return (
        patch("lionagi.studio.config.RETENTION_INTERVAL_SECONDS", int(interval)),
        patch("lionagi.studio.services.db_maintenance.get_last_prune_at", new=last),
        patch("lionagi.studio.services.db_maintenance.prune_old_data", new=prune),
        prune,
        last,
    )


async def _run(engine, *, last_prune, interval=DAY, now=None):
    """Start a pass and wait for it, so a test can assert on what it did.

    Waiting is this helper's job, not the tick's. Everything under test here
    runs in the background task ``_maybe_start_prune`` spawns.
    """
    p_interval, p_last, p_prune, prune, last = _patches(last_prune=last_prune, interval=interval)
    with p_interval, p_last, p_prune:
        engine._maybe_start_prune(time.time() if now is None else now)
        if engine._retention_task is not None:
            await engine._retention_task
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
    assert engine._last_retention_run is not None, "the clock has to start, or it never becomes due"
    assert engine._last_retention_run >= start


@pytest.mark.asyncio
async def test_a_database_that_has_never_been_pruned_is_pruned_one_interval_later():
    """The other half: not pruning at startup must not mean never pruning."""
    engine = _engine()
    start = time.time()
    prune, last = await _run(engine, last_prune=None, now=start)
    assert prune.await_count == 0

    prune, last = await _run(engine, last_prune=None, now=start + DAY + 1)
    assert prune.await_count == 1


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
    failing = AsyncMock(side_effect=RuntimeError("disk full"))
    p_interval, p_last, _, _, _ = _patches(last_prune=now - 2 * DAY)
    engine._last_retention_run = now - 2 * DAY
    with (
        p_interval,
        p_last,
        patch("lionagi.studio.services.db_maintenance.prune_old_data", new=failing),
    ):
        engine._maybe_start_prune(now)
        await engine._retention_task
        engine._maybe_start_prune(now + 60)
        if engine._retention_task is not None and not engine._retention_task.done():
            await engine._retention_task
    assert failing.await_count == 1
    assert engine._last_retention_run is not None and engine._last_retention_run >= now


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
        engine._maybe_start_prune(time.time())
        await engine._retention_task
    assert prune.await_count == 1, "control: the pass did run"
    assert vacuum.await_count == 0


@pytest.mark.asyncio
async def test_a_prune_run_from_the_admin_route_delays_the_automatic_one():
    """The recorded prune decides, not the anchor this process happens to hold.

    Someone pruning by hand commits an event the scheduler never sees any other
    way. Resolving the anchor once and keeping it would let an automatic pass
    follow a manual one immediately, having measured its interval from a prune
    two intervals ago.
    """
    engine = _engine()
    now = time.time()
    engine._last_retention_run = now - 30 * DAY  # stale: what this process last saw

    prune, last = await _run(engine, last_prune=now - 60, now=now)

    assert last.await_count == 1, "the gate opened, so the recorded prune has to be re-read"
    assert prune.await_count == 0, "a prune a minute ago means the interval has not elapsed"
    assert engine._last_retention_run >= now - 60, "the anchor has to adopt the newer prune"


@pytest.mark.asyncio
async def test_a_tick_inside_the_interval_reads_nothing():
    """The re-read costs one query when the gate opens, not one per tick."""
    engine = _engine()
    now = time.time()
    engine._last_retention_run = now - DAY / 2

    _, last = await _run(engine, last_prune=now - DAY / 2, now=now)

    assert last.await_count == 0
    assert engine._retention_task is None, "no task should have been started at all"


@pytest.mark.asyncio
async def test_the_next_prune_is_measured_from_when_the_last_one_finished():
    """A prune that runs for a long time must not leave the next one due sooner.

    Stamping the tick's timestamp meant a sweep that took a third of the
    interval got its next pass a third of an interval early, and one that ran
    longer than the interval would have been due again the moment it finished.
    """
    engine = _engine()
    # A tick timestamp far behind the wall clock stands in for a prune that ran
    # for a long time: the two differ by exactly the sweep's duration, and only
    # one of them is a sound base for the next interval.
    tick_time = time.time() - 10 * DAY

    started = time.time()
    await _run(engine, last_prune=tick_time - 2 * DAY, now=tick_time)

    assert engine._last_retention_run >= started, (
        "the anchor came from the tick's timestamp, not from when the prune ended"
    )


@pytest.mark.asyncio
async def test_the_tick_does_not_wait_for_the_prune():
    """The finding this shape exists for: an unbounded sweep held the tick.

    ``_maybe_start_prune`` is a plain function returning None, so a tick cannot
    await it even by accident. What this pins is the rest of the contract: the
    call returns while the prune is still running, and a tick arriving during
    one starts no second sweep.
    """
    engine = _engine()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def held_prune(**kwargs):
        entered.set()
        await release.wait()
        return {"sessions_pruned": 0}

    p_interval, p_last, p_prune, prune, _ = _patches(
        last_prune=time.time() - 2 * DAY, prune=AsyncMock(side_effect=held_prune)
    )
    with p_interval, p_last, p_prune:
        engine._maybe_start_prune(time.time())
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not engine._retention_task.done(), "control: the prune is still in flight"

        first_task = engine._retention_task
        engine._maybe_start_prune(time.time())
        assert engine._retention_task is first_task, "a second sweep must not start"
        assert prune.await_count == 1

        release.set()
        await asyncio.wait_for(engine._retention_task, timeout=2)

    assert prune.await_count == 1


@pytest.mark.asyncio
async def test_stopping_cancels_a_prune_still_in_flight():
    """A pass left running past shutdown outlives the engine that owns it."""
    engine = _engine()
    entered = asyncio.Event()

    async def held_prune(**kwargs):
        entered.set()
        await asyncio.sleep(3600)

    p_interval, p_last, p_prune, _, _ = _patches(
        last_prune=time.time() - 2 * DAY, prune=AsyncMock(side_effect=held_prune)
    )
    with p_interval, p_last, p_prune:
        engine._maybe_start_prune(time.time())
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.wait_for(engine.stop(), timeout=2)

    assert engine._retention_task is None
