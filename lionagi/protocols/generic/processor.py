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
    """Manages a queue of events with capacity-limited, async processing.

    Subclass this to provide custom event handling logic or permission
    checks. The processor can enqueue events, handle them in batches, and
    respect a capacity limit that is refreshed periodically.
    """

    event_type: ClassVar[type[Event]]

    def __init__(
        self,
        queue_capacity: int,
        capacity_refresh_time: float,
        concurrency_limit: int,
        max_queue_size: int = 0,
    ) -> None:
        """Initializes a Processor instance.

        Args:
            queue_capacity (int):
                The maximum number of events processed in one batch.
            capacity_refresh_time (float):
                The time in seconds after which processing capacity is reset.
            concurrency_limit (int):
                Maximum concurrent event processing tasks.
            max_queue_size (int):
                Maximum queue size for backpressure. 0 means unlimited.
                When the queue is full, enqueue() will block until space
                is available.

        Raises:
            ValueError: If `queue_capacity` < 1, or
                `capacity_refresh_time` <= 0.
        """
        super().__init__()
        if queue_capacity < 1:
            raise ValueError("Queue capacity must be greater than 0.")
        if capacity_refresh_time <= 0:
            raise ValueError("Capacity refresh time must be larger than 0.")

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
        """int: The current capacity available for processing."""
        return self._available_capacity

    @available_capacity.setter
    def available_capacity(self, value: int) -> None:
        self._available_capacity = value

    @property
    def execution_mode(self) -> bool:
        """bool: Indicates if the processor is actively executing events."""
        return self._execution_mode

    @execution_mode.setter
    def execution_mode(self, value: bool) -> None:
        self._execution_mode = value

    async def enqueue(self, event: Event) -> None:
        """Adds an event to the queue asynchronously.

        Blocks if the queue is full (backpressure) until space is available.

        Args:
            event (Event): The event to enqueue.
        """
        await self.queue.put(event)

    def try_enqueue(self, event: Event) -> bool:
        """Non-blocking enqueue. Returns False if queue is full.

        Args:
            event (Event): The event to enqueue.

        Returns:
            True if enqueued, False if queue is full.
        """
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False

    @property
    def queue_full(self) -> bool:
        """True if the queue is at capacity (backpressure active)."""
        if self.max_queue_size == 0:
            return False
        return self.queue.qsize() >= self.max_queue_size

    async def dequeue(self) -> Event:
        """Retrieves the next event from the queue.

        Returns:
            Event: The next event in the queue.
        """
        return await self.queue.get()

    async def stop(self) -> None:
        """Signals the processor to stop processing events."""
        self._stop_event.set()

    async def start(self) -> None:
        """Clears the stop signal, allowing event processing to resume."""
        # Create a new event since ConcurrencyEvent doesn't have clear()
        if self._stop_event.is_set():
            self._stop_event = ConcurrencyEvent()

    def is_stopped(self) -> bool:
        """Checks whether the processor is in a stopped state.

        Returns:
            bool: True if the processor has been signaled to stop.
        """
        return self._stop_event.is_set()

    @classmethod
    async def create(cls, **kwargs: Any) -> "Processor":
        """Asynchronously constructs a new Processor instance.

        Args:
            **kwargs:
                Additional initialization arguments passed to the constructor.

        Returns:
            Processor: A newly instantiated processor.
        """
        return cls(**kwargs)

    async def process(self) -> None:
        """Dequeues and processes events up to the available capacity.

        Marks events as PROCESSING, invokes them asynchronously, and waits
        for tasks to complete. Resets capacity afterward if any events
        were processed.

        A denied event is never silently dropped:

        - a *terminal* denial (``handle_denied`` returns ``True``) gives the
          event a terminal status (e.g. ``SKIPPED``) and frees its slot;
        - a *deferred* denial (``handle_denied`` returns ``False`` — e.g. rate
          limiting) re-enqueues the event so a later ``process()`` cycle (or a
          concurrent ``forward()``) retries it once capacity replenishes,
          instead of dropping it out of the queue while leaving it ``PENDING``.

        The cycle stops once every still-queued event has been deferred, so a
        saturated limit cannot busy-spin the loop.
        """
        events_processed = 0
        deferred = 0

        async with create_task_group() as tg:
            while self.available_capacity > 0 and not self.queue.empty():
                next_event = await self.dequeue()

                if not await self.request_permission(**next_event.request):
                    if await self.handle_denied(next_event):
                        # Terminal denial: the event now holds a terminal
                        # status; consume its capacity slot and move on.
                        self._available_capacity -= 1
                    else:
                        # Deferred denial: put the event back so it is retried
                        # rather than lost. Do NOT consume capacity — the event
                        # has not been processed.
                        await self.enqueue(next_event)
                        deferred += 1
                        if deferred >= self.queue.qsize():
                            # Every queued event has been deferred this lap;
                            # capacity is exhausted for now. Stop to avoid
                            # busy-spinning until the limit replenishes.
                            break
                    continue

                # invoke()/stream() are total: a business failure is captured
                # as FAILED status, not raised, so one event's failure never
                # aborts the TaskGroup. Cancellation (BaseException) still
                # propagates — correctly aborting the group.
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

    async def request_permission(self, **kwargs: Any) -> bool:
        """Determines if an event may proceed.

        Override this method for custom checks (e.g., rate limits, user
        permissions).

        Args:
            **kwargs: Additional request parameters.

        Returns:
            bool: True if the event is allowed, False otherwise.
        """
        return True

    async def handle_denied(self, event: Event) -> bool:
        """Handle an event whose ``request_permission`` returned False.

        Called once per denied event, after it has been dequeued. Returns
        whether the denial is *terminal*:

        - ``True`` (default): the denial is a rejection. The base marks the
          event ``SKIPPED`` (a terminal status) so it is not left stuck
          ``PENDING`` outside the queue, and ``process()`` frees its slot.
        - ``False``: the denial is a *deferral* ("try again shortly" rather
          than "reject"). ``process()`` re-enqueues the event so a later cycle
          retries it. Subclasses whose denial is rate-limit backpressure (e.g.
          :class:`RateLimitedAPIProcessor`) override this to return ``False``.
        """
        event.status = EventStatus.SKIPPED
        return True

    async def execute(self) -> None:
        """Continuously processes events until `stop()` is called.

        Respects the capacity refresh time between processing cycles.
        """
        self.execution_mode = True
        await self.start()

        while not self.is_stopped():
            await self.process()
            await anyio.sleep(self.capacity_refresh_time)

        self.execution_mode = False


