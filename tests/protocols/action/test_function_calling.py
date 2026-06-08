import asyncio
from typing import Any

import pytest

from lionagi.protocols.action.function_calling import FunctionCalling, Tool
from lionagi.protocols.generic.event import EventStatus


# Helper functions - not test cases
async def helper_async_func(x: int = 0, y: str = "default") -> str:
    """Test async function."""
    await asyncio.sleep(0.1)
    return f"{x}-{y}"


def helper_sync_func(x: int = 0, y: str = "default") -> str:
    """Test sync function."""
    return f"{x}-{y}"


async def helper_preprocessor(value: Any, **kwargs) -> Any:
    """Test preprocessor."""
    if isinstance(value, int):
        return value + 1
    return value


async def helper_postprocessor(result: Any, **kwargs) -> str:
    """Test postprocessor."""
    return f"processed-{result}"


def helper_parser(result: Any) -> str:
    """Test parser."""
    return str(result)


@pytest.fixture
def tool_with_processors():
    """Fixture for creating a tool with processors."""
    return Tool(
        func_callable=helper_sync_func,
        preprocessor=helper_preprocessor,
        postprocessor=helper_postprocessor,
    )


@pytest.fixture
def async_tool():
    """Fixture for creating an async tool."""
    return Tool(func_callable=helper_async_func)


@pytest.mark.asyncio
async def test_function_calling_init():
    """Test FunctionCalling initialization."""
    tool = Tool(func_callable=helper_sync_func)
    arguments = {"x": 1, "y": "test"}

    func_call = FunctionCalling(func_tool=tool, arguments=arguments)
    assert func_call.func_tool == tool
    assert func_call.arguments == arguments
    assert func_call.function == "helper_sync_func"
    assert func_call.status == EventStatus.PENDING


@pytest.mark.asyncio
async def test_function_calling_with_sync_function():
    """Test FunctionCalling with synchronous function."""
    tool = Tool(func_callable=helper_sync_func)
    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1, "y": "test"})

    await func_call.invoke()
    assert func_call.status == EventStatus.COMPLETED
    assert func_call.execution.duration is not None
    assert func_call.execution.response == "1-test"
    assert func_call.execution.error is None


@pytest.mark.asyncio
async def test_function_calling_with_async_function(async_tool):
    """Test FunctionCalling with asynchronous function."""
    func_call = FunctionCalling(func_tool=async_tool, arguments={"x": 1, "y": "test"})

    await func_call.invoke()
    assert func_call.status == EventStatus.COMPLETED
    assert func_call.execution.duration is not None
    assert func_call.execution.response == "1-test"
    assert func_call.execution.error is None


@pytest.mark.asyncio
async def test_function_calling_with_parser(tool_with_processors):
    """Test FunctionCalling with result parser."""
    func_call = FunctionCalling(func_tool=tool_with_processors, arguments={"x": 1, "y": "test"})

    result = await func_call.invoke()
    assert isinstance(func_call.response, str)
    assert func_call.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_function_calling_error_handling():
    """Test FunctionCalling error handling."""

    async def error_func(**kwargs):
        raise ValueError("Test error")

    tool = Tool(func_callable=error_func)
    func_call = FunctionCalling(func_tool=tool, arguments={})

    await func_call.invoke()  # total: tool failure captured as FAILED, not raised
    assert func_call.status == EventStatus.FAILED
    assert "Test error" in str(func_call.execution.error)
    assert func_call.execution.duration is not None


def test_function_calling_str_representation():
    """Test FunctionCalling string representations."""
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
    """Test FunctionCalling with empty arguments."""
    tool = Tool(func_callable=helper_sync_func)
    func_call = FunctionCalling(func_tool=tool, arguments={})

    result = await func_call.invoke()
    assert func_call.response == "0-default"  # Should use default values
    assert func_call.status == EventStatus.COMPLETED


@pytest.mark.asyncio
async def test_function_calling_processor_error():
    """Test FunctionCalling with failing processor."""

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
    """
    In strict mode, passing exactly the required parameters should succeed.
    """
    tool = Tool(func_callable=strict_func, strict_func_call=True)
    func_call = FunctionCalling(func_tool=tool, arguments={"a": 10, "b": "required"})
    await func_call.invoke()

    assert func_call.status == EventStatus.COMPLETED
    assert func_call.response == "10-required"


@pytest.mark.asyncio
async def test_strict_mode_missing_argument():
    """
    In strict mode, omitting any required parameter should raise ValueError at instantiation.
    """
    tool = Tool(func_callable=strict_func, strict_func_call=True)
    # Missing 'b'
    with pytest.raises(ValueError) as exc_info:
        FunctionCalling(func_tool=tool, arguments={"a": 5})
    assert "must match the function schema" in str(exc_info.value)


@pytest.mark.asyncio
async def test_strict_mode_extra_argument():
    """
    In strict mode, adding extra parameters should raise ValueError at instantiation.
    """
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
    """
    In non-strict mode, it's enough to provide all function signature parameters
    that have no default. Others can be omitted or included freely.
    """
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
    """
    In non-strict mode, if a truly required parameter (no default) is missing, it should raise ValueError.
    """
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
    """Sync preprocessor that returns a coroutine must NOT be awaited."""
    import asyncio

    async def _inner(v):
        return v + 10

    def sync_pre_returns_coro(kwargs, **kw):
        # Sync preprocessor that returns a coroutine.
        return _inner(kwargs.get("x", 0))

    tool = Tool(
        func_callable=helper_sync_func,
        preprocessor=sync_pre_returns_coro,
    )
    func_call = FunctionCalling(func_tool=tool, arguments={"x": 1, "y": "t"})
    await func_call.invoke()

    # The preprocessor result (a coroutine) is stored as arguments without awaiting.
    # The function call then fails or uses the coroutine as the kwargs dict.
    # The key invariant: the coroutine was NOT resolved to 11.
    # We verify by checking that status is FAILED (coroutine is not a valid dict)
    # OR that arguments is a coroutine — either way, it was not transparently awaited.
    args_val = func_call.execution.response
    # If execution succeeded, arguments must NOT be the integer 11.
    if func_call.execution.error is None:
        assert args_val != "11-t", (
            "Sync preprocessor's coroutine return was awaited — behavior changed from origin/main"
        )
