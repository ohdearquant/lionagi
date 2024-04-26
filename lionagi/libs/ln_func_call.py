from __future__ import annotations

import functools
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from typing import Any, Callable, Coroutine

from lionagi.libs.sys_util import SysUtil
from lionagi.libs.ln_convert import to_list
from lionagi.libs.ln_async import AsyncUtil


def lcall(
    input_: Any,
    /,
    func: Callable,
    *,
    flatten: bool = False,
    dropna: bool = False,
    **kwargs,
) -> list[Any]:
    """
    applies a function to each element of the input list, with options to flatten
    results and drop None values.

    this function facilitates the batch application of a transformation or operation
    to each input_ in an input list. it is versatile, supporting both flattening of the
    result list and removal of None values from the output, making it suitable for a
    wide range of data manipulation tasks. additional arguments to the applied
    function can be passed dynamically, allowing for flexible function application.

    Args:
            input_ (Any):
                    The input list or iterable to process. each element will be passed to the
                    provided `func` Callable.
            func (Callable):
                    The function to apply to each element of `input_`. this function can be any
                    Callable that accepts the elements of `input_` as arguments.
            flatten (bool, optional):
                    If True, the resulting list is flattened. useful when `func` returns a list.
                    defaults to False.
            dropna (bool, optional):
                    If True, None values are removed from the final list. defaults to False.
            **kwargs:
                    Additional keyword arguments to be passed to `func`.

    Returns:
            list[Any]:
                    The list of results after applying `func` to each input element, modified
                    according to `flatten` and `dropna` options.

    Examples:
            Apply a doubling function to each element:
            >>> lcall([1, 2, 3], lambda x: x * 2)
            [2, 4, 6]

            apply a function that returns lists, then flatten the result:
            >>> lcall([1, 2, None], lambda x: [x, x] if x else x, flatten=True, dropna=True)
            [1, 1, 2, 2]
    """
    lst = to_list(input_, dropna=dropna)
    if len(to_list(func)) != 1:
        raise ValueError("There must be one and only one function for list calling.")

    return to_list([func(i, **kwargs) for i in lst], flatten=flatten, dropna=dropna)


async def alcall(
    input_: Any | None = None,
    func: Callable = None,
    *,
    flatten: bool = False,
    dropna=False,
    **kwargs,
) -> list[Any]:
    # noinspection GrazieInspection
    """
    asynchronously applies a function to each element in the input.

    this async function is designed for operations where a given async function needs to
    be applied to each element of an input list concurrently. it allows for the results
    to be flattened and provides the flexibility to pass additional keyword arguments to
    the function being applied. this is especially useful in scenarios where processing
    each element of the input can be performed independently and concurrently, improving
    efficiency and overall execution time.

    Args:
            input_ (Any, optional):
                    The input to process. defaults to None, which requires `func` to be capable of
                    handling the absence of explicit input.
            func (Callable, optional):
                    The asynchronous function to apply. defaults to None.
            flatten (bool, optional):
                    Whether to flatten the result. useful when `func` returns a list or iterable
                    that should be merged into a single list. defaults to False.
            **kwargs:
                    Keyword arguments to pass to the function.

    Returns:
            list[Any]:
                    A list of results after asynchronously applying the function to each element
                    of the input, potentially flattened.

    examples:
            >>> async def square(x): return x * x
            >>> await alcall([1, 2, 3], square)
            [1, 4, 9]
    """
    tasks = []
    if input_ is not None:
        lst = to_list(input_)
        tasks = [AsyncUtil.handle_async_sync(func, i, **kwargs) for i in lst]

    else:
        tasks = [AsyncUtil.handle_async_sync(func, **kwargs)]

    outs = await AsyncUtil.execute_tasks(*tasks)
    outs_ = []
    for i in outs:
        outs_.append(await i if isinstance(i, (Coroutine, asyncio.Future)) else i)

    return to_list(outs_, flatten=flatten, dropna=dropna)


async def mcall(
    input_: Any, /, func: Any, *, explode: bool = False, **kwargs
) -> tuple[Any]:
    """
    asynchronously map a function or functions over an input_ or inputs.

    Args:
            input_ (Any):
                    The input_ or inputs to process.
            func (Any):
                    The function or functions to apply.
            explode (bool, optional):
                    Whether to apply each function to each input_. default is False.
            **kwargs:
                    Keyword arguments to pass to the function.

    Returns:
            list[Any]: A list of results after applying the function(s).

    examples:
            >>> async def add_one(x):
            >>>     return x + 1
            >>> asyncio.run(mcall([1, 2, 3], add_one))
            [2, 3, 4]

    """
    inputs_ = to_list(input_, dropna=True)
    funcs_ = to_list(func, dropna=True)

    if explode:
        tasks = [_alcall(inputs_, f, flatten=True, **kwargs) for f in funcs_]
    elif len(inputs_) == len(funcs_):
        tasks = [
            AsyncUtil.handle_async_sync(func, inp, **kwargs)
            for inp, func in zip(inputs_, funcs_)
        ]
    else:
        raise ValueError(
            "Inputs and functions must be the same length for map calling."
        )
    return await AsyncUtil.execute_tasks(*tasks)


