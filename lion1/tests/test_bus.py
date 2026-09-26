import asyncio
import dataclasses
import time

import pytest

from lionagi.bus import Bus, Event


class HandlerFailure(Exception):
    pass


def test_an_event_carries_its_kind_payload_and_the_time_it_was_made():
    before = time.time()
    e = Event("run.started")
    after = time.time()
    assert e.kind == "run.started"
    assert e.payload == {}
    assert before <= e.at <= after
    assert Event("a").payload is not Event("b").payload
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.kind = "run.completed"


@pytest.mark.parametrize(
    "pattern, kind, expected",
    [
        ("*", "run.started", True),
        ("*", "command.settled", True),
        ("run.started", "run.started", True),
        ("run.started", "run.start", False),
        ("run.start", "run.started", False),
        ("run.", "run.started", True),
        ("run.", "run", False),
        ("run", "run.started", False),
        ("run.*", "run.started", False),
        ("command.", "commander.x", False),
        ("round.", "run.started", False),
        ("*.started", "run.started", False),
    ],
)
def test_a_pattern_matches_exactly_by_a_prefix_ending_in_a_dot_or_everything(pattern, kind, expected):
    assert Bus.matches(pattern, kind) is expected


def test_emit_records_the_event_and_calls_each_matching_handler_in_subscription_order():
    bus = Bus()
    seen = []
    bus.subscribe("command.", lambda e: seen.append(("prefix", e)))
    bus.subscribe("run.started", lambda e: seen.append(("exact", e)))
    bus.subscribe("*", lambda e: seen.append(("all", e)))
    started = bus.emit("run.started", run_id="r1", budget=3)
    assert started.kind == "run.started"
    assert started.payload == {"run_id": "r1", "budget": 3}
    assert seen == [("exact", started), ("all", started)]
    assert all(e is started for _, e in seen)
    settled = bus.emit("command.settled")
    assert seen[2:] == [("prefix", settled), ("all", settled)]
    assert bus.log == [started, settled]
    assert bus.errors == [] and bus.tasks == set()


def test_on_subscribes_the_function_it_decorates_and_returns_it_unchanged():
    bus = Bus()
    seen = []

    def handler(e):
        seen.append(e.kind)

    assert bus.on("inbox.")(handler) is handler
    bus.emit("inbox.queued")
    bus.emit("run.started")
    assert seen == ["inbox.queued"]


def test_unsubscribe_stops_delivery_and_a_second_call_changes_nothing():
    bus = Bus()
    seen, others = [], []
    unsubscribe = bus.subscribe("*", lambda e: seen.append(e.kind))
    bus.subscribe("*", lambda e: others.append(e.kind))
    bus.emit("a")
    unsubscribe()
    bus.emit("b")
    unsubscribe()
    bus.emit("c")
    assert seen == ["a"]
    assert others == ["a", "b", "c"]


def test_a_handler_subscribed_during_an_emit_first_hears_the_next_event():
    bus = Bus()
    seen = []

    def late(e):
        seen.append(e.kind)

    def adds_late(e):
        if e.kind == "first":
            bus.subscribe("*", late)

    bus.subscribe("*", adds_late)
    bus.emit("first")
    bus.emit("second")
    assert seen == ["second"]


def test_a_raising_handler_is_recorded_and_never_stops_the_emitter_or_the_handlers_after_it():
    bus = Bus()
    seen = []
    boom = HandlerFailure("boom")

    def bad(e):
        raise boom

    bus.subscribe("*", bad)
    bus.subscribe("*", lambda e: seen.append(e.kind))
    event = bus.emit("command.requested")
    assert seen == ["command.requested"]
    assert bus.errors == [(event, bad, boom)]
    assert bus.log == [event]


def test_a_return_value_that_is_not_awaitable_is_ignored():
    bus = Bus()
    bus.subscribe("*", lambda e: 42)
    bus.emit("x")
    assert bus.tasks == set() and bus.errors == []


def test_emit_returns_before_a_coroutine_handler_starts_and_drain_runs_it():
    async def main():
        bus = Bus()
        started = []

        async def handler(e):
            started.append(e.kind)

        bus.subscribe("*", handler)
        event = bus.emit("command.requested")
        assert started == []
        assert len(bus.tasks) == 1
        await bus.drain()
        assert started == ["command.requested"]
        assert bus.tasks == set()
        assert bus.errors == []
        assert bus.log == [event]

    asyncio.run(main())


