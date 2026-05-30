from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.work import WorkItemStatus
from lionagi.work.engine import WorkerEngine
from lionagi.work.worker import clear_worker_registry, worker


@pytest.mark.asyncio
async def test_submit_cancel_status():
    clear_worker_registry()

    branch = MagicMock()
    branch.operate = AsyncMock(return_value={"status": "ok"})

    @worker()
    def noop(**kwargs):
        return {"instruction": "noop"}

    engine = WorkerEngine(branch=branch)
    item_id = await engine.submit("noop", {"instruction": "noop"})

    cancelled = await engine.cancel(item_id)
    assert cancelled
    item = await engine.status(item_id)
    assert item.status == WorkItemStatus.CANCELLED


@pytest.mark.asyncio
async def test_run_queue_dependency_order():
    clear_worker_registry()

    branch = MagicMock()
    execution: list[str] = []

    async def _fake_operate(*, instruction: str, **kwargs):
        execution.append(instruction)
        return {"instruction": instruction}

    branch.operate = AsyncMock(side_effect=_fake_operate)

    @worker()
    def parse_input(**kwargs):
        return {"instruction": kwargs["instruction"]}

    @worker()
    def render_output(**kwargs):
        return {"instruction": kwargs["instruction"]}

    engine = WorkerEngine(branch=branch)
    first_id = await engine.submit("parse_input", {"instruction": "first"})
    await engine.submit("render_output", {"instruction": "second"}, depends_on=[first_id])

    completed = await engine.run_queue()
    assert len(completed) == 2
    assert execution == ["first", "second"]
    assert all(item.status == WorkItemStatus.COMPLETED for item in completed)


@pytest.mark.asyncio
async def test_run_queue_uses_branch_operate():
    clear_worker_registry()

    branch = MagicMock()
    branch.operate = AsyncMock(return_value={"result": 123})

    @worker()
    def compute(**kwargs):
        return {"instruction": "compute"}

    engine = WorkerEngine(branch=branch)
    await engine.submit("compute", {"instruction": "compute"})
    await engine.run_queue()

    branch.operate.assert_awaited_once()
