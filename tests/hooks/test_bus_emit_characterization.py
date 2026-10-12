# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""What ``HookBus.emit`` does today: order, StopHook, handler failure, cancellation, slow handlers.

Covers both profiles the bus selects between: the observational points
(failures logged and swallowed, later handlers still run) and the blocking
points ``TOOL_PRE`` and ``USER_PROMPT_SUBMIT`` (first ordinary failure
propagates). A case that one profile decides runs over every point of that
profile, so a point that moves to the other profile fails there. A case both
profiles share runs over every live point, so a special case added for one
point fails there, unless the case names its points in its parameters (one
point of each profile, or two observational points). The two special cases
there are today have tests of their own, and the shared cases they would fail
leave their point out: ``SESSION_END`` flushes the message retry queues, which
the no-flush case skips, and ``MESSAGE_ADD`` records no ``HookSignal``, which
the recording cases skip.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from lionagi.hooks import HookBus, HookPoint, HookSignal, StopHook
from lionagi.hooks._message_retry import MessagePersistRetryQueue, PendingMessageEvent
from lionagi.hooks.bus import _current_emitting_bus
from lionagi.session.observer import SessionObserver

BLOCKING_POINTS = (HookPoint.TOOL_PRE, HookPoint.USER_PROMPT_SUBMIT)
OBSERVATIONAL_POINTS = (
    HookPoint.SESSION_START,
    HookPoint.SESSION_END,
    HookPoint.BRANCH_CREATE,
    HookPoint.BRANCH_END,
    HookPoint.API_PRE_CALL,
    HookPoint.API_POST_CALL,
    HookPoint.API_STREAM_CHUNK,
    HookPoint.TOOL_POST,
    HookPoint.TOOL_ERROR,
    HookPoint.MESSAGE_ADD,
)
LIVE_POINTS = BLOCKING_POINTS + OBSERVATIONAL_POINTS
# MESSAGE_ADD is represented on the signal transport by MessageAdded, so an
# emit there records no HookSignal; every other live point records one.
RECORDING_POINTS = tuple(p for p in LIVE_POINTS if p is not HookPoint.MESSAGE_ADD)


class _Abort(BaseException):
    """A failure outside the ``Exception`` hierarchy, so ``except Exception`` misses it."""


def test_the_live_point_groups_are_the_whole_vocabulary_minus_the_dormant_point():
    assert set(LIVE_POINTS) | {HookPoint.ARTIFACT_CREATED} == set(HookPoint)
    assert not set(BLOCKING_POINTS) & set(OBSERVATIONAL_POINTS)


# ── registration order and sequencing ────────────────────────────────────────


@pytest.mark.parametrize("point", LIVE_POINTS)
async def test_handlers_run_in_registration_order_and_only_an_awaitable_return_is_awaited(
    point, caplog
):
    bus = HookBus()
    calls: list[tuple[str, dict]] = []

    def plain(**kwargs):
        calls.append(("sync", kwargs))
        return "returned, unused"

    async def native(**kwargs):
        calls.append(("async", kwargs))
        return "returned, unused"

    async def deferred():
        calls.append(("awaited", {}))

    def returns_an_awaitable(**kwargs):
        calls.append(("sync returning an awaitable", kwargs))
        return deferred()

    for handler in (plain, native, returns_an_awaitable):
        bus.on(point, handler)

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        result = await bus.emit(point, session_id="s", n=1)

    arguments = {"session_id": "s", "n": 1}
    assert result is None
    assert calls == [
        ("sync", arguments),
        ("async", arguments),
        ("sync returning an awaitable", arguments),
        ("awaited", {}),
    ]
    # awaiting the plain return would fail, logged on an observational point
    assert [r for r in caplog.records if r.name == "lionagi.hooks"] == []


