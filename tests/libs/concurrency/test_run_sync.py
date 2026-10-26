"""Worker offload controls and callable argument boundaries."""

import inspect
import threading
from contextvars import ContextVar
from functools import partial

import anyio
import pytest

from lionagi.ln.concurrency import CapacityLimiter, run_sync
from lionagi.ln.concurrency import utils as utils_mod

pytestmark = pytest.mark.anyio

_WAIT_SECONDS = 5


async def _wait_thread_event(event: threading.Event) -> None:
    with anyio.fail_after(_WAIT_SECONDS + 1):
        observed = await anyio.to_thread.run_sync(
            event.wait,
            _WAIT_SECONDS,
            abandon_on_cancel=True,
            limiter=anyio.CapacityLimiter(1),
        )
        assert observed, "worker did not reach its synchronization point"


@pytest.mark.parametrize("use_kwargs", [False, True])
async def test_run_sync_preserves_arguments_result_and_context(use_kwargs):
    context = ContextVar("worker_context")
    caller_value = object()
    worker_value = object()
    result = object()
    argument = object()
    observed = []

    def worker(value, extra):
        observed.append((value, extra, context.get()))
        context.set(worker_value)
        return result

    token = context.set(caller_value)
    try:
        if use_kwargs:
            actual = await run_sync(worker, argument, extra=17)
        else:
            actual = await run_sync(worker, argument, 17)
        assert actual is result
        assert observed == [(argument, 17, caller_value)]
        assert context.get() is caller_value
    finally:
        context.reset(token)


class WorkerFailure(BaseException):
    pass


@pytest.mark.parametrize("error_type", [ValueError, WorkerFailure])
@pytest.mark.parametrize("abandon_on_cancel", [False, True])
async def test_run_sync_preserves_uncancelled_error_identity(error_type, abandon_on_cancel):
    error = error_type("worker failure")

    def worker():
        raise error

    with pytest.raises(error_type) as raised:
        await run_sync(worker, abandon_on_cancel=abandon_on_cancel)
    assert raised.value is error


async def test_run_sync_partial_keeps_reserved_target_keywords():
    business_limiter = object()
    business_abandon = object()
    business_cancellable = object()

    def worker(value, *, limiter, abandon_on_cancel, cancellable):
        return value, limiter, abandon_on_cancel, cancellable

    bound = partial(
        worker,
        limiter=business_limiter,
        abandon_on_cancel=business_abandon,
    )
    actual = await run_sync(bound, 17, cancellable=business_cancellable)
    assert actual == (17, business_limiter, business_abandon, business_cancellable)


@pytest.mark.parametrize("use_kwargs", [False, True])
@pytest.mark.parametrize("use_limiter", [False, True])
async def test_run_sync_delegates_controls_and_unwraps_ln_limiter(
    monkeypatch, use_kwargs, use_limiter
):
    limiter = CapacityLimiter(1) if use_limiter else None
    observed = []

    async def offload(func, *args, abandon_on_cancel=False, limiter=None):
        observed.append((abandon_on_cancel, limiter))
        return func(*args)

    def worker(value, *, extra=0):
        return value + extra

    monkeypatch.setattr(utils_mod.anyio.to_thread, "run_sync", offload)
    target_kwargs = {"extra": 2} if use_kwargs else {}
    assert await run_sync(worker, 1, abandon_on_cancel=True, limiter=limiter, **target_kwargs) == (
        3 if use_kwargs else 1
    )
    assert observed == [(True, limiter._lim if limiter is not None else None)]


async def test_run_sync_controls_are_keyword_only():
    signature = inspect.signature(run_sync)
    for name, default in (("abandon_on_cancel", False), ("limiter", None)):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is default


@pytest.mark.parametrize("setting", [None, False, True])
@pytest.mark.parametrize("raise_late", [False, True])
async def test_run_sync_scope_cancellation_orders_host_and_worker(setting, raise_late):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    host_exited = anyio.Event()
    scopes = []
    outcomes = []
    result = object()
    error = ValueError("late worker error")

    def worker():
        started.set()
        try:
            assert release.wait(_WAIT_SECONDS), "test did not release worker"
            if raise_late:
                raise error
            return result
        finally:
            finished.set()

    async def host():
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    options = {} if setting is None else {"abandon_on_cancel": setting}
                    outcomes.append(await run_sync(worker, **options))
                except ValueError as exc:
                    outcomes.append(exc)
        finally:
            host_exited.set()

    async with anyio.create_task_group() as group:
        group.start_soon(host)
        try:
            await _wait_thread_event(started)
            scopes[0].cancel()
            with anyio.fail_after(_WAIT_SECONDS):
                await anyio.wait_all_tasks_blocked()
            assert not finished.is_set()
            assert host_exited.is_set() is (setting is True)
        finally:
            release.set()
            with anyio.CancelScope(shield=True):
                if started.is_set():
                    await _wait_thread_event(finished)
                with anyio.fail_after(_WAIT_SECONDS):
                    await host_exited.wait()

    if setting is True:
        assert outcomes == []
    else:
        assert outcomes == [error if raise_late else result]


