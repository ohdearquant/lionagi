import gc
import weakref
from functools import partial

import pytest

from lionagi.ln.concurrency import is_coro_func


def test_is_coro_func_basic():
    async def coro():
        pass

    def func():
        pass

    assert is_coro_func(coro) is True
    assert is_coro_func(func) is False
    # repeated classification
    assert is_coro_func(coro) is True
    assert is_coro_func(func) is False


class _CallbackOwner:
    def sync(self, *args):
        pass

    async def asynchronous(self, *args):
        pass


class _SyncCallable:
    def __call__(self):
        pass


class _AsyncCallable:
    async def __call__(self):
        pass


class _UnhashableSyncCallable(_SyncCallable):
    __hash__ = None


class _UnhashableAsyncCallable(_AsyncCallable):
    __hash__ = None


class _UnhashableOwner(_CallbackOwner):
    __hash__ = None


@pytest.mark.parametrize(
    "shape,expected",
    [
        ("sync-function", False),
        ("async-function", True),
        ("sync-bound-method", False),
        ("async-bound-method", True),
        ("sync-partial-function", False),
        ("async-partial-function", True),
        ("sync-partial-bound-method", False),
        ("async-partial-bound-method", True),
        ("hashable-sync-callable-instance", False),
        ("hashable-async-callable-instance", False),
    ],
    ids=[
        "sync-function",
        "async-function",
        "sync-bound-method",
        "async-bound-method",
        "sync-partial-function",
        "async-partial-function",
        "sync-partial-bound-method",
        "async-partial-bound-method",
        "hashable-sync-callable-instance",
        "hashable-async-callable-instance",
    ],
)
def test_is_coro_func_preserves_callable_classification(shape, expected):
    def function(*args):
        pass

    async def coroutine(*args):
        pass

    owner = _CallbackOwner()
    choices = {
        "sync-function": function,
        "async-function": coroutine,
        "sync-bound-method": owner.sync,
        "async-bound-method": owner.asynchronous,
        "sync-partial-function": partial(function, "synthetic-argument"),
        "async-partial-function": partial(coroutine, "synthetic-argument"),
        "sync-partial-bound-method": partial(owner.sync, "synthetic-argument"),
        "async-partial-bound-method": partial(owner.asynchronous, "synthetic-argument"),
        "hashable-sync-callable-instance": _SyncCallable(),
        "hashable-async-callable-instance": _AsyncCallable(),
    }
    assert is_coro_func(choices[shape]) is expected
    assert is_coro_func(choices[shape]) is expected


@pytest.mark.parametrize(
    "method_name,wrapped,expected",
    [
        ("sync", False, False),
        ("asynchronous", False, True),
        ("sync", True, False),
        ("asynchronous", True, True),
    ],
    ids=[
        "sync-bound-method",
        "async-bound-method",
        "sync-partial-bound-method",
        "async-partial-bound-method",
    ],
)
def test_is_coro_func_does_not_retain_a_bound_owner(method_name, wrapped, expected):
    def classify_and_release():
        owner = _CallbackOwner()
        released = weakref.ref(owner)
        callback = getattr(owner, method_name)
        if wrapped:
            callback = partial(callback, "synthetic-argument")
        assert is_coro_func(callback) is expected
        return released

    released = classify_and_release()
    gc.collect()
    assert released() is None, "classifying a callback must not retain its owner"


@pytest.mark.parametrize(
    "shape,expected",
    [
        ("sync-callable-instance", False),
        ("async-callable-instance", False),
        ("sync-owner-bound-method", False),
        ("async-owner-bound-method", True),
    ],
    ids=[
        "sync-callable-instance",
        "async-callable-instance",
        "sync-owner-bound-method",
        "async-owner-bound-method",
    ],
)
def test_is_coro_func_classifies_unhashable_instances_and_owner_bound_methods(shape, expected):
    owner = _UnhashableOwner()
    choices = {
        "sync-callable-instance": _UnhashableSyncCallable(),
        "async-callable-instance": _UnhashableAsyncCallable(),
        "sync-owner-bound-method": owner.sync,
        "async-owner-bound-method": owner.asynchronous,
    }
    assert is_coro_func(choices[shape]) is expected


def test_a_constructed_branch_can_be_collected_with_registered_callbacks():
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    def construct_and_release():
        model = iModel(provider="openai", model="no-request", api_key="dummy-key-for-testing")
        branch = Branch(
            system="Keep this constructed branch local.",
            system_datetime=False,
            chat_model=model,
        )
        assert branch.on_message_added == [branch._schedule_emit, branch._track_message_in_view]
        assert branch.msgs._on_pile_rewritten == [branch._sync_view_with_pile]
        assert branch.system is not None and len(branch.messages) == 1
        assert model.calls_sent == 0
        released = weakref.ref(branch)
        del branch
        return released

    released = construct_and_release()
    gc.collect()
    assert released() is None, "constructing a Branch must not retain its callback owner"


def test_is_coro_func_classifies_a_constructed_branch_coroutine():
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    model = iModel(provider="openai", model="no-request", api_key="dummy-key-for-testing")
    branch = Branch(
        system="Classify the real Branch coroutine without calling it.",
        system_datetime=False,
        chat_model=model,
    )
    assert is_coro_func(branch.drain_signals) is True
    assert is_coro_func(branch._schedule_emit) is False
    assert model.calls_sent == 0


def test_is_coro_func_does_not_retain_a_closure_owner():
    def classify_and_release():
        owner = _CallbackOwner()
        released = weakref.ref(owner)

        def callback():
            return owner

        assert is_coro_func(callback) is False
        return released

    released = classify_and_release()
    gc.collect()
    assert released() is None, "classifying a closure must not retain its captured owner"
