# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect

import anyio
import pytest

import lionagi.ln.concurrency.patterns as patterns
from lionagi.ln.concurrency import retry


class RefusedConnection(ConnectionError):
    pass


class StopRetry(BaseException):
    pass


@pytest.fixture
def retry_waits(monkeypatch):
    waits = []

    async def record_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(patterns, "effective_deadline", lambda: None)
    monkeypatch.setattr(patterns.anyio, "sleep", record_sleep)
    return waits


def test_exclude_is_keyword_only_and_defaults_to_an_empty_tuple():
    parameter = inspect.signature(retry).parameters["exclude"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == ()


@pytest.mark.anyio
async def test_excluded_subclass_escapes_before_backoff(anyio_backend, monkeypatch):
    original = RefusedConnection("refused")
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        raise original

    def unexpected_backoff(*_args):
        pytest.fail("an excluded error reached backoff work")

    async def unexpected_sleep(_delay):
        pytest.fail("an excluded error slept")

    monkeypatch.setattr(patterns, "effective_deadline", lambda: 10.0)
    monkeypatch.setattr(patterns, "current_time", unexpected_backoff)
    monkeypatch.setattr(patterns.random, "random", unexpected_backoff)
    monkeypatch.setattr(patterns.anyio, "sleep", unexpected_sleep)

    with pytest.raises(RefusedConnection) as caught:
        await retry(failing, attempts=4, retry_on=(OSError,), exclude=(ConnectionError,))

    assert caught.value is original
    assert calls == 1


@pytest.mark.anyio
async def test_an_unexcluded_sibling_still_retries(anyio_backend, retry_waits):
    calls = 0
    result = object()

    async def succeeds_on_third_attempt():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise FileNotFoundError("retry this sibling")
        return result

    returned = await retry(
        succeeds_on_third_attempt,
        attempts=3,
        base_delay=0.125,
        max_delay=1.0,
        jitter=0,
        retry_on=(OSError,),
        exclude=(ConnectionError,),
    )

    assert returned is result
    assert calls == 3
    assert retry_waits == [0.125, 0.25]


@pytest.mark.anyio
@pytest.mark.parametrize("exclude_mode", ["omitted", "empty"])
@pytest.mark.parametrize("outcome", ["success", "exhausted"])
async def test_empty_exclude_preserves_attempts_and_delays(
    anyio_backend, retry_waits, exclude_mode, outcome
):
    original = ConnectionError("retryable")
    result = object()
    calls = 0

    async def attempt():
        nonlocal calls
        calls += 1
        if outcome == "success" and calls == 3:
            return result
        raise original

    options = {"exclude": ()} if exclude_mode == "empty" else {}
    if outcome == "success":
        returned = await retry(
            attempt, attempts=3, base_delay=0.125, max_delay=1.0, jitter=0, **options
        )
        assert returned is result
    else:
        with pytest.raises(ConnectionError) as caught:
            await retry(attempt, attempts=3, base_delay=0.125, max_delay=1.0, jitter=0, **options)
        assert caught.value is original

    assert calls == 3
    assert retry_waits == [0.125, 0.25]


@pytest.mark.anyio
@pytest.mark.parametrize("chain_kind", ["cause", "context"])
async def test_excluded_error_keeps_its_identity_and_exception_chain(
    anyio_backend, retry_waits, chain_kind
):
    original = ValueError("refused", 17)
    preceding = LookupError("original diagnostic")
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        if chain_kind == "cause":
            raise original from preceding
        try:
            raise preceding
        except LookupError:
            raise original

    with pytest.raises(ValueError) as caught:
        await retry(failing, attempts=4, exclude=(ValueError,))

    assert caught.value is original
    assert caught.value.args == ("refused", 17)
    assert caught.value.__cause__ is (preceding if chain_kind == "cause" else None)
    assert caught.value.__context__ is (preceding if chain_kind == "context" else None)
    assert caught.value.__suppress_context__ is (chain_kind == "cause")
    assert calls == 1
    assert retry_waits == []


@pytest.mark.anyio
@pytest.mark.parametrize("retry_family", ["base_exception", "backend_cancellation"])
async def test_exclusion_does_not_override_cancellation_validation(anyio_backend, retry_family):
    called = False

    async def attempt():
        nonlocal called
        called = True

    forbidden_type = (
        BaseException if retry_family == "base_exception" else anyio.get_cancelled_exc_class()
    )
    with pytest.raises(
        ValueError, match="retry_on must not include the cancellation exception type"
    ):
        await retry(attempt, retry_on=(forbidden_type,), exclude=(BaseException,))

    assert called is False


@pytest.mark.anyio
async def test_enclosing_cancellation_still_escapes_retry(anyio_backend):
    calls = 0

    with anyio.CancelScope() as scope:

        async def cancelled_attempt():
            nonlocal calls
            calls += 1
            scope.cancel()
            await anyio.lowlevel.checkpoint()

        await retry(cancelled_attempt, exclude=(Exception,))
        pytest.fail("retry returned after enclosing cancellation")

    assert scope.cancelled_caught is True
    assert calls == 1


@pytest.mark.anyio
async def test_exclusion_can_match_an_explicit_base_exception_family(anyio_backend, retry_waits):
    original = StopRetry("stop")
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        raise original

    with pytest.raises(StopRetry) as caught:
        await retry(failing, retry_on=(StopRetry,), exclude=(BaseException,))

    assert caught.value is original
    assert calls == 1
    assert retry_waits == []
