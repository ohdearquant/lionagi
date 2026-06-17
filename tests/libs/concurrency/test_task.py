import pytest

from lionagi.ln.concurrency.task import TaskGroup, create_task_group


@pytest.mark.asyncio
async def test_task_group_creation():
    async with create_task_group() as tg:
        assert isinstance(tg, TaskGroup)


@pytest.mark.asyncio
async def test_task_group_start_soon():
    results = []

    async def task(value):
        results.append(value)

    async with create_task_group() as tg:
        tg.start_soon(task, 1)
        tg.start_soon(task, 2)
        tg.start_soon(task, 3)

    # After the task group exits, all tasks should be complete
    assert sorted(results) == [1, 2, 3]


@pytest.mark.asyncio
async def test_task_group_start():
    ran = []

    async def simple_task():
        ran.append("done")

    async with create_task_group() as tg:
        tg.start_soon(simple_task)

    assert ran == ["done"], "Task should have run before task group exits"


@pytest.mark.asyncio
async def test_task_group_error_propagation():
    async def raising_task():
        raise ValueError("propagated")

    with pytest.raises((ValueError, Exception)) as exc_info:
        async with create_task_group() as tg:
            tg.start_soon(raising_task)

    # Either ValueError directly or wrapped in ExceptionGroup
    exc = exc_info.value
    if hasattr(exc, "exceptions"):
        assert any(isinstance(e, ValueError) for e in exc.exceptions)
    else:
        assert isinstance(exc, ValueError)
        assert "propagated" in str(exc)


@pytest.mark.asyncio
async def test_task_group_multiple_errors():
    async def failing_task_1():
        raise ValueError("Task 1 failed")

    async def failing_task_2():
        raise RuntimeError("Task 2 failed")

    try:
        async with create_task_group() as tg:
            tg.start_soon(failing_task_1)
            tg.start_soon(failing_task_2)
    except Exception as eg:
        # Check that both exceptions are in the group
        assert len(eg.exceptions) == 2
        assert any(isinstance(e, ValueError) for e in eg.exceptions)
        assert any(isinstance(e, RuntimeError) for e in eg.exceptions)
    else:
        pytest.fail("Expected ExceptionGroup was not raised")