class Executor(Observer):
    """Manages events via a Processor and stores them in a `Pile`.

    Subclass this to customize how events are forwarded or tracked.
    Typically, you configure an internal Processor, then add events to
    the Pile, which eventually are passed along to the Processor for
    execution.
    """

    processor_type: ClassVar[type[Processor]]

    def __init__(
        self,
        processor_config: dict[str, Any] | None = None,
        strict_event_type: bool = False,
    ) -> None:
        """Initializes the Executor.

        Args:
            processor_config (dict[str, Any] | None):
                Configuration parameters for creating the Processor.
            strict_event_type (bool):
                If True, the underlying Pile enforces exact type matching
                for Event objects.
        """
        self.processor_config = processor_config or {}
        self.pending = Progression()
        self.processor: Processor | None = None
        self.pile: Pile[Event] = Pile(
            item_type=self.processor_type.event_type,
            strict_type=strict_event_type,
        )

    @property
    def event_type(self) -> type[Event]:
        """type[Event]: The Event subclass handled by the processor."""
        return self.processor_type.event_type

    @property
    def strict_event_type(self) -> bool:
        """bool: Indicates if the Pile enforces exact event type matching."""
        return self.pile.strict_type

    async def forward(self) -> None:
        """Forwards all pending events from the pile to the processor.

        After all events are enqueued, it calls `processor.process()` for
        immediate handling.
        """
        while len(self.pending) > 0:
            id_ = self.pending.popleft()
            event = self.pile[id_]
            await self.processor.enqueue(event)

        await self.processor.process()

    async def start(self) -> None:
        """Initializes and starts the processor if it has not been created."""
        if not self.processor:
            await self._create_processor()
        await self.processor.start()

    async def stop(self) -> None:
        """Stops the processor if it exists."""
        if self.processor:
            await self.processor.stop()

    async def _create_processor(self) -> None:
        """Instantiates the processor using the stored config."""
        self.processor = await self.processor_type.create(**self.processor_config)

    async def append(self, event: Event) -> None:
        """Adds a new Event to the pile and marks it as pending.

        Args:
            event (Event): The event to add.
        """
        # Use async methods to avoid deadlock between sync/async locks
        await self.pile.ainclude(event)
        self.pending.include(event)

    @property
    def completed_events(self) -> Pile[Event]:
        """Pile[Event]: All events in COMPLETED status."""
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.COMPLETED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def pending_events(self) -> Pile[Event]:
        """Pile[Event]: All events currently in PENDING status."""
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.PENDING],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def failed_events(self) -> Pile[Event]:
        """Pile[Event]: All events whose status is FAILED."""
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.FAILED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def cancelled_events(self) -> Pile[Event]:
        """Pile[Event]: All events whose status is CANCELLED."""
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.CANCELLED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    @property
    def skipped_events(self) -> Pile[Event]:
        """Pile[Event]: All events whose status is SKIPPED."""
        return Pile(
            collections=[e for e in self.pile if e.status == EventStatus.SKIPPED],
            item_type=self.processor_type.event_type,
            strict_type=self.strict_event_type,
        )

    def status_counts(self) -> dict[str, int]:
        """Return a count of events by status.

        Returns:
            dict mapping status value strings to counts.
        """
        counts: dict[str, int] = {}
        for event in self.pile:
            key = event.status.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def cleanup_completed(self) -> int:
        """Remove completed events from the pile to free memory.

        Returns:
            Number of events removed.
        """
        completed_ids = [e.id for e in self.pile if e.status == EventStatus.COMPLETED]
        for eid in completed_ids:
            self.pile.pop(eid)
        return len(completed_ids)

    def inspect_state(self) -> dict:
        """Return a summary of executor state for debugging.

        Returns:
            dict with event counts, queue size, processor status.
        """
        return {
            "total_events": len(self.pile),
            "status_counts": self.status_counts(),
            "pending_queue": len(self.pending),
            "processor_running": (self.processor.execution_mode if self.processor else False),
            "processor_stopped": (self.processor.is_stopped() if self.processor else True),
        }

    def __contains__(self, ref: ID[Event].Ref) -> bool:
        """Checks if a given Event or ID reference is present in the pile.

        Args:
            ref (ID[Event].Ref):
                A reference to an Event (e.g., the Event object, its ID, etc.).

        Returns:
            bool: True if the referenced event is in the pile, False otherwise.
        """
        return ref in self.pile
