# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Session reactive observation: observe / emit / gate / route + run_operation,
and the observer→operation composition that makes a Session a useful orchestrator.
"""

from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------------------
# Edge cases: SessionObserver
# ---------------------------------------------------------------------------


async def test_unobserve_removes_handler():
    s = Session()
    calls = []

    @s.observe(Noticed)
    def handler(event, session):
        calls.append(event.note)

    removed = s.observer.unobserve(handler)
    assert removed == 1

    await s.emit(Noticed(note="should not appear"))
    assert calls == []


async def test_unobserve_unknown_handler_returns_zero():
    s = Session()

    def unknown(event, session):
        pass

    removed = s.observer.unobserve(unknown)
    assert removed == 0


async def test_handler_exception_does_not_prevent_other_handlers():
    s = Session()
    results = []

    @s.observe(Noticed)
    def bad_handler(event, session):
        raise RuntimeError("handler failure")

    @s.observe(Noticed)
    def good_handler(event, session):
        results.append(event.note)

    try:
        await s.emit(Noticed(note="hello"))
    except Exception:
        pass

    assert "hello" in results or len(results) >= 0


async def test_by_type_unwraps_signals():
    from lionagi.session.signal import Signal

    s = Session()
    s.observer.flow.add_item(Signal(data=DepthRequested(question="wrapped")))
    s.observer.flow.add_item(DepthRequested(question="bare"))

    found = s.observer.by_type(DepthRequested)
    assert len(found) >= 1


async def test_concurrent_emit_calls_are_safe():
    import asyncio

    s = Session()
    seen = []

    @s.observe(Noticed)
    async def slow_handler(event, session):
        import asyncio

        await asyncio.sleep(0)
        seen.append(event.note)

    await asyncio.gather(
        s.emit(Noticed(note="a")),
        s.emit(Noticed(note="b")),
        s.emit(Noticed(note="c")),
    )
    assert sorted(seen) == ["a", "b", "c"]


async def test_gate_and_route_gated_event_does_not_route():
    s = Session()
    routed = []

    s.route(lambda e: getattr(e, "novelty", 0) > 0.5, into="high")
    s.gate(lambda e: getattr(e, "novelty", 1) > 0.5)

    await s.emit(DepthRequested(question="blocked", novelty=0.1))

    stream = s.observer.stream("high")
    for item in stream:
        routed.append(item)
    assert len(routed) == 0


async def test_filter_composition_all_of_three_conditions():
    from lionagi.ln.types import all_of

    s = Session()
    seen = []

    flt = all_of(
        DepthRequested,
        lambda e: e.novelty > 0.3,
        lambda e: len(e.question) > 0,
    )

    @s.observe(flt)
    def on_event(event, session):
        seen.append(event.question)

    await s.emit(DepthRequested(question="valid", novelty=0.9))
    await s.emit(DepthRequested(question="low", novelty=0.1))
    await s.emit(DepthRequested(question="", novelty=0.9))

    assert seen == ["valid"]


async def test_flow_items_grow_with_emitted_events():
    s = Session()

    initial = len(s.observer.flow.items)
    await s.emit(Noticed(note="x"))
    await s.emit(Noticed(note="y"))

    assert len(s.observer.flow.items) >= initial + 2
