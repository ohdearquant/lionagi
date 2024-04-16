from collections import deque
from enum import Enum
from typing import Any, Callable, Dict, List

from pydantic import BaseModel, Field

from lionagi import logging as _logging
from lionagi.core.generic import BaseComponent
from lionagi.libs import func_call
import asyncio

class WorkStatus(str, Enum):
    """Enum to represent different statuses of work."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Work(BaseComponent):
    """Base component for handling individual units of work."""
    form_id: str = Field(..., description="ID of the form for this work")
    priority: int = Field(default=0, description="Priority of the work")
    status: WorkStatus = Field(
        default=WorkStatus.PENDING, description="Current status of the work"
    )
    deliverables: Dict[str, Any] | list = Field(
        default={}, description="Deliverables produced by the work"
    )
    dependencies: List['Work'] = Field(
        default_factory=list, description="List of work items this work depends on"
    )


class WorkLog(BaseModel):
    """Model to store and manage work logs."""
    logs: Dict[str, Work] = Field(default={}, description="Logs of work items")
    pending: deque = Field(
        default_factory=de, description="Priority queue of pending work items"
    )
    errored: deque = Field(
        default_factory=deque, description="Queue of errored work items"
    )
    
    def append(self, work: Work):
        """Append a work item to the logs and pending queue."""
        self.logs[str(work.form_id)] = work
        self.pending.append(str(work.form_id))

    def get_by_status(self, status: WorkStatus) -> Dict[str, Work]:
        """Get work items by their status."""
        return {
            wid: work for wid, work in self.logs.items() if work.status == status
        }


class WorkFunction(BaseComponent):
    """Work function management and execution."""
    function: Callable
    args: List[Any] = Field(default_factory=list)
    kwargs: Dict[str, Any] = Field(default_factory=dict)
    retry_kwargs: Dict[str, Any] = Field(default_factory=dict)
    worklog: WorkLog = Field(default_factory=WorkLog)
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
    
    

import unittest
from unittest.mock import AsyncMock, patch

from lionagi.libs import func_call


class TestWork(unittest.TestCase):
    def setUp(self):
        self.work = Work(form_id="123")

    def test_initial_status(self):
        """Test the initial status is set to PENDING."""
        self.assertEqual(self.work.status, WorkStatus.PENDING)

    def test_initial_deliverables(self):
        """Test the initial deliverables are empty."""
        self.assertEqual(self.work.deliverables, {})

    def test_initial_dependencies(self):
        """Test the initial dependencies are empty."""
        self.assertEqual(self.work.dependencies, [])


class TestWorkLog(unittest.TestCase):
    def setUp(self):
        self.work_log = WorkLog()
        self.work = Work(form_id="123")
        self.work_log.append(self.work)

    def test_append_work(self):
        """Test appending work adds to logs and pending queue."""
        self.assertIn("123", self.work_log.logs)
        self.assertIn("123", self.work_log.pending)

    def test_get_by_status(self):
        """Test retrieving works by status."""
        result = self.work_log.get_by_status(WorkStatus.PENDING)
        self.assertEqual(result, {"123": self.work})


class TestWorkFunction(unittest.TestCase):
    def setUp(self):
        self.work_function = WorkFunction(function=AsyncMock(return_value="result"))
        self.work = Work(form_id="123")
        self.work_log = WorkLog()
        self.work_log.append(self.work)
        self.work_function.worklog = self.work_log

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_execute(self, mocked_sleep):
        """Test executing work changes its status and handles results."""
        with patch.object(func_call, 'rcall', new_callable=AsyncMock) as mock_rcall:
            mock_rcall.return_value = "completed"
            await self.work_function.execute()
            self.assertEqual(self.work.status, WorkStatus.COMPLETED)
            self.assertNotIn("123", self.work_function.worklog.pending)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_execute_failure(self, mocked_sleep):
        """Test handling failure during work execution."""
        with patch.object(func_call, 'rcall', side_effect=Exception("Error")):
            await self.work_function.execute()
            self.assertEqual(self.work.status, WorkStatus.FAILED)
            self.assertIn("123", self.work_function.worklog.errored)


if __name__ == "__main__":
    unittest.main()