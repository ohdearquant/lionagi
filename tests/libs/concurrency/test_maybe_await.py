# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Result classification preserves awaitable execution and caller ownership."""

import asyncio

import pytest

from lionagi.ln.concurrency import maybe_await, utils

pytestmark = pytest.mark.asyncio


class _Awaitable:
    def __init__(self, result):
        self.result = result
        self.awaited = 0

    def __await__(self):
        self.awaited += 1
        if False:  # pragma: no cover - this is a generator-based await protocol
            yield None
        return self.result


@pytest.mark.parametrize("value", [None, False, 0, "value", []])
async def test_default_passes_through_nonawaitable_identity(value):
    assert await maybe_await(value) is value


async def test_default_awaits_a_real_coroutine_once():
    marker = object()
    calls = []

    async def work():
        calls.append("ran")
        return marker

    coroutine = work()
    try:
        assert await maybe_await(coroutine) is marker
        assert calls == ["ran"]
    finally:
        coroutine.close()


async def test_default_awaits_a_real_pending_future():
    marker = object()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    release = loop.call_soon(future.set_result, marker)
    try:
        assert await maybe_await(future) is marker
        assert future.done()
    finally:
        release.cancel()
        if not future.done():
            future.cancel()


async def test_default_awaits_a_real_task():
    marker = object()
    calls = []

    async def work():
        calls.append("ran")
        return marker

    task = asyncio.create_task(work())
    try:
        assert await maybe_await(task) is marker
        assert task.done()
        assert calls == ["ran"]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_default_awaits_a_custom_awaitable_once_without_recursing():
    inner = _Awaitable(object())
    outer = _Awaitable(inner)
    assert await maybe_await(outer) is inner
    assert outer.awaited == 1
    assert inner.awaited == 0


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_awaited_coroutine_preserves_the_exception_object(failure_type):
    error = failure_type("original")

    async def fail():
        raise error

    coroutine = fail()
    try:
        with pytest.raises(failure_type) as info:
            await maybe_await(coroutine)
        assert info.value is error
    finally:
        coroutine.close()


@pytest.mark.parametrize("kind", ["future", "task", "custom"])
async def test_coroutine_classifier_returns_other_awaitables_unchanged(kind):
    marker = object()
    task = None
    if kind == "future":
        value = asyncio.get_running_loop().create_future()
        value.set_result(marker)
    elif kind == "task":

        async def work():
            return marker

        task = asyncio.create_task(work())
        await task
        value = task
    else:
        value = _Awaitable(marker)
    try:
        assert await maybe_await(value, classifier=asyncio.iscoroutine) is value
        if kind == "custom":
            assert value.awaited == 0
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def test_coroutine_classifier_still_awaits_coroutines():
    marker = object()

    async def work():
        return marker

    coroutine = work()
    try:
        assert await maybe_await(coroutine, classifier=asyncio.iscoroutine) is marker
    finally:
        coroutine.close()


async def test_falsey_callable_classifier_is_used():
    value = _Awaitable(object())
    calls = []

    class Decline:
        def __bool__(self):
            return False

        def __call__(self, result):
            calls.append(result)
            return False

    assert await maybe_await(value, classifier=Decline()) is value
    assert calls == [value]
    assert value.awaited == 0


async def test_default_classifier_is_resolved_at_call_time(monkeypatch):
    value = _Awaitable(object())
    calls = []

    def decline(result):
        calls.append(result)
        return False

    with monkeypatch.context() as patch:
        patch.setattr(utils.inspect, "isawaitable", decline)
        assert await maybe_await(value) is value
    assert calls == [value]
    assert value.awaited == 0


async def test_classifier_exception_is_not_replaced_or_awaited():
    value = _Awaitable(object())
    error = LookupError("classification failed")
    calls = []

    def fail(result):
        calls.append(result)
        raise error

    with pytest.raises(LookupError) as info:
        await maybe_await(value, classifier=fail)
    assert info.value is error
    assert calls == [value]
    assert value.awaited == 0
