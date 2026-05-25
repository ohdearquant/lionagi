# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Resilience patterns for API clients.

This module provides resilience patterns for API clients, including
the CircuitBreaker pattern and retry with exponential backoff.
"""

import functools
import logging
import random
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeVar

import anyio

from lionagi.ln.concurrency import Lock

T = TypeVar("T")
logger = logging.getLogger(__name__)


class APIClientError(Exception):
    """Base exception for all API client errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        response_data: dict[str, Any] | None = None,
    ):
        """
        Initialize the API client error.

        Args:
            message: The error message.
            status_code: The HTTP status code, if applicable.
            headers: The response headers, if applicable.
            response_data: The response data, if applicable.
        """
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}
        self.response_data = response_data or {}
        super().__init__(message)


class CircuitBreakerOpenError(APIClientError):
    """Exception raised when a circuit breaker is open."""

    def __init__(self, message: str, retry_after: float | None = None):
        """
        Initialize the circuit breaker open error.

        Args:
            message: The error message.
            retry_after: The time to wait before retrying, in seconds.
        """
        super().__init__(message)
        self.retry_after = retry_after


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker pattern implementation for preventing calls to failing services.

    The circuit breaker pattern prevents repeated calls to a failing service,
    based on the principle of "fail fast" for better system resilience. When
    a service fails repeatedly, the circuit opens and rejects requests for a
    period of time, then transitions to a half-open state to test if the
    service has recovered.

    Example:
        ```python
        # Create a circuit breaker with a failure threshold of 5
        # and a recovery time of 30 seconds
        breaker = CircuitBreaker(failure_threshold=5, recovery_time=30.0)

        # Execute a function with circuit breaker protection
        try:
            result = await breaker.execute(my_async_function, arg1, arg2, kwarg1=value1)
        except CircuitBreakerOpenError:
            # Handle the case where the circuit is open
            with contextlib.suppress(Exception):
                # Alternative approach using contextlib.suppress
                pass
        ```
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_time: float = 30.0,
        half_open_max_calls: int = 1,
        excluded_exceptions: set[type[Exception]] | None = None,
        name: str = "default",
    ):
        """
        Initialize the circuit breaker.

        Args:
            failure_threshold: Number of failures before opening the circuit.
            recovery_time: Time in seconds to wait before transitioning to half-open.
            half_open_max_calls: Maximum number of calls allowed in half-open state.
            excluded_exceptions: Set of exception types that should not count as failures.
            name: Name of the circuit breaker for logging and metrics.
        """
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.half_open_max_calls = half_open_max_calls
        self.excluded_exceptions = excluded_exceptions or set()
        self.name = name

        # State variables
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = 0
        self._half_open_calls = 0
        self._lock = Lock()

        # Metrics
        self._metrics = {
            "success_count": 0,
            "failure_count": 0,
            "rejected_count": 0,
            "state_changes": [],
        }

        logger.debug(
            f"Initialized CircuitBreaker '{self.name}' with failure_threshold={failure_threshold}, "
            f"recovery_time={recovery_time}, half_open_max_calls={half_open_max_calls}"
        )

    @property
    def metrics(self) -> dict[str, Any]:
        """Get circuit breaker metrics."""
        return self._metrics.copy()

    def to_dict(self):
        return {
            "failure_threshold": self.failure_threshold,
            "recovery_time": self.recovery_time,
            "half_open_max_calls": self.half_open_max_calls,
            "name": self.name,
        }

    async def _change_state(self, new_state: CircuitState) -> None:
        """
        Change circuit state with logging and metrics tracking.

        Args:
            new_state: The new circuit state.
        """
        old_state = self.state
        if new_state != old_state:
            self.state = new_state
            self._metrics["state_changes"].append(
                {
                    "time": time.time(),
                    "from": old_state,
                    "to": new_state,
                }
            )

            logger.info(
                f"Circuit '{self.name}' state changed from {old_state.value} to {new_state.value}"
            )

            # Reset counters on state change
            if new_state == CircuitState.HALF_OPEN:
                self._half_open_calls = 0
            elif new_state == CircuitState.CLOSED:
                self.failure_count = 0

    async def _check_state(self) -> bool:
        """
        Check circuit state and determine if request can proceed.

        Returns:
            True if request can proceed, False otherwise.
        """
        async with self._lock:
            now = time.time()

            if self.state == CircuitState.OPEN:
                # Check if recovery time has elapsed
                if now - self.last_failure_time >= self.recovery_time:
                    await self._change_state(CircuitState.HALF_OPEN)
                else:
                    recovery_remaining = self.recovery_time - (
                        now - self.last_failure_time
                    )
                    self._metrics["rejected_count"] += 1

                    logger.warning(
                        f"Circuit '{self.name}' is OPEN, rejecting request. "
                        f"Try again in {recovery_remaining:.2f}s"
                    )

                    return False

            if self.state == CircuitState.HALF_OPEN:
                # Only allow a limited number of calls in half-open state
                if self._half_open_calls >= self.half_open_max_calls:
                    self._metrics["rejected_count"] += 1

                    logger.warning(
                        f"Circuit '{self.name}' is HALF_OPEN and at capacity. Try again later."
                    )

                    return False

                self._half_open_calls += 1

            return True

    async def execute(
        self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        """
        Execute a coroutine with circuit breaker protection.

        Args:
            func: The coroutine function to execute.
            *args: Positional arguments for the function.
            **kwargs: Keyword arguments for the function.

        Returns:
            The result of the function execution.

        Raises:
            CircuitBreakerOpenError: If the circuit is open.
            Exception: Any exception raised by the function.
        """
        # Check if circuit allows this call
        can_proceed = await self._check_state()
        if not can_proceed:
            remaining = self.recovery_time - (time.time() - self.last_failure_time)
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' is open. Retry after {remaining:.2f} seconds",
                retry_after=remaining,
            )

        try:
            logger.debug(
                f"Executing {func.__name__} with circuit '{self.name}' state: {self.state.value}"
            )
            result = await func(*args, **kwargs)

            # Handle success
            async with self._lock:
                self._metrics["success_count"] += 1

                # On success in half-open state, close the circuit
                if self.state == CircuitState.HALF_OPEN:
                    await self._change_state(CircuitState.CLOSED)

            return result

        except Exception as e:
            # Determine if this exception should count as a circuit failure
            is_excluded = any(
                isinstance(e, exc_type) for exc_type in self.excluded_exceptions
            )

            if not is_excluded:
                async with self._lock:
                    self.failure_count += 1
                    self.last_failure_time = time.time()
                    self._metrics["failure_count"] += 1

                    # Log failure
                    logger.warning(
                        f"Circuit '{self.name}' failure: {e}. "
                        f"Count: {self.failure_count}/{self.failure_threshold}"
                    )

                    # Check if we need to open the circuit
                    if (
                        self.state == CircuitState.CLOSED
                        and self.failure_count >= self.failure_threshold
                    ) or self.state == CircuitState.HALF_OPEN:
                        await self._change_state(CircuitState.OPEN)

            logger.exception(f"Circuit breaker '{self.name}' caught exception")
            raise


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        jitter: bool = True,
        jitter_factor: float = 0.2,
        retry_exceptions: tuple[type[Exception], ...] = (
            APIClientError,
            CircuitBreakerOpenError,
            ConnectionError,
            TimeoutError,
        ),
        exclude_exceptions: tuple[type[Exception], ...] = (),
    ):
        """
        Initialize retry configuration.

        Args:
            max_retries: Maximum number of retry attempts.
            base_delay: Initial delay between retries in seconds.
            max_delay: Maximum delay between retries in seconds.
            backoff_factor: Multiplier applied to delay after each retry.
            jitter: Whether to add randomness to delay timings.
            jitter_factor: How much randomness to add as a percentage.
            retry_exceptions: Tuple of exception types that should trigger retry.
                Defaults to transient errors only (API, connection, timeout).
            exclude_exceptions: Tuple of exception types that should not be retried.
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        self.jitter_factor = jitter_factor
        self.retry_exceptions = retry_exceptions
        self.exclude_exceptions = exclude_exceptions

    def to_dict(self) -> dict[str, Any]:
        """
        Convert configuration to a dictionary.

        Returns:
            Dictionary representation of the configuration.
        """
        return {
            "max_retries": self.max_retries,
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "backoff_factor": self.backoff_factor,
            "jitter": self.jitter,
            "jitter_factor": self.jitter_factor,
        }

    def as_kwargs(self) -> dict[str, Any]:
        """
        Convert configuration to keyword arguments for retry_with_backoff.

        Returns:
            Dictionary of keyword arguments.
        """
        return {
            "max_retries": self.max_retries,
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "backoff_factor": self.backoff_factor,
            "jitter": self.jitter,
            "retry_exceptions": self.retry_exceptions,
            "exclude_exceptions": self.exclude_exceptions,
        }


async def retry_with_backoff(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    retry_exceptions: tuple[type[Exception], ...] = (
        APIClientError,
        CircuitBreakerOpenError,
        ConnectionError,
        TimeoutError,
    ),
    exclude_exceptions: tuple[type[Exception], ...] = (),
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    jitter_factor: float = 0.2,
    **kwargs: Any,
) -> T:
    """
    Retry an async function with exponential backoff.

    Args:
        func: The async function to retry.
        *args: Positional arguments for the function.
        retry_exceptions: Tuple of exception types to retry.
        exclude_exceptions: Tuple of exception types to not retry.
        max_retries: Maximum number of retries.
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.
        backoff_factor: Factor to increase delay with each retry.
        jitter: Whether to add randomness to the delay.
        jitter_factor: How much randomness to add as a percentage.
        **kwargs: Keyword arguments for the function.

    Returns:
        The result of the function execution.

    Raises:
        Exception: The last exception raised by the function after all retries.
    """
    retries = 0
    delay = base_delay

    while True:
        try:
            return await func(*args, **kwargs)
        except exclude_exceptions:
            # Don't retry these exceptions
            logger.debug(f"Not retrying {func.__name__} for excluded exception type")
            raise
        except retry_exceptions as e:
            # No need to store the exception since we're raising it if max retries reached
            retries += 1
            if retries > max_retries:
                logger.warning(
                    f"Maximum retries ({max_retries}) reached for {func.__name__}"
                )
                raise

            # Calculate backoff with optional jitter
            if jitter:
                # This is not used for cryptographic purposes, just for jitter
                jitter_amount = random.uniform(
                    1.0 - jitter_factor, 1.0 + jitter_factor
                )  # noqa: S311
                current_delay = min(delay * jitter_amount, max_delay)
            else:
                current_delay = min(delay, max_delay)

            logger.info(
                f"Retry {retries}/{max_retries} for {func.__name__} "
                f"after {current_delay:.2f}s delay. Error: {e!s}"
            )

            # Increase delay for next iteration
            delay = delay * backoff_factor

            # Wait before retrying
            await anyio.sleep(current_delay)


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_time: float = 30.0,
    half_open_max_calls: int = 1,
    excluded_exceptions: set[type[Exception]] | None = None,
    name: str | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Decorator to apply circuit breaker pattern to an async function.

    Args:
        failure_threshold: Number of failures before opening the circuit.
        recovery_time: Time in seconds to wait before transitioning to half-open.
        half_open_max_calls: Maximum number of calls allowed in half-open state.
        excluded_exceptions: Set of exception types that should not count as failures.
        name: Name of the circuit breaker for logging and metrics.

    Returns:
        Decorator function that applies circuit breaker pattern.
    """

    def decorator(
        func: Callable[..., Awaitable[T]],
    ) -> Callable[..., Awaitable[T]]:
        # Create a unique name for the circuit breaker if not provided
        cb_name = name or f"cb_{func.__module__}_{func.__qualname__}"

        # Create circuit breaker instance
        cb = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_time=recovery_time,
            half_open_max_calls=half_open_max_calls,
            excluded_exceptions=excluded_exceptions,
            name=cb_name,
        )

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await cb.execute(func, *args, **kwargs)

        return wrapper

    return decorator


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    jitter_factor: float = 0.2,
    retry_exceptions: tuple[type[Exception], ...] = (
        APIClientError,
        CircuitBreakerOpenError,
        ConnectionError,
        TimeoutError,
    ),
    exclude_exceptions: tuple[type[Exception], ...] = (),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Decorator to apply retry with backoff pattern to an async function.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.
        backoff_factor: Multiplier applied to delay after each retry.
        jitter: Whether to add randomness to delay timings.
        jitter_factor: How much randomness to add as a percentage.
        retry_exceptions: Tuple of exception types that should trigger retry.
        exclude_exceptions: Tuple of exception types that should not be retried.

    Returns:
        Decorator function that applies retry pattern.
    """

    def decorator(
        func: Callable[..., Awaitable[T]],
    ) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await retry_with_backoff(
                func,
                *args,
                retry_exceptions=retry_exceptions,
                exclude_exceptions=exclude_exceptions,
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
                backoff_factor=backoff_factor,
                jitter=jitter,
                jitter_factor=jitter_factor,
                **kwargs,
            )

        return wrapper

    return decorator