async def bcall(
    input_: Any, /, func: Callable, *, batch_size: int, **kwargs
) -> list[Any]:
    """
    asynchronously call a function on batches of inputs.

    Args:
            input_ (Any): The input_ to process.
            func (Callable): The function to apply.
            batch_size (int): The size of each batch.
            **kwargs: Keyword arguments to pass to the function.

    Returns:
            list[Any]: A list of results after applying the function in batches.

    examples:
            >>> async def sum_batch(batch_): return sum(batch_)
            >>> asyncio.run(bcall([1, 2, 3, 4], sum_batch, batch_size=2))
            [3, 7]
    """
    results = []
    input_ = to_list(input_)
    for i in range(0, len(input_), batch_size):
        batch = input_[i : i + batch_size]
        batch_results = await alcall(batch, func, **kwargs)
        results.extend(batch_results)

    return results


async def tcall(
    func: Callable,
    *args,
    delay: float = 0,
    err_msg: str | None = None,
    ignore_err: bool = False,
    timing: bool = False,
    timeout: float | None = None,
    **kwargs,
) -> Any:
    """
    asynchronously executes a function with an optional delay, error handling, and timing.

    this utility allows for the asynchronous invocation of a Callable with added controls
    for execution delay, customizable timeout, and optional error suppression. it can
    also measure the execution time if required. this function is useful in scenarios
    where operations need to be scheduled with a delay, executed within a certain time
    frame, or when monitoring execution duration.

    Args:
            func (Callable):
                    The asynchronous function to be called.
            *args:
                    Positional arguments to pass to the function.
            delay (float, optional):
                    Time in seconds to wait before executing the function. default to 0.
            err_msg (str | None, optional):
                    Custom error message to display if an error occurs. defaults to None.
            ignore_err (bool, optional):
                    If True, suppresses any errors that occur during function execution,
                    optionally returning a default value. defaults to False.
            timing (bool, optional):
                    If True, returns a tuple containing the result of the function and the
                    execution duration in seconds. defaults to False.
            timeout (float | None, optional):
                    Maximum time in seconds allowed for the function execution. if the execution
                    exceeds this time, a timeout error is raised. defaults to None.
            **kwargs:
                    Keyword arguments to pass to the function.

    Returns:
            Any:
                    The result of the function call. if `timing` is True, returns a tuple of
                    (result, execution duration).

    examples:
            >>> async def sample_function(x):
            ...     return x * x
            >>> await tcall(sample_function, 3, delay=1, timing=True)
            (9, execution_duration)
    """

    async def async_call() -> tuple[Any, float]:
        start_time = SysUtil.get_now(datetime_=False)
        if timeout is not None:
            result = await AsyncUtil.execute_timeout(func(*args, **kwargs), timeout)
            duration = SysUtil.get_now(datetime_=False) - start_time
            return (result, duration) if timing else result
        try:
            await AsyncUtil.sleep(delay)
            result = await func(*args, **kwargs)
            duration = SysUtil.get_now(datetime_=False) - start_time
            return (result, duration) if timing else result
        except Exception as e:
            handle_error(e)

    def sync_call() -> tuple[Any, float]:
        start_time = SysUtil.get_now(datetime_=False)
        try:
            SysUtil.sleep(delay)
            result = func(*args, **kwargs)
            duration = SysUtil.get_now(datetime_=False) - start_time
            return (result, duration) if timing else result
        except Exception as e:
            handle_error(e)

    def handle_error(e: Exception):
        _msg = f"{err_msg} Error: {e}" if err_msg else f"An error occurred: {e}"
        logging.error(_msg)
        if not ignore_err:
            raise

    return await async_call() if is_coroutine_func(func) else sync_call()


