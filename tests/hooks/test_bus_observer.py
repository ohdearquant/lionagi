# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0076 — HookBus re-based onto the observer transport.

The observer is the single event *record*; HookBus is the ordered /
blocking dispatch *discipline* over it. These tests prove a bound bus
records a HookSignal per emit, reactive observers can subscribe, and the
ordered / StopHook / blocking semantics are unchanged.
"""

from __future__ import annotations

import pytest

from lionagi.hooks import (
    HookBus,
    HookPoint,
    HookSignal,
    StopHook,
    build_session_bus,
)
from lionagi.session.observer import SessionObserver

# ── Transport recording ──────────────────────────────────────────────────────


async def test_bound_emit_records_hooksignal_on_observer():
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    await bus.emit(HookPoint.MESSAGE_ADD, message={}, session_id="s")

    recs = obs.by_type(HookSignal)
    assert len(recs) == 1
    # HookPoint is a str-enum stored by value on the Signal — compare by ==.
    assert recs[0].point == HookPoint.MESSAGE_ADD
    assert recs[0].kwargs == {"message": {}, "session_id": "s"}


async def test_unbound_bus_records_nothing():
    bus = HookBus()  # no observer
    # Dispatches fine, simply records nowhere — no crash, no transport.
    await bus.emit(HookPoint.SESSION_START, session_id="s")
    assert bus._observer is None


async def test_reactive_observe_sees_emission_point_and_kwargs():
    obs = SessionObserver()
    seen: list[tuple] = []
    obs.observe(HookSignal, handler=lambda s, _c: seen.append((s.point, s.kwargs)))

    bus = HookBus(observer=obs)
    await bus.emit(HookPoint.API_POST_CALL, model="claude", tokens={"total": 9})

    assert seen == [(HookPoint.API_POST_CALL, {"model": "claude", "tokens": {"total": 9}})]


async def test_bind_and_unbind():
    obs = SessionObserver()
    bus = HookBus().bind(obs)
    await bus.emit(HookPoint.API_POST_CALL, model="x")
    assert len(obs.by_type(HookSignal)) == 1

    bus.bind(None)  # unbind — subsequent emits record nowhere
    await bus.emit(HookPoint.API_POST_CALL, model="y")
    assert len(obs.by_type(HookSignal)) == 1


# ── Dispatch discipline is preserved when bound ──────────────────────────────


async def test_ordered_dispatch_unchanged_when_bound():
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    calls: list[str] = []

    async def h1(**kw):
        calls.append("h1")

    async def h2(**kw):
        calls.append("h2")

    bus.on(HookPoint.SESSION_START, h1)
    bus.on(HookPoint.SESSION_START, h2)
    await bus.emit(HookPoint.SESSION_START, session_id="s")

    assert calls == ["h1", "h2"]  # registration order
    assert len(obs.by_type(HookSignal)) == 1


async def test_stop_hook_short_circuits_yet_still_records():
    obs = SessionObserver()
    bus = HookBus(observer=obs)
    calls: list[str] = []

    async def stopper(**kw):
        calls.append("stopper")
        raise StopHook

    async def never(**kw):  # pragma: no cover
        calls.append("never")

    bus.on(HookPoint.MESSAGE_ADD, stopper)
    bus.on(HookPoint.MESSAGE_ADD, never)
    await bus.emit(HookPoint.MESSAGE_ADD, message={}, session_id="s")

    assert calls == ["stopper"]  # short-circuit intact
    assert len(obs.by_type(HookSignal)) == 1  # a short-circuited emit is still recorded


async def test_blocking_guard_pass_records_but_raise_does_not():
    obs = SessionObserver()
    bus = HookBus(observer=obs)

    async def guard_ok(**kw):
        return None

    bus.on(HookPoint.TOOL_PRE, guard_ok)
    await bus.emit(HookPoint.TOOL_PRE, tool_name="ls")
    assert len(obs.by_type(HookSignal)) == 1  # passed guard → recorded

    bus.off(HookPoint.TOOL_PRE, guard_ok)

    async def guard_block(**kw):
        raise PermissionError("denied")

    bus.on(HookPoint.TOOL_PRE, guard_block)
    with pytest.raises(PermissionError, match="denied"):
        await bus.emit(HookPoint.TOOL_PRE, tool_name="rm")
    # Blocking raise propagates and is NOT recorded — deny-audit arrives with
    # the real pre-invoke gate (ADR-0076 Follow-up 1).
    assert len(obs.by_type(HookSignal)) == 1


# ── Transport isolation ──────────────────────────────────────────────────────


async def test_transport_failure_never_breaks_dispatch():
    class BrokenObserver:
        async def emit(self, *_a, **_k):
            raise RuntimeError("transport down")

    bus = HookBus(observer=BrokenObserver())
    calls: list[int] = []

    async def h(**kw):
        calls.append(1)

    bus.on(HookPoint.SESSION_START, h)
    # The broken transport must not turn a successful dispatch into a failure.
    await bus.emit(HookPoint.SESSION_START, session_id="s")
    assert calls == [1]


# ── build_session_bus binds the observer ─────────────────────────────────────


async def test_build_session_bus_binds_observer():
    obs = SessionObserver()
    bus = build_session_bus(observer=obs)
    # API_POST_CALL has no default handler, so this records without firing any
    # StateDB-touching builtin.
    await bus.emit(HookPoint.API_POST_CALL, model="x")
    assert any(r.point == HookPoint.API_POST_CALL for r in obs.by_type(HookSignal))