async def test_run_sync_ln_limiter_serializes_real_workers():
    limiter = CapacityLimiter(1)
    started = {name: threading.Event() for name in ("first", "second")}
    release = {name: threading.Event() for name in started}
    finished = {name: threading.Event() for name in started}
    counter_lock = threading.Lock()
    active = 0
    peak = 0
    results = []

    def worker(name):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
        started[name].set()
        try:
            assert release[name].wait(_WAIT_SECONDS), "test did not release worker"
            return name
        finally:
            with counter_lock:
                active -= 1
            finished[name].set()

    async def call(name):
        results.append(await run_sync(worker, name, limiter=limiter))

    async with anyio.create_task_group() as group:
        try:
            group.start_soon(call, "first")
            await _wait_thread_event(started["first"])
            group.start_soon(call, "second")
            with anyio.fail_after(_WAIT_SECONDS):
                await anyio.wait_all_tasks_blocked()
            assert not started["second"].is_set()
            assert limiter.borrowed_tokens == 1
            release["first"].set()
            await _wait_thread_event(started["second"])
            assert finished["first"].is_set()
        finally:
            for event in release.values():
                event.set()
            with anyio.CancelScope(shield=True):
                for name in started:
                    if started[name].is_set():
                        await _wait_thread_event(finished[name])

    assert results == ["first", "second"]
    assert peak == 1
    assert limiter.borrowed_tokens == 0


async def test_run_sync_custom_limiter_replaces_default_and_none_uses_default():
    default = anyio.to_thread.current_default_thread_limiter()
    old_total = default.total_tokens
    custom = CapacityLimiter(1)
    default_entered = threading.Event()
    default_done = anyio.Event()

    def default_worker():
        default_entered.set()

    async def default_call():
        await run_sync(default_worker, limiter=None)
        default_done.set()

    try:
        default.total_tokens = 1
        async with anyio.create_task_group() as group:
            async with default:
                group.start_soon(default_call)
                with anyio.fail_after(_WAIT_SECONDS):
                    await anyio.wait_all_tasks_blocked()
                assert default.statistics().tasks_waiting == 1
                assert not default_entered.is_set()
                with anyio.fail_after(_WAIT_SECONDS):
                    assert await run_sync(lambda: "custom", limiter=custom) == "custom"
                assert not default_entered.is_set()
            with anyio.fail_after(_WAIT_SECONDS):
                await default_done.wait()
    finally:
        default.total_tokens = old_total
    assert default_entered.is_set()


@pytest.mark.parametrize(
    ("anyio_backend", "expected_borrowed"),
    [("asyncio", 0), ("trio", 1)],
    ids=["asyncio-releases-wait-token", "trio-keeps-worker-token"],
)
async def test_abandoned_wait_retains_the_backend_token_lifetime(anyio_backend, expected_borrowed):
    limiter = CapacityLimiter(1)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    host_exited = anyio.Event()
    scopes = []

    def worker():
        started.set()
        try:
            assert release.wait(_WAIT_SECONDS), "test did not release worker"
        finally:
            finished.set()

    async def host():
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await run_sync(worker, abandon_on_cancel=True, limiter=limiter)
        finally:
            host_exited.set()

    async with anyio.create_task_group() as group:
        group.start_soon(host)
        try:
            await _wait_thread_event(started)
            scopes[0].cancel()
            with anyio.fail_after(_WAIT_SECONDS):
                await host_exited.wait()
            assert not finished.is_set()
            assert limiter.borrowed_tokens == expected_borrowed
            assert limiter.available_tokens == 1 - expected_borrowed
        finally:
            release.set()
            for scope in scopes:
                scope.cancel()
            with anyio.CancelScope(shield=True):
                with anyio.fail_after(_WAIT_SECONDS):
                    await host_exited.wait()
                if started.is_set():
                    await _wait_thread_event(finished)
                with anyio.fail_after(_WAIT_SECONDS):
                    await limiter.acquire()
                limiter.release()

    assert limiter.borrowed_tokens == 0


@pytest.mark.parametrize("kind", ["object", "native"])
async def test_run_sync_rejects_wrong_limiter_type_before_worker_starts(kind):
    entered = threading.Event()
    limiter = object() if kind == "object" else anyio.CapacityLimiter(1)
    with pytest.raises(
        TypeError,
        match=r"limiter must be a lionagi\.ln\.concurrency\.CapacityLimiter or None",
    ):
        await run_sync(entered.set, limiter=limiter)
    assert not entered.is_set()


class _TruthinessTrap:
    def __bool__(self) -> bool:
        raise AssertionError("abandon_on_cancel must not be coerced to bool")


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param(0, id="zero"),
        pytest.param(1, id="one"),
        pytest.param(None, id="none"),
        pytest.param(1.0, id="float"),
        pytest.param("false", id="string"),
        pytest.param([], id="empty-list"),
        pytest.param(object(), id="object"),
        pytest.param(_TruthinessTrap(), id="truthiness-trap"),
    ],
)
async def test_run_sync_rejects_non_bool_abandon_before_worker_starts(setting):
    entered = threading.Event()
    try:
        with pytest.raises(TypeError, match=r"abandon_on_cancel.*bool"):
            await run_sync(entered.set, abandon_on_cancel=setting)
    finally:
        assert not entered.is_set()