async def rcall(
    func: Callable,
    *args,
    retries: int = 0,
    delay: float = 1.0,
    backoff_factor: float = 2.0,
    default: Any = None,
    timeout: float | None = None,
    timing: bool = False,
    **kwargs,
) -> Any:
    """
    asynchronously retries a function call with exponential backoff.

    designed for resilience, this function attempts to call an asynchronous function
    multiple times with increasing delays between attempts. this is particularly
    beneficial for operations that may fail due to transient issues. the function
    supports specifying a timeout for each attempt, a default return value in case of
    persistent failures, and a backoff factor to control the delay increase.

    Args:
            func (Callable):
                    The asynchronous function to retry.
            *args:
                    Positional arguments for the function.
            retries (int, optional):
                    The number of retry attempts before giving up. default to 0.
            delay (float, optional):
                    Initial delay between retries in seconds. default to 1.0.
            backoff_factor (float, optional):
                    Multiplier for the delay between retries, for exponential backoff.
                    default to 2.0.
            default (Any, optional):
                    A value to return if all retries fail. defaults to None.
            timeout (float | None, optional):
                    Maximum duration in seconds for each attempt. defaults to None.
            **kwargs:
                    Keyword arguments for the function.

    Returns:
            Any:
                    The result of the function call if successful within the retry attempts,
                    otherwise the `default` value if specified.

    examples:
            >>> async def fetch_data():
            ...     # Simulate a fetch operation that might fail
            ...     raise Exception("temporary error")
            >>> await rcall(fetch_data, retries=3, delay=2, default="default value")
            'default value'
    """
    last_exception = None
    result = None
    start_time = SysUtil.get_now(datetime_=False)
    for attempt in range(retries + 1) if retries == 0 else range(retries):
        try:
            result = await _tcall(func, *args, timeout=timeout, **kwargs)
            if timing:
                return (result, SysUtil.get_now(datetime_=False) - start_time)
            return result
        except Exception as e:
            last_exception = e
            if attempt < retries:
                await AsyncUtil.sleep(delay)
                delay *= backoff_factor
            else:
                break
    if result is None and default is not None:
        return default
    elif last_exception is not None:
        raise last_exception
    else:
        raise RuntimeError("rcall failed without catching an exception")


def _dropna(lst_: list[Any]) -> list[Any]:
    """
    Remove None values from a list.

    Args:
            lst_ (list[Any]): A list potentially containing None values.

    Returns:
            list[Any]: A list with None values removed.

    Examples:
            >>> _dropna([1, None, 3, None])
            [1, 3]
    """
    return [item for item in lst_ if item is not None]


async def _alcall(
    input_: Any, func: Callable, flatten: bool = False, **kwargs
) -> list[Any]:
    """
    asynchronously apply a function to each element in the input_.

    Args:
            input (Any): The input_ to process.
            func (Callable): The function to apply.
            flatten (bool, optional): Whether to flatten the result. default is False.
            **kwargs: Keyword arguments to pass to the function.

    Returns:
            list[Any]: A list of results after asynchronously applying the function.

    examples:
            >>> async def square(x): return x * x
            >>> asyncio.run(alcall([1, 2, 3], square))
            [1, 4, 9]
    """
    lst = to_list(input_)
    tasks = [AsyncUtil.handle_async_sync(func, i, **kwargs) for i in lst]
    outs = await AsyncUtil.execute_tasks(*tasks)
    return to_list(outs, flatten=flatten)


async def _tcall(
    func: Callable,
    *args,
    delay: float = 0,
    err_msg: str | None = None,
    ignore_err: bool = False,
    timing: bool = False,
    default: Any = None,
    timeout: float | None = None,
    **kwargs,
) -> Any:
    """
    asynchronously call a function with optional delay, timeout, and error handling.

    Args:
            func (Callable): The function to call.
            *args: Positional arguments to pass to the function.
            delay (float): Delay before calling the function, in seconds.
            err_msg (str | None): Custom error message.
            ignore_err (bool): If True, ignore errors and return default.
            timing (bool): If True, return a tuple (result, duration).
            default (Any): Default value to return on error.
            timeout (float | None): Timeout for the function call, in seconds.
            **kwargs: Keyword arguments to pass to the function.

    Returns:
            Any: The result of the function call, or (result, duration) if timing is True.

    examples:
            >>> async def example_func(x): return x
            >>> asyncio.run(tcall(example_func, 5, timing=True))
            (5, duration)
    """
    start_time = SysUtil.get_now(datetime_=False)
    try:
        await AsyncUtil.sleep(delay)
        # Apply timeout to the function call
        if timeout is not None:
            coro = ""
            if is_coroutine_func(func):
                coro = func(*args, **kwargs)
            else:

                async def coro_():
                    return func(*args, **kwargs)

                coro = coro_()

            result = await asyncio.wait_for(coro, timeout)

        else:
            if is_coroutine_func(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)
        duration = SysUtil.get_now(datetime_=False) - start_time
        return (result, duration) if timing else result
    except asyncio.TimeoutError as e:
        err_msg = f"{err_msg} Error: {e}" if err_msg else f"An error occurred: {e}"
        logging.error(err_msg)
        if ignore_err:
            return (
                (default, SysUtil.get_now(datetime_=False) - start_time)
                if timing
                else default
            )
        else:
            raise e  # Re-raise the timeout exception
    except Exception as e:
        err_msg = f"{err_msg} Error: {e}" if err_msg else f"An error occurred: {e}"
        logging.error(err_msg)
        if ignore_err:
            return (
                (default, SysUtil.get_now(datetime_=False) - start_time)
                if timing
                else default
            )
        else:
            raise e


