# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Resilience patterns: circuit breaker and retry with exponential backoff."""

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeVar

from lionagi.ln.concurrency import Lock
from lionagi.ln.concurrency import retry as _canonical_retry

T = TypeVar("T")
logger = logging.getLogger(__name__)


class APIClientError(Exception):
    """Base exception for API client errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        response_data: dict[str, Any] | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}
        self.response_data = response_data or {}
        super().__init__(message)


class CircuitBreakerOpenError(APIClientError):
    """Raised when a circuit breaker is open."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Fail-fast circuit breaker for async service calls."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_time: float = 30.0,
        half_open_max_calls: int = 1,
        excluded_exceptions: set[type[Exception]] | None = None,
        name: str = "default",
    ):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.half_open_max_calls = half_open_max_calls
        self.excluded_exceptions = excluded_exceptions or set()
        self.name = name

        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = 0
        self._half_open_calls = 0
        self._lock = Lock()

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
        return self._metrics.copy()

    def to_dict(self):
        return {
            "failure_threshold": self.failure_threshold,
            "recovery_time": self.recovery_time,
            "half_open_max_calls": self.half_open_max_calls,
            "name": self.name,
        }

    async def _change_state(self, new_state: CircuitState) -> None:
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

            if new_state == CircuitState.HALF_OPEN:
                self._half_open_calls = 0
            elif new_state == CircuitState.CLOSED:
                self.failure_count = 0

    async def _check_state(self) -> bool:
        async with self._lock:
            now = time.time()

            if self.state == CircuitState.OPEN:
                if now - self.last_failure_time >= self.recovery_time:
                    await self._change_state(CircuitState.HALF_OPEN)
                else:
                    recovery_remaining = self.recovery_time - (now - self.last_failure_time)
                    self._metrics["rejected_count"] += 1

                    logger.warning(
                        f"Circuit '{self.name}' is OPEN, rejecting request. "
                        f"Try again in {recovery_remaining:.2f}s"
                    )

                    return False

            if self.state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    self._metrics["rejected_count"] += 1

                    logger.warning(
                        f"Circuit '{self.name}' is HALF_OPEN and at capacity. Try again later."
                    )

                    return False

                self._half_open_calls += 1

            return True

    async def execute(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute with circuit breaker protection."""
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

            async with self._lock:
                self._metrics["success_count"] += 1

                if self.state == CircuitState.HALF_OPEN:
                    await self._change_state(CircuitState.CLOSED)

            return result

        except Exception as e:
            is_excluded = any(isinstance(e, exc_type) for exc_type in self.excluded_exceptions)

            if not is_excluded:
                async with self._lock:
                    self.failure_count += 1
                    self.last_failure_time = time.time()
                    self._metrics["failure_count"] += 1

                    logger.warning(
                        f"Circuit '{self.name}' failure: {e}. "
                        f"Count: {self.failure_count}/{self.failure_threshold}"
                    )

                    if (
                        self.state == CircuitState.CLOSED
                        and self.failure_count >= self.failure_threshold
                    ) or self.state == CircuitState.HALF_OPEN:
                        await self._change_state(CircuitState.OPEN)

            logger.exception(f"Circuit breaker '{self.name}' caught exception")
            raise


class RetryConfig:
    """Configuration for retry with exponential backoff."""

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
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        self.jitter_factor = jitter_factor
        self.retry_exceptions = retry_exceptions
        self.exclude_exceptions = exclude_exceptions

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "backoff_factor": self.backoff_factor,
            "jitter": self.jitter,
            "jitter_factor": self.jitter_factor,
        }

    def as_kwargs(self) -> dict[str, Any]:
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
    async def _fn() -> T:
        return await func(*args, **kwargs)

    if exclude_exceptions:
        # Runtime dispatch: exclude_exceptions must be checked per-instance,
        # not per-type, to correctly handle subclass hierarchies (e.g.
        # retry_on=(OSError,), exclude=(ConnectionError,) — ConnectionError
        # IS-A OSError but must not be retried).
        class _Excluded(BaseException):
            def __init__(self, inner: Exception):
                self.inner = inner

        _inner = _fn

        async def _fn_guarded() -> T:
            try:
                return await _inner()
            except exclude_exceptions as exc:
                raise _Excluded(exc) from exc

        try:
            return await _canonical_retry(
                _fn_guarded,
                attempts=max_retries + 1,
                base_delay=base_delay,
                max_delay=max_delay,
                backoff_factor=backoff_factor,
                retry_on=retry_exceptions,
                jitter=jitter_factor if jitter else 0.0,
            )
        except _Excluded as wrapper:
            raise wrapper.inner from wrapper.__cause__

    return await _canonical_retry(
        _fn,
        attempts=max_retries + 1,
        base_delay=base_delay,
        max_delay=max_delay,
        backoff_factor=backoff_factor,
        retry_on=retry_exceptions,
        jitter=jitter_factor if jitter else 0.0,
    )


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_time: float = 30.0,
    half_open_max_calls: int = 1,
    excluded_exceptions: set[type[Exception]] | None = None,
    name: str | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator applying circuit breaker pattern to an async function."""

    def decorator(
        func: Callable[..., Awaitable[T]],
    ) -> Callable[..., Awaitable[T]]:
        cb_name = name or f"cb_{func.__module__}_{func.__qualname__}"
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
    """Decorator applying retry with exponential backoff to an async function."""

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