@pytest.mark.parametrize("point", LIVE_POINTS)
async def test_each_handler_completes_before_the_next_is_invoked(point):
    bus = HookBus()
    trace: list[str] = []

    async def first(**_kwargs):
        trace.append("first:start")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        trace.append("first:end")

    async def second(**_kwargs):
        trace.append("second:start")
        trace.append("second:end")

    bus.on(point, first)
    bus.on(point, second)
    await bus.emit(point)

    assert trace == ["first:start", "first:end", "second:start", "second:end"]


async def test_a_handler_registered_twice_runs_twice_and_off_removes_one_copy():
    bus = HookBus()
    calls: list[str] = []

    async def handler(**_kwargs):
        calls.append("handler")

    bus.on(HookPoint.SESSION_START, handler)
    bus.on(HookPoint.SESSION_START, handler)
    await bus.emit(HookPoint.SESSION_START)
    assert len(calls) == 2

    calls.clear()
    bus.off(HookPoint.SESSION_START, handler)
    await bus.emit(HookPoint.SESSION_START)
    assert len(calls) == 1

    bus.off(HookPoint.SESSION_START, handler)
    await bus.emit(HookPoint.SESSION_START)
    assert len(calls) == 1


async def test_handlers_registered_on_another_point_do_not_run():
    bus = HookBus()
    calls: list[str] = []
    bus.on(HookPoint.SESSION_END, lambda **_kwargs: calls.append("session.end"))
    bus.on(HookPoint.TOOL_POST, lambda **_kwargs: calls.append("tool.post"))

    await bus.emit(HookPoint.TOOL_POST)

    assert calls == ["tool.post"]


@pytest.mark.parametrize("point", (HookPoint.SESSION_START, HookPoint.TOOL_PRE))
async def test_emit_accepts_a_point_by_its_string_value(point):
    bus = HookBus()
    calls: list[str] = []
    bus.on(point.value, lambda **_kwargs: calls.append("ran"))

    await bus.emit(point.value)

    assert calls == ["ran"]


async def test_an_unknown_point_string_is_a_value_error_and_no_handler_runs():
    bus = HookBus()
    calls: list[str] = []
    bus.on(HookPoint.SESSION_START, lambda **_kwargs: calls.append("ran"))

    with pytest.raises(ValueError):
        await bus.emit("session.starts")
    with pytest.raises(ValueError):
        await bus.blocking_emit("session.starts")

    assert calls == []


@pytest.mark.parametrize("point", RECORDING_POINTS)
async def test_the_record_holds_the_emitted_arguments_after_a_handler_changes_its_own(point):
    # each handler gets its own dict from **kwargs, so the next handler is
    # unaffected whatever the bus does; the record is the bus's own copy
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    seen: list[dict] = []

    def mutate(**kwargs):
        kwargs["session_id"] = "changed"
        kwargs["added"] = True

    def observe(**kwargs):
        seen.append(kwargs)

    bus.on(point, mutate)
    bus.on(point, observe)
    await bus.emit(point, session_id="s")

    assert seen == [{"session_id": "s"}]
    assert [(r.point, r.kwargs) for r in obs.by_type(HookSignal)] == [(point, {"session_id": "s"})]


class _FailingStore:
    """A store whose live-message writes fail with each of ``failures`` in turn, and
    with the last one on every write after it."""

    def __init__(self, order: list[str], *failures: BaseException) -> None:
        self.order = order
        self.failures = list(failures)

    async def _persist_live_message(self, _message, **_kwargs) -> None:
        self.order.append("write attempt")
        raise self.failures.pop(0) if len(self.failures) > 1 else self.failures[0]


async def _bus_with_one_pending_message(store: _FailingStore, **kwargs) -> HookBus:
    """A bus holding the retry queue the message hook registers, with one write pending."""
    bus = HookBus(**kwargs)
    queue = MessagePersistRetryQueue(
        store, logger=logging.getLogger("tests.hooks.retry"), owner="hook session s"
    )
    bus._message_retry_queues = {"key": queue}
    assert not await queue.submit(PendingMessageEvent(message={"role": "user"}, session_id="s"))
    assert queue.pending_count == 1
    store.order.clear()
    return bus


