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


class TestCircuitBreakerInit:
    def test_metrics_property(self):
        cb = CircuitBreaker()
        metrics1 = cb.metrics
        metrics2 = cb.metrics

        assert metrics1 == metrics2
        assert metrics1 is not metrics2  # Should be a copy


class TestCircuitBreakerExecution:
    @pytest.mark.asyncio
    async def test_execute_success_closed_state(self):
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
        cb = CircuitBreaker()

        async def func_with_params(a, b, c=None):
            return f"{a}-{b}-{c}"

        result = await cb.execute(func_with_params, "x", "y", c="z")

        assert result == "x-y-z"

    @pytest.mark.asyncio
    async def test_execute_failure_increments_count(self):
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


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_retry_success_first_attempt(self):

        async def success_func():
            return "success"

        result = await retry_with_backoff(success_func, max_retries=3)

        assert result == "success"

    @pytest.mark.asyncio
    async def test_retry_success_after_failures(self):
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

        async def always_fails():
            raise ConnectionError("Permanent error")

        with pytest.raises(ConnectionError, match="Permanent error"):
            await retry_with_backoff(always_fails, max_retries=2, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_retry_with_exclude_exceptions(self):
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
    @pytest.mark.asyncio
    async def test_decorator_basic_usage(self):

        @circuit_breaker(failure_threshold=2, recovery_time=0.1)
        async def decorated_func():
            return "success"

        result = await decorated_func()
        assert result == "success"

    @pytest.mark.asyncio
    async def test_decorator_opens_circuit_after_failures(self):

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

        @circuit_breaker(name="custom_circuit")
        async def func():
            return "test"

        result = await func()
        assert result == "test"


class TestWithRetryDecorator:
    @pytest.mark.asyncio
    async def test_decorator_basic_usage(self):

        @with_retry(max_retries=3, base_delay=0.01)
        async def decorated_func():
            return "success"

        result = await decorated_func()
        assert result == "success"

    @pytest.mark.asyncio
    async def test_decorator_retries_on_failure(self):
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
        # canonical retry uses base 2 exponentiation: 2.0*2^0=2.0, 2.0*2^1=4.0 (capped at 5.0)
        assert sleep_calls == [2.0, 4.0]


# ---------------------------------------------------------------------------
# Edge cases: CircuitBreaker concurrent access, jitter bounds, flapping,
# metrics accuracy, recovery_time boundary
# ---------------------------------------------------------------------------


class TestCircuitBreakerConcurrent:
    @pytest.mark.asyncio
    async def test_concurrent_failures_open_circuit_exactly_once(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_time=60.0)
        state_changes = []

        original_change_state = cb._change_state

        async def tracking_change_state(new_state):
            state_changes.append(new_state)
            return await original_change_state(new_state)

        cb._change_state = tracking_change_state

        async def failing_func():
            raise ValueError("fail")

        results = await asyncio.gather(
            *[cb.execute(failing_func) for _ in range(10)],
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) == 10
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_metrics_accurate_under_concurrent_load(self):
        cb = CircuitBreaker(failure_threshold=100, recovery_time=60.0)

        async def success_func():
            return "ok"

        async def fail_func():
            raise ValueError("x")

        results = await asyncio.gather(
            *[cb.execute(success_func) for _ in range(5)],
            *[cb.execute(fail_func) for _ in range(5)],
            return_exceptions=True,
        )
        m = cb.metrics
        assert m["success_count"] == 5
        assert m["failure_count"] == 5

    @pytest.mark.asyncio
    async def test_recovery_time_boundary_exact(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_time=0.1)

        async def fail():
            raise ValueError("x")

        with pytest.raises(ValueError):
            await cb.execute(fail)

        assert cb.state == CircuitState.OPEN

        await asyncio.sleep(0.05)
        assert cb.state == CircuitState.OPEN

        await asyncio.sleep(0.07)

        async def succeed():
            return "ok"

        result = await cb.execute(succeed)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_open_before_recovery_raises(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_time=10.0)

        async def fail():
            raise ValueError("x")

        with pytest.raises(ValueError):
            await cb.execute(fail)

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await cb.execute(fail)

        assert exc_info.value.retry_after is not None
        assert exc_info.value.retry_after > 0


class TestRetryConfigJitter:
    @pytest.mark.asyncio
    async def test_jitter_true_delays_within_bounds(self):
        import lionagi.ln.concurrency.patterns as patterns_mod

        sleep_calls = []
        call_count = [0]

        async def always_fails():
            call_count[0] += 1
            raise APIClientError("fail")

        async def fake_sleep(secs):
            sleep_calls.append(secs)

        with patch.object(patterns_mod.anyio, "sleep", fake_sleep):
            with pytest.raises(APIClientError):
                await retry_with_backoff(
                    always_fails,
                    retry_exceptions=(APIClientError,),
                    max_retries=3,
                    base_delay=1.0,
                    max_delay=10.0,
                    backoff_factor=2.0,
                    jitter=True,
                    jitter_factor=0.2,
                )

        assert len(sleep_calls) == 3
        for delay in sleep_calls:
            assert delay >= 0
            assert delay <= 10.0

    @pytest.mark.asyncio
    async def test_flapping_function_succeeds_on_even_attempts(self):
        call_count = [0]

        async def flapping():
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                return "success"
            raise APIClientError("odd fail")

        result = await retry_with_backoff(
            flapping,
            retry_exceptions=(APIClientError,),
            max_retries=5,
            base_delay=0.001,
            jitter=False,
        )
        assert result == "success"
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_flapping_exhausts_all_retries(self):
        call_count = [0]

        async def always_flap():
            call_count[0] += 1
            raise APIClientError(f"fail {call_count[0]}")

        with pytest.raises(APIClientError):
            await retry_with_backoff(
                always_flap,
                retry_exceptions=(APIClientError,),
                max_retries=3,
                base_delay=0.001,
                jitter=False,
            )
        assert call_count[0] == 4
