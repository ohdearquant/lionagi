import asyncio
from typing import Callable, Any
from lionagi.libs import func_call
from functools import wraps
from pydantic import Field

from lionagi.core.generic import Component

from .schema import Work, WorkLog



class WorkFunction:
    
    def __init__(
        self, assignment, function, retry_kwargs=None, 
        instruction = None, capacity=5
    ):
        
        self.assignment = assignment
        self.function = function
        self.retry_kwargs = retry_kwargs or {}
        self.instruction = instruction or function.__doc__
        self.worklog = WorkLog(capacity=capacity)
    
    
    @property
    def name(self):
        return self.function.__name__

    async def perform(self, *args, **kwargs):
        kwargs = {**self.retry_kwargs, **kwargs}
        return await func_call.rcall(self.function, *args, **kwargs)
    
    async def process(self, refresh_time=1):
        await self.worklog.process(refresh_time=refresh_time)
    
    async def stop(self):
        await self.worklog.queue.stop()
    
    
    
def work(assignment, capacity=5):
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
                    capacity=capacity
                )
                
            work_func: WorkFunction = self.work_functions[func.__name__]
            task = asyncio.create_task(work_func.perform(*args, **kwargs))
            work = Work(async_task=task)
            work_func: WorkFunction = self.work_functions[func.__name__]
            await work_func.worklog.append(work)
            return True
        
        return wrapper
    return decorator