async def test_session_end_flushes_message_retries_before_its_handlers(caplog):
    order: list[str] = []
    bus = await _bus_with_one_pending_message(_FailingStore(order, RuntimeError("db down")))
    bus.on(HookPoint.SESSION_END, lambda **_kwargs: order.append("session.end handler"))

    with caplog.at_level(logging.ERROR, logger="tests.hooks.retry"):
        await bus.emit(HookPoint.SESSION_END)

    # the queue logs the lost write and returns False; the handlers still run
    assert order == ["write attempt", "session.end handler"]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [(r.name, r.getMessage()) for r in errors] == [
        (
            "tests.hooks.retry",
            "live persist gave up for hook session s at teardown: 1 event(s) were never "
            "written and are lost",
        )
    ]


@pytest.mark.parametrize("point", tuple(p for p in LIVE_POINTS if p is not HookPoint.SESSION_END))
async def test_no_other_point_flushes_message_retries(point):
    order: list[str] = []
    bus = await _bus_with_one_pending_message(_FailingStore(order, RuntimeError("db down")))
    bus.on(point, lambda **_kwargs: order.append("handler"))

    await bus.emit(point)

    assert order == ["handler"]


async def test_a_failure_escaping_the_session_end_flush_reaches_the_emitter_before_any_handler():
    # A write failing with an Exception is logged by the queue as lost
    # (test_session_end_flushes_message_retries_before_its_handlers); one outside the
    # Exception hierarchy escapes the queue, its event still pending, and then the emit.
    obs = SessionObserver()
    order: list[str] = []
    bus = await _bus_with_one_pending_message(
        _FailingStore(order, RuntimeError("db down"), _Abort()), observer=obs
    )
    bus.on(HookPoint.SESSION_END, lambda **_kwargs: order.append("handler"))

    with pytest.raises(_Abort):
        await bus.emit(HookPoint.SESSION_END)

    assert order == ["write attempt"]
    assert bus._message_retry_queues["key"].pending_count == 1
    assert obs.by_type(HookSignal) == []
    assert _current_emitting_bus() is None


async def test_the_emitting_bus_is_visible_to_observational_handlers_only():
    bus = HookBus()
    seen: list[HookBus | None] = []
    bus.on(HookPoint.SESSION_START, lambda **_kwargs: seen.append(_current_emitting_bus()))
    bus.on(HookPoint.TOOL_PRE, lambda **_kwargs: seen.append(_current_emitting_bus()))

    await bus.emit(HookPoint.SESSION_START)
    await bus.emit(HookPoint.TOOL_PRE)

    assert seen == [bus, None]
    assert _current_emitting_bus() is None


# ── StopHook ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("point", LIVE_POINTS)
async def test_stop_hook_ends_the_chain_and_the_emitter_sees_a_normal_return(point, caplog):
    bus = HookBus()
    calls: list[str] = []

    def before(**_kwargs):
        calls.append("before")

    def stopper(**_kwargs):
        calls.append("stopper")
        raise StopHook

    def after(**_kwargs):  # pragma: no cover - the chain has ended
        calls.append("after")

    bus.on(point, before)
    bus.on(point, stopper)
    bus.on(point, after)

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        result = await bus.emit(point)

    assert result is None
    assert calls == ["before", "stopper"]
    assert [r for r in caplog.records if r.name == "lionagi.hooks"] == []


@pytest.mark.parametrize("point", (HookPoint.SESSION_START, HookPoint.TOOL_PRE))
async def test_a_stop_hook_subclass_with_arguments_also_ends_the_chain(point):
    class Halt(StopHook):
        pass

    bus = HookBus()
    calls: list[str] = []

    def stopper(**_kwargs):
        calls.append("stopper")
        raise Halt("enough")

    bus.on(point, stopper)
    bus.on(point, lambda **_kwargs: calls.append("after"))
    await bus.emit(point)

    assert calls == ["stopper"]


