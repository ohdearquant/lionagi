# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterization of TerminalCallbackRegistry.emit's fan-out: matching
handlers run concurrently under one budget for the whole emit; a handler that
raises is logged and swallowed from the caller, while a handler still running
when the shared budget expires is cancelled along with its siblings and nothing
is logged for the overrun; every handler receives the same envelope instance."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from lionagi.state.lifecycle.callbacks import (
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)


def _envelope(event_id: str = "ev-fanout") -> RunTerminalEnvelope:
    return RunTerminalEnvelope(
        event_id=event_id,
        entity=EntityRef(kind="session", id="sess-1"),
        previous_status="running",
        terminal_status="completed",
        reason_code="run.completed.ok",
        occurred_at=time.time(),
    )


@pytest.mark.asyncio
async def test_all_matching_handlers_run_concurrently():
    # Every handler reaches its first await before any of them is released, so a
    # fan-out that runs handlers one at a time never sets all_started and times
    # out instead of completing.
    registry = TerminalCallbackRegistry()
    started: list[str] = []
    all_started = asyncio.Event()
    release = asyncio.Event()

    def _make(name: str):
        async def _handler(_env) -> None:
            started.append(name)
            if len(started) == 4:
                all_started.set()
            await release.wait()

        return _handler

    for name in ("a", "b", "c", "d"):
        registry.register(name, _make(name))

    task = asyncio.ensure_future(registry.emit(_envelope()))
    await asyncio.wait_for(all_started.wait(), timeout=5.0)
    release.set()
    await asyncio.wait_for(task, timeout=5.0)

    assert sorted(started) == ["a", "b", "c", "d"]


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_overrunning_handler_is_cut_and_the_fast_sibling_completes():
    # A single hanging handler must not hold the emit open: the fast sibling
    # finishes (0.03s, 13x inside the 0.4s budget) and the emit returns when the
    # deadline cuts the hanging handler's 5s sleep (12.5x the budget).
    registry = TerminalCallbackRegistry(budget_seconds=0.4)
    fast_done: list[str] = []
    hung_done: list[str] = []

    async def _fast(_env):
        await asyncio.sleep(0.03)
        fast_done.append("fast")

    async def _hang(_env):
        await asyncio.sleep(5.0)
        hung_done.append("hung")

    registry.register("fast", _fast)
    registry.register("hang", _hang)

    start = time.monotonic()
    await registry.emit(_envelope())
    elapsed = time.monotonic() - start

    assert fast_done == ["fast"]
    assert hung_done == []
    # 10x headroom over the 0.4s budget, well before the hang's 5s sleep: an
    # emit that never cut the hanging handler would have returned at 5s.
    assert elapsed < 4.0


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_one_budget_measured_from_the_emit_covers_every_handler():
    # Pin: one budget is measured from the emit and every matching handler starts
    # under it, so no handler runs past it and a handler that finishes does not
    # extend it for the others. This cannot tell that apart from a per-handler
    # budget armed for every handler in the same pass, because all of those expire
    # at the same instant. It does separate it from a budget taken one handler at a
    # time as each starts: the finisher is released only once the stuck handler has
    # started, so a one-at-a-time budget would spend the finisher's budget while
    # nobody has set that event yet, and finished would stay empty. The result
    # depends on "finish" being registered before "stuck": with "stuck" first, a
    # one-at-a-time budget would run it to its own deadline, cancel it, and then
    # let "finish" through.
    # The 0.5s budget against the 6.0s wait_for is 12x, the margin
    # docs/internals/ci.md:22-23 asks of slow-timing tests; the finisher completes
    # once the stuck handler has started, so no sleep ratio is relied on.
    registry = TerminalCallbackRegistry(budget_seconds=0.5)
    stuck_started = asyncio.Event()
    finished: list[str] = []
    stuck_completed: list[str] = []

    async def _finish(_env):
        await stuck_started.wait()
        finished.append("finish")

    async def _stuck(_env):
        stuck_started.set()
        await asyncio.Event().wait()
        stuck_completed.append("stuck")

    registry.register("finish", _finish)
    registry.register("stuck", _stuck)

    await asyncio.wait_for(registry.emit(_envelope("ev-shared-deadline")), timeout=6.0)

    assert finished == ["finish"]
    assert stuck_completed == []


@pytest.mark.asyncio
async def test_raising_handler_is_swallowed_and_siblings_still_complete():
    # A handler that raises must not reach the caller (emit returns None and
    # does not raise) and must not stop a sibling from running to completion.
    registry = TerminalCallbackRegistry()
    ran: list[str] = []

    def _boom(_env):
        raise RuntimeError("handler blew up")

    async def _ok(_env):
        await asyncio.sleep(0)
        ran.append("ok")

    registry.register("boom", _boom)
    registry.register("ok", _ok)

    result = await registry.emit(_envelope())

    assert result is None
    assert ran == ["ok"]


