# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Broadcaster (lionagi/service/broadcaster.py)."""

import gc

import pytest

from lionagi.service.broadcaster import Broadcaster

# ---------------------------------------------------------------------------
# Helpers -- fresh event/broadcaster types per test to avoid cross-pollution
# ---------------------------------------------------------------------------


def _make_broadcaster():
    """Return a unique (EventType, BroadcasterSubclass) pair."""

    class _Evt:
        def __init__(self, value=None):
            self.value = value

    class _Bcst(Broadcaster):
        _event_type = _Evt

    return _Evt, _Bcst


# ---------------------------------------------------------------------------
# Subclass / _event_type
# ---------------------------------------------------------------------------


class TestSubclass:
    """Subclass must define _event_type."""

    def test_subclass_with_event_type(self):
        Evt, Bcst = _make_broadcaster()
        assert Bcst._event_type is Evt


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    def test_subscribe_adds_callback(self):
        Evt, Bcst = _make_broadcaster()
        called = []

        def handler(e):
            called.append(e)

        Bcst.subscribe(handler)
        assert Bcst.get_subscriber_count() == 1

    def test_subscribe_idempotent(self):
        """Subscribing the same callback twice does not duplicate."""
        Evt, Bcst = _make_broadcaster()

        def handler(e):
            pass

        Bcst.subscribe(handler)
        Bcst.subscribe(handler)
        assert Bcst.get_subscriber_count() == 1


# ---------------------------------------------------------------------------
# unsubscribe
# ---------------------------------------------------------------------------


class TestUnsubscribe:
    def test_unsubscribe_removes_callback(self):
        Evt, Bcst = _make_broadcaster()

        def handler(e):
            pass

        Bcst.subscribe(handler)
        assert Bcst.get_subscriber_count() == 1
        Bcst.unsubscribe(handler)
        assert Bcst.get_subscriber_count() == 0

    def test_unsubscribe_nonexistent_is_noop(self):
        Evt, Bcst = _make_broadcaster()

        def handler(e):
            pass

        # Should not raise
        Bcst.unsubscribe(handler)
        assert Bcst.get_subscriber_count() == 0


# ---------------------------------------------------------------------------
# broadcast -- sync subscribers
# ---------------------------------------------------------------------------


class TestBroadcastSync:
    @pytest.mark.asyncio
    async def test_broadcast_calls_sync_subscriber(self):
        Evt, Bcst = _make_broadcaster()
        received = []

        def handler(e):
            received.append(e.value)

        Bcst.subscribe(handler)
        await Bcst.broadcast(Evt(value=42))
        assert received == [42]

    @pytest.mark.asyncio
    async def test_broadcast_calls_multiple_sync_subscribers(self):
        Evt, Bcst = _make_broadcaster()
        results_a, results_b = [], []

        def handler_a(e):
            results_a.append(e.value)

        def handler_b(e):
            results_b.append(e.value)

        Bcst.subscribe(handler_a)
        Bcst.subscribe(handler_b)
        await Bcst.broadcast(Evt(value="hi"))
        assert results_a == ["hi"]
        assert results_b == ["hi"]


# ---------------------------------------------------------------------------
# broadcast -- async subscribers
# ---------------------------------------------------------------------------


class TestBroadcastAsync:
    @pytest.mark.asyncio
    async def test_broadcast_calls_async_subscriber(self):
        Evt, Bcst = _make_broadcaster()
        received = []

        async def handler(e):
            received.append(e.value)

        Bcst.subscribe(handler)
        await Bcst.broadcast(Evt(value="async"))
        assert received == ["async"]

    @pytest.mark.asyncio
    async def test_broadcast_mixed_sync_async(self):
        Evt, Bcst = _make_broadcaster()
        sync_results, async_results = [], []

        def sync_handler(e):
            sync_results.append(e.value)

        async def async_handler(e):
            async_results.append(e.value)

        Bcst.subscribe(sync_handler)
        Bcst.subscribe(async_handler)
        await Bcst.broadcast(Evt(value="mix"))
        assert sync_results == ["mix"]
        assert async_results == ["mix"]


# ---------------------------------------------------------------------------
# broadcast -- wrong event type
# ---------------------------------------------------------------------------


