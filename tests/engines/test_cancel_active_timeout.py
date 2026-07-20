# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Bounded cleanup timeout in cancel_active() for non-cooperative tasks."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from lionagi.engines.engine import Engine, EngineRun


class _StubEngine(Engine):
    async def _run(self, run, *a, **kw):  # pragma: no cover
        return ""


# ---------------------------------------------------------------------------
# Non-cooperative child: catches CancelledError and loops indefinitely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_cancel_active_returns_within_deadline_for_non_cooperative_task():
    """cancel_active() must return within cancel_timeout_s even when a child
    catches CancelledError and never re-raises it."""
    done = asyncio.Event()

    async def non_cooperative():
        try:
            while not done.is_set():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    # Absorb — simulate a misbehaving task that never re-raises.
                    pass
        finally:
            done.set()

    run = _StubEngine(cancel_timeout_s=0.2).new_run()
    task = asyncio.ensure_future(non_cooperative())
    run._active.add(task)
    task.add_done_callback(run._active.discard)

    # Yield so the task starts and reaches its first await before we cancel.
    await asyncio.sleep(0)

    t0 = time.monotonic()
    await run.cancel_active()
    elapsed = time.monotonic() - t0

    # Ten times the configured timeout leaves scheduler headroom while still
    # distinguishing bounded cleanup from the non-cooperative 60s task.
    assert elapsed < 2.0, f"cancel_active hung for {elapsed:.2f}s, expected <2s"
    # _active must be cleared.
    assert run._active == set(), "_active must be cleared after cancel_active()"

    # Stop the abandoned task so the test runner doesn't crash on teardown.
    done.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_active_logs_abandonment_for_non_cooperative_task():
    """A loud warning must be logged when tasks are abandoned at timeout."""
    import lionagi.engines.engine as _eng_mod

    done = asyncio.Event()
    logged: list[str] = []

    class _CapHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logged.append(record.getMessage())

    handler = _CapHandler(logging.WARNING)
    _eng_mod.logger.addHandler(handler)
    old_level = _eng_mod.logger.level
    _eng_mod.logger.setLevel(logging.WARNING)
    try:

        async def non_cooperative():
            try:
                while not done.is_set():
                    try:
                        await asyncio.sleep(60)
                    except asyncio.CancelledError:
                        pass  # absorb — never re-raises
            finally:
                done.set()

        run = _StubEngine(cancel_timeout_s=0.15).new_run()
        task = asyncio.ensure_future(non_cooperative())
        run._active.add(task)
        task.add_done_callback(run._active.discard)

        # Yield so the task starts and reaches its first await before we cancel.
        await asyncio.sleep(0)

        await run.cancel_active()
    finally:
        _eng_mod.logger.removeHandler(handler)
        _eng_mod.logger.setLevel(old_level)

    # Must have logged a warning about abandonment.
    abandon_logs = [m for m in logged if "abandon" in m.lower()]
    assert abandon_logs, f"Expected a warning about abandoned tasks; got logged messages: {logged}"
    # Warning must mention the count (1 task).
    assert any("1" in m for m in abandon_logs), (
        f"Warning must include the count of abandoned tasks; got: {abandon_logs}"
    )

    # Stop the abandoned task so the test runner doesn't crash on teardown.
    done.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Cooperative child: should complete normally without hitting the timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_active_awaits_cooperative_task_to_completion():
    """A cooperative child (re-raises CancelledError) must be awaited to
    completion — the timeout path must NOT fire for well-behaved tasks."""
    run = _StubEngine(cancel_timeout_s=5.0).new_run()
    cleanup_ran = asyncio.Event()

    async def cooperative():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Brief cleanup, then re-raise.
            await asyncio.sleep(0.05)
            cleanup_ran.set()
            raise

    task = asyncio.ensure_future(cooperative())
    run._active.add(task)
    task.add_done_callback(run._active.discard)

    # Yield once so the task starts and reaches its first await — otherwise
    # asyncio cancels a not-yet-started coroutine without running any of it.
    await asyncio.sleep(0)

    await run.cancel_active()

    # Cooperative cleanup must have completed before cancel_active returned.
    assert cleanup_ran.is_set(), (
        "cooperative child cleanup must complete before cancel_active returns"
    )
    assert run._active == set()


# ---------------------------------------------------------------------------
# Default timeout is exposed and is a sensible value
# ---------------------------------------------------------------------------


def test_cancel_timeout_default_on_engine():
    """Engine must expose cancel_timeout_s with a sensible positive default."""
    eng = _StubEngine()
    assert hasattr(eng, "cancel_timeout_s"), "Engine must have cancel_timeout_s attribute"
    assert eng.cancel_timeout_s > 0, "Default cancel_timeout_s must be positive"
    # Sensible range: between 1 second and 120 seconds.
    assert 1.0 <= eng.cancel_timeout_s <= 120.0, (
        f"Default cancel_timeout_s={eng.cancel_timeout_s} is outside sensible range [1, 120]"
    )


def test_cancel_timeout_configurable():
    """cancel_timeout_s must be settable at construction time."""
    eng = _StubEngine(cancel_timeout_s=7.5)
    assert eng.cancel_timeout_s == 7.5


# ---------------------------------------------------------------------------
# Engine.run() lifetime guarantee preserved with non-cooperative task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_engine_run_lifetime_guarantee_with_non_cooperative_task():
    """Engine.run() must return within bounded time even when a spawned
    child is non-cooperative, without raising CancelledError to the caller."""
    done = asyncio.Event()

    class _SpawnNonCoopEngine(Engine):
        async def _run(self, run, *a, **kw):
            async def non_cooperative():
                try:
                    while not done.is_set():
                        try:
                            await asyncio.sleep(60)
                        except asyncio.CancelledError:
                            pass  # absorb — never re-raises
                finally:
                    done.set()

            run.spawn(non_cooperative())
            # Simulate internal cancellation (as deadline watchdog would do).
            run._budget_notified = True
            assert run._run_task is not None
            run._run_task.cancel()
            await asyncio.sleep(10)
            return "never"

    eng = _SpawnNonCoopEngine(cancel_timeout_s=0.2)
    t0 = time.monotonic()
    result = await eng.run()
    elapsed = time.monotonic() - t0

    # Must return normally (internal cancellation absorbed, not propagated).
    assert result is None
    # Must respect the bounded timeout, not wait forever.
    assert elapsed < 2.0, f"Engine.run() hung for {elapsed:.2f}s, expected <2s"

    # Allow the abandoned task to exit cleanly via the done flag.
    done.set()
    await asyncio.sleep(0)
