# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Session reactive observation: observe / emit / gate / route + run_operation,
and the observer→operation composition that makes a Session a useful orchestrator.
"""

from __future__ import annotations

import asyncio

import pytest

from lionagi.ln.concurrency._compat import BaseExceptionGroup
from lionagi.ln.types import Filter
from lionagi.protocols.generic.event import Event
from lionagi.session.session import Session


class DepthRequested(Event):
    question: str = ""
    novelty: float = 0.7


class Noticed(Event):
    note: str = ""


async def test_run_operation_direct_invoke():
    s = Session()

    @s.operation()
    async def deepen(question: str):
        return f"deepened: {question}"

    assert await s.run_operation("deepen", question="RAFT") == "deepened: RAFT"


async def test_run_operation_unknown_raises():
    s = Session()
    with pytest.raises(ValueError, match="Unknown operation"):
        await s.run_operation("nope")


async def test_observe_and_emit_passes_session():
    s = Session()
    seen = []

    @s.observe(DepthRequested)
    async def on_depth(event, session):
        # handler receives the bound Session
        assert session is s
        seen.append(event.question)
        return "ok"

    results = await s.emit(DepthRequested(question="x"))
    assert seen == ["x"]
    assert results == ["ok"]


async def test_gate_denies_dispatch_but_records():
    s = Session()
    fired = []

    @s.observe(DepthRequested)
    def on_depth(event, session):
        fired.append(event.question)

    s.gate(lambda e: getattr(e, "novelty", 1) > 0.5)

    await s.emit(DepthRequested(question="high", novelty=0.9))
    await s.emit(DepthRequested(question="low", novelty=0.1))  # gated out

    assert fired == ["high"]  # only the allowed one dispatched
    # both recorded (audit trail)
    assert len(s.observer.by_type(DepthRequested)) == 2


async def test_gate_raise_denies_but_still_records():
    # A gate that raises denies dispatch — but the event is still recorded,
    # and the exception does not propagate out of emit (audit contract).
    s = Session()
    fired = []

    @s.observe(DepthRequested)
    def on_depth(event, session):
        fired.append(event.question)

    def raising_gate(_event):
        raise RuntimeError("gate exploded")

    s.gate(raising_gate)

    results = await s.emit(DepthRequested(question="x", novelty=0.9))
    assert results == []  # no dispatch
    assert fired == []
    assert len(s.observer.by_type(DepthRequested)) == 1  # recorded despite raise


async def test_route_condition_stream():
    s = Session()
    s.route(lambda e: getattr(e, "novelty", 0) > 0.7, into="high_novelty")

    await s.emit(DepthRequested(question="a", novelty=0.9))
    await s.emit(DepthRequested(question="b", novelty=0.2))

    streamed = [e.question for e in s.observer.stream("high_novelty")]
    assert streamed == ["a"]


async def test_route_failure_propagates_before_subscriber_invocation():
    session = Session()
    subscriber_calls: list[str] = []

    def fail_route(_event):
        raise LookupError("route failed")

    session.route(fail_route, into="never")
    session.observe(Noticed, handler=lambda _event, _session: subscriber_calls.append("called"))

    with pytest.raises(LookupError, match="route failed"):
        await session.emit(Noticed(note="route failure"))
    assert subscriber_calls == []


async def test_observer_triggers_operation():
    """The synthesis: an observed event drives a registered operation."""
    s = Session()

    @s.operation()
    async def record(note: str):
        return f"recorded: {note}"

    @s.observe(Noticed)
    async def on_notice(event, session):
        return await session.run_operation("record", note=event.note)

    results = await s.emit(Noticed(note="cross-thread link"))
    assert results == ["recorded: cross-thread link"]


async def test_multiple_handlers_same_event():
    s = Session()
    calls = []

    @s.observe(Noticed)
    def first(event, session):
        calls.append("first")

    @s.observe(Noticed)
    def second(event, session):
        calls.append("second")

    await s.emit(Noticed(note="x"))
    assert calls == ["first", "second"]


class _TrackingAwaitable:
    def __init__(self, events: list[str], marker: str):
        self.events = events
        self.marker = marker
        self.awaited = False

    def __await__(self):
        self.awaited = True
        self.events.append(self.marker)
        if False:  # pragma: no cover - makes this a generator-based awaitable
            yield None
        return self.marker


class _RaisingFilter(Filter):
    def matches(self, _payload):
        raise LookupError("filter failed")


async def test_profile_invokes_every_handler_before_gathering_returned_awaitables():
    s = Session()
    events: list[str] = []

    def first(_event, _session):
        events.append("invoke-first")
        return _TrackingAwaitable(events, "await-first")

    def second(_event, _session):
        events.append("invoke-second")
        return "second-result"

    s.observe(Noticed, handler=first)
    s.observe(Noticed, handler=second)

    assert await s.emit(Noticed(note="x")) == ["second-result", "await-first"]
    assert events == ["invoke-first", "invoke-second", "await-first"]


async def test_profile_filter_failure_stops_before_gather_and_leaves_prior_awaitable_unrun():
    s = Session()
    events: list[str] = []
    pending = _TrackingAwaitable(events, "must-not-await")

    s.observe(Noticed, handler=lambda _event, _session: pending)
    s.observe(_RaisingFilter(), handler=lambda _event, _session: None)

    with pytest.raises(LookupError, match="filter failed"):
        await s.emit(Noticed(note="x"))
    assert pending.awaited is False
    assert events == []


async def test_profile_sync_invocation_failure_stops_and_leaves_prior_awaitable_unrun():
    session = Session()
    events: list[str] = []
    pending = _TrackingAwaitable(events, "must-not-await")
    later_calls: list[str] = []

    session.observe(Noticed, handler=lambda _event, _session: pending)

    def fail(_event, _session):
        raise LookupError("invocation failed")

    session.observe(Noticed, handler=fail)
    session.observe(
        Noticed,
        handler=lambda _event, _session: later_calls.append("later"),
    )

    with pytest.raises(LookupError, match="invocation failed"):
        await session.emit(Noticed(note="x"))
    assert pending.awaited is False
    assert events == []
    assert later_calls == []


async def test_profile_unwraps_one_returned_awaitable_failure_and_groups_many():
    async def runtime_failure(_event, _session):
        raise RuntimeError("one")

    one = Session()
    one.observe(Noticed, handler=runtime_failure)
    with pytest.raises(RuntimeError, match="one"):
        await one.emit(Noticed(note="one"))

    async def value_failure(_event, _session):
        raise ValueError("two")

    many = Session()
    many.observe(Noticed, handler=runtime_failure)
    many.observe(Noticed, handler=value_failure)
    with pytest.raises(BaseExceptionGroup) as excinfo:
        await many.emit(Noticed(note="many"))

    assert sorted(type(exc).__name__ for exc in excinfo.value.exceptions) == [
        "RuntimeError",
        "ValueError",
    ]


async def test_profile_returns_handler_cancellation_and_cancelled_sibling_results():
    s = Session()
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def cancel_self(_event, _session):
        await sibling_started.wait()
        raise asyncio.CancelledError("handler cancelled")

    async def sibling(_event, _session):
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    s.observe(Noticed, handler=cancel_self)
    s.observe(Noticed, handler=sibling)
    results = await asyncio.wait_for(s.emit(Noticed(note="cancel")), timeout=1.0)

    assert len(results) == 2
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert sibling_cancelled.is_set()


async def test_profile_emitter_cancellation_propagates_and_cleans_up_handler_work():
    session = Session()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(_event, _session):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    session.observe(Noticed, handler=slow)
    task = asyncio.create_task(session.emit(Noticed(note="cancel emitter")))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
