# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``SESSION_END`` flushes the message retry queues before its handlers run.

The flush is the bus's (``lionagi.hooks.bus``); the queue it flushes is the state
store's (``lionhub.state.message_retry``), so the cases that need a real queue live
here. The rest of what ``HookBus.emit`` does is characterized in the core suite.
"""

from __future__ import annotations

import logging

import pytest

from lionagi.hooks import HookBus, HookPoint, HookSignal
from lionagi.hooks.bus import _current_emitting_bus
from lionagi.session.observer import SessionObserver
from lionhub.state.message_retry import MessagePersistRetryQueue, PendingMessageEvent

# Every point an emit carries today; ARTIFACT_CREATED is dormant.
LIVE_POINTS = tuple(point for point in HookPoint if point is not HookPoint.ARTIFACT_CREATED)


class _Abort(BaseException):
    """A failure outside the ``Exception`` hierarchy, so ``except Exception`` misses it."""


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
