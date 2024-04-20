import asyncio
import unittest
from unittest.mock import patch
from lionagi.libs.ln_queue import AsyncQueue


class TestAsyncQueue(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = AsyncQueue(max_concurrent_tasks=3)

    def tearDown(self):
        self.loop.close()

    def test_enqueue_dequeue(self):
        async def test():
            await self.queue.enqueue('task1')
            result = await self.queue.dequeue()
            self.assertEqual(result, 'task1')
        self.loop.run_until_complete(test())

    def test_stop(self):
        async def test():
            await self.queue.stop()
            self.assertTrue(self.queue.stopped())
        self.loop.run_until_complete(test())

    @patch('lionagi.libs.ln_func_call.rcall', autospec=True)
    def test_process_requests(self, mock_rcall):
        future = asyncio.Future()
        future.set_result("Processed successfully")
        mock_rcall.return_value = future

        async def processor(task):
            return f"Processed {task}"

        async def add_tasks():
            for i in range(5):
                await self.queue.enqueue(f"task{i}")
            # This ensures that the queue stops after all tasks are added
            await self.queue.stop()

        async def process():
            await self.queue.process_requests(processor, retry_kwargs={'retries': 2})

        self.loop.create_task(add_tasks())
        self.loop.run_until_complete(process())
        # mock_rcall.assert_called()

    def test_concurrency_limit(self):
        async def add_tasks():
            for i in range(10):
                await self.queue.enqueue(f"task{i}")

        async def process():
            await asyncio.sleep(0.1)
            await self.queue.stop()

        self.loop.run_until_complete(add_tasks())
        self.loop.run_until_complete(process())

if __name__ == "__main__":
    unittest.main()
