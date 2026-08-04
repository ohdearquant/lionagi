# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Caller-owned ordered retries for atomic live-message persistence."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class PendingMessageEvent:
    """Complete payload for one live-message persistence attempt."""

    message: dict[str, Any]
    session_id: str
    branch_progression_id: str | None = None
    session_progression_id: str | None = None
    system_branch_id: str | None = None
    system_branch_update_before_activity: bool = False
    activity_at: float | None = None
    on_persisted: Callable[[], None] | None = None


class MessagePersistRetryQueue:
    """Preserve message order while retrying failed atomic persistence events."""

    _MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, db: Any, *, logger: Any, owner: str) -> None:
        self._db = db
        self._logger = logger
        self._owner = owner
        self._pending: deque[PendingMessageEvent] = deque()
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._retry_deferred = False
        self._state = "healthy"
        self._reported_loss_count: int | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def submit(self, event: PendingMessageEvent) -> bool:
        """Queue ``event`` and persist pending events in their original order."""
        async with self._lock:
            self._pending.append(event)
            if self._retry_deferred:
                return False
            return await self._drain_locked()

    async def flush(self) -> bool:
        """Make the one additional teardown attempt for every pending event."""
        async with self._lock:
            if not self._pending:
                return True
            return await self._drain_locked(force=True)

    async def flush_final(self) -> bool:
        """Flush at teardown, and say so when events did not make it.

        ``flush`` reports failure only by returning ``False``, and that is the
        whole of what the failing path says. The state log is deliberately quiet
        when nothing changed, and a queue that has been failing long enough to
        reach teardown is already in the state it would be transitioning to, so
        the last attempt these events will ever get is exactly the attempt that
        emits nothing. A caller that drops the return value is then told nothing
        at all, by a queue that just lost their messages.

        Losing them is an outcome rather than a state change, so it is reported
        here whichever state the queue was already in. The count is included
        because it is the part nobody can reconstruct afterwards: the events are
        gone, and no later sweep has anything left to find.

        Teardown reaches this more than once. The hook bus flushes on
        ``SESSION_END`` and the run teardown flushes before it reads completion
        evidence, and both traverse the same queues without either being able to
        see the other. The repeated *attempt* is wanted — the store may have come
        back between them, and the second call is a real last chance — so what is
        suppressed is the repeated *report*: the same loss, restated, reads as
        two separate incidents. A count that has changed is a different fact and
        is reported again, and a flush that finally succeeds clears the marker so
        a later loss is not swallowed as a repeat.
        """
        flushed = await self.flush()
        if flushed:
            self._reported_loss_count = None
            return True

        lost = self.pending_count
        if lost != self._reported_loss_count:
            self._reported_loss_count = lost
            self._logger.error(
                "live persist gave up for %s at teardown: %d event(s) were never "
                "written and are lost",
                self._owner,
                lost,
            )
        return False

    async def _drain_locked(self, *, force: bool = False) -> bool:
        if self._retry_deferred and not force:
            return False

        while self._pending:
            event = self._pending[0]
            try:
                await self._db._persist_live_message(
                    event.message,
                    session_id=event.session_id,
                    branch_progression_id=event.branch_progression_id,
                    session_progression_id=event.session_progression_id,
                    system_branch_id=event.system_branch_id,
                    system_branch_update_before_activity=(
                        event.system_branch_update_before_activity
                    ),
                    activity_at=event.activity_at,
                )
            except ValueError as exc:
                # StateDB._validate_message raises ValueError for a
                # deterministically malformed message (missing content/role);
                # the same message will fail identically on every retry, so
                # drop it instead of leaving it at the queue head where it
                # would head-of-line-block every message queued behind it.
                self._pending.popleft()
                self._consecutive_failures = 0
                self._logger.warning(
                    "live persist dropping malformed message for %s "
                    "(non-retryable validation error): %s",
                    self._owner,
                    exc,
                    exc_info=exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001 -- persistence is best effort here
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
                    self._retry_deferred = True
                    self._transition("deferred", exc)
                else:
                    self._transition("retrying", exc)
                return False

            self._pending.popleft()
            self._consecutive_failures = 0
            if event.on_persisted is not None:
                event.on_persisted()

        self._retry_deferred = False
        self._transition("healthy")
        return True

    def _transition(self, state: str, exc: Exception | None = None) -> None:
        if state == self._state:
            return
        self._state = state
        if state == "retrying":
            self._logger.warning(
                "live persist write failed for %s; event queued for ordered retry: %s",
                self._owner,
                exc,
                exc_info=exc,
            )
        elif state == "deferred":
            self._logger.warning(
                "live persist retries deferred for %s after %d consecutive failures; "
                "pending events will retry at teardown",
                self._owner,
                self._consecutive_failures,
                exc_info=exc,
            )
        elif state == "healthy":
            self._logger.debug("live persist retries recovered for %s", self._owner)
