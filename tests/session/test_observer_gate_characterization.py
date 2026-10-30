# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterization of SessionObserver.emit / authorize and Branch.authorize.

Pins today's order of work in ``emit`` (gate, flow append, routes, subscribers),
what a denial skips, how sync and async subscribers are run, and what happens
when a gate, route or subscriber raises or the emitter is cancelled.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from lionagi.protocols.generic.event import Event
from lionagi.session.branch import Branch
from lionagi.session.observer import SessionObserver
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied, Signal


class Noticed(Event):
    note: str = ""


class _Action:
    def __init__(self, function: str) -> None:
        self.function = function


class _RecordingDb:
    """Stand-in for the StateDB handle that bind_db_persistence writes to."""

    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict] = []
        self.fail = fail

    # The real StateDB.insert_session_signal signature, so a call the real store
    # would refuse with TypeError is refused here too, not recorded.
    async def insert_session_signal(
        self,
        *,
        session_id: str,
        kind: str,
        op_id: str = "",
        ts: float,
        payload: dict,
    ) -> int:
        if self.fail:
            raise RuntimeError("db down")
        self.rows.append(
            {
                "session_id": session_id,
                "kind": kind,
                "op_id": op_id,
                "ts": ts,
                "payload": payload,
            }
        )
        return len(self.rows)


# Bounds a wait that only a deadlock or a swallowed cancellation would exhaust;
# generous so a loaded host is never mistaken for either.
_STALL_TIMEOUT = 10.0


def _flow_ids(obs: SessionObserver) -> list:
    return [item.id for item in obs.flow.items]


async def _cancelled_outcome(task: asyncio.Task) -> None:
    """Wait (bounded) for a cancelled emitter task and re-raise its outcome."""
    done, _ = await asyncio.wait({task}, timeout=_STALL_TIMEOUT)
    assert task in done, "the cancelled emitter did not finish"
    task.result()


# gate runs first


async def test_gate_runs_before_flow_append_routes_and_subscribers():
    obs = SessionObserver()
    order: list[str] = []
    flow_size_at_gate: list[int] = []

    def gate(_payload):
        order.append("gate")
        flow_size_at_gate.append(len(obs.flow.items))
        return True

    obs.gate(gate)
    obs.route(lambda _p: order.append("route") or True, into="s")
    obs.observe(Noticed, handler=lambda _e, _c: order.append("subscriber"))

    await obs.emit(Noticed(note="x"))

    assert order == ["gate", "route", "subscriber"]
    assert flow_size_at_gate == [0]  # the event is appended after the verdict


async def test_async_gate_is_awaited_before_routes_and_subscribers():
    obs = SessionObserver()
    order: list[str] = []

    async def gate(_payload):
        order.append("gate-start")
        await asyncio.sleep(0)
        order.append("gate-end")
        return True

    obs.gate(gate)
    obs.route(lambda _p: order.append("route") or True, into="s")
    obs.observe(Noticed, handler=lambda _e, _c: order.append("subscriber"))

    await obs.emit(Noticed(note="x"))

    assert order == ["gate-start", "gate-end", "route", "subscriber"]


async def test_gate_is_called_once_per_emit_with_the_unwrapped_payload():
    obs = SessionObserver()
    seen: list = []
    obs.gate(lambda payload: seen.append(payload) or True)

    event = Noticed(note="plain event")
    await obs.emit(event)
    await obs.emit(Signal(data="wrapped"))
    await obs.emit("raw string")

    assert seen[0] is event  # a non-Signal Observable is its own payload
    assert seen[1:] == ["wrapped", "raw string"]


@pytest.mark.parametrize(
    "verdict", [None, 0, "", False], ids=["None", "zero", "empty-str", "False"]
)
async def test_false_or_non_boolean_gate_verdict_denies(verdict):
    obs = SessionObserver()
    fired: list = []
    obs.observe(Noticed, handler=lambda e, _c: fired.append(e))
    obs.gate(lambda _p: verdict)

    assert await obs.emit(Noticed(note="x")) == []
    assert fired == []


@pytest.mark.parametrize("verdict", [1, "yes", object()], ids=["one", "str", "object"])
async def test_truthy_non_boolean_gate_verdict_denies(verdict):
    obs = SessionObserver()
    fired: list = []
    obs.observe(Noticed, handler=lambda e, _c: fired.append(e))
    obs.gate(lambda _p: verdict)

    assert await obs.emit(Noticed(note="x")) == []
    assert fired == []
    assert len(obs.flow.items) == 1


