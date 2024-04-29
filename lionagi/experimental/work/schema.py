from collections import deque
from enum import Enum
import asyncio
from typing import Any

from lionagi.libs import SysUtil
from lionagi.core.generic import Component

from .async_queue import WorkQueue

class WorkStatus(str, Enum):
    """Enum to represent different statuses of work."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    
    
class Work(Component):
    status: WorkStatus = WorkStatus.PENDING
    result: Any = None
    error: Any = None
    async_task: asyncio.Task | None = None
    completion_timestamp: str | None = None
    
    async def perform(self):
        try:        
            result = await self.async_task
            self.result = result
            self.status = WorkStatus.COMPLETED
            self.async_task = None
        except Exception as e:
            self.error = e
            self.status = WorkStatus.FAILED
        finally:
            self.completion_timestamp = SysUtil.get_timestamp()


    def __str__(self):
        return f"Work(id={self.id_}, status={self.status}, created_at={self.timestamp}, completed_at={self.completion_timestamp})"

class WorkLog:
    
    def __init__(self, capacity=5, pile=None):
        self.pile = pile or {}
        self.pending_sequence = deque()
        self.queue = WorkQueue(capacity=capacity)

    async def append(self, work: Work):
        self.pile[work.id_] = work
        self.pending_sequence.append(work.id_)
        
    async def forward(self):
        if not self.queue.available_capacity:
            return
        else:
            while self.pending_sequence and self.queue.available_capacity:
                work = self.pile[self.pending_sequence.popleft()]
                work.status = WorkStatus.IN_PROGRESS
                await self.queue.enqueue(work)
                

    async def stop(self):
        await self.queue.stop()

    @property
    def stopped(self):
        return self.queue.stopped

    @property
    def completed_work(self):
        return {k: v for k, v in self.pile.items() if v.status == WorkStatus.COMPLETED}
