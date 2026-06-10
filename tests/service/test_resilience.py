"""Tests for lionagi.service.resilience module - Circuit breakers, retry logic, timeouts."""

import asyncio
from unittest.mock import patch

import pytest

from lionagi.service.resilience import (
    APIClientError,
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    RetryConfig,
    circuit_breaker,
    retry_with_backoff,
    with_retry,
)


class TestAPIClientError:
    """Test APIClientError exception."""

    def test_init_basic(self):
        """Test basic APIClientError initialization."""
        error = APIClientError("Test error")

        assert error.message == "Test error"
        assert error.status_code is None
        assert error.headers == {}
        assert error.response_data == {}
        assert str(error) == "Test error"

    def test_init_with_all_params(self):
        """Test APIClientError with all parameters."""
        headers = {"Content-Type": "application/json"}
        response_data = {"error": "details"}

        error = APIClientError(
            "Test error",
            status_code=500,
            headers=headers,
            response_data=response_data,
        )

        assert error.status_code == 500
        assert error.headers == headers
        assert error.response_data == response_data


class TestCircuitBreakerOpenError:
    """Test CircuitBreakerOpenError exception."""

    def test_init_basic(self):
        """Test basic CircuitBreakerOpenError initialization."""
        error = CircuitBreakerOpenError("Circuit open")

        assert error.message == "Circuit open"
        assert error.retry_after is None

    def test_init_with_retry_after(self):
        """Test CircuitBreakerOpenError with retry_after."""
        error = CircuitBreakerOpenError("Circuit open", retry_after=30.0)

        assert error.retry_after == 30.0


class TestCircuitBreakerInit:
    """Test CircuitBreaker initialization."""

    def test_init_defaults(self):
        """Test CircuitBreaker with default parameters."""
        cb = CircuitBreaker()

        assert cb.failure_threshold == 5
        assert cb.recovery_time == 30.0
        assert cb.half_open_max_calls == 1
        assert cb.excluded_exceptions == set()
        assert cb.name == "default"
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_init_custom_params(self):
        """Test CircuitBreaker with custom parameters."""
        excluded = {ValueError, TypeError}

        cb = CircuitBreaker(
            failure_threshold=10,
            recovery_time=60.0,
            half_open_max_calls=3,
            excluded_exceptions=excluded,
            name="test_cb",
        )

        assert cb.failure_threshold == 10
        assert cb.recovery_time == 60.0
        assert cb.half_open_max_calls == 3
        assert cb.excluded_exceptions == excluded
        assert cb.name == "test_cb"

    def test_metrics_property(self):
        """Test metrics property returns copy."""
        cb = CircuitBreaker()
        metrics1 = cb.metrics
        metrics2 = cb.metrics

        assert metrics1 == metrics2
        assert metrics1 is not metrics2  # Should be a copy

    def test_to_dict(self):
        """Test to_dict method."""
        cb = CircuitBreaker(
            failure_threshold=10,
            recovery_time=60.0,
            half_open_max_calls=3,
            name="test",
        )

        result = cb.to_dict()

        assert result["failure_threshold"] == 10
        assert result["recovery_time"] == 60.0
        assert result["half_open_max_calls"] == 3
        assert result["name"] == "test"


