"""
A class that manages asynchronous task processing with controlled concurrency.
"""

from typing import Any, Callable
import asyncio
from lionagi.libs import func_call

class AsyncQueue:
    """
    This class handles the enqueueing and processing of tasks with a limit on
    how many can run simultaneously, using an asyncio.Queue for task storage and
    an asyncio.Semaphore to limit concurrency.

    Attributes:
        queue (asyncio.Queue): The queue to store tasks.
        _stop_event (asyncio.Event): Event to signal processing should stop.
        max_concurrent_tasks (int): Maximum number of tasks processed concurrently.
        semaphore (asyncio.Semaphore): Controls concurrent access to task execution.
    """
    
    def __init__(self, max_concurrent_tasks=5):
        """
        Initializes the AsyncQueue with a concurrency limit.

        Args:
            max_concurrent_tasks (int): The maximum number of concurrent tasks
                                        allowed. Default is 5.
        """
        self.queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self.max_concurrent_tasks = max_concurrent_tasks
        self.semaphore = asyncio.Semaphore(max_concurrent_tasks)

    async def enqueue(self, input_: Any) -> None:
        """
        Enqueues an item to be processed asynchronously.

        Args:
            input_ (Any): The item to be enqueued.
        """
        await self.queue.put(input_)

    async def dequeue(self) -> Any:
        """
        Dequeues an item for processing.

        Returns:
            Any: The dequeued item.
        """
        return await self.queue.get()

    async def join(self) -> None:
        """Waits for all items in the queue to be processed."""
        await self.queue.join()

    async def stop(self) -> None:
        """Signals the queue to stop processing new items."""
        self._stop_event.set()

    def stopped(self) -> bool:
        """
        Checks if the stop signal has been issued.

        Returns:
            bool: True if the queue has been stopped, otherwise False.
        """
        return self._stop_event.is_set()
    
    async def process_requests(self, func: Callable, retry_kwargs: dict = {}) -> None:
        """
        Processes tasks from the queue using the provided function with retries.

        This method continuously processes tasks from the queue using the specified
        function until a stop event is triggered. Handles concurrency using a
        semaphore and manages task completion.

        Args:
            func (Callable): The function to process each task.
            retry_kwargs (dict): Keyword arguments for retry behavior. Default is
                                 an empty dictionary.
        """
        tasks = set()
        while not self.stopped():
            if len(tasks) >= self.max_concurrent_tasks:
                _, done = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks.difference_update(done)

            async with self.semaphore:
                input_ = await self.dequeue()
                if input_ is None:
                    await self.stop()
                    break
                task = asyncio.create_task(func_call.rcall(func, input_, **retry_kwargs))
                tasks.add(task)

        if tasks:
            await asyncio.wait(tasks)