class CallDecorator:
    """
    Provides a collection of decorators to enhance asynchronous function calls with
    additional behaviors such as timeouts, retries, throttling, and more. These
    decorators are designed to support both synchronous and asynchronous functions,
    allowing for flexible and efficient execution patterns in a variety of contexts.

    The decorators include functionality for applying timeouts, retrying with
    exponential backoff, limiting concurrency, caching results, and preprocessing or
    postprocessing inputs and outputs. They can be applied directly to functions or
    methods to modify their behavior without altering the original logic.

    Usage of these decorators simplifies the management of asynchronous operations,
    making it easier to implement robust error handling, rate limiting, and result
    caching. This is particularly beneficial in environments where operations are
    I/O-bound and can greatly benefit from asynchronous execution.
    """

    @staticmethod
    def timeout(timeout: int) -> Callable:
        """
        Applies a timeout to an asynchronous function call, ensuring that the function
        execution completes within the specified duration.

        This decorator is crucial for operations where a strict execution time limit is
        required. It prevents the function from running indefinitely by raising an
        asyncio.TimeoutError if the execution time exceeds the specified timeout.

        Args:
                timeout (int):
                        The maximum duration, in seconds, that the function is allowed to execute.

        Returns:
                Callable:
                        A decorated function that enforces the specified execution timeout.

        Examples:
                >>> @CallDecorator.timeout(5)
                ... async def long_running_task():
                ...     # Implementation that may exceed the timeout duration
                ...     await asyncio.sleep(10)
                ...     return "Completed"
                ... # Executing `long_running_task` will raise an asyncio.TimeoutError after 5
                ... # seconds
        """

        def decorator(func: Callable[..., Any]) -> Callable:
            @functools.wraps(func)
            async def wrapper(*args, **kwargs) -> Any:
                return await rcall(func, *args, timeout=timeout, **kwargs)

            return wrapper

        return decorator

    @staticmethod
    def retry(
        retries: int = 3, delay: float = 2.0, backoff_factor: float = 2.0
    ) -> Callable:
        """
        Decorates an asynchronous function to automatically retry on failure,
        with configurable retries, delay, and exponential backoff.

        This decorator is useful for handling operations that may fail due to transient
        issues, such as network connectivity problems or temporary provider
        unavailability. By automatically retrying the function call, it increases the
        robustness of the application without complicating the core logic with retry
        mechanisms.

        Args:
                retries (int, optional):
                        The number of retry attempts before giving up. Defaults to 3.
                delay (float, optional):
                        The initial delay between retries, in seconds. Defaults to 2.0.
                backoff_factor (float, optional):
                        The multiplier applied to the delay for each subsequent retry, for
                        exponential backoff. Default to 2.0.

        Returns:
                Callable:
                        A decorated asynchronous function with retry logic based on the specified
                        parameters.

        Examples:
                >>> @CallDecorator.retry(retries=2, delay=1, backoff_factor=2)
                ... async def fetch_data():
                ...     # Implementation that might fail transiently
                ...     raise ConnectionError("Temporary failure")
                ... # `fetch_data` will automatically retry on ConnectionError, up to 2 times,
                ... # with delays of 1s and 2s.
        """

        def decorator(func: Callable[..., Any]) -> Callable:
            @functools.wraps(func)
            async def wrapper(*args, **kwargs) -> Any:
                return await rcall(
                    func,
                    *args,
                    retries=retries,
                    delay=delay,
                    backoff_factor=backoff_factor,
                    **kwargs,
                )

            return wrapper

        return decorator

    @staticmethod
    def default(default_value: Any) -> Callable:
        """
        Decorates an asynchronous function to return a default value in case of an
        exception, allowing the function to gracefully handle errors without
        interrupting the application flow.

        This decorator simplifies error handling by encapsulating the try-except logic
        within the decoration process, providing a default result when an operation
        fails. This is particularly useful for non-critical operations where a fallback
        value can prevent the application from crashing or halting due to minor errors.

        Args:
                default_value (Any):
                        The value to return if the decorated function raises an exception.

        Returns:
                Callable:
                        A decorated asynchronous function that returns `default_value` in case of
                        error.

        Examples:
                >>> @CallDecorator.default(default_value="Fetch failed")
                ... async def get_resource():
                ...     # Implementation that might raise an exception
                ...     raise RuntimeError("Resource not available")
                ... # Executing `get_resource` will return "Fetch failed" instead of raising
                ... # an error
        """

        def decorator(func: Callable[..., Any]) -> Callable:
            @functools.wraps(func)
            async def wrapper(*args, **kwargs) -> Any:
                return await rcall(func, *args, default=default_value, **kwargs)

            return wrapper

        return decorator

    @staticmethod
    def throttle(period: int) -> Callable:
        """
        Decorates an asynchronous function to limit its execution frequency to not
        exceed one call per specified period. This is useful for rate-limiting calls to
        external services, APIs, or any operation where maintaining a maximum call
        frequency is required to avoid overloading resources or hitting API rate limits.

        The throttling is achieved by introducing a delay if the time elapsed since the
        last call is less than the specified period. This ensures that the decorated
        function does not execute more frequently than the allowed rate.

        Args:
                period (int):
                        The minimum time interval, in seconds, between consecutive calls to the
                        decorated function.

        Returns:
                Callable:
                        A decorated asynchronous function that adheres to the specified call
                        frequency limit.

        Examples:
                >>> @CallDecorator.throttle(2)
                ... async def fetch_data():
                ...     # Implementation that fetches data from an external source
                ...     pass
                ... # `fetch_data` will not be called more often than once every 2 seconds.
        """
        return Throttle(period)

    @staticmethod
    def map(function: Callable[[Any], Any]) -> Callable:
        """
        Decorates an asynchronous function to apply a specified mapping function to
        each element in the list returned by the decorated function. This is
        particularly useful for post-processing the results of asynchronous operations,
        such as transforming data fetched from an API or processing items in a
        collection concurrently.

        The mapping function is applied to each input_ in the output list of the
        decorated function, enabling patterned modifications or transformations to be
        succinctly applied to a collection of asynchronous results.

        Args:
                function (Callable[[Any], Any]):
                        A mapping function to apply to each element of the list returned by the
                        decorated function.

        Returns:
                Callable:
                        A decorated asynchronous function whose results are transformed by the
                        specified mapping function.

        Examples:
                >>> @CallDecorator.map(lambda x: x.upper())
                ... async def get_names():
                ...     # Asynchronously fetches a list of names
                ...     return ["alice", "bob", "charlie"]
                ... # `get_names` now returns ["ALICE", "BOB", "CHARLIE"]
        """

        def decorator(func: Callable[..., list[Any]]) -> Callable:
            if is_coroutine_func(func):

                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs) -> list[Any]:
                    values = await func(*args, **kwargs)
                    return [function(value) for value in values]

                return async_wrapper
            else:

                @functools.wraps(func)
                def sync_wrapper(*args, **kwargs) -> list[Any]:
                    values = func(*args, **kwargs)
                    return [function(value) for value in values]

                return sync_wrapper

        return decorator

    @staticmethod
    def compose(*functions: Callable[[Any], Any]) -> Callable:
        """
        Creates a decorator to sequentially apply multiple functions, where the output
        of one function becomes the input to the next. This enables function
        composition, allowing for elegant and flexible chaining of operations.

        This decorator is particularly useful when you need to perform a series of
        transformations or operations on data, especially when those operations need to
        be applied in a specific order. It supports both synchronous and asynchronous
        functions but requires all functions to be of the same type (all synchronous or
        all asynchronous).

        Args:
                *functions (Callable[[Any], Any]):
                        A variable number of functions that are to be composed together. Each
                        function must accept a single argument and return a value.

        Returns:
                Callable:
                        A decorator that, when applied to a function, composes it with the
                        specified functions, creating a pipeline of function calls.

        Raises:
                ValueError:
                        If the provided functions mix synchronous and asynchronous types, as they
                        cannot be composed together.

        Examples:
                >>> def double(x): return x * 2
                >>> def increment(x): return x + 1
                >>> @CallDecorator.compose(increment, double)
                ... def start_value(x):
                ...     return x
                >>> start_value(3)
                7  # The value is doubled to 6, then incremented to 7
        """

        def decorator(func: Callable) -> Callable:
            if not any(is_coroutine_func(f) for f in functions):

                @functools.wraps(func)
                def sync_wrapper(*args, **kwargs):
                    value = func(*args, **kwargs)
                    for function in functions:
                        try:
                            value = function(value)
                        except Exception as e:
                            raise ValueError(
                                f"Error in function {function.__name__}: {e}"
                            )
                    return value

                return sync_wrapper
            elif all(is_coroutine_func(f) for f in functions):

                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs):
                    value = func(*args, **kwargs)
                    for function in functions:
                        try:
                            value = await function(value)
                        except Exception as e:
                            raise ValueError(
                                f"Error in function {function.__name__}: {e}"
                            )
                    return value

                return async_wrapper
            else:
                raise ValueError(
                    "Cannot compose both synchronous and asynchronous functions."
                )

        return decorator

    @staticmethod
    def pre_post_process(
        preprocess: Callable[..., Any] = None,
        postprocess: Callable[..., Any] = None,
        preprocess_args=[],
        preprocess_kwargs={},
        postprocess_args=[],
        postprocess_kwargs={},
    ) -> Callable:
        """
        Decorates a function with preprocessing and postprocessing steps, allowing for
        modifications to the arguments before the function call and to the result after
        the function call. This decorator is versatile, supporting both synchronous and
        asynchronous functions, and enhances the modularity and reusability of code by
        abstracting common preprocessing and postprocessing patterns into decorator form.

        Preprocessing can include any modifications or checks to the arguments, such as
        validation or transformation, while postprocessing allows for adjustments to the
        function's output, such as formatting results or applying additional computations.

        Args:
                preprocess (Callable[..., Any]):
                        A function to preprocess the arguments passed to the decorated function.
                        It must accept the same arguments as the decorated function.
                postprocess (Callable[..., Any]):
                        A function to postprocess the result of the decorated function. It must
                        accept a single argument, which is the output of the decorated function.

        Returns:
                Callable:
                        A decorated function that applies the specified preprocessing and
                        postprocessing steps to its execution.

        Examples:
                >>> @CallDecorator.pre_post_process(lambda x: x - 1, lambda x: x * 2)
                ... async def process_value(x):
                ...     return x + 2
                >>> asyncio.run(process_value(5))
                12  # Input 5 is preprocessed to 4, processed to 6, and postprocessed to 12
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if is_coroutine_func(func):

                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs) -> Any:
                    preprocessed_args = [
                        preprocess(arg, *preprocess_args, **preprocess_kwargs)
                        for arg in args
                    ]
                    preprocessed_kwargs = {
                        k: preprocess(v, *preprocess_args, **preprocess_kwargs)
                        for k, v in kwargs.items()
                    }
                    result = await func(*preprocessed_args, **preprocessed_kwargs)
                    return postprocess(result, *postprocess_args, **postprocess_kwargs)

                return async_wrapper
            else:

                @functools.wraps(func)
                def sync_wrapper(*args, **kwargs) -> Any:
                    preprocessed_args = [
                        preprocess(arg, *preprocess_args, **preprocess_kwargs)
                        for arg in args
                    ]
                    preprocessed_kwargs = {
                        k: preprocess(v, *preprocess_args, **preprocess_kwargs)
                        for k, v in kwargs.items()
                    }
                    result = func(*preprocessed_args, **preprocessed_kwargs)
                    return postprocess(result, *postprocess_args, **postprocess_kwargs)

                return sync_wrapper

        return decorator

    @staticmethod
    def cache(func: Callable, ttl=600, maxsize=None) -> Callable:
        """
        Decorates a function (synchronous or asynchronous) to cache its results for a
        specified time-to-live (TTL). This caching mechanism prevents re-execution of
        the function with the same arguments, improving efficiency, especially for I/O
        bound or computationally intensive operations.

        Args: func (Callable): The target function to cache. Can be either synchronous
        or asynchronous. ttl (int, optional): The time-to-live of the cache entries in
        seconds. Defaults to 600 seconds. maxsize (Optional[int], optional): The
        maximum size of the cache. If None, the cache is unbounded. Applies only to
        synchronous functions.

        Returns:
                Callable: A decorated version of the function with caching applied. Subsequent
                calls with the same arguments within the TTL will return the cached result.

        Examples:
                >>> @CallDecorator.cache(ttl=10)
                ... async def fetch_data(key):
                ...     # Simulate a database fetch
                ...     return "data for " + key
                ... # Subsequent calls to `fetch_data` with the same `key` within 10 seconds
                ... # will return the cached result without re-executing the function body.
        """

        if is_coroutine_func(func):
            # Asynchronous function handling
            @AsyncUtil.cached(ttl=ttl)
            async def cached_async(*args, **kwargs) -> Any:
                return await func(*args, **kwargs)

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                return await cached_async(*args, **kwargs)

            return async_wrapper

        else:
            # Synchronous function handling
            @functools.lru_cache(maxsize=maxsize)
            def cached_sync(*args, **kwargs) -> Any:
                return func(*args, **kwargs)

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs) -> Any:
                return cached_sync(*args, **kwargs)

            return sync_wrapper

    @staticmethod
    def filter(predicate: Callable[[Any], bool]) -> Callable:
        """
        Decorates a function to filter its list result based on a given predicate. The
        predicate determines which items in the list should be included in the final
        result. This decorator can be applied to both synchronous and asynchronous
        functions returning lists.

        Args:
                predicate (Callable[[Any], bool]):
                        A function that evaluates each input_ in the list. Items for which the
                        predicate returns True are included in the final result.

        Returns:
                Callable:
                A decorated function that filters its list result according to the predicate.

        Examples:
                >>> @CallDecorator.filter(lambda x: x % 2 == 0)
                ... async def get_even_numbers():
                ...     return [1, 2, 3, 4, 5]
                >>> asyncio.run(get_even_numbers())
                [2, 4]
                ... # The result list is filtered to include only even numbers.
        """

        def decorator(func: Callable[..., list[Any]]) -> Callable:
            if is_coroutine_func(func):

                @functools.wraps(func)
                async def wrapper(*args, **kwargs) -> list[Any]:
                    values = await func(*args, **kwargs)
                    return [value for value in values if predicate(value)]

                return wrapper
            else:

                @functools.wraps(func)
                def wrapper(*args, **kwargs) -> list[Any]:
                    values = func(*args, **kwargs)
                    return [value for value in values if predicate(value)]

                return wrapper

        return decorator

    @staticmethod
    def reduce(function: Callable[[Any, Any], Any], initial: Any) -> Callable:
        """
        Decorates a function to apply a reduction to its list result, combining all
        items in the list into a single value using the specified reduction function.
        This is useful for aggregating results or performing cumulative operations on
        the list returned by the decorated function. The decorator supports both
        synchronous and asynchronous functions.

        Args:
                function (Callable[[Any, Any], Any]):
                        The reduction function to apply to the list. It should take two arguments
                        and return a single value that is the result of combining them.
                initial (Any):
                        The initial value for the reduction process. This value is used as the
                        starting point for the reduction and should be an identity value for the
                        reduction operation.

        Returns:
                Callable:
                        A decorated function that applies the specified reduction to its list
                        result,
                producing a single aggregated value.

        Examples:
                >>> @CallDecorator.reduce(lambda x, y: x + y, 0)
                ... async def sum_numbers():
                ...     return [1, 2, 3, 4]
                >>> asyncio.run(sum_numbers())
                10
                ... # The numbers in the list are summed, resulting in a single value.
        """

        def decorator(func: Callable[..., list[Any]]) -> Callable:
            if is_coroutine_func(func):

                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs) -> Any:
                    values = await func(*args, **kwargs)
                    return functools.reduce(function, values, initial)

                return async_wrapper
            else:

                @functools.wraps(func)
                def sync_wrapper(*args, **kwargs) -> Any:
                    values = func(*args, **kwargs)
                    return functools.reduce(function, values, initial)

                return sync_wrapper

        return decorator

    @staticmethod
    def max_concurrency(limit: int = 5) -> Callable:
        """
        Limits the number of concurrent executions for an asynchronous function to
        ensure that no more than a specified number of instances of the function run
        simultaneously. This is particularly useful for controlling resource usage and
        preventing overload when dealing with IO-bound operations or external services
        that can only handle a limited amount of concurrent requests.

        Args:
                limit (int):
                        The maximum number of concurrent executions allowed for the decorated
                        function.

        Returns:
                Callable:
                        An asynchronous function wrapper that enforces the concurrency limit.

        Examples:
                >>> @CallDecorator.max_concurrency(3)
                ... async def process_data(input_):
                ...     # Asynchronous processing logic here
                ...     pass
                ... # No more than 3 instances of `process_data` will run concurrently.
        """

        def decorator(func: Callable) -> Callable:
            if not is_coroutine_func(func):
                raise TypeError(
                    "max_concurrency decorator can only be used with async functions."
                )
            semaphore = AsyncUtil.semaphore(limit)

            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                async with semaphore:
                    return await func(*args, **kwargs)

            return wrapper

        return decorator

    # # noinspection PyRedeclaration
    # @staticmethod
    # def throttle(period: int) -> Callable:
    #     """
    #     A static method to create a throttling decorator. This method utilizes the
    #     _Throttle class to enforce a minimum time period between successive calls of the
    #     decorated function.

    #     Args:
    #         period (int):
    #             The minimum time period, in seconds, that must elapse between successive
    #             calls to the decorated function.

    #     Returns:
    #         Callable:
    #             A decorator that applies a throttling mechanism to the decorated function,
    #             ensuring that the function is not called more frequently than the
    #             specified period.

    #     Examples:
    #         >>> @CallDecorator.throttle(2)  # Ensures at least 2 seconds between calls
    #         ... async def fetch_data(): pass

    #         This decorator is particularly useful in scenarios like rate-limiting API
    #         calls or reducing the frequency of resource-intensive operations.
    #     """
    #     return Throttle(period)

    @staticmethod
    def force_async(fn):
        pool = ThreadPoolExecutor()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            future = pool.submit(fn, *args, **kwargs)
            return AsyncUtil.wrap_future(future)  # make it awaitable

        return wrapper

    @staticmethod
    def count_calls(func):
        async def wrapper(*args, **kwargs):
            wrapper.calls += 1
            return await func(*args, **kwargs)

        wrapper.calls = 0
        return wrapper