@pytest.mark.parametrize("point", (HookPoint.SESSION_START, HookPoint.TOOL_PRE))
async def test_stop_hook_ending_the_chain_does_not_persist_beyond_the_emit_that_raised_it(point):
    bus = HookBus()
    calls: list[str] = []
    stop_next = [True]

    def first(**_kwargs):
        calls.append("first")
        if stop_next:
            stop_next.clear()
            raise StopHook

    bus.on(point, first)
    bus.on(point, lambda **_kwargs: calls.append("second"))

    await bus.emit(point)
    assert calls == ["first"]

    await bus.emit(point)
    assert calls == ["first", "first", "second"]


async def test_stop_hook_ends_only_the_chain_of_its_own_point():
    bus = HookBus()
    calls: list[str] = []

    def stopper(**_kwargs):
        raise StopHook

    bus.on(HookPoint.TOOL_POST, stopper)
    bus.on(HookPoint.TOOL_ERROR, lambda **_kwargs: calls.append("tool.error"))

    await bus.emit(HookPoint.TOOL_POST)
    await bus.emit(HookPoint.TOOL_ERROR)

    assert calls == ["tool.error"]


async def test_a_message_add_emit_runs_its_handlers_and_records_no_hook_signal():
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    calls: list[str] = []
    bus.on(HookPoint.MESSAGE_ADD, lambda **_kwargs: calls.append("handler"))

    await bus.emit(HookPoint.MESSAGE_ADD, session_id="s")
    await bus.emit(HookPoint.SESSION_START, session_id="s")

    assert calls == ["handler"]
    assert [r.point for r in obs.by_type(HookSignal)] == [HookPoint.SESSION_START]


@pytest.mark.parametrize("point", (HookPoint.SESSION_START, HookPoint.TOOL_PRE))
async def test_a_failing_record_is_logged_and_the_emit_still_returns(point, caplog):
    class FailingObserver:
        async def emit(self, _signal) -> None:
            raise RuntimeError("transport down")

    bus = HookBus(observer=FailingObserver())
    calls: list[str] = []
    bus.on(point, lambda **_kwargs: calls.append("handler"))

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        result = await bus.emit(point)

    assert result is None
    assert calls == ["handler"]
    records = [r for r in caplog.records if r.name == "lionagi.hooks"]
    assert [r.getMessage() for r in records] == [f"HookSignal record failed: {point.value}"]


@pytest.mark.parametrize("point", RECORDING_POINTS)
async def test_a_short_circuited_emit_is_still_recorded_once_with_the_original_arguments(point):
    obs = SessionObserver()
    bus = HookBus(observer=obs)

    def stopper(**_kwargs):
        raise StopHook

    bus.on(point, stopper)
    await bus.emit(point, session_id="s")

    assert [(r.point, r.kwargs) for r in obs.by_type(HookSignal)] == [(point, {"session_id": "s"})]


async def test_a_logged_failure_before_a_stop_hook_still_logs_and_the_stop_hook_still_ends_the_chain(
    caplog,
):
    bus = HookBus()
    calls: list[str] = []

    def boom(**_kwargs):
        calls.append("boom")
        raise RuntimeError("explode")

    def stopper(**_kwargs):
        calls.append("stopper")
        raise StopHook

    bus.on(HookPoint.TOOL_POST, boom)
    bus.on(HookPoint.TOOL_POST, stopper)
    bus.on(HookPoint.TOOL_POST, lambda **_kwargs: calls.append("after"))

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        await bus.emit(HookPoint.TOOL_POST)

    assert calls == ["boom", "stopper"]
    records = [r for r in caplog.records if r.name == "lionagi.hooks"]
    assert [r.getMessage() for r in records] == ["Hook failed: tool.post"]


