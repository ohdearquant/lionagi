import asyncio
from typing import Any

import pytest

from lionagi.protocols.action.function_calling import FunctionCalling, Tool
from lionagi.protocols.generic.event import EventStatus


# Helper functions - not test cases
async def helper_async_func(x: int = 0, y: str = "default") -> str:
    await asyncio.sleep(0.1)
    return f"{x}-{y}"


def helper_sync_func(x: int = 0, y: str = "default") -> str:
    return f"{x}-{y}"


async def helper_preprocessor(value: Any, **kwargs) -> Any:
    if isinstance(value, int):
        return value + 1
    return value


async def helper_postprocessor(result: Any, **kwargs) -> str:
    return f"processed-{result}"


def helper_parser(result: Any) -> str:
    return str(result)


@pytest.fixture
def tool_with_processors():
    return Tool(
        func_callable=helper_sync_func,
        preprocessor=helper_preprocessor,
        postprocessor=helper_postprocessor,
    )


@pytest.fixture
def async_tool():
    return Tool(func_callable=helper_async_func)


@pytest.mark.asyncio
async def test_function_calling_init():
    tool = Tool(func_callable=helper_sync_func)
    arguments = {"x": 1, "y": "test"}

    func_call = FunctionCalling(func_tool=tool, arguments=arguments)
    assert func_call.func_tool == tool
    assert func_call.arguments == arguments
    assert func_call.function == "helper_sync_func"
    assert func_call.status == EventStatus.PENDING


@pytest.mark.asyncio
async def test_function_calling_with_sync_function():
    tool = Tool(func_callable=helper_sync_func)
    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1, "y": "test"})

    await func_call.invoke()
    assert func_call.status == EventStatus.COMPLETED
    assert func_call.execution.duration is not None
    assert func_call.execution.response == "1-test"
    assert func_call.execution.error is None


@pytest.mark.asyncio
async def test_function_calling_with_async_function(async_tool):
    func_call = FunctionCalling(func_tool=async_tool, arguments={"x": 1, "y": "test"})

    await func_call.invoke()
    assert func_call.status == EventStatus.COMPLETED
    assert func_call.execution.duration is not None
    assert func_call.execution.response == "1-test"
    assert func_call.execution.error is None


@pytest.mark.asyncio
async def test_function_calling_with_parser(tool_with_processors):
    func_call = FunctionCalling(func_tool=tool_with_processors, arguments={"x": 1, "y": "test"})

    result = await func_call.invoke()
    assert isinstance(func_call.response, str)
    assert func_call.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_function_calling_error_handling():
    async def error_func(**kwargs):
        raise ValueError("Test error")

    tool = Tool(func_callable=error_func)
    func_call = FunctionCalling(func_tool=tool, arguments={})

    await func_call.invoke()  # total: tool failure captured as FAILED, not raised
    assert func_call.status == EventStatus.FAILED
    assert "Test error" in str(func_call.execution.error)
    assert func_call.execution.duration is not None


def test_function_calling_str_representation():
    tool = Tool(func_callable=helper_sync_func)
    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1, "y": "test"})

    # Test __str__
    str_rep = str(func_call)
    assert "helper_sync_func" in str_rep
    assert "{'x': 1, 'y': 'test'}" in str_rep

    # Test __repr__
    repr_rep = repr(func_call)
    assert "FunctionCalling" in repr_rep
    assert "helper_sync_func" in repr_rep
    assert "{'x': 1, 'y': 'test'}" in repr_rep


@pytest.mark.asyncio
async def test_function_calling_with_empty_arguments():
    tool = Tool(func_callable=helper_sync_func)
    func_call = FunctionCalling(func_tool=tool, arguments={})

    result = await func_call.invoke()
    assert func_call.response == "0-default"  # Should use default values
    assert func_call.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_function_calling_processor_error():
    async def error_processor(value: Any, **kwargs) -> Any:
        raise ValueError("Processor error")

    tool = Tool(func_callable=helper_sync_func, preprocessor=error_processor)

    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1})

    await func_call.invoke()  # total: processor failure captured as FAILED, not raised
    assert func_call.response is None
    assert func_call.status == EventStatus.FAILED
    assert "Processor error" in str(func_call.execution.error)


##################################################
#                  Helper Functions              #
##################################################
def strict_func(a: int, b: str) -> str:
    """All parameters are required, no defaults."""
    return f"{a}-{b}"


def non_strict_func(a: int, b: str = "default", c: bool = True) -> str:
    """Only 'a' is strictly required; 'b' and 'c' have defaults."""
    return f"{a}-{b}-{c}"


##################################################
#                Strict Mode Tests               #
##################################################


@pytest.mark.asyncio
async def test_strict_mode_exact_arguments():
    tool = Tool(func_callable=strict_func, strict_func_call=True)
    func_call = FunctionCalling(func_tool=tool, arguments={"a": 10, "b": "required"})
    await func_call.invoke()

    assert func_call.status == EventStatus.COMPLETED
    assert func_call.response == "10-required"


@pytest.mark.asyncio
async def test_strict_mode_missing_argument():
    tool = Tool(func_callable=strict_func, strict_func_call=True)
    # Missing 'b'
    with pytest.raises(ValueError) as exc_info:
        FunctionCalling(func_tool=tool, arguments={"a": 5})
    assert "must match the function schema" in str(exc_info.value)