class Throttle:
    """
    A class that provides a throttling mechanism for function calls.

    When used as a decorator, it ensures that the decorated function can only be called
    once per specified period. Subsequent calls within this period are delayed to enforce
    this constraint.

    Attributes:
            period (int): The minimum time period (in seconds) between successive calls.

    Methods:
            __call__: Decorates a synchronous function with throttling.
            __call_async__: Decorates an asynchronous function with throttling.
    """

    def __init__(self, period: int) -> None:
        """
        Initializes a new instance of _Throttle.

        Args:
                period (int): The minimum time period (in seconds) between successive calls.
        """
        self.period = period
        self.last_called = 0

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """
        Decorates a synchronous function with the throttling mechanism.

        Args:
                func (Callable[..., Any]): The synchronous function to be throttled.

        Returns:
                Callable[..., Any]: The throttled synchronous function.
        """

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            elapsed = SysUtil.get_now(datetime_=False) - self.last_called
            if elapsed < self.period:
                SysUtil.sleep(self.period - elapsed)
            self.last_called = SysUtil.get_now(datetime_=False)
            return func(*args, **kwargs)

        return wrapper

    async def __call_async__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """
        Decorates an asynchronous function with the throttling mechanism.

        Args:
                func (Callable[..., Any]): The asynchronous function to be throttled.

        Returns:
                Callable[..., Any]: The throttled asynchronous function.
        """

        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            elapsed = SysUtil.get_now(datetime_=False) - self.last_called
            if elapsed < self.period:
                await AsyncUtil.sleep(self.period - elapsed)
            self.last_called = SysUtil.get_now(datetime_=False)
            return await func(*args, **kwargs)

        return wrapper