# ── handler failure ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("point", OBSERVATIONAL_POINTS)
async def test_an_observational_failure_is_logged_and_later_handlers_still_run(point, caplog):
    bus = HookBus()
    calls: list[str] = []

    def sync_boom(**_kwargs):
        calls.append("sync_boom")
        raise ValueError("sync")

    def ok(**_kwargs):
        calls.append("ok")

    async def async_boom(**_kwargs):
        calls.append("async_boom")
        raise RuntimeError("async")

    def last(**_kwargs):
        calls.append("last")

    for handler in (sync_boom, ok, async_boom, last):
        bus.on(point, handler)

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        result = await bus.emit(point)

    assert result is None
    assert calls == ["sync_boom", "ok", "async_boom", "last"]
    records = [r for r in caplog.records if r.name == "lionagi.hooks"]
    assert [r.getMessage() for r in records] == [f"Hook failed: {point.value}"] * 2
    assert [r.levelno for r in records] == [logging.ERROR] * 2
    assert [r.exc_info[0] for r in records] == [ValueError, RuntimeError]


@pytest.mark.parametrize(
    "point", tuple(p for p in OBSERVATIONAL_POINTS if p is not HookPoint.MESSAGE_ADD)
)
async def test_an_observational_failure_is_recorded_without_a_denial_marker(point):
    obs = SessionObserver()
    bus = HookBus(observer=obs)

    def boom(**_kwargs):
        raise RuntimeError("explode")

    bus.on(point, boom)
    await bus.emit(point, session_id="s")

    assert [(r.point, r.kwargs) for r in obs.by_type(HookSignal)] == [(point, {"session_id": "s"})]


@pytest.mark.parametrize("point", BLOCKING_POINTS)
async def test_a_blocking_first_failure_is_raised_verbatim_and_later_handlers_do_not_run(
    point, caplog
):
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    calls: list[str] = []
    error = ValueError("denied")

    def ok(**_kwargs):
        calls.append("ok")

    def deny(**_kwargs):
        calls.append("deny")
        raise error

    def later(**_kwargs):  # pragma: no cover - the chain has ended
        calls.append("later")

    for handler in (ok, deny, later):
        bus.on(point, handler)

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        with pytest.raises(ValueError) as excinfo:
            await bus.emit(point, tool_name="rm")

    assert excinfo.value is error
    assert calls == ["ok", "deny"]
    assert [r for r in caplog.records if r.name == "lionagi.hooks"] == []
    assert [(r.point, r.kwargs) for r in obs.by_type(HookSignal)] == [
        (point, {"tool_name": "rm", "denied": True, "exception": "ValueError: denied"})
    ]


@pytest.mark.parametrize("point", (HookPoint.SESSION_START, HookPoint.TOOL_POST))
async def test_blocking_emit_propagates_the_first_failure_even_on_an_observational_point(point):
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    calls: list[str] = []

    def deny(**_kwargs):
        calls.append("deny")
        raise PermissionError("no")

    bus.on(point, deny)
    bus.on(point, lambda **_kwargs: calls.append("later"))

    with pytest.raises(PermissionError, match="no"):
        await bus.blocking_emit(point, session_id="s")

    assert calls == ["deny"]
    assert [r.kwargs for r in obs.by_type(HookSignal)] == [
        {"session_id": "s", "denied": True, "exception": "PermissionError: no"}
    ]


@pytest.mark.parametrize("point", BLOCKING_POINTS)
async def test_blocking_emit_forwards_the_passed_kwargs_unchanged(point):
    bus = HookBus()
    seen: list[dict] = []
    bus.on(point, lambda **kwargs: seen.append(kwargs))

    await bus.blocking_emit(point, tool_name="ls", args={"path": "/"})

    assert seen == [{"tool_name": "ls", "args": {"path": "/"}}]


@pytest.mark.parametrize("point", LIVE_POINTS)
async def test_a_failure_outside_the_exception_hierarchy_escapes_every_profile(point, caplog):
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    calls: list[str] = []

    def abort(**_kwargs):
        raise _Abort

    bus.on(point, abort)
    bus.on(point, lambda **_kwargs: calls.append("later"))

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        with pytest.raises(_Abort):
            await bus.emit(point)

    assert calls == []
    assert [r for r in caplog.records if r.name == "lionagi.hooks"] == []
    assert obs.by_type(HookSignal) == []
    assert _current_emitting_bus() is None