def test_a_tracked_task_leaves_the_set_when_it_finishes():
    async def main():
        bus = Bus()
        gate = asyncio.Event()

        async def waits(e):
            await gate.wait()

        bus.subscribe("*", waits)
        bus.emit("x")
        (task,) = bus.tasks
        await asyncio.sleep(0)
        assert not task.done()
        assert bus.tasks == {task}
        gate.set()
        await task
        assert bus.tasks == set()

    asyncio.run(main())


def test_drain_also_waits_for_tasks_scheduled_while_it_waits():
    async def main():
        bus = Bus()
        order = []

        async def first(e):
            await asyncio.sleep(0)
            bus.emit("second")
            order.append("first")

        async def second(e):
            # still running when the first task finishes, so one pass over the tracked set is not enough
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            order.append("second")

        bus.subscribe("first", first)
        bus.subscribe("second", second)
        bus.emit("first")
        await bus.drain()
        assert order == ["first", "second"]
        assert bus.tasks == set()
        assert [e.kind for e in bus.log] == ["first", "second"]

    asyncio.run(main())


def test_drain_returns_when_a_tracked_task_was_cancelled_and_records_no_error():
    async def main():
        bus = Bus()
        gate = asyncio.Event()

        async def waits(e):
            await gate.wait()

        bus.subscribe("*", waits)
        bus.emit("x")
        await asyncio.sleep(0)
        (task,) = bus.tasks
        task.cancel()
        await bus.drain()
        assert task.cancelled()
        assert bus.tasks == set()
        assert bus.errors == []

    asyncio.run(main())


def test_drain_with_nothing_scheduled_returns_at_once():
    async def main():
        bus = Bus()
        await bus.drain()
        assert bus.tasks == set()

    asyncio.run(main())


def test_a_raising_coroutine_is_recorded_once_it_runs_and_never_reaches_the_emitter():
    async def main():
        bus = Bus()
        seen = []

        async def bad(e):
            raise HandlerFailure("late")

        bus.subscribe("*", bad)
        bus.subscribe("*", lambda e: seen.append(e.kind))
        event = bus.emit("command.settled")
        assert seen == ["command.settled"]
        assert bus.errors == []
        await bus.drain()
        ((got_event, got_handler, exc),) = bus.errors
        assert got_event is event and got_handler is bad
        assert type(exc) is HandlerFailure and str(exc) == "late"
        assert bus.tasks == set()

    asyncio.run(main())


def test_a_returned_future_is_tracked_like_a_coroutine():
    async def main():
        bus = Bus()
        loop = asyncio.get_running_loop()
        ok, failing = loop.create_future(), loop.create_future()
        bus.subscribe("ok", lambda e: ok)
        bus.subscribe("fail", lambda e: failing)
        bus.emit("ok")
        event = bus.emit("fail")
        assert len(bus.tasks) == 2
        ok.set_result(None)
        failing.set_exception(HandlerFailure("no"))
        await bus.drain()
        assert bus.tasks == set()
        assert [(e, str(x)) for e, _, x in bus.errors] == [(event, "no")]

    asyncio.run(main())


def test_the_log_keeps_ten_thousand_events_by_default():
    assert Bus().keep == 10_000


def test_keep_bounds_the_log_to_the_newest_events():
    bus = Bus(keep=3)
    events = [bus.emit(f"e{i}") for i in range(3)]
    assert bus.log == events
    events += [bus.emit(f"e{i}") for i in range(3, 5)]
    assert bus.log == events[2:]
    assert [e.kind for e in bus.log] == ["e2", "e3", "e4"]


def test_keep_zero_logs_nothing_and_still_delivers():
    bus = Bus(keep=0)
    seen = []
    bus.subscribe("*", lambda e: seen.append(e.kind))
    bus.emit("a")
    bus.emit("b")
    assert seen == ["a", "b"]
    assert bus.log == [] and bus.events() == []


def test_events_reads_the_log_through_a_pattern():
    bus = Bus()
    kinds = ["run.started", "command.requested", "command.settled", "inbox.queued", "run.completed"]
    for k in kinds:
        bus.emit(k)
    assert [e.kind for e in bus.events()] == kinds
    assert [e.kind for e in bus.events("*")] == kinds
    assert [e.kind for e in bus.events("command.")] == ["command.requested", "command.settled"]
    assert [e.kind for e in bus.events("run.completed")] == ["run.completed"]
    assert bus.events("round.") == []
