import asyncio
from typing import Any, Callable
from lionagi.libs.ln_func_call import tcall


class AsyncQueue:
    """
    A queue class that handles asynchronous operations using asyncio.

    This class provides an asynchronous queue that can enqueue items, process them
    asynchronously, and support graceful shutdowns. It is designed to facilitate
    concurrent task processing in an orderly and controlled manner.

    Attributes:
        queue (asyncio.Queue):
            A queue to hold items for asynchronous processing.
        _stop_event (asyncio.Event):
            An event to signal when the queue should stop processing.

    Methods:
        enqueue(input_):
            Add an input_ to the queue for processing.
        dequeue():
            Remove and return an input_ from the queue.
        join():
            Wait until all items in the queue have been processed.
        stop():
            Signal to stop processing new items in the queue.
        stopped():
            Check if the queue has been signaled to stop.
        process_requests(func):
            Process items using a provided function.
    """

    def __init__(self) -> None:
        """
        Initializes an AsyncQueue object with an empty asyncio Queue and a stop event.
        """
        self.queue = asyncio.Queue()
        self._stop_event = asyncio.Event()

    async def enqueue(self, input_: Any) -> None:
        """
        Asynchronously add an input_ to the queue for processing.

        Parameters:
            input_ (Any): The input_ to be added to the queue.

        Example:
            >>> async_queue = AsyncQueue()
            >>> asyncio.run(async_queue.enqueue('Task 1'))
        """
        await self.queue.put(input_)

    async def dequeue(self) -> Any:
        """
        Asynchronously remove and return an input_ from the queue.

        If the queue is empty, this method will wait until an input_ is available.

        Returns:
            Any: The next input_ from the queue.

        Example:
            >>> async_queue = AsyncQueue()
            >>> asyncio.run(async_queue.enqueue('Task 1'))
            >>> asyncio.run(async_queue.dequeue())
            'Task 1'
        """
        return await self.queue.get()

    async def join(self) -> None:
        """
        Asynchronously wait until all items in the queue have been processed.

        This method blocks until every input_ that has been enqueued is processed,
        ensuring that all tasks are completed.

        Example:
            >>> async_queue = AsyncQueue()
            >>> asyncio.run(async_queue.enqueue('Task 1'))
            >>> asyncio.run(async_queue.join())  # This will block until 'Task 1' is processed.
        """
        await self.queue.join()

    async def stop(self) -> None:
        """
        Signal the queue to stop processing new items.

        Once called, the queue will not process any new items after the current ones
        are completed, allowing for a graceful shutdown.

        Example:
            >>> async_queue = AsyncQueue()
            >>> asyncio.run(async_queue.stop())  # This signals the queue to stop processing.
        """
        self._stop_event.set()

    def stopped(self) -> bool:
        """
        Check if the queue has been signaled to stop processing.

        Returns:
            bool: True if a stop has been signaled, False otherwise.

        Example:
            >>> async_queue = AsyncQueue()
            >>> asyncio.run(async_queue.stop())
            >>> async_queue.stopped()
            True
        """
        return self._stop_event.is_set()

    # async def process_requests(self, func: Callable[[Any], Any]) -> None:
    #     """
    #     Asynchronously process items from the queue using the provided function.

    #     Continuously dequeues items and applies the given function to each.
    #     The processing stops when the queue is signaled to stop or a sentinel value (`None`) is dequeued.

    #     Parameters:
    #         func (Callable[[Any], Any]): A coroutine function to process items from the queue.

    #     Example:
    #         >>> async def sample_processing(task):
    #         ...     print("Processing:", task)
    #         >>> async_queue = AsyncQueue()
    #         >>> asyncio.run(async_queue.enqueue('Task 1'))
    #         >>> asyncio.run(async_queue.process_requests(sample_processing))
    #         Processing: Task 1
    #     """
    #     while not self.stopped():
    #         input_ = await self.dequeue()
    #         if input_ is None:  # Using `None` as a sentinel value to cease processing.
    #             await self.stop()
    #             break
    #         await func(input_)

    async def process_requests(self, func, timeout=None):
        """
        Process items with timeout management for each task.
        """
        tasks = set()
        while not self.stopped():
            if len(tasks) >= self.max_concurrent_tasks:
                done, tasks = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

            input_ = await self.dequeue()
            if input_ is None:
                await self.stop()
                break

            # Wrap the function call with tcall for timeout handling
            task = asyncio.create_task(tcall(input_, func, timeout=timeout))
            tasks.add(task)

        if tasks:
            await asyncio.wait(tasks)