async def test_true_gate_verdict_allows():
    obs = SessionObserver()
    fired: list = []
    obs.observe(Noticed, handler=lambda e, _c: fired.append(e))
    obs.gate(lambda _p: True)

    await obs.emit(Noticed(note="x"))
    assert len(fired) == 1


async def test_async_gate_raising_denies_without_propagating():
    obs = SessionObserver()
    fired: list = []
    obs.observe(Noticed, handler=lambda e, _c: fired.append(e))

    async def broken(_payload):
        raise RuntimeError("policy engine down")

    obs.gate(broken)

    assert await obs.emit(Noticed(note="x")) == []
    assert fired == []
    assert len(obs.flow.items) == 1


async def test_gate_can_be_cleared_to_allow_dispatch_again():
    obs = SessionObserver()
    fired: list = []
    obs.observe(Noticed, handler=lambda e, _c: fired.append(e.note))
    obs.gate(lambda _p: False)
    await obs.emit(Noticed(note="denied"))
    obs.clear_gate()
    await obs.emit(Noticed(note="allowed"))

    assert fired == ["allowed"]


async def test_gate_none_is_rejected_and_keeps_the_previous_gate():
    obs = SessionObserver()
    fired: list = []
    obs.observe(Noticed, handler=lambda e, _c: fired.append(e))
    obs.gate(lambda _p: False)

    with pytest.raises(TypeError, match="clear_gate"):
        obs.gate(None)
    await obs.emit(Noticed(note="x"))

    assert fired == []


# the event is appended whatever the verdict


async def test_event_is_appended_to_flow_on_allow_and_on_deny_in_order():
    obs = SessionObserver()
    obs.gate(lambda e: e.note != "drop")
    first, second, third = Noticed(note="keep"), Noticed(note="drop"), Noticed(note="keep")

    await obs.emit(first)
    await obs.emit(second)
    await obs.emit(third)

    assert _flow_ids(obs) == [first.id, second.id, third.id]


async def test_non_observable_event_is_wrapped_in_a_signal_on_allow_and_deny():
    obs = SessionObserver()
    obs.gate(lambda payload: payload != "drop")

    await obs.emit("keep")
    await obs.emit("drop")

    items = list(obs.flow.items)
    assert [type(i) for i in items] == [Signal, Signal]
    assert [i.data for i in items] == ["keep", "drop"]


async def test_denied_emit_records_the_event_but_no_gate_denied_signal():
    obs = SessionObserver()
    obs.gate(lambda _p: False)

    await obs.emit(Noticed(note="x"))

    assert len(obs.flow.items) == 1
    assert obs.by_type(GateDenied) == []


async def test_by_type_unwraps_signal_payloads_for_denied_events_too():
    obs = SessionObserver()
    obs.gate(lambda _p: False)

    await obs.emit(Signal(data=Noticed(note="inside")))

    assert len(obs.by_type(Noticed)) == 1


# denial skips routes and subscribers, so bound persistence never sees the event


async def test_denial_returns_empty_and_skips_route_conditions_and_subscribers():
    obs = SessionObserver()
    route_calls: list = []
    subscriber_calls: list = []
    obs.route(lambda p: route_calls.append(p) or True, into="audit")
    obs.observe(Noticed, handler=lambda e, _c: subscriber_calls.append(e))
    obs.gate(lambda _p: False)

    assert await obs.emit(Noticed(note="x")) == []

    assert route_calls == []  # the condition itself is never evaluated
    assert subscriber_calls == []
    assert obs.stream("audit") == []


async def test_allowed_event_is_routed_to_named_streams_and_dispatched():
    obs = SessionObserver()
    route_payloads: list = []
    obs.route(lambda p: route_payloads.append(p) or True, into="all")
    obs.route(lambda p: p.note == "other", into="other")
    obs.observe(Noticed, handler=lambda e, _c: e.note)
    event = Noticed(note="x")

    assert await obs.emit(event) == ["x"]

    assert route_payloads == [event]
    assert [e.id for e in obs.stream("all")] == [event.id]
    assert obs.stream("other") == []


async def test_route_stream_holds_the_envelope_while_condition_sees_the_payload():
    obs = SessionObserver()
    seen: list = []
    obs.route(lambda p: seen.append(p) or True, into="s")
    envelope = Signal(data="payload")

    await obs.emit(envelope)

    assert seen == ["payload"]
    assert [e.id for e in obs.stream("s")] == [envelope.id]