class TestCircuitBreakerExecution:
    """Test CircuitBreaker execute method."""

    @pytest.mark.asyncio
    async def test_execute_success_closed_state(self):
        """Test successful execution in closed state."""
        cb = CircuitBreaker(failure_threshold=3)

        async def success_func():
            return "success"

        result = await cb.execute(success_func)

        assert result == "success"
        assert cb.state == CircuitState.CLOSED
        assert cb.metrics["success_count"] == 1
        assert cb.metrics["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_execute_with_args_kwargs(self):
        """Test execute passes args and kwargs correctly."""
        cb = CircuitBreaker()

        async def func_with_params(a, b, c=None):
            return f"{a}-{b}-{c}"

        result = await cb.execute(func_with_params, "x", "y", c="z")

        assert result == "x-y-z"

    @pytest.mark.asyncio
    async def test_execute_failure_increments_count(self):
        """Test failure increments failure count."""
        cb = CircuitBreaker(failure_threshold=3)

        async def failing_func():
            raise ValueError("Test error")

        with pytest.raises(ValueError):
            await cb.execute(failing_func)

        assert cb.failure_count == 1
        assert cb.metrics["failure_count"] == 1
        assert cb.state == CircuitState.CLOSED  # Not enough failures yet

    @pytest.mark.asyncio
    async def test_execute_opens_circuit_after_threshold(self):
        """Test circuit opens after reaching failure threshold."""
        cb = CircuitBreaker(failure_threshold=3)

        async def failing_func():
            raise ValueError("Test error")

        # Cause 3 failures
        for _ in range(3):
            with pytest.raises(ValueError):
                await cb.execute(failing_func)

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    @pytest.mark.asyncio
    async def test_execute_rejects_when_open(self):
        """Test circuit rejects calls when open."""
        cb = CircuitBreaker(failure_threshold=2, recovery_time=10.0)

        async def failing_func():
            raise ValueError("Test error")

        # Open the circuit
        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.execute(failing_func)

        assert cb.state == CircuitState.OPEN

        # Next call should be rejected immediately
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await cb.execute(failing_func)

        assert "is open" in str(exc_info.value)
        assert cb.metrics["rejected_count"] == 1

    @pytest.mark.asyncio
    async def test_execute_excluded_exceptions_dont_count(self):
        """Test excluded exceptions don't increment failure count."""
        cb = CircuitBreaker(failure_threshold=2, excluded_exceptions={KeyError})

        async def func_with_excluded_error():
            raise KeyError("excluded")

        # Raise excluded exception multiple times
        for _ in range(3):
            with pytest.raises(KeyError):
                await cb.execute(func_with_excluded_error)

        # Circuit should still be closed since exceptions were excluded
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_execute_transitions_to_half_open(self):
        """Test circuit transitions to half-open after recovery time."""
        cb = CircuitBreaker(failure_threshold=2, recovery_time=0.1)

        async def failing_func():
            raise ValueError("Test error")

        # Open the circuit
        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.execute(failing_func)

        assert cb.state == CircuitState.OPEN

        # Wait for recovery time
        await asyncio.sleep(0.15)

        # Next check should transition to half-open
        async def success_func():
            return "success"

        result = await cb.execute(success_func)

        # Should have transitioned through half-open to closed
        assert result == "success"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_execute_half_open_success_closes_circuit(self):
        """Test successful call in half-open state closes circuit."""
        cb = CircuitBreaker(failure_threshold=1, recovery_time=0.1)

        async def failing_func():
            raise ValueError("Test error")

        # Open circuit
        with pytest.raises(ValueError):
            await cb.execute(failing_func)

        # Wait for recovery
        await asyncio.sleep(0.15)

        # Success should close circuit
        async def success_func():
            return "success"

        await cb.execute(success_func)

        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_execute_half_open_failure_reopens_circuit(self):
        """Test failure in half-open state reopens circuit."""
        cb = CircuitBreaker(failure_threshold=1, recovery_time=0.1)

        async def failing_func():
            raise ValueError("Test error")

        # Open circuit
        with pytest.raises(ValueError):
            await cb.execute(failing_func)

        # Wait for recovery to half-open
        await asyncio.sleep(0.15)

        # Fail in half-open should reopen circuit
        with pytest.raises(ValueError):
            await cb.execute(failing_func)

        assert cb.state == CircuitState.OPEN


class TestRetryConfig:
    """Test RetryConfig class."""

    def test_init_defaults(self):
        """Test RetryConfig with default values."""
        config = RetryConfig()

        assert config.max_retries == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 60.0
        assert config.backoff_factor == 2.0
        assert config.jitter is True
        assert config.jitter_factor == 0.2

    def test_init_custom_values(self):
        """Test RetryConfig with custom values."""
        config = RetryConfig(
            max_retries=5,
            base_delay=2.0,
            max_delay=120.0,
            backoff_factor=3.0,
            jitter=False,
            jitter_factor=0.5,
        )

        assert config.max_retries == 5
        assert config.base_delay == 2.0
        assert config.max_delay == 120.0
        assert config.backoff_factor == 3.0
        assert config.jitter is False
        assert config.jitter_factor == 0.5

    def test_to_dict(self):
        """Test to_dict method."""
        config = RetryConfig(max_retries=5, base_delay=2.0)
        result = config.to_dict()

        assert result["max_retries"] == 5
        assert result["base_delay"] == 2.0
        assert "retry_exceptions" not in result  # Not in to_dict

    def test_as_kwargs(self):
        """Test as_kwargs method."""
        config = RetryConfig(
            max_retries=5,
            retry_exceptions=(ValueError,),
            exclude_exceptions=(KeyError,),
        )

        kwargs = config.as_kwargs()

        assert kwargs["max_retries"] == 5
        assert kwargs["retry_exceptions"] == (ValueError,)
        assert kwargs["exclude_exceptions"] == (KeyError,)


class TestRetryWithBackoff:
    """Test retry_with_backoff function."""

    @pytest.mark.asyncio
    async def test_retry_success_first_attempt(self):
        """Test retry succeeds on first attempt."""

        async def success_func():
            return "success"

        result = await retry_with_backoff(success_func, max_retries=3)

        assert result == "success"

    @pytest.mark.asyncio
    async def test_retry_success_after_failures(self):
        """Test retry succeeds after some failures."""
        attempts = {"count": 0}

        async def flaky_func():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ConnectionError("Transient error")
            return "success"

        result = await retry_with_backoff(flaky_func, max_retries=5, base_delay=0.01)

        assert result == "success"
        assert attempts["count"] == 3

    @pytest.mark.asyncio
    async def test_retry_exhausts_attempts(self):
        """Test retry gives up after max attempts."""

        async def always_fails():
            raise ConnectionError("Permanent error")

        with pytest.raises(ConnectionError, match="Permanent error"):
            await retry_with_backoff(always_fails, max_retries=2, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_retry_with_exclude_exceptions(self):
        """Test excluded exceptions are not retried."""
        attempts = {"count": 0}

        async def func_with_excluded_error():
            attempts["count"] += 1
            raise KeyError("Should not retry")

        with pytest.raises(KeyError):
            await retry_with_backoff(
                func_with_excluded_error,
                max_retries=3,
                exclude_exceptions=(KeyError,),
                base_delay=0.01,
            )

        # Should only have tried once
        assert attempts["count"] == 1

    @pytest.mark.asyncio
    async def test_retry_with_specific_exceptions(self):
        """Test only specified exceptions trigger retry."""
        attempts = {"count": 0}

        async def func_with_different_error():
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise ValueError("Retry this")
            raise TypeError("Don't retry this")

        with pytest.raises(TypeError):
            await retry_with_backoff(
                func_with_different_error,
                retry_exceptions=(ValueError,),
                max_retries=3,
                base_delay=0.01,
            )

        # Should have retried ValueError once, then raised TypeError
        assert attempts["count"] == 2

    @pytest.mark.asyncio
    async def test_retry_backoff_increases_delay(self):
        """Test backoff increases delay between retries."""
        delays = []

        async def func_that_tracks_delays():
            raise ConnectionError("Error")

        async def fake_sleep(d):
            delays.append(d)

        with patch("lionagi.ln.concurrency.patterns.anyio.sleep", side_effect=fake_sleep):
            with pytest.raises(ConnectionError):
                await retry_with_backoff(
                    func_that_tracks_delays,
                    max_retries=3,
                    base_delay=1.0,
                    backoff_factor=2.0,
                    jitter=False,
                )

        # Delays should increase: 1.0, 2.0, 4.0
        assert len(delays) == 3
        assert delays[0] == 1.0
        assert delays[1] == 2.0
        assert delays[2] == 4.0

    @pytest.mark.asyncio
    async def test_retry_respects_max_delay(self):
        """Test max_delay caps the backoff."""
        delays = []

        async def failing_func():
            raise ConnectionError("Error")

        async def fake_sleep(d):
            delays.append(d)

        with patch("lionagi.ln.concurrency.patterns.anyio.sleep", side_effect=fake_sleep):
            with pytest.raises(ConnectionError):
                await retry_with_backoff(
                    failing_func,
                    max_retries=5,
                    base_delay=10.0,
                    max_delay=15.0,
                    backoff_factor=2.0,
                    jitter=False,
                )

        # All delays should be capped at max_delay
        assert all(d <= 15.0 for d in delays)


class TestCircuitBreakerDecorator:
    """Test circuit_breaker decorator."""

    @pytest.mark.asyncio
    async def test_decorator_basic_usage(self):
        """Test circuit_breaker decorator on function."""

        @circuit_breaker(failure_threshold=2, recovery_time=0.1)
        async def decorated_func():
            return "success"

        result = await decorated_func()
        assert result == "success"

    @pytest.mark.asyncio
    async def test_decorator_opens_circuit_after_failures(self):
        """Test decorator opens circuit after threshold."""

        @circuit_breaker(failure_threshold=2, recovery_time=1.0)
        async def failing_func():
            raise ValueError("Error")

        # Cause failures
        for _ in range(2):
            with pytest.raises(ValueError):
                await failing_func()

        # Circuit should now be open
        with pytest.raises(CircuitBreakerOpenError):
            await failing_func()

    @pytest.mark.asyncio
    async def test_decorator_with_custom_name(self):
        """Test decorator with custom circuit name."""

        @circuit_breaker(name="custom_circuit")
        async def func():
            return "test"

        result = await func()
        assert result == "test"


class TestWithRetryDecorator:
    """Test with_retry decorator."""

    @pytest.mark.asyncio
    async def test_decorator_basic_usage(self):
        """Test with_retry decorator on function."""

        @with_retry(max_retries=3, base_delay=0.01)
        async def decorated_func():
            return "success"

        result = await decorated_func()
        assert result == "success"

    @pytest.mark.asyncio
    async def test_decorator_retries_on_failure(self):
        """Test decorator retries failed calls."""
        attempts = {"count": 0}

        @with_retry(max_retries=3, base_delay=0.01)
        async def flaky_func():
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise ConnectionError("Transient")
            return "success"

        result = await flaky_func()
        assert result == "success"
        assert attempts["count"] == 2

    @pytest.mark.asyncio
    async def test_decorator_with_exclude_exceptions(self):
        """Test decorator doesn't retry excluded exceptions."""
        attempts = {"count": 0}

        @with_retry(max_retries=3, exclude_exceptions=(KeyError,), base_delay=0.01)
        async def func_with_excluded():
            attempts["count"] += 1
            raise KeyError("No retry")

        with pytest.raises(KeyError):
            await func_with_excluded()

        assert attempts["count"] == 1

    @pytest.mark.asyncio
    async def test_retry_with_backoff_excluded_exception_does_not_sleep_or_retry(self):
        from unittest.mock import patch

        import lionagi.service.resilience as resilience_mod

        sleep_calls = []

        async def failing():
            raise ValueError("excluded")

        async def fake_sleep(secs):
            sleep_calls.append(secs)

        import lionagi.ln.concurrency.patterns as patterns_mod

        with patch.object(patterns_mod.anyio, "sleep", fake_sleep):
            with pytest.raises(ValueError, match="excluded"):
                await retry_with_backoff(
                    failing,
                    exclude_exceptions=(ValueError,),
                    max_retries=3,
                    base_delay=1.0,
                )

        assert len(sleep_calls) == 0

    @pytest.mark.asyncio
    async def test_retry_with_backoff_no_jitter_uses_capped_delay_sequence(self):
        from unittest.mock import patch

        import lionagi.service.resilience as resilience_mod

        sleep_calls = []
        call_count = [0]

        async def sometimes_failing():
            call_count[0] += 1
            if call_count[0] < 3:
                raise APIClientError("fail")
            return "success"

        async def fake_sleep(secs):
            sleep_calls.append(secs)

        import lionagi.ln.concurrency.patterns as patterns_mod

        with patch.object(patterns_mod.anyio, "sleep", fake_sleep):
            result = await retry_with_backoff(
                sometimes_failing,
                retry_exceptions=(APIClientError,),
                base_delay=2.0,
                backoff_factor=10.0,
                max_delay=5.0,
                jitter=False,
            )

        assert result == "success"
        # backoff_factor=10.0 with base_delay=2.0: attempt 1 → 2.0*10^0=2.0, attempt 2 → 2.0*10^1=5.0 (capped at max_delay=5.0)
        assert sleep_calls == [2.0, 5.0]
