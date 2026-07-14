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
