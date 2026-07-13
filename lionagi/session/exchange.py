# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exchange: async message router between entity mailboxes (outbox/inbox per Flow)."""

from __future__ import annotations

import threading
from typing import Any, cast
from uuid import UUID

from pydantic import PrivateAttr

from lionagi._errors import ItemExistsError, ItemNotFoundError
from lionagi.ln.concurrency import gather, sleep
from lionagi.protocols.generic import Element, Flow, Pile, Progression
from lionagi.protocols.messages import Message

__all__ = ("OUTBOX", "Exchange")

OUTBOX = "outbox"
"""Standard outbox progression name."""


def _inbox_name(sender: UUID) -> str:
    """Inbox progression name for a sender: inbox_{uuid}."""
    return f"inbox_{sender}"


class Exchange(Element):
    """Async message router; each entity gets a Flow with outbox and per-sender inboxes."""

    flows: Pile[Flow[Message, Progression]] = None  # type: ignore
    _owner_index: dict[UUID, UUID] = PrivateAttr(default_factory=dict)
    _stop: bool = PrivateAttr(default=False)
    _in_flight: dict[UUID, list[Message]] = PrivateAttr(default_factory=dict)
    _in_flight_lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.flows is None:
            self.flows = Pile()

    def register(self, owner_id: UUID) -> Flow[Message, Progression]:
        """Create mailbox Flow for entity. Raises ValueError if exists."""
        if owner_id in self._owner_index:
            raise ValueError(f"Owner {owner_id} already registered")

        flow: Flow[Message, Progression] = Flow(
            name=str(owner_id),
        )
        flow.add_progression(Progression(name=OUTBOX))

        self.flows.include(flow)
        self._owner_index[owner_id] = flow.id

        return flow

    def unregister(self, owner_id: UUID) -> Flow[Message, Progression] | None:
        """Remove entity mailbox. Returns removed Flow or None."""
        flow_id = self._owner_index.pop(owner_id, None)
        if flow_id is None:
            return None
        return self.flows.pop(flow_id, None)

    def get(self, owner_id: UUID) -> Flow[Message, Progression] | None:
        """Get entity's mailbox Flow or None."""
        flow_id = self._owner_index.get(owner_id)
        if flow_id is None:
            return None
        return self.flows.get(flow_id, None)

    def has(self, owner_id: UUID) -> bool:
        """True if entity is registered."""
        return owner_id in self._owner_index

    @property
    def owner_ids(self) -> list[UUID]:
        """All registered entity UUIDs."""
        return list(self._owner_index.keys())

    async def collect(self, owner_id: UUID) -> int:
        """Route outbox to recipient inboxes (two-phase lock); raises ValueError if unregistered."""
        deliveries: list[tuple[UUID, Message]] = []

        async with self.flows:
            flow = self.get(owner_id)
            if flow is None:
                raise ValueError(f"Owner {owner_id} not registered")

            outbox = flow.get_progression(OUTBOX)

            while len(outbox) > 0:
                message_id = outbox.popleft()
                message = flow.items.pop(message_id, None)
                if message is None:
                    continue

                if message.is_broadcast:
                    for other_id in self._owner_index:
                        if other_id != owner_id:
                            try:
                                message_copy = message.model_copy(deep=True)
                            except Exception:
                                message_copy = message.model_copy()
                            deliveries.append((other_id, message_copy))
                elif message.recipient is not None and message.recipient in self._owner_index:
                    deliveries.append((message.recipient, message))
            self._mark_in_flight(deliveries, increment=True)
        if deliveries:
            try:
                await gather(
                    *[
                        self._deliver_to(recipient_id, message)
                        for recipient_id, message in deliveries
                    ],
                    return_exceptions=True,
                )
            finally:
                self._mark_in_flight(deliveries, increment=False)

        unique_messages = {message.id for _, message in deliveries}
        return len(unique_messages)

    def _mark_in_flight(self, deliveries: list[tuple[UUID, Message]], *, increment: bool) -> None:
        """Track recipients while messages are between outbox and inbox."""
        with self._in_flight_lock:
            for recipient_id, message in deliveries:
                if increment:
                    self._in_flight.setdefault(recipient_id, []).append(message)
                else:
                    self._remove_in_flight_locked(recipient_id, message)

    def _remove_in_flight_locked(self, recipient_id: UUID, message: Message) -> bool:
        pending = self._in_flight.get(recipient_id)
        if not pending:
            return False
        for index, candidate in enumerate(pending):
            if candidate is message:
                pending.pop(index)
                if not pending:
                    self._in_flight.pop(recipient_id, None)
                return True
        return False

    def _deliver_locked(self, recipient_id: UUID, message: Message) -> None:
        recipient_flow = self.get(recipient_id)
        if recipient_flow is None:
            return

        inbox_name = _inbox_name(cast(UUID, message.sender))
        try:
            recipient_flow.add_progression(Progression(name=inbox_name))
        except ItemExistsError:
            pass
        recipient_flow.add_item(message, progressions=inbox_name)

    def peek_pending(self, owner_id: UUID) -> tuple[list[Message], bool]:
        """Atomically peek at delivered and in-transit mail for an owner."""
        with self._in_flight_lock:
            return self.receive(owner_id), bool(self._in_flight.get(owner_id))

    def drain_pending(self, owner_id: UUID) -> list[Message]:
        """Atomically deliver and consume all pending mail for an owner."""
        with self._in_flight_lock:
            for message in list(self._in_flight.get(owner_id, [])):
                self._deliver_locked(owner_id, message)
                self._remove_in_flight_locked(owner_id, message)

            pending = self.receive(owner_id)
            drained: list[Message] = []
            senders = {cast(UUID, message.sender) for message in pending}
            for sender in senders:
                while message := self.pop_message(owner_id=owner_id, sender=sender):
                    drained.append(message)
            return drained

    async def _deliver_to(self, recipient_id: UUID, message: Message) -> None:
        """Deliver to recipient inbox. No-op if recipient unregistered."""
        with self._in_flight_lock:
            pending = self._in_flight.get(recipient_id, [])
            if not any(candidate is message for candidate in pending):
                return
            self._deliver_locked(recipient_id, message)
            self._remove_in_flight_locked(recipient_id, message)

    async def collect_all(self) -> int:
        """Route all outboxes. Returns total messages routed."""
        total = 0
        for owner_id in list(self._owner_index.keys()):
            try:
                total += await self.collect(owner_id)
            except ValueError:
                continue
        return total

    async def sync(self) -> int:
        """Alias for collect_all(). Returns messages routed."""
        return await self.collect_all()

    def collect_sync(self, owner_id: UUID) -> int:
        """Synchronous twin of collect(), for callers that cannot await."""
        flow = self.get(owner_id)
        if flow is None:
            raise ValueError(f"Owner {owner_id} not registered")

        deliveries: list[tuple[UUID, Message]] = []
        outbox = flow.get_progression(OUTBOX)
        while len(outbox) > 0:
            message_id = outbox.popleft()
            message = flow.items.pop(message_id, None)
            if message is None:
                continue
            if message.is_broadcast:
                for other_id in self._owner_index:
                    if other_id != owner_id:
                        try:
                            message_copy = message.model_copy(deep=True)
                        except Exception:
                            message_copy = message.model_copy()
                        deliveries.append((other_id, message_copy))
            elif message.recipient is not None and message.recipient in self._owner_index:
                deliveries.append((message.recipient, message))

        for recipient_id, message in deliveries:
            recipient_flow = self.get(recipient_id)
            if recipient_flow is None:
                continue
            inbox_name = _inbox_name(message.sender)
            try:
                recipient_flow.add_progression(Progression(name=inbox_name))
            except ItemExistsError:
                pass
            recipient_flow.add_item(message, progressions=inbox_name)

        return len({m.id for _, m in deliveries})

    def collect_all_sync(self) -> int:
        """Synchronous twin of collect_all()."""
        total = 0
        for owner_id in list(self._owner_index.keys()):
            try:
                total += self.collect_sync(owner_id)
            except ValueError:
                continue
        return total

    async def run(self, interval: float = 1.0) -> None:
        """Continuous sync loop. Call stop() to exit.

        Does not reset ``_stop`` on entry: a stop() issued before this
        coroutine gets its first turn (e.g. the DAG it watches over failed
        immediately) must make run() return right away instead of clearing
        that signal and looping forever. Construct a fresh Exchange for a new
        run rather than reusing one that has already been stopped.
        """
        while not self._stop:
            await self.sync()
            await sleep(interval)

    def stop(self) -> None:
        """Signal run() loop to exit."""
        self._stop = True

    def send(
        self,
        sender: UUID,
        recipient: UUID | None,
        content: Any,
        channel: str | None = None,
    ) -> Message:
        """Create and queue message. Raises ValueError if sender not registered."""
        flow = self.get(sender)
        if flow is None:
            raise ValueError(f"Sender {sender} not registered")

        message = Message(sender=sender, recipient=recipient, content=content, channel=channel)
        flow.add_item(message, progressions=OUTBOX)
        return message

    def receive(self, owner_id: UUID, sender: UUID | None = None) -> list[Message]:
        """Peek at inbound messages (non-destructive). Filter by sender if provided."""
        flow = self.get(owner_id)
        if flow is None:
            return []

        result = []
        # Iterate over progressions Pile (public API) instead of _progression_names
        for progression in flow.progressions:
            prog_name = progression.name
            if prog_name and prog_name.startswith("inbox_"):
                if sender is not None:
                    expected_name = _inbox_name(sender)
                    if prog_name != expected_name:
                        continue
                for message_id in progression:
                    message = flow.items.get(message_id, None)
                    if message is not None:
                        result.append(message)
        return result

    def pop_message(self, owner_id: UUID, sender: UUID) -> Message | None:
        """Pop oldest message from sender's inbox (FIFO). Returns None if empty."""
        flow = self.get(owner_id)
        if flow is None:
            return None

        inbox_name = _inbox_name(sender)
        try:
            inbox = flow.get_progression(inbox_name)
        except (KeyError, ItemNotFoundError):
            return None

        if len(inbox) == 0:
            return None

        message_id = inbox.popleft()
        return flow.items.pop(message_id, None)

    def __len__(self) -> int:
        """Count of registered entities."""
        return len(self._owner_index)

    def __contains__(self, owner_id: UUID) -> bool:
        """Support 'uuid in exchange' syntax."""
        return self.has(owner_id)

    def __repr__(self) -> str:
        pending = 0
        for flow in self.flows:
            try:
                outbox = flow.get_progression(OUTBOX)
                pending += len(outbox)
            except KeyError:
                pass  # No outbox progression
        return f"Exchange(entities={len(self)}, pending_out={pending})"