def _custom_error_handler(error: Exception, error_map: dict[type, Callable]) -> None:
    # noinspection PyUnresolvedReferences
    """
    handle errors based on a given error mapping.

    Args:
            error (Exception):
                    The error to handle.
            error_map (Dict[type, Callable]):
                    A dictionary mapping error types to handler functions.

    examples:
            >>> def handle_value_error(e): print("ValueError occurred")
            >>> custom_error_handler(ValueError(), {ValueError: handle_value_error})
            ValueError occurred
    """
    if handler := error_map.get(type(error)):
        handler(error)
    else:
        logging.error(f"Unhandled error: {error}")


async def call_handler(
    func: Callable, *args, error_map: dict[type, Callable] = None, **kwargs
) -> Any:
    """
    call a function with error handling, supporting both synchronous and asynchronous
    functions.

    Args:
            func (Callable):
                    The function to call.
            *args:
                    Positional arguments to pass to the function.
            error_map (Dict[type, Callable], optional):
                    A dictionary mapping error types to handler functions.
            **kwargs:
                    Keyword arguments to pass to the function.

    Returns:
            Any: The result of the function call.

    Raises:
            Exception: Propagates any exceptions not handled by the error_map.

    examples:
            >>> async def async_add(x, y): return x + y
            >>> asyncio.run(_call_handler(async_add, 1, 2))
            3
    """
    try:
        if not is_coroutine_func(func):
            return func(*args, **kwargs)

        # Checking for a running event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # No running event loop
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(func(*args, **kwargs))

            loop.close()
            return result

        if loop.is_running():
            return await func(*args, **kwargs)

    except Exception as e:
        if error_map:
            _custom_error_handler(e, error_map)
        else:
            logging.error(f"Error in call_handler: {e}")
        raise


# consistent naming and using AsyncUtil function
def is_coroutine_func(func: Callable) -> bool:
    """
    checks if the specified function is an asyncio coroutine function.

    this utility function is critical for asynchronous programming in Python, allowing
    developers to distinguish between synchronous and asynchronous functions.
    understanding whether a function is coroutine-enabled is essential for making
    correct asynchronous calls and for integrating synchronous functions into
    asynchronous codebases correctly.

    Args:
            func (Callable):
                    The function to check for coroutine compatibility.

    Returns:
            bool:
                    True if `func` is an asyncio coroutine function, False otherwise. this
                    determination is based on whether the function is defined with `async def`.

    examples:
            >>> async def async_func(): pass
            >>> def sync_func(): pass
            >>> is_coroutine_func(async_func)
            True
            >>> is_coroutine_func(sync_func)
            False
    """
    return AsyncUtil.is_coroutine_func(func)


# for backward compatibility
def lru_cache(*args, **kwargs):
    return functools.lru_cache(*args, **kwargs)