async def test_bound_persistence_sees_allowed_events_and_never_denied_ones():
    obs = SessionObserver()
    db = _RecordingDb()
    obs.bind_db_persistence("sess-1", db=db)
    obs.gate(lambda payload: payload != "drop")

    await obs.emit(Signal(data="keep"))
    await obs.emit(Signal(data="drop"))
    obs.clear_gate()
    await obs.emit(Signal(data="again"))

    assert [r["payload"]["data"] for r in db.rows] == ["keep", "again"]
    assert {r["session_id"] for r in db.rows} == {"sess-1"}
    assert {r["kind"] for r in db.rows} == {"Signal"}
    assert len(obs.flow.items) == 3  # the flow still holds the denied one


async def test_denial_skips_routes_subscribers_and_persistence_together():
    obs = SessionObserver()
    db = _RecordingDb()
    subscriber_calls: list = []
    obs.bind_db_persistence("sess-1", db=db)
    obs.route(lambda _p: True, into="s")
    obs.observe(Signal, handler=lambda e, _c: subscriber_calls.append(e))
    obs.gate(lambda _p: False)

    await obs.emit(Signal(data="x"))

    assert db.rows == []
    assert subscriber_calls == []
    assert obs.stream("s") == []
    assert len(obs.flow.items) == 1


async def test_bound_persistence_ignores_events_that_are_not_signals():
    obs = SessionObserver()
    db = _RecordingDb()
    obs.bind_db_persistence("sess-1", db=db)

    await obs.emit(Noticed(note="x"))

    assert db.rows == []


async def test_persistence_failure_is_swallowed_and_later_subscribers_still_run():
    obs = SessionObserver()
    db = _RecordingDb(fail=True)
    obs.bind_db_persistence("sess-1", db=db)
    obs.observe(Signal, handler=lambda e, _c: "after")

    async def later_async(_e, _c):
        # Gathered beside the persistence call: an escaping store error would
        # cancel this and replace the results below.
        await asyncio.sleep(0)
        return "after-async"

    obs.observe(Signal, handler=later_async)

    assert await obs.emit(Signal(data="x")) == ["after", None, "after-async"]


async def test_unbound_persistence_skips_a_store_known_to_be_absent(monkeypatch):
    import lionagi.state.db as state_db

    opened: list = []
    monkeypatch.setattr(state_db, "state_db_known_absent", lambda: True)
    monkeypatch.setattr(state_db, "StateDB", lambda: opened.append(1))
    obs = SessionObserver()
    obs.bind_db_persistence("sess-1")

    assert await obs.emit(Signal(data="x")) == [None]
    assert opened == []


