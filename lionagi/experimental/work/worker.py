from abc import ABC
import asyncio
import logging
from typing import Any, Callable
from functools import wraps
from lionagi.libs import func_call
from lionagi.core import Session
from ..report.report import Report, Form
from .schema import Work, WorkFunction


class Worker(ABC):
    # This is a class that will be used to create a worker object
    # work_functions are keyed by assignment {assignment: WorkFunction}

    name: str = "Worker"
    work_functions: dict[str, WorkFunction] = {}
    stopped: bool = False
    reports: dict[str, Report] = {}
    default_form: Form = Form
    session: Session = Session()

    async def append_report(self, report: Report):
        if report.id_ not in self.reports:
            self.reports[report.id_] = report
            return True
        return False

    @property
    def workable_reports(self) -> dict | None:
        if len(a := {k: v for k, v in self.reports.items() if v.workable}) > 0:
            return a
        return None

    async def stop(self):
        self.stopped = True
        logging.info(f"Stopping worker {self.name}")
        non_stopped_ = []

        for func in self.work_functions.values():
            worklog = func.worklog
            await worklog.stop()
            if not worklog.stopped:
                non_stopped_.append(func.name)

        if len(non_stopped_) > 0:
            logging.error(f"Could not stop worklogs: {non_stopped_}")

        logging.info(f"Stopped worker {self.name}")

    async def process(self, refresh_time=1):
        while self.workable_reports and not self.stopped:
            tasks = [
                asyncio.create_task(func.process())
                for func in self.work_functions.values()
            ]
            await asyncio.wait(tasks)

        await asyncio.sleep(refresh_time)


def work(assignment=None, capacity=5, refresh_time=1):
    def decorator(func: Callable):

        @wraps(func)
        async def wrapper(self: Worker, **kwargs):
            if getattr(self, "work_functions", None) is None:
                self.work_functions = {}

            if assignment not in self.work_functions:
                self.work_functions[assignment] = WorkFunction(
                    assignment=assignment,
                    function=func,
                    retry_kwargs=kwargs.get("retry_kwargs") or {},
                    instruction=kwargs.get("instruction", None) or func.__doc__,
                    capacity=capacity,
                    refresh_time=refresh_time,
                )

            work_func: WorkFunction = self.work_functions[assignment]

            form: Form = kwargs.get("form", None) or self.default_form(
                assignment=work_func.assignment
            )

            for k, v in kwargs.items():
                if k in form.work_fields:
                    form.fill(**{k: v})

            try:
                form.check_workable()
            except Exception as e:
                raise ValueError(f"Not workable") from e

            task = asyncio.create_task(work_func.perform(self, **kwargs))
            work = Work(async_task=task, form_id=form.id_)
            await work_func.worklog.append(work)
            work_func.worklog.add_count += 1
            return True

        return wrapper

    return decorator


# # Example
# from lionagi import Session
# from lionagi.experimental.work.work_function import work


# class MyWorker(Worker):

#     @work(assignment="instruction, context -> response")
#     async def chat(instruction=None, context=None):
#         session = Session()
#         return await session.chat(instruction=instruction, context=context)


# await a.chat(instruction="Hello", context={})
