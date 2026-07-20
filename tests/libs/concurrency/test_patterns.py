from contextlib import nullcontext

import anyio
import pytest

from lionagi.ln.concurrency import bounded_map, fail_after, gather, race, retry


@pytest.fixture
def retry_sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry backoff requests without advancing a real backend clock."""
    import lionagi.ln.concurrency.patterns as patterns_mod

    calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(delay)

    monkeypatch.setattr(patterns_mod.anyio, "sleep", fake_sleep)
    return calls


@pytest.mark.slow
@pytest.mark.anyio
async def test_gather_first_error_cancels_peers(anyio_backend):
    peer_ready = anyio.Event()
    failure_released = anyio.Event()
    cancellation_order: list[str] = []

    async def boom():
        await peer_ready.wait()
        failure_released.set()
        raise RuntimeError("x")

    async def peer():
        peer_ready.set()
        try:
            # Once boom releases the barrier and raises, gather has through the
            # next scheduler checkpoint to cancel this peer. A late/absent
            # cancellation lets the checkpoint complete and fails below.
            await failure_released.wait()
            await anyio.lowlevel.checkpoint()
        except BaseException:
            cancellation_order.append("cancelled_before_deadline")
            raise
        raise AssertionError("peer was not cancelled by the next checkpoint")

    with pytest.raises(RuntimeError):
        await gather(boom(), peer(), return_exceptions=False)

    assert cancellation_order == ["cancelled_before_deadline"]


@pytest.mark.anyio
async def test_bounded_map_raises_and_cancels_others(anyio_backend):
    started = 0
    cancelled = anyio.Event()

    async def fn(x):
        nonlocal started
        started += 1
        if x == 3:
            await anyio.sleep(0.01)
            raise ValueError("boom")
        try:
            await anyio.sleep(0.1)
        except BaseException:
            cancelled.set()
            raise
        return x

    with pytest.raises(ValueError):
        await bounded_map(fn, range(6), limit=2)
    assert started >= 2  # at least limited concurrency started
    assert cancelled.is_set()


@pytest.mark.anyio
async def test_race_multiple_exceptions_vs_success(anyio_backend):
    async def err1():
        await anyio.sleep(0.005)
        raise RuntimeError("e1")

    async def err2():
        await anyio.sleep(0.006)
        raise ValueError("e2")

    async def ok():
        await anyio.sleep(0.004)
        return "ok"

    assert await race(err1(), ok(), err2()) == "ok"


@pytest.mark.anyio
async def test_retry_respects_attempts_count(anyio_backend, retry_sleep_calls):
    calls = {"n": 0}

    async def always():
        calls["n"] += 1
        raise TimeoutError("x")

    with pytest.raises(TimeoutError):
        await retry(
            always,
            attempts=3,
            base_delay=0.001,
            max_delay=0.002,
            retry_on=(TimeoutError,),
        )
    assert calls["n"] == 3
    assert len(retry_sleep_calls) == 2


@pytest.mark.anyio
async def test_gather_empty_returns_empty(anyio_backend):
    res = await gather()
    assert res == []


@pytest.mark.anyio
async def test_gather_return_exceptions_true(anyio_backend):
    async def success(x):
        await anyio.sleep(0.001 * x)  # Reduced timing
        return f"result_{x}"

    async def failure(x):
        await anyio.sleep(0.001 * x)  # Reduced timing
        raise ValueError(f"error_{x}")

    # Mix successes and failures
    results = await gather(success(1), failure(2), success(3), failure(4), return_exceptions=True)

    assert len(results) == 4
    assert results[0] == "result_1"
    assert isinstance(results[1], ValueError)
    assert str(results[1]) == "error_2"
    assert results[2] == "result_3"
    assert isinstance(results[3], ValueError)
    assert str(results[3]) == "error_4"


@pytest.mark.anyio
async def test_gather_return_exceptions_preserves_order(anyio_backend):
    async def task(i):
        # Reverse sleep times to test order preservation
        await anyio.sleep(0.001 * (6 - i))  # Much reduced timing
        if i % 2 == 0:
            return i
        raise RuntimeError(f"error_{i}")

    results = await gather(*[task(i) for i in range(6)], return_exceptions=True)

    assert len(results) == 6
    for i in range(6):
        if i % 2 == 0:
            assert results[i] == i
        else:
            assert isinstance(results[i], RuntimeError)
            assert str(results[i]) == f"error_{i}"


@pytest.mark.anyio
async def test_bounded_map_empty_and_large_limit(anyio_backend):
    out = await bounded_map(lambda x: x, [], limit=999)
    assert out == []


@pytest.mark.anyio
async def test_bounded_map_respects_limit(anyio_backend):
    LIMIT = 3
    TASKS = 10  # Reduced from 20
    current_concurrency = 0
    max_observed_concurrency = 0

    async def worker(x):
        nonlocal current_concurrency, max_observed_concurrency
        current_concurrency += 1
        max_observed_concurrency = max(max_observed_concurrency, current_concurrency)

        await anyio.sleep(0.001)  # Minimal sleep

        current_concurrency -= 1
        return x

    results = await bounded_map(worker, range(TASKS), limit=LIMIT)
    assert max_observed_concurrency == LIMIT
    assert results == list(range(TASKS))  # Verify correct results


@pytest.mark.anyio
async def test_bounded_map_with_return_exceptions(anyio_backend):
    async def worker(x):
        await anyio.sleep(0.001)
        if x % 3 == 0:
            raise ValueError(f"error_{x}")
        return x * 2

    results = await bounded_map(worker, range(9), limit=3, return_exceptions=True)

    assert len(results) == 9
    for i in range(9):
        if i % 3 == 0:
            assert isinstance(results[i], ValueError)
            assert str(results[i]) == f"error_{i}"
        else:
            assert results[i] == i * 2


@pytest.mark.slow
@pytest.mark.anyio
async def test_race_single_and_loser_cancelled(anyio_backend):
    # single
    async def one():
        await anyio.sleep(0.002)
        return 1

    assert await race(one()) == 1
    # loser is cancelled
    cancelled = anyio.Event()

    async def slow():
        try:
            await anyio.sleep(10)
            return "slow"
        except BaseException:
            cancelled.set()
            raise

    async def fast():
        await anyio.sleep(0.002)
        return "fast"

    assert await race(slow(), fast()) == "fast"
    assert cancelled.is_set()


@pytest.mark.anyio
async def test_race_first_failure_propagates(anyio_backend):
    cancelled = []

    async def fast_failure():
        await anyio.sleep(0.005)
        raise ValueError("I fail fast")

    async def slow_success():
        try:
            await anyio.sleep(0.05)
            return "success"
        except BaseException:
            cancelled.append("slow_success")
            raise

    async def slower_success():
        try:
            await anyio.sleep(0.1)
            return "also_success"
        except BaseException:
            cancelled.append("slower_success")
            raise

    # First completion is a failure, should propagate immediately
    with pytest.raises(ValueError) as exc_info:
        await race(fast_failure(), slow_success(), slower_success())

    assert str(exc_info.value) == "I fail fast"
    # Give a moment for cancellations
    await anyio.sleep(0.01)
    # At least one should be cancelled
    assert len(cancelled) > 0


@pytest.mark.anyio
async def test_race_all_failures_returns_first(anyio_backend):
    async def fail1():
        await anyio.sleep(0.01)
        raise ValueError("first")

    async def fail2():
        await anyio.sleep(0.02)
        raise RuntimeError("second")

    async def fail3():
        await anyio.sleep(0.03)
        raise TypeError("third")

    with pytest.raises(ValueError) as exc_info:
        await race(fail1(), fail2(), fail3())

    assert str(exc_info.value) == "first"


@pytest.mark.anyio
async def test_race_requires_at_least_one(anyio_backend):
    with pytest.raises(ValueError):
        await race()


@pytest.mark.anyio
async def test_retry_deadline_capped_by_parent(anyio_backend, monkeypatch: pytest.MonkeyPatch):
    import lionagi.ln.concurrency.patterns as patterns_mod

    calls = {"n": 0}
    sleep_calls: list[float] = []

    async def always():
        calls["n"] += 1
        raise TimeoutError("x")

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    clock = iter((9.0, 10.0))
    monkeypatch.setattr(patterns_mod, "effective_deadline", lambda: 10.0)
    monkeypatch.setattr(patterns_mod, "current_time", lambda: next(clock))
    monkeypatch.setattr(patterns_mod, "move_on_at", lambda _deadline: nullcontext())
    monkeypatch.setattr(patterns_mod.anyio, "sleep", fake_sleep)

    with pytest.raises(TimeoutError):
        await retry(
            always,
            attempts=50,
            base_delay=0.01,
            max_delay=0.1,
            retry_on=(TimeoutError,),
            jitter=0.0,
        )

    assert calls["n"] == 1
    assert sleep_calls == [0.01]


@pytest.mark.anyio
async def test_retry_with_and_without_jitter(anyio_backend, retry_sleep_calls):
    seen = {"n": 0}

    async def boom():
        seen["n"] += 1
        raise TimeoutError("nope")

    # no jitter path
    with pytest.raises(TimeoutError):
        await retry(
            boom,
            attempts=2,
            base_delay=0.001,
            max_delay=0.002,
            retry_on=(TimeoutError,),
            jitter=0.0,
        )
    # jitter>0 path executes as well
    with pytest.raises(TimeoutError):
        await retry(
            boom,
            attempts=2,
            base_delay=0.001,
            max_delay=0.002,
            retry_on=(TimeoutError,),
            jitter=0.001,
        )
    assert seen["n"] >= 4
    assert len(retry_sleep_calls) == 2


@pytest.mark.anyio
async def test_retry_eventual_success(anyio_backend, retry_sleep_calls):
    attempts = {"count": 0}

    async def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError(f"Attempt {attempts['count']} failed")
        return "success"

    result = await retry(flaky, attempts=5, base_delay=0.001, retry_on=(ConnectionError,))

    assert result == "success"
    assert attempts["count"] == 3  # Succeeded on third attempt
    assert len(retry_sleep_calls) == 2


@pytest.mark.anyio
async def test_retry_exception_filtering(anyio_backend):
    attempts = {"count": 0}

    async def raises_wrong_exception():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("Not in retry_on list")
        raise TimeoutError("Should not reach here")

    # ValueError is not in retry_on, should fail immediately
    with pytest.raises(ValueError) as exc_info:
        await retry(
            raises_wrong_exception,
            attempts=5,
            base_delay=0.001,
            retry_on=(
                TimeoutError,
                ConnectionError,
            ),  # ValueError not included
        )

    assert str(exc_info.value) == "Not in retry_on list"
    assert attempts["count"] == 1  # Only one attempt made


@pytest.mark.anyio
async def test_retry_mixed_exceptions(anyio_backend, retry_sleep_calls):
    attempts = {"count": 0}

    async def mixed_failures():
        attempts["count"] += 1
        if attempts["count"] <= 2:
            # Retryable
            raise TimeoutError(f"Timeout {attempts['count']}")
        elif attempts["count"] == 3:
            # Non-retryable
            raise ValueError("Critical error")
        return "should_not_reach"

    with pytest.raises(ValueError) as exc_info:
        await retry(
            mixed_failures,
            attempts=10,
            base_delay=0.001,
            retry_on=(TimeoutError,),
        )

    assert str(exc_info.value) == "Critical error"
    assert attempts["count"] == 3  # Two retries then critical failure
    assert len(retry_sleep_calls) == 2


# NOTE: Lines 82 and 168 are defensive code for rare edge cases where
# an ExceptionGroup contains ONLY cancellation exceptions. These are
# extremely difficult to test reliably due to the nature of structured
# concurrency and cancellation propagation. They represent defensive
# programming for theoretical edge cases that are unlikely to occur in practice.


@pytest.mark.anyio
async def test_bounded_map_invalid_limit(anyio_backend):
    async def dummy(x):
        return x

    # Test with limit = 0
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await bounded_map(dummy, [1, 2, 3], limit=0)

    # Test with negative limit
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await bounded_map(dummy, [1, 2, 3], limit=-1)


@pytest.mark.anyio
async def test_retry_deadline_expired_immediately(anyio_backend, monkeypatch: pytest.MonkeyPatch):
    import lionagi.ln.concurrency.patterns as patterns_mod

    calls = {"n": 0}

    async def always_fail():
        calls["n"] += 1
        raise TimeoutError("Failed")

    async def unexpected_sleep(_delay: float) -> None:
        pytest.fail("retry slept after its effective deadline had expired")

    monkeypatch.setattr(patterns_mod, "effective_deadline", lambda: 10.0)
    monkeypatch.setattr(patterns_mod, "current_time", lambda: 10.0)
    monkeypatch.setattr(patterns_mod.anyio, "sleep", unexpected_sleep)

    with pytest.raises(TimeoutError):
        await retry(
            always_fail,
            attempts=100,
            base_delay=1.0,
            max_delay=2.0,
            retry_on=(TimeoutError,),
            jitter=0.0,
        )

    assert calls["n"] == 1


@pytest.mark.anyio
async def test_retry_backoff_factor_honored(anyio_backend):
    """backoff_factor param is used; non-default values change the delay sequence."""
    from unittest.mock import patch

    import lionagi.ln.concurrency.patterns as patterns_mod

    sleep_calls = []
    attempt_count = [0]

    async def fail_twice():
        attempt_count[0] += 1
        if attempt_count[0] < 3:
            raise TimeoutError("x")
        return "ok"

    async def fake_sleep(secs):
        sleep_calls.append(secs)

    with patch.object(patterns_mod.anyio, "sleep", fake_sleep):
        result = await retry(
            fail_twice,
            attempts=3,
            base_delay=1.0,
            max_delay=100.0,
            backoff_factor=5.0,
            retry_on=(TimeoutError,),
            jitter=0.0,
        )

    assert result == "ok"
    # With backoff_factor=5.0: attempt 1 → 1.0*5^0=1.0, attempt 2 → 1.0*5^1=5.0
    assert sleep_calls == [1.0, 5.0], f"Expected [1.0, 5.0] but got {sleep_calls}"


@pytest.mark.anyio
async def test_retry_backoff_factor_exactly_one_accepted(anyio_backend):
    """backoff_factor=1.0 is the boundary and must be accepted (constant delay)."""
    from unittest.mock import patch

    import lionagi.ln.concurrency.patterns as patterns_mod

    sleep_calls = []
    attempt_count = [0]

    async def fail_twice():
        attempt_count[0] += 1
        if attempt_count[0] < 3:
            raise TimeoutError("x")
        return "ok"

    async def fake_sleep(secs):
        sleep_calls.append(secs)

    with patch.object(patterns_mod.anyio, "sleep", fake_sleep):
        result = await retry(
            fail_twice,
            attempts=3,
            base_delay=1.0,
            max_delay=100.0,
            backoff_factor=1.0,
            retry_on=(TimeoutError,),
            jitter=0.0,
        )

    assert result == "ok"
    # With backoff_factor=1.0: all delays are base_delay*1^n = 1.0 (constant)
    assert sleep_calls == [1.0, 1.0], f"Expected [1.0, 1.0] but got {sleep_calls}"


@pytest.mark.anyio
@pytest.mark.parametrize("bad_factor", [0.5, 0, -2.0])
async def test_retry_backoff_factor_invalid_rejected(bad_factor, anyio_backend):
    """backoff_factor < 1.0 must raise ValueError immediately."""

    async def noop():
        return "never called"  # pragma: no cover

    with pytest.raises(ValueError, match="backoff_factor must be >= 1.0"):
        await retry(
            noop,
            attempts=3,
            base_delay=1.0,
            max_delay=100.0,
            backoff_factor=bad_factor,
            retry_on=(TimeoutError,),
        )


@pytest.mark.anyio
async def test_retry_backoff_factor_large_capped_by_max_delay(anyio_backend):
    """Large backoff_factor still gets capped by max_delay."""
    from unittest.mock import patch

    import lionagi.ln.concurrency.patterns as patterns_mod

    sleep_calls = []
    attempt_count = [0]

    async def fail_three_times():
        attempt_count[0] += 1
        if attempt_count[0] < 4:
            raise TimeoutError("x")
        return "ok"

    async def fake_sleep(secs):
        sleep_calls.append(secs)

    with patch.object(patterns_mod.anyio, "sleep", fake_sleep):
        result = await retry(
            fail_three_times,
            attempts=4,
            base_delay=1.0,
            max_delay=5.0,
            backoff_factor=100.0,
            retry_on=(TimeoutError,),
            jitter=0.0,
        )

    assert result == "ok"
    # Without cap: 1.0, 100.0, 10000.0.  The exact sequence proves the large
    # factor was actually applied then capped — a base-2 implementation would
    # produce [1.0, 2.0, 4.0], which an `all(d <= 5.0)` check cannot catch.
    assert sleep_calls == [1.0, 5.0, 5.0]