async def test_unbound_persistence_opens_a_fresh_store_per_signal(monkeypatch):
    import lionagi.state.db as state_db

    db = _RecordingDb()
    entered: list[str] = []

    class _FreshStore:
        async def __aenter__(self):
            entered.append("enter")
            return db

        async def __aexit__(self, *exc):
            entered.append("exit")

    monkeypatch.setattr(state_db, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(state_db, "StateDB", _FreshStore)
    obs = SessionObserver()
    obs.bind_db_persistence("sess-1")

    await obs.emit(Signal(data="x"))
    await obs.emit(Signal(data="y"))

    assert entered == ["enter", "exit", "enter", "exit"]
    assert [r["session_id"] for r in db.rows] == ["sess-1", "sess-1"]
    assert [r["payload"]["data"] for r in db.rows] == ["x", "y"]


async def test_unbind_removes_persistence_but_not_other_subscribers():
    obs = SessionObserver()
    db = _RecordingDb()
    obs.bind_db_persistence("sess-1", db=db)
    obs.observe(Signal, handler=lambda e, _c: "other")

    obs.unbind_db_persistence()
    assert await obs.emit(Signal(data="x")) == ["other"]
    assert db.rows == []


# sync callbacks run inline, then the returned awaitables are gathered


async def test_coroutine_body_starts_only_after_every_sync_subscriber_ran():
    obs = SessionObserver()
    order: list[str] = []

    async def first(_event, _ctx):
        order.append("async-body")
        return "async"

    def second(_event, _ctx):
        order.append("sync")
        return "sync"

    obs.observe(Noticed, handler=first)
    obs.observe(Noticed, handler=second)

    assert await obs.emit(Noticed(note="x")) == ["sync", "async"]
    assert order == ["sync", "async-body"]


async def test_returned_awaitables_are_gathered_concurrently():
    obs = SessionObserver()
    a_started, b_started = asyncio.Event(), asyncio.Event()

    async def a(_event, _ctx):
        a_started.set()
        await b_started.wait()
        return "a"

    async def b(_event, _ctx):
        b_started.set()
        await a_started.wait()
        return "b"

    obs.observe(Noticed, handler=a)
    obs.observe(Noticed, handler=b)

    # sequential awaiting would deadlock: each waits for the other to start
    results = await asyncio.wait_for(obs.emit(Noticed(note="x")), timeout=_STALL_TIMEOUT)
    assert results == ["a", "b"]


async def test_results_are_sync_first_then_async_in_registration_order():
    obs = SessionObserver()

    async def a1(_e, _c):
        return "a1"

    async def a2(_e, _c):
        return "a2"

    obs.observe(Noticed, handler=a1)
    obs.observe(Noticed, handler=lambda _e, _c: "s1")
    obs.observe(Noticed, handler=a2)
    obs.observe(Noticed, handler=lambda _e, _c: "s2")

    assert await obs.emit(Noticed(note="x")) == ["s1", "s2", "a1", "a2"]


async def test_subscriber_receives_the_session_as_context_or_the_observer_itself():
    contexts: list = []
    standalone = SessionObserver()
    standalone.observe(Noticed, handler=lambda _e, ctx: contexts.append(ctx))
    await standalone.emit(Noticed(note="x"))
    assert contexts == [standalone]

    session = Session()
    session.observe(Noticed, handler=lambda _e, ctx: contexts.append(ctx))
    await session.emit(Noticed(note="x"))
    assert contexts[1] is session


# a subscriber, route or gate that raises, and cancellation


async def test_sync_subscriber_raise_propagates_after_event_is_recorded_and_routed():
    obs = SessionObserver()
    obs.route(lambda _p: True, into="s")

    def boom(_event, _ctx):
        raise LookupError("subscriber failed")

    obs.observe(Noticed, handler=boom)
    event = Noticed(note="x")

    with pytest.raises(LookupError, match="subscriber failed"):
        await obs.emit(event)

    assert _flow_ids(obs) == [event.id]
    assert [e.id for e in obs.stream("s")] == [event.id]


async def test_route_condition_raise_propagates_after_event_is_recorded():
    obs = SessionObserver()
    event = Noticed(note="x")

    def broken(_payload):
        raise LookupError("route failed")

    obs.route(broken, into="never")

    with pytest.raises(LookupError, match="route failed"):
        await obs.emit(event)

    assert _flow_ids(obs) == [event.id]
    assert obs.stream("never") == []


async def test_sync_subscriber_raise_drops_awaitables_collected_before_it_unawaited():
    obs = SessionObserver()
    ran: list[str] = []
    collected: list = []

    async def body():
        ran.append("async-body")

    def earlier_async(_event, _ctx):
        coro = body()
        collected.append(coro)
        return coro

    def boom(_event, _ctx):
        raise LookupError("subscriber failed")

    obs.observe(Noticed, handler=earlier_async)
    obs.observe(Noticed, handler=boom)

    with pytest.raises(LookupError, match="subscriber failed"):
        await obs.emit(Noticed(note="x"))

    try:
        assert ran == []
        assert inspect.getcoroutinestate(collected[0]) == inspect.CORO_CREATED
    finally:
        collected[0].close()


async def test_async_subscriber_raise_cancels_running_siblings_after_sync_subscribers_ran():
    obs = SessionObserver()
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    sync_calls: list = []

    async def failing(_event, _ctx):
        await sibling_started.wait()
        raise ValueError("async failed")

    async def sibling(_event, _ctx):
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    obs.observe(Noticed, handler=lambda e, _c: sync_calls.append(e))
    obs.observe(Noticed, handler=failing)
    obs.observe(Noticed, handler=sibling)
    event = Noticed(note="x")

    with pytest.raises(ValueError, match="async failed"):
        await asyncio.wait_for(obs.emit(event), timeout=_STALL_TIMEOUT)

    assert sibling_cancelled.is_set()
    assert sync_calls == [event]
    assert _flow_ids(obs) == [event.id]


async def test_branch_emit_propagates_a_subscriber_failure_but_safe_emit_does_not():
    session = Session()

    def boom(_event, _ctx):
        raise LookupError("subscriber failed")

    session.observe(Noticed, handler=boom)
    branch = session.default_branch

    with pytest.raises(LookupError, match="subscriber failed"):
        await branch.emit(Noticed(note="x"))

    assert await branch._safe_emit(Noticed(note="y")) is None
    assert len(session.observer.by_type(Noticed)) == 2


async def test_emitter_cancelled_while_gate_is_pending_records_nothing_and_dispatches_nothing():
    obs = SessionObserver()
    started = asyncio.Event()
    fired: list = []
    obs.observe(Noticed, handler=lambda e, _c: fired.append(e))

    async def slow_gate(_payload):
        started.set()
        await asyncio.Event().wait()

    obs.gate(slow_gate)
    task = asyncio.create_task(obs.emit(Noticed(note="x")))
    await asyncio.wait_for(started.wait(), timeout=_STALL_TIMEOUT)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await _cancelled_outcome(task)
    assert len(obs.flow.items) == 0  # cancellation is not read as a denial
    assert fired == []


async def test_gate_raising_cancelled_error_propagates_instead_of_denying():
    obs = SessionObserver()

    def gate(_payload):
        raise asyncio.CancelledError("gate cancelled")

    obs.gate(gate)

    with pytest.raises(asyncio.CancelledError):
        await obs.emit(Noticed(note="x"))
    assert len(obs.flow.items) == 0


async def test_emitter_cancelled_during_gather_keeps_event_in_flow_and_stream():
    obs = SessionObserver()
    started = asyncio.Event()
    obs.route(lambda _p: True, into="s")

    async def slow(_event, _ctx):
        started.set()
        await asyncio.Event().wait()

    obs.observe(Noticed, handler=slow)
    event = Noticed(note="x")
    task = asyncio.create_task(obs.emit(event))
    await asyncio.wait_for(started.wait(), timeout=_STALL_TIMEOUT)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await _cancelled_outcome(task)
    assert _flow_ids(obs) == [event.id]
    assert [e.id for e in obs.stream("s")] == [event.id]


# authorize and Branch.authorize


async def test_authorize_without_gate_allows_and_records_nothing():
    obs = SessionObserver()

    assert await obs.authorize(_Action("rm")) is True
    assert len(obs.flow.items) == 0


async def test_authorize_passes_the_action_itself_to_the_gate():
    obs = SessionObserver()
    seen: list = []
    obs.gate(lambda action: seen.append(action) or True)
    action = _Action("ls")

    await obs.authorize(action)

    assert seen == [action]


async def test_authorize_denial_appends_redacted_gate_denied():
    obs = SessionObserver()
    obs.gate(lambda action: action.function != "rm")
    denied, allowed = _Action("rm"), _Action("ls")

    assert await obs.authorize(denied) is False
    assert await obs.authorize(allowed) is True

    items = list(obs.flow.items)
    assert [type(i) for i in items] == [GateDenied]
    assert items[0].data is None


@pytest.mark.parametrize(
    "raises", [RuntimeError("down"), ValueError("bad")], ids=["RuntimeError", "ValueError"]
)
async def test_authorize_raising_gate_denies_and_records_gate_denied(raises):
    obs = SessionObserver()

    async def broken(_action):
        raise raises

    obs.gate(broken)

    assert await obs.authorize(_Action("x")) is False
    assert len(obs.by_type(GateDenied)) == 1


async def test_authorize_cancelled_gate_propagates_and_records_nothing():
    obs = SessionObserver()

    def gate(_action):
        raise asyncio.CancelledError("gate cancelled")

    obs.gate(gate)

    with pytest.raises(asyncio.CancelledError):
        await obs.authorize(_Action("x"))
    assert len(obs.flow.items) == 0


async def test_authorize_denial_is_routed_dispatched_and_persisted():
    obs = SessionObserver()
    db = _RecordingDb()
    subscriber_calls: list = []
    route_calls: list = []
    obs.bind_db_persistence("sess-1", db=db)
    obs.observe(GateDenied, handler=lambda e, _c: subscriber_calls.append(e))
    obs.route(lambda p: route_calls.append(p) or True, into="s")
    gate_calls: list = []
    obs.gate(lambda a: gate_calls.append(a) or False)
    action = _Action("x")

    assert await obs.authorize(action) is False

    # The denial is published without re-evaluating the gate.
    assert gate_calls == [action]
    assert len(obs.by_type(GateDenied)) == 1
    assert subscriber_calls == obs.by_type(GateDenied)
    assert route_calls == [None]
    assert len(db.rows) == 1
    assert db.rows[0]["kind"] == "GateDenied"
    assert "data" not in db.rows[0]["payload"]


async def test_authorize_after_clear_gate_allows_again():
    obs = SessionObserver()
    obs.gate(lambda _a: False)
    assert await obs.authorize(_Action("x")) is False
    obs.clear_gate()
    assert await obs.authorize(_Action("x")) is True
    assert len(obs.by_type(GateDenied)) == 1


async def test_session_authorize_and_branch_authorize_share_the_observer_gate():
    session = Session()
    session.gate(lambda action: action.function != "rm")
    branch = session.default_branch

    assert await branch.authorize(_Action("rm")) is False
    assert await session.authorize(_Action("rm")) is False
    assert await branch.authorize(_Action("ls")) is True
    assert len(session.observer.by_type(GateDenied)) == 2


async def test_standalone_branch_authorize_allows_without_an_observer():
    assert await Branch().authorize(_Action("rm")) is True
