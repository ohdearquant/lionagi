import asyncio
from typing import Any, Callable, Dict, List
from pydantic import Field
from functools import wraps
from lionagi import logging as _logging
from lionagi.libs import func_call
from lionagi.core.generic import Component

from .schema import Work, WorkStatus
from ..work.worklog import WorkLog
from .worker import Worker


class WorkFunction(Component):
    """Work function management and execution."""

    function: Callable
    args: List[Any] = Field(default_factory=list)
    kwargs: Dict[str, Any] = Field(default_factory=dict)
    retry_kwargs: Dict[str, Any] = Field(default_factory=dict)
    worklog: WorkLog = Field(default_factory=WorkLog)
    instruction: str = Field(
        default="", description="Instruction for the work function"
    )
    refresh_time: float = Field(
        default=0.5, description="Time to wait before checking for pending work"
    )

    @property
    def name(self):
        """Get the name of the work function."""
        return self.function.__name__

    async def execute(self):
        """Execute pending work items."""
        while self.worklog.pending:
            work_id = self.worklog.pending.popleft()
            work = self.worklog.logs[work_id]
            if work.status == WorkStatus.PENDING:
                try:
                    await func_call.rcall(self._execute, work, **work.retry_kwargs)
                except Exception as e:
                    work.status = WorkStatus.FAILED
                    _logging.error(f"Work {work.id_} failed with error: {e}")
                    self.worklog.errored.append(work.id_)
            else:
                _logging.warning(
                    f"Work {work.id_} is in {work.status} state "
                    "and cannot be executed."
                )
            await asyncio.sleep(self.refresh_time)

    async def _execute(self, work: Work):
        """Execute a single work item."""
        work.status = WorkStatus.IN_PROGRESS
        result = await self.function(*self.args, **self.kwargs)
        work.deliverables = result
        work.status = WorkStatus.COMPLETED
        return result


def workfunc(func):

    @wraps(func)
    async def wrapper(self: Worker, *args, **kwargs):
        # Retrieve the worker instance ('self')
        if not hasattr(self, "work_functions"):
            self.work_functions = {}

        if func.__name__ not in self.work_functions:
            # Create WorkFunction with the function and its docstring as instruction
            self.work_functions[func.__name__] = WorkFunction(
                function=func,
                instruction=func.__doc__,
                args=args,
                kwargs=kwargs,
                retry_kwargs=kwargs.pop("retry_kwargs", {}),
            )

        # Retrieve the existing WorkFunction
        work_function: WorkFunction = self.work_functions[func.__name__]
        # Update args and kwargs for this call
        work_function.args = args
        work_function.kwargs = kwargs

        # Execute the function using WorkFunction's managed execution process
        return await work_function.execute()

    return wrapper
