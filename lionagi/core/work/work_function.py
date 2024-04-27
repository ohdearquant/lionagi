import asyncio
from lionagi.libs import func_call
from functools import wraps

from .schema import Work, WorkLog


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


def work(assignment=None, capacity=5, refresh_time=1):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, retry_kwargs=None, instruction=None, **kwargs):
            if getattr(self, "work_functions", None) is None:
                self.work_functions = {}

            if func.__name__ not in self.work_functions:
                self.work_functions[func.__name__] = WorkFunction(
                    assignment=assignment,
                    function=func,
                    retry_kwargs=retry_kwargs or {},
                    instruction=instruction or func.__doc__,
                    capacity=capacity,
                    refresh_time=refresh_time,
                )

            work_func: WorkFunction = self.work_functions[func.__name__]
            task = asyncio.create_task(work_func.perform(self, *args, **kwargs))
            work = Work(async_task=task)
            await work_func.worklog.append(work)
            work_func.worklog.add_count += 1
            return True

        return wrapper

    return decorator