@pytest.mark.asyncio
async def test_raising_handler_does_not_cancel_a_concurrently_running_sibling():
    # The sibling is mid-await in its first suspension when the other handler
    # raises; the raise is contained per handler, so the sibling is neither
    # cancelled nor lost.
    registry = TerminalCallbackRegistry()
    sibling_mid_await = asyncio.Event()
    sibling_finished: list[str] = []

    async def _boom(_env):
        await sibling_mid_await.wait()
        raise RuntimeError("handler blew up")

    async def _sibling(_env):
        await asyncio.sleep(0)  # first suspension
        sibling_mid_await.set()
        await asyncio.sleep(0)
        sibling_finished.append("sibling")

    registry.register("boom", _boom)
    registry.register("sibling", _sibling)

    await asyncio.wait_for(registry.emit(_envelope()), timeout=2.0)

    assert sibling_mid_await.is_set()
    assert sibling_finished == ["sibling"]


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_overrunning_handler_never_reaches_its_post_deadline_side_effect():
    # The caller returns once the shared 0.2s budget expires and the cancelled
    # handler's statement after its await (a 5s sleep, 25x the budget) never runs;
    # the caller sees no exception. The bound below is the 10x guard, well above
    # the 0.2s cut itself.
    registry = TerminalCallbackRegistry(budget_seconds=0.2)
    after_deadline: list[str] = []

    async def _slow(_env):
        await asyncio.sleep(5.0)
        after_deadline.append("late")

    registry.register("slow", _slow)

    start = time.monotonic()
    result = await asyncio.wait_for(registry.emit(_envelope()), timeout=3.0)
    elapsed = time.monotonic() - start

    assert result is None
    assert after_deadline == []
    assert elapsed < 2.0  # 10x the 0.2s budget; the cut lands near 0.2s


@pytest.mark.asyncio
@pytest.mark.slow_timing
async def test_shared_budget_cancels_a_still_running_sibling_and_logs_no_overrun(caplog):
    # When the shared budget expires, every handler still running is cancelled:
    # a sibling that has not finished is cancelled too, neither handler reaches
    # its post-await statement, and the overrun is not logged as an ordinary
    # failure.
    registry = TerminalCallbackRegistry(budget_seconds=0.2)
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    completed: list[str] = []

    async def _overrun(_env):
        await asyncio.sleep(5.0)
        completed.append("overrun")

    async def _sibling(_env):
        sibling_started.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        completed.append("sibling")

    registry.register("overrun", _overrun)
    registry.register("sibling", _sibling)

    with caplog.at_level(logging.WARNING, logger="lionagi.state.lifecycle.callbacks"):
        await registry.emit(_envelope("ev-shared-cut"))

    assert sibling_started.is_set()
    assert sibling_cancelled.is_set()
    assert completed == []
    assert [
        record for record in caplog.records if record.name == "lionagi.state.lifecycle.callbacks"
    ] == []


@pytest.mark.asyncio
async def test_every_matching_handler_receives_the_same_envelope_instance():
    registry = TerminalCallbackRegistry()
    seen: list[RunTerminalEnvelope] = []
    first_seen = asyncio.Event()
    second_seen = asyncio.Event()

    async def _one(env):
        seen.append(env)
        first_seen.set()
        await second_seen.wait()

    async def _two(env):
        seen.append(env)
        second_seen.set()
        await first_seen.wait()

    registry.register("one", _one)
    registry.register("two", _two)

    envelope = _envelope("ev-identity")
    await asyncio.wait_for(registry.emit(envelope), timeout=2.0)

    assert len(seen) == 2
    assert all(item is envelope for item in seen)


@pytest.mark.asyncio
async def test_handlers_are_started_in_registration_order():
    # Handlers run concurrently, so completion order is unspecified; the order in
    # which their bodies first run is pinned to registration order. Registered
    # out of alphabetical order so a fan-out that sorted targets by name would
    # differ. The sleep below is only to let all three start; the assertion waits
    # on the gate, not on the sleep.
    registry = TerminalCallbackRegistry()
    started: list[str] = []
    gate = asyncio.Event()

    async def _record(name: str) -> None:
        started.append(name)
        await gate.wait()

    def _make(name: str):
        async def _recorder(_env) -> None:
            await _record(name)

        return _recorder

    for name in ("b", "c", "a"):
        registry.register(name, _make(name))

    task = asyncio.ensure_future(registry.emit(_envelope()))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert started == ["b", "c", "a"]


@pytest.mark.asyncio
async def test_all_matching_override_registrations_run_concurrently():
    # Every matching override registration fires (not just the first), and the
    # two overrides run concurrently: each sets one event and waits on the
    # other's, so a one-at-a-time fan-out leaves the first waiting and this
    # test's own 2s wait_for times out, well before the registry's 5s budget.
    # The non-override handler is replaced for this envelope.
    registry = TerminalCallbackRegistry(budget_seconds=5.0)
    fired: list[str] = []
    a_started = asyncio.Event()
    b_started = asyncio.Event()

    async def _override_a(_env):
        a_started.set()
        await b_started.wait()
        fired.append("override-a")

    async def _override_b(_env):
        b_started.set()
        await a_started.wait()
        fired.append("override-b")

    async def _plain(_env):
        fired.append("plain")

    registry.register("plain", _plain)
    registry.register("override-a", _override_a, kinds=["session"], override=True)
    registry.register("override-b", _override_b, kinds=["session"], override=True)

    await asyncio.wait_for(registry.emit(_envelope()), timeout=2.0)

    assert sorted(fired) == ["override-a", "override-b"]
