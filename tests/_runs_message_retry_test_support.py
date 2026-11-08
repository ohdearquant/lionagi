# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

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
