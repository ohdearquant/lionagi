from collections import deque
from enum import Enum
import asyncio
from typing import Any

from lionagi.libs import SysUtil, CallDecorator as cd, func_call
from lionagi.core.generic import BaseComponent


class WorkStatus(str, Enum):
    """Enum to represent different statuses of work."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Work(BaseComponent):
    form_id: str | None = None
    status: WorkStatus = WorkStatus.PENDING
    result: Any = None
    error: Any = None
    async_task: asyncio.Task | None = None
    completion_timestamp: str | None = None
    execution_duration: str | None = None

    @cd.count_calls
    async def perform(self):
        try:
            result, duration = await self.async_task
            self.result = result
            self.execution_duration = duration
            self.status = WorkStatus.COMPLETED
            del self.async_task
        except Exception as e:
            self.error = e
            self.status = WorkStatus.FAILED

        self.completion_timestamp = SysUtil.get_timestamp(sep=None)

    def __str__(self):

        return f"Work(id={self.id_}, status={self.status.value}, created_at={self.timestamp[:-6]},completed_at={self.completion_timestamp[:-6]}, execution_duration={self.execution_duration:.6f})"


class WorkQueue:

    def __init__(self, capacity=None):

        self.queue = asyncio.Queue(capacity)
        self._stop_event = asyncio.Event()
        self.capacity = capacity
        self.semaphore = asyncio.Semaphore(capacity)
        self.count = 0

    async def enqueue(self, work) -> None:
        await self.queue.put(work)

    async def dequeue(self):
        return await self.queue.get()

    async def join(self) -> None:
        await self.queue.join()

    async def stop(self) -> None:
        self._stop_event.set()

    @property
    def available_capacity(self):
        if (a := self.capacity - self.queue.qsize()) > 0:
            return a
        return None

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    async def process(self) -> None:
        tasks = set()
        while self.queue.qsize() > 0 and not self.stopped:
            if not self.available_capacity and tasks:
                _, done = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks.difference_update(done)

            async with self.semaphore:
                next: Work = await self.dequeue()
                if next is None:
                    break
                task = asyncio.create_task(next.perform())
                tasks.add(task)

            if tasks:
                await asyncio.wait(tasks)
                self.count += len(tasks)

    async def clear_count(self):
        self.count = 0


class WorkLog:

    def __init__(self, capacity=None, pile=None, refresh_time=None):
        self.pile = pile or {}
        self.pending_sequence = deque()
        self.queue = WorkQueue(capacity=capacity)
        self.refresh_time = refresh_time
        self.add_count = 0

    async def register_form(): ...

    async def append(self, work: Work):
        self.pile[work.id_] = work
        self.pending_sequence.append(work.id_)

    async def forward(self):
        if not self.queue.available_capacity:
            return False
        else:
            while self.pending_sequence and self.queue.available_capacity:
                work = self.pile[self.pending_sequence.popleft()]
                work.status = WorkStatus.IN_PROGRESS
                await self.queue.enqueue(work)
                return True
            return False

    async def process(self, refresh_time=None):
        while self.pending_sequence and not self.queue.stopped:
            while await self.forward():
                await self.queue.process()
                # await asyncio.sleep(refresh_time or self.refresh_time)
        await asyncio.sleep(refresh_time or self.refresh_time)

    async def stop(self):
        await self.queue.stop()

    @property
    def stopped(self):
        return self.queue.stopped

    @property
    def completed_work(self):
        return {k: v for k, v in self.pile.items() if v.status == WorkStatus.COMPLETED}


class WorkFunction:

    def __init__(
        self,
        assignment,
        function,
        retry_kwargs,
        instruction,
        capacity,
        refresh_time,
    ):

        self.assignment = assignment
        self.function = function
        self.retry_kwargs = retry_kwargs or {}
        self.instruction = instruction or function.__doc__
        self.worklog = WorkLog(capacity=capacity, refresh_time=refresh_time)

    @property
    def name(self):
        return self.function.__name__

    async def perform(self, *args, **kwargs):
        kwargs = {**self.retry_kwargs, **kwargs}
        return await func_call.rcall(self.function, *args, timing=True, **kwargs)

    async def process(self, refresh_time=None):
        await self.worklog.process(refresh_time)

    async def stop(self):
        await self.worklog.queue.stop()

    async def clear_count(self):
        self.worklog.queue.clear_count()

    @property
    def count(self):
        return self.worklog.queue.count

    @property
    def completed_work(self):
        return self.worklog.completed_work

    def __repr__(self):
        return f"<WorkFunction {self.name}>"

    def __str__(self):
        return f"WorkFunction(name={self.name}, assignment={self.assignment}, instruction={self.instruction})"
