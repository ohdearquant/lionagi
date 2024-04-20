from .schema import Work, WorkStatus
from ._logger import WorkLog
from .work_function import WorkFunction

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