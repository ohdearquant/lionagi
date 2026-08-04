# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for MessagePersistRetryQueue: permanent-failure classification
must not head-of-line-block later messages."""

from __future__ import annotations

import logging
from typing import Any

from lionagi.hooks._message_retry import MessagePersistRetryQueue, PendingMessageEvent


class _FakeDB:
    """Stands in for StateDB._persist_live_message: raises ValueError for ids in
    `fail_ids` (mirrors _validate_message's deterministic validation failure) and
    records everything else as persisted."""

    def __init__(self, fail_ids: set[str]) -> None:
        self.fail_ids = fail_ids
        self.persisted: list[str] = []

    async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
        if message["id"] in self.fail_ids:
            raise ValueError("messages.content is NOT NULL")
        self.persisted.append(message["id"])


def _event(msg_id: str) -> PendingMessageEvent:
    return PendingMessageEvent(
        message={"id": msg_id, "content": "x", "role": "user"},
        session_id="s1",
    )


async def test_permanent_validation_error_does_not_block_later_messages():
    """A message at the queue head that permanently fails validation must be
    dropped, not left blocking every message submitted after it."""
    db = _FakeDB(fail_ids={"bad-1"})
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    await queue.submit(_event("bad-1"))
    await queue.submit(_event("good-1"))

    assert db.persisted == ["good-1"], (
        "good-1 must be persisted even though bad-1 (queued ahead of it) "
        "permanently fails validation"
    )
    assert queue.pending_count == 0


async def test_permanent_validation_error_is_dropped_not_persisted():
    """The permanently-invalid message itself is never persisted (dropped, not retried)."""
    db = _FakeDB(fail_ids={"bad-1"})
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    ok = await queue.submit(_event("bad-1"))

    assert ok is True  # queue fully drained (the bad item was dropped, not stuck)
    assert db.persisted == []
    assert queue.pending_count == 0


async def test_permanent_validation_error_logs_warning(caplog):
    db = _FakeDB(fail_ids={"bad-1"})
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    with caplog.at_level(logging.WARNING, logger="test"):
        await queue.submit(_event("bad-1"))

    assert any(
        "dropping malformed message" in rec.message and "non-retryable" in rec.message
        for rec in caplog.records
    )


async def test_transient_error_still_head_of_line_blocks_until_deferred():
    """Non-ValueError (transient) failures keep the pre-existing ordered-retry
    behavior: the head item stays queued and blocks later items until it
    either succeeds or the queue defers after MAX_CONSECUTIVE_FAILURES."""

    class _AlwaysBusyDB:
        def __init__(self) -> None:
            self.persisted: list[str] = []

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            raise RuntimeError("simulated sqlite busy")

    db = _AlwaysBusyDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    await queue.submit(_event("stuck-1"))
    await queue.submit(_event("later-1"))

    assert db.persisted == []
    assert queue.pending_count == 2


class _RefusingDB:
    """Refuses every write, the way a contended sqlite does."""

    def __init__(self) -> None:
        self.persisted: list[str] = []
        self.attempts = 0

    async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
        self.attempts += 1
        raise RuntimeError("database is locked")


async def _deferred_queue(db, logger):
    """A queue driven past its consecutive-failure limit, as real traffic does."""
    queue = MessagePersistRetryQueue(db, logger=logger, owner="b1")
    for i in range(4):
        await queue.submit(_event(f"m{i}"))
    return queue


async def test_a_teardown_that_loses_messages_says_so(caplog):
    """The last attempt these events get is the one that used to say nothing.

    By teardown the queue is already deferred, so the state log stays quiet
    because nothing changed, and the only other signal is a return value.
    """
    db = _RefusingDB()
    logger = logging.getLogger("test")
    queue = await _deferred_queue(db, logger)
    assert queue.pending_count == 4

    with caplog.at_level(logging.ERROR, logger="test"):
        flushed = await queue.flush_final()

    assert flushed is False
    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 1, "the loss is reported once, not per event and not never"
    assert "lost" in losses[0].message
    # The count is the part nobody can reconstruct once the events are gone.
    assert "4" in losses[0].message


async def test_a_healthy_teardown_stays_quiet(caplog):
    """The other direction. A report that always fires is a report nobody reads,
    and this one exists to be believed the once it appears."""
    db = _FakeDB(fail_ids=set())
    logger = logging.getLogger("test")
    queue = MessagePersistRetryQueue(db, logger=logger, owner="b1")
    await queue.submit(_event("good-1"))

    with caplog.at_level(logging.DEBUG, logger="test"):
        flushed = await queue.flush_final()

    assert flushed is True
    assert db.persisted == ["good-1"]
    assert [rec for rec in caplog.records if rec.levelno >= logging.WARNING] == []


async def test_a_teardown_that_recovers_stays_quiet(caplog):
    """A queue that was failing and then drains is not a loss. Reporting one
    here is exactly the crying-wolf this is meant to avoid."""

    class _RecoveringDB:
        def __init__(self) -> None:
            self.persisted: list[str] = []
            self.refuse = True

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            if self.refuse:
                raise RuntimeError("database is locked")
            self.persisted.append(message["id"])

    db = _RecoveringDB()
    logger = logging.getLogger("test")
    queue = await _deferred_queue(db, logger)
    db.refuse = False

    with caplog.at_level(logging.WARNING, logger="test"):
        flushed = await queue.flush_final()

    assert flushed is True
    assert queue.pending_count == 0
    assert db.persisted == ["m0", "m1", "m2", "m3"], "order is preserved across the recovery"
    assert [rec for rec in caplog.records if rec.levelno >= logging.ERROR] == []


async def test_the_bus_reports_whether_its_queues_emptied(caplog):
    """The bus teardown discarded this, so nothing downstream could tell."""
    from lionagi.hooks.bus import HookBus

    logger = logging.getLogger("test")
    bus = HookBus()
    bus._message_retry_queues = {"b1": await _deferred_queue(_RefusingDB(), logger)}

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await bus.flush_message_retries() is False

    healthy = MessagePersistRetryQueue(_FakeDB(fail_ids=set()), logger=logger, owner="b2")
    await healthy.submit(_event("good-1"))
    bus._message_retry_queues = {"b2": healthy}

    assert await bus.flush_message_retries() is True


async def test_the_run_teardown_reports_whether_its_queues_emptied():
    """The second teardown that discarded it. What runs after this reads the
    run's completion evidence."""
    from lionagi.cli._runs import _flush_pending_message_events

    logger = logging.getLogger("test")
    lost = await _deferred_queue(_RefusingDB(), logger)
    assert await _flush_pending_message_events({"message_retry_queues": [lost]}) is False

    healthy = MessagePersistRetryQueue(_FakeDB(fail_ids=set()), logger=logger, owner="b2")
    await healthy.submit(_event("good-1"))
    assert await _flush_pending_message_events({"message_retry_queues": [healthy]}) is True


async def test_both_teardown_paths_over_one_queue_report_the_loss_once(caplog):
    """The two teardowns reach the same queue, and neither can see the other.

    The hook bus flushes on ``SESSION_END`` and the run teardown flushes before
    reading completion evidence. One queue traversed by both would report its
    loss twice, and one loss restated reads as two incidents — the crying-wolf
    the report exists to avoid.

    Asserted through both real entry points rather than by calling
    ``flush_final`` twice, because the duplication is a property of the two
    paths meeting, not of the method.
    """
    from lionagi.cli._runs import _flush_pending_message_events
    from lionagi.hooks.bus import HookBus

    logger = logging.getLogger("test")
    queue = await _deferred_queue(_RefusingDB(), logger)

    bus = HookBus()
    bus._message_retry_queues = {"b1": queue}

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await bus.flush_message_retries() is False
        assert await _flush_pending_message_events({"message_retry_queues": [queue]}) is False

    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 1, (
        f"one loss, reported once across both teardown paths; got {len(losses)}"
    )
    assert "4" in losses[0].message

    # Both calls still returned False. Suppressing the repeated report must not
    # suppress the answer a caller acts on.
    assert queue.pending_count == 4


async def test_a_loss_that_grows_between_teardowns_is_reported_again(caplog):
    """The over-suppression arm. A second call is silenced because it restates
    one fact, not because a queue may only ever report once: events lost since
    the first report are a different fact and nobody else will name them."""
    logger = logging.getLogger("test")
    db = _RefusingDB()
    queue = await _deferred_queue(db, logger)

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await queue.flush_final() is False
        await queue.submit(_event("m4"))
        assert await queue.flush_final() is False

    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 2, "a changed count is a new fact"
    assert "4" in losses[0].message
    assert "5" in losses[1].message


async def test_a_recovery_between_teardowns_does_not_swallow_a_later_loss(caplog):
    """The marker is cleared by a flush that succeeds, so a queue that recovers
    and then loses new events reports that loss rather than reading it as a
    repeat of the one already on the record."""

    class _FlakyDB:
        def __init__(self) -> None:
            self.persisted: list[str] = []
            self.refuse = True

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            if self.refuse:
                raise RuntimeError("database is locked")
            self.persisted.append(message["id"])

    logger = logging.getLogger("test")
    db = _FlakyDB()
    queue = await _deferred_queue(db, logger)

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await queue.flush_final() is False  # 4 lost, reported
        db.refuse = False
        assert await queue.flush_final() is True  # drains, marker cleared
        db.refuse = True
        for i in range(4):
            await queue.submit(_event(f"later-{i}"))
        assert await queue.flush_final() is False  # 4 again, but a new 4

    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 2, (
        "the second loss has the same count as the first and is still a different loss"
    )
