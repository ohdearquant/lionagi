# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Session gates honor typed verdicts and retain legacy truthiness."""

import asyncio

import pytest

from lionagi.protocols.action.gate import GateResult, adapt_session_gate
from lionagi.session.control import ToolInvocation
from lionagi.session.observer import SessionObserver
from lionagi.session.signal import GateDenied, Signal


class TruthyDecision:
    def __bool__(self):
        return True


class BrokenTruthiness:
    def __bool__(self):
        raise RuntimeError("cannot evaluate verdict")


def _gate(value, calls, is_async):
    def check(payload):
        calls.append(payload)
        return value

    async def async_check(payload):
        return check(payload)

    return async_check if is_async else check


INVALID_VERDICTS = [
    pytest.param(
        GateResult("yes", "policy", "tool", "authorize", "reason"),
        "allowed",
        "str",
        id="allowed-string",
    ),
    pytest.param(
        GateResult(1, "policy", "tool", "authorize", "reason"), "allowed", "int", id="allowed-int"
    ),
    pytest.param(
        GateResult(TruthyDecision(), "policy", "tool", "authorize", "reason"),
        "allowed",
        "TruthyDecision",
        id="allowed-object",
    ),
    pytest.param(
        GateResult(True, "policy", "tool", "authorize", "reason", errored=None),
        "errored",
        "NoneType",
        id="errored-none",
    ),
    pytest.param(
        GateResult(True, "policy", "tool", "authorize", "reason", errored=0),
        "errored",
        "int",
        id="errored-int",
    ),
    pytest.param(
        GateResult(True, "policy", "tool", "authorize", "reason", errored="fault"),
        "errored",
        "str",
        id="errored-string",
    ),
    pytest.param(
        GateResult(True, "policy", "tool", "authorize", "reason", errored=True),
        "allowed",
        "errored",
        id="contradiction",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("allowed,errored", [(True, False), (False, False), (False, True)])
async def test_adapter_preserves_typed_verdict(is_async, allowed, errored):
    action = ToolInvocation(function="tool", arguments={"x": 1})
    rewritten = None if allowed else {"x": 2}
    verdict = GateResult(allowed, "policy", "tool", "authorize", "reason", rewritten, errored)
    calls = []

    result = await adapt_session_gate(_gate(verdict, calls, is_async))(action)

    assert calls == [action]
    assert result == verdict


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("equal", [False, True], ids=["different", "equal"])
async def test_adapter_rejects_unsupported_argument_rewrite(is_async, equal):
    action = ToolInvocation(function="tool", arguments={"x": 1})
    rewritten = {"x": 1} if equal else {"x": 2}
    verdict = GateResult(True, "policy", "tool", "authorize", "reason", rewritten)
    calls = []

    result = await adapt_session_gate(_gate(verdict, calls, is_async))(action)

    assert calls == [action]
    assert result.allowed is False
    assert result.errored is True
    assert "rewrite" in result.reason.lower()
    assert any(
        phrase in result.reason.lower()
        for phrase in ("not supported", "unsupported", "does not apply")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("verdict,field,detail", INVALID_VERDICTS)
async def test_adapter_refuses_invalid_typed_decision(is_async, verdict, field, detail):
    action = ToolInvocation(function="tool", arguments={})
    calls = []

    result = await adapt_session_gate(_gate(verdict, calls, is_async))(action)

    assert calls == [action]
    assert result.allowed is False
    assert result.errored is True
    assert field in result.reason.lower()
    assert detail in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("allowed", [False, True], ids=["deny", "allow"])
async def test_observer_honors_typed_verdict_on_both_paths(is_async, allowed):
    observer = SessionObserver()
    action = ToolInvocation(function="tool", arguments={})
    event = Signal(data="event-data")
    calls, dispatched, routed = [], [], []
    observer.gate(
        _gate(GateResult(allowed, "policy", "tool", "authorize", "reason"), calls, is_async)
    )
    observer.observe(Signal, handler=lambda item, _ctx: dispatched.append(item) or "delivered")
    observer.route(lambda payload: routed.append(payload) or True, into="selected")

    assert await observer.authorize(action) is allowed
    assert [item.data for item in observer.by_type(GateDenied)] == ([] if allowed else [action])
    assert await observer.emit(event) == (["delivered"] if allowed else [])
    assert calls == [action, "event-data"]
    assert dispatched == ([event] if allowed else [])
    assert routed == (["event-data"] if allowed else [])
    assert observer.stream("selected") == ([event] if allowed else [])
    assert event in list(observer.flow.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("verdict,field,detail", INVALID_VERDICTS)
async def test_invalid_typed_verdict_never_routes_or_dispatches(is_async, verdict, field, detail):
    observer = SessionObserver()
    event = Signal(data="event-data")
    calls, dispatched, routed = [], [], []
    observer.gate(_gate(verdict, calls, is_async))
    observer.observe(Signal, handler=lambda item, _ctx: dispatched.append(item))
    observer.route(lambda payload: routed.append(payload) or True, into="selected")

    assert await observer.emit(event) == []
    assert calls == ["event-data"]
    assert dispatched == routed == []
    assert observer.stream("selected") == []
    assert list(observer.flow.items) == [event]
    assert observer.by_type(GateDenied) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "value,allowed",
    [
        (True, True),
        ("legacy", True),
        (1, True),
        ({"allowed": True}, True),
        (False, False),
        (None, False),
        (0, False),
        ("", False),
        ([], False),
        ({}, False),
    ],
)
async def test_legacy_truthiness_on_both_paths(is_async, value, allowed):
    observer = SessionObserver()
    calls, dispatched = [], []
    action = ToolInvocation(function="tool", arguments={})
    event = Signal(data="event-data")
    observer.gate(_gate(value, calls, is_async))
    observer.observe(Signal, handler=lambda item, _ctx: dispatched.append(item) or "delivered")

    assert await observer.authorize(action) is allowed
    assert [item.data for item in observer.by_type(GateDenied)] == ([] if allowed else [action])
    assert await observer.emit(event) == (["delivered"] if allowed else [])
    assert dispatched == ([event] if allowed else [])
    assert event in list(observer.flow.items)
    assert calls == [action, "event-data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_legacy_truthiness_error_fails_closed(is_async):
    observer = SessionObserver()
    action = ToolInvocation(function="tool", arguments={})
    event = Signal(data="event-data")
    calls, dispatched = [], []
    observer.gate(_gate(BrokenTruthiness(), calls, is_async))
    observer.observe(Signal, handler=lambda item, _ctx: dispatched.append(item))

    assert await observer.authorize(action) is False
    assert [item.data for item in observer.by_type(GateDenied)] == [action]
    assert await observer.emit(event) == []
    assert dispatched == []
    assert event in list(observer.flow.items)
    assert calls == [action, "event-data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["adapter", "authorize", "emit"])
async def test_gate_cancellation_propagates_without_recording(path):
    async def cancel(_payload):
        raise asyncio.CancelledError()

    action = ToolInvocation(function="tool", arguments={})
    observer = SessionObserver()
    observer.gate(cancel)
    with pytest.raises(asyncio.CancelledError):
        if path == "adapter":
            await adapt_session_gate(cancel)(action)
        elif path == "authorize":
            await observer.authorize(action)
        else:
            await observer.emit(Signal(data="event-data"))
    assert list(observer.flow.items) == []