class TestBroadcastWrongType:
    """Broadcast raises ValueError for wrong event type."""

    @pytest.mark.asyncio
    async def test_wrong_event_type_raises(self):
        Evt, Bcst = _make_broadcaster()

        class OtherEvent:
            pass

        with pytest.raises(ValueError, match="must be of type"):
            await Bcst.broadcast(OtherEvent())


# ---------------------------------------------------------------------------
# broadcast -- exception suppression
# ---------------------------------------------------------------------------


class TestBroadcastExceptionSuppression:
    """Subscriber exceptions are suppressed; other subscribers still run."""

    @pytest.mark.asyncio
    async def test_exception_suppressed(self):
        Evt, Bcst = _make_broadcaster()
        after_error = []

        def bad_handler(e):
            raise RuntimeError("boom")

        def good_handler(e):
            after_error.append(e.value)

        Bcst.subscribe(bad_handler)
        Bcst.subscribe(good_handler)
        # Should not raise despite bad_handler exploding
        await Bcst.broadcast(Evt(value="ok"))
        assert after_error == ["ok"]


# ---------------------------------------------------------------------------
# get_subscriber_count
# ---------------------------------------------------------------------------


class TestGetSubscriberCount:
    def test_count_zero(self):
        _, Bcst = _make_broadcaster()
        assert Bcst.get_subscriber_count() == 0

    def test_count_after_subscribe(self):
        _, Bcst = _make_broadcaster()

        def h1(e):
            pass

        def h2(e):
            pass

        Bcst.subscribe(h1)
        assert Bcst.get_subscriber_count() == 1
        Bcst.subscribe(h2)
        assert Bcst.get_subscriber_count() == 2

    def test_count_after_unsubscribe(self):
        _, Bcst = _make_broadcaster()

        def h(e):
            pass

        Bcst.subscribe(h)
        Bcst.unsubscribe(h)
        assert Bcst.get_subscriber_count() == 0


# ---------------------------------------------------------------------------
# weakref cleanup
# ---------------------------------------------------------------------------


class TestWeakrefCleanup:
    """Callbacks stored as weakrefs are cleaned up after the referent dies."""

    def test_weakref_cleanup_bound_method(self):
        Evt, Bcst = _make_broadcaster()

        class Listener:
            def on_event(self, e):
                pass

        obj = Listener()
        Bcst.subscribe(obj.on_event)
        assert Bcst.get_subscriber_count() == 1

        del obj
        gc.collect()
        # Dead weakref should be pruned on next count
        assert Bcst.get_subscriber_count() == 0


# ---------------------------------------------------------------------------
# Subclass isolation
# ---------------------------------------------------------------------------


class TestSubclassIsolation:
    """Each Broadcaster subclass maintains its own subscriber list."""

    def test_isolation(self):
        class EvtA:
            pass

        class EvtB:
            pass

        class BcstA(Broadcaster):
            _event_type = EvtA

        class BcstB(Broadcaster):
            _event_type = EvtB

        def handler_a(e):
            pass

        def handler_b(e):
            pass

        BcstA.subscribe(handler_a)
        BcstB.subscribe(handler_b)

        assert BcstA.get_subscriber_count() == 1
        assert BcstB.get_subscriber_count() == 1

        # Removing from A does not affect B
        BcstA.unsubscribe(handler_a)
        assert BcstA.get_subscriber_count() == 0
        assert BcstB.get_subscriber_count() == 1


# ---------------------------------------------------------------------------
# Singleton pattern per subclass
# ---------------------------------------------------------------------------


class TestSingleton:
    """Each subclass is a singleton; base Broadcaster has its own slot."""

    def test_singleton_per_subclass(self):
        Evt, Bcst = _make_broadcaster()
        inst1 = Bcst()
        inst2 = Bcst()
        assert inst1 is inst2

    def test_different_subclasses_are_different_singletons(self):
        class EvtX:
            pass

        class EvtY:
            pass

        class BcstX(Broadcaster):
            _event_type = EvtX

        class BcstY(Broadcaster):
            _event_type = EvtY

        assert BcstX() is not BcstY()

    def test_subclass_singleton_separate_from_base(self):
        Evt, Bcst = _make_broadcaster()
        sub_inst = Bcst()
        # Base Broadcaster itself should have its own slot
        # (instantiating base would succeed independently)
        assert Bcst._instance is sub_inst