@pytest.mark.asyncio
async def test_strict_mode_extra_argument():
    tool = Tool(func_callable=strict_func, strict_func_call=True)
    # Extra 'c'
    with pytest.raises(ValueError) as exc_info:
        FunctionCalling(func_tool=tool, arguments={"a": 5, "b": "ok", "c": 99})
    assert "must match the function schema" in str(exc_info.value)


##################################################
#               Non-strict Mode Tests            #
##################################################


@pytest.mark.asyncio
async def test_non_strict_mode_minimum_required():
    tool = Tool(func_callable=non_strict_func, strict_func_call=False)
    # 'b' and 'c' are optional in the Python signature, so we only need 'a'.
    func_call = FunctionCalling(func_tool=tool, arguments={"a": 42})
    await func_call.invoke()

    assert func_call.status == EventStatus.COMPLETED
    assert func_call.response == "42-default-True"


@pytest.mark.asyncio
async def test_non_strict_mode_extra_arguments():
    tool = Tool(func_callable=non_strict_func, strict_func_call=False)
    # 'd' is extra, not in the function signature.
    func_call = FunctionCalling(func_tool=tool, arguments={"a": 1, "b": "override"})
    await func_call.invoke()

    assert func_call.status == EventStatus.COMPLETED
    # The function itself won't use 'd', but no error is raised.
    assert func_call.response == "1-override-True"


@pytest.mark.asyncio
async def test_non_strict_mode_missing_required_argument():
    tool = Tool(func_callable=non_strict_func, strict_func_call=False)
    # 'a' is required, so if we omit it, we fail.
    with pytest.raises(ValueError) as exc_info:
        FunctionCalling(func_tool=tool, arguments={})
    assert "must match the function schema" in str(exc_info.value)


##################################################
#   Regression: is_coro_func-gated behavior      #
##################################################


@pytest.mark.asyncio
async def test_sync_func_callable_returning_coroutine_is_not_awaited():
    """Sync func_callable that returns a coroutine must NOT be awaited.

    Old origin/main only awaited when is_coro_func(func_callable) is True.
    A sync function that happens to return a coroutine object must hand that
    coroutine back to the caller as-is, not resolve it.
    """
    import asyncio

    async def _inner():
        return 7

    def sync_returns_coro(x: int = 0):
        # Sync callable that constructs and returns a coroutine — intentionally.
        return _inner()

    tool = Tool(func_callable=sync_returns_coro)
    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1})
    await func_call.invoke()

    # The response must be the coroutine object itself, not the resolved value 7.
    assert asyncio.iscoroutine(func_call.execution.response), (
        "Expected response to be a coroutine object because sync callable; "
        f"got {type(func_call.execution.response)!r} = {func_call.execution.response!r}"
    )
    # Clean up the unawaited coroutine to avoid ResourceWarning.
    func_call.execution.response.close()


@pytest.mark.asyncio
async def test_sync_preprocessor_returning_coroutine_is_not_awaited():
    """Sync preprocessor that returns a coroutine must NOT be awaited.

    The sync preprocessor's return value is stored as self.arguments without
    being awaited.  A coroutine is not a valid kwargs dict, so func_callable
    receives a coroutine and raises TypeError — the event ends FAILED and
    self.arguments still holds the coroutine object.
    """
    import asyncio

    async def _inner(v):
        return v + 10

    def sync_pre_returns_coro(kwargs, **kw):
        # Sync preprocessor that returns a coroutine — intentionally.
        return _inner(kwargs.get("x", 0))

    tool = Tool(
        func_callable=helper_sync_func,
        preprocessor=sync_pre_returns_coro,
    )
    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1, "y": "t"})
    await func_call.invoke()

    # The coroutine was NOT awaited → stored as arguments → TypeError → FAILED.
    assert func_call.status == EventStatus.FAILED, (
        "Expected FAILED because a coroutine cannot be unpacked as kwargs; "
        f"got status={func_call.status!r}"
    )
    assert asyncio.iscoroutine(func_call.arguments), (
        "Expected func_call.arguments to be the unawaited coroutine object; "
        f"got {type(func_call.arguments)!r}"
    )
    # Close the coroutine to avoid ResourceWarning about unawaited coroutines.
    func_call.arguments.close()


@pytest.mark.asyncio
async def test_sync_postprocessor_returning_coroutine_is_not_awaited():
    """Sync postprocessor that returns a coroutine must NOT be awaited.

    The response stored in execution is the coroutine object itself, not the
    resolved value — matching origin/main is_coro_func-gated behavior.
    """
    import asyncio

    async def _inner(v):
        return f"resolved:{v}"

    def sync_post_returns_coro(result, **kw):
        # Sync postprocessor that returns a coroutine — intentionally.
        return _inner(result)

    tool = Tool(
        func_callable=helper_sync_func,
        postprocessor=sync_post_returns_coro,
    )
    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1, "y": "t"})
    await func_call.invoke()

    assert func_call.status == EventStatus.COMPLETED, (
        f"Expected COMPLETED; got {func_call.status!r} error={func_call.execution.error!r}"
    )
    assert asyncio.iscoroutine(func_call.execution.response), (
        "Expected response to be a coroutine object because sync postprocessor; "
        f"got {type(func_call.execution.response)!r} = {func_call.execution.response!r}"
    )
    # Close the coroutine to avoid ResourceWarning.
    func_call.execution.response.close()
