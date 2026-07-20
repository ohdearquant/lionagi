# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from typing import Any, ClassVar

import anyio

from lionagi.ln.concurrency import ConcurrencyEvent, Semaphore, create_task_group

from .._concepts import Observer
from .element import ID
from .event import Event, EventStatus
from .pile import Pile
from .progression import Progression

__all__ = (
    "Processor",
    "Executor",
)


class Processor(Observer):
    """Capacity-limited async event processor with permission checks."""

    event_type: ClassVar[type[Event]]

    def __init__(
        self,
        queue_capacity: int,
        capacity_refresh_time: float,
        concurrency_limit: int,
        max_queue_size: int = 0,
    ) -> None:
        super().__init__()
        if queue_capacity < 1:
            raise ValueError("Queue capacity must be greater than 0.")
        if capacity_refresh_time <= 0:
            raise ValueError("Capacity refresh time must be larger than 0.")
        if max_queue_size < 0:
            raise ValueError("Queue size must be non-negative.")

        self.queue_capacity = queue_capacity
        self.capacity_refresh_time = capacity_refresh_time
        self.max_queue_size = max_queue_size
        # TODO(#1043 Phase 3): migrate to lionagi.ln.concurrency.Queue (API shape differs)
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self._available_capacity = queue_capacity
        self._execution_mode = False
        self._stop_event = ConcurrencyEvent()
        if concurrency_limit:
            self._concurrency_sem = Semaphore(concurrency_limit)
        else:
            self._concurrency_sem = None

    @property
    def available_capacity(self) -> int:
        return self._available_capacity

    @available_capacity.setter
    def available_capacity(self, value: int) -> None:
        self._available_capacity = value

    @property
    def execution_mode(self) -> bool:
        return self._execution_mode

    @execution_mode.setter
    def execution_mode(self, value: bool) -> None:
        self._execution_mode = value

    async def enqueue(self, event: Event) -> None:
        await self.queue.put(event)

    def try_enqueue(self, event: Event) -> bool:
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False

    @property
    def queue_full(self) -> bool:
        if self.max_queue_size == 0:
            return False
        return self.queue.qsize() >= self.max_queue_size

    async def dequeue(self) -> Event:
        return await self.queue.get()

    async def stop(self) -> None:
        self._stop_event.set()

    async def start(self) -> None:
        if self._stop_event.is_set():
            self._stop_event = ConcurrencyEvent()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    @classmethod
    async def create(cls, **kwargs: Any) -> "Processor":
        return cls(**kwargs)

    async def process(self) -> None:
        """Dequeue and process events up to available capacity; stops once all
        queued events have been deferred, to avoid busy-spin."""
        events_processed = 0
        deferred = 0

        async with create_task_group() as tg:
            while self.available_capacity > 0 and not self.queue.empty():
                next_event = await self.dequeue()

                if not await self.request_permission(**next_event.request):
                    if await self.handle_denied(next_event):
                        events_processed += 1
                        self._available_capacity -= 1
                    else:
                        # Deferred: re-enqueue for retry, don't consume capacity.
                        await self.enqueue(next_event)
                        deferred += 1
                        if deferred >= self.queue.qsize():
                            break
                    continue

                if next_event.streaming:

                    async def consume_stream(event):
                        async for _ in event.stream():
                            pass

                    if self._concurrency_sem:

                        async def stream_with_sem(event):
                            async with self._concurrency_sem:
                                await consume_stream(event)

                        tg.start_soon(stream_with_sem, next_event)
                    else:
                        tg.start_soon(consume_stream, next_event)
                else:
                    if self._concurrency_sem:

                        async def invoke_with_sem(event):
                            async with self._concurrency_sem:
                                await event.invoke()

                        tg.start_soon(invoke_with_sem, next_event)
                    else:
                        tg.start_soon(next_event.invoke)

                events_processed += 1
                deferred = 0
                self._available_capacity -= 1

        if events_processed > 0:
            self.available_capacity = self.queue_capacity

    async def join(self) -> None:
        """Block until queue is drained. Sleeps on no-progress cycles."""
        while not self.queue.empty():
            before = self.queue.qsize()
            await self.process()
            if not self.queue.empty() and self.queue.qsize() >= before:
                await anyio.sleep(self.capacity_refresh_time)

    async def request_permission(self, **kwargs: Any) -> bool:
        return True

    async def handle_denied(self, event: Event) -> bool:
        """Handle denied event. Return True for terminal (SKIPPED), False for deferral."""
        event.status = EventStatus.SKIPPED
        return True

    async def execute(self) -> None:
        self.execution_mode = True
        await self.start()

        while not self.is_stopped():
            await self.process()
            await anyio.sleep(self.capacity_refresh_time)

        self.execution_mode = False


class Executor(Observer):
    """Manages events via a Processor, storing them in a Pile."""

    processor_type: ClassVar[type[Processor]]

    def __init__(
        self,
        processor_config: dict[str, Any] | None = None,
        strict_event_type: bool = False,
    ) -> None:
        self.processor_config = processor_config or {}
        self.pending = Progression()
        self.processor: Processor | None = None
        self.pile: Pile[Event] = Pile(
            item_type=self.processor_type.event_type,
            strict_type=strict_event_type,
        )

    @property
    def event_type(self) -> type[Event]:
        return self.processor_type.event_type

    @property
    def strict_event_type(self) -> bool:
        return self.pile.strict_type

    async def forward(self) -> None:
        while len(self.pending) > 0:
            id_ = self.pending.popleft()
            event = self.pile[id_]
            await self.processor.enqueue(event)

        await self.processor.process()

    async def start(self) -> None:
        if not self.processor:
            await self._create_processor()
        await self.processor.start()

    async def stop(self) -> None:
        if self.processor:
            await self.processor.stop()

    async def _create_processor(self) -> None:
        self.processor = await self.processor_type.create(**self.processor_config)

    async def append(self, event: Event) -> None:
        await self.pile.ainclude(event)
        self.pending.include(event)

    @property
    def completed_events(self) -> Pile[Event]:
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.COMPLETED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def pending_events(self) -> Pile[Event]:
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.PENDING],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def failed_events(self) -> Pile[Event]:
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.FAILED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def cancelled_events(self) -> Pile[Event]:
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.CANCELLED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def skipped_events(self) -> Pile[Event]:
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.SKIPPED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.pile:
            key = event.status.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def cleanup_completed(self) -> int:
        completed_ids = [e.id for e in self.pile if e.status == EventStatus.COMPLETED]
        for eid in completed_ids:
            self.pile.pop(eid)
        return len(completed_ids)

    def inspect_state(self) -> dict:
        return {
            "total_events": len(self.pile),
            "status_counts": self.status_counts(),
            "pending_queue": len(self.pending),
            "processor_running": (self.processor.execution_mode if self.processor else False),
            "processor_stopped": (self.processor.is_stopped() if self.processor else True),
        }

    def __contains__(self, ref: ID[Event].Ref) -> bool:
        return ref in self.pile