# ── cancellation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("point", LIVE_POINTS)
async def test_emitter_cancellation_propagates_from_the_awaited_handler_and_leaves_no_trace(
    point, caplog
):
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    started = asyncio.Event()
    calls: list[str] = []

    async def slow(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            calls.append("cancelled inside the handler")
            raise

    bus.on(point, slow)
    bus.on(point, lambda **_kwargs: calls.append("later"))

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        task = asyncio.create_task(bus.emit(point))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1.0)
        assert done == {task}
        with pytest.raises(asyncio.CancelledError):
            task.result()

    assert calls == ["cancelled inside the handler"]
    assert [r for r in caplog.records if r.name == "lionagi.hooks"] == []
    assert obs.by_type(HookSignal) == []
    assert _current_emitting_bus() is None


@pytest.mark.parametrize("point", (HookPoint.SESSION_START, HookPoint.TOOL_PRE))
async def test_a_handler_that_swallows_cancellation_lets_the_chain_and_the_emit_complete(point):
    bus = HookBus()
    started = asyncio.Event()
    calls: list[str] = []

    async def swallow(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            calls.append("swallowed")

    bus.on(point, swallow)
    bus.on(point, lambda **_kwargs: calls.append("later"))

    task = asyncio.create_task(bus.emit(point))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    await asyncio.wait_for(task, timeout=1.0)

    assert calls == ["swallowed", "later"]
    assert not task.cancelled()


@pytest.mark.parametrize("point", LIVE_POINTS)
async def test_a_handler_raising_cancellation_stops_the_chain_and_is_never_logged(point, caplog):
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    calls: list[str] = []

    async def cancel(**_kwargs):
        raise asyncio.CancelledError

    bus.on(point, cancel)
    bus.on(point, lambda **_kwargs: calls.append("later"))

    with caplog.at_level(logging.DEBUG, logger="lionagi.hooks"):
        with pytest.raises(asyncio.CancelledError):
            await bus.emit(point)

    assert calls == []
    assert [r for r in caplog.records if r.name == "lionagi.hooks"] == []
    assert obs.by_type(HookSignal) == []
    assert _current_emitting_bus() is None


# ── slow handlers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("point", LIVE_POINTS)
async def test_a_slow_handler_delays_the_emitter_and_the_next_handler(point):
    bus = HookBus()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def slow(**_kwargs):
        started.set()
        await release.wait()
        calls.append("slow")

    bus.on(point, slow)
    bus.on(point, lambda **_kwargs: calls.append("later"))

    task = asyncio.create_task(bus.emit(point))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await asyncio.sleep(0.01)
    assert not task.done()
    assert calls == []

    release.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert calls == ["slow", "later"]


@pytest.mark.parametrize("point", (HookPoint.SESSION_START, HookPoint.TOOL_PRE))
async def test_the_emission_is_recorded_only_after_the_slow_handler_finishes(point):
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(**_kwargs):
        started.set()
        await release.wait()

    bus.on(point, slow)

    task = asyncio.create_task(bus.emit(point))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await asyncio.sleep(0.01)
    assert obs.by_type(HookSignal) == []

    release.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(obs.by_type(HookSignal)) == 1


async def test_two_emits_on_one_bus_are_not_serialized_against_each_other():
    bus = HookBus()
    release = asyncio.Event()
    both_running = asyncio.Event()
    running = 0

    async def slow(**_kwargs):
        nonlocal running
        running += 1
        if running == 2:
            both_running.set()
        await release.wait()

    bus.on(HookPoint.SESSION_START, slow)

    first = asyncio.create_task(bus.emit(HookPoint.SESSION_START))
    second = asyncio.create_task(bus.emit(HookPoint.SESSION_START))
    await asyncio.wait_for(both_running.wait(), timeout=1.0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)

    assert running == 2
