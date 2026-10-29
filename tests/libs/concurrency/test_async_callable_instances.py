"""Async callable instances keep coroutine dispatch and hook preflight semantics."""

import asyncio
import gc
import weakref
from functools import partial

import pytest

from lionagi.ln.concurrency import is_coro_func
from lionagi.protocols.messages.manager import MessageManager


class _AsyncRecorder:
    def __init__(self):
        self.seen = []

    async def __call__(self, message):
        await asyncio.sleep(0)
        self.seen.append(message)
        return message


class _UnhashableAsyncRecorder(_AsyncRecorder):
    __hash__ = None


def _wrapped(callback, wrapping):
    if wrapping == "partial":
        return partial(callback)
    return callback


@pytest.mark.parametrize("kind", [_AsyncRecorder, _UnhashableAsyncRecorder])
@pytest.mark.parametrize("wrapping", ["direct", "partial"])
def test_async_callable_instances_are_classified_without_retaining_them(kind, wrapping):
    def classify():
        callback = kind()
        reference = weakref.ref(callback)
        assert is_coro_func(_wrapped(callback, wrapping)) is True
        return reference

    reference = classify()
    gc.collect()
    assert reference() is None


def test_callable_instance_uses_its_type_call_slot():
    class Callback(_AsyncRecorder):
        def __getattribute__(self, name):
            if name == "__call__":
                raise AssertionError("instance attributes are not special-method dispatch")
            return super().__getattribute__(name)

    assert is_coro_func(Callback()) is True


def test_sync_function_with_an_async_call_attribute_stays_sync():
    def callback():
        return "sync"

    callback.__call__ = _AsyncRecorder().__call__
    assert is_coro_func(callback) is False


def test_non_callable_with_an_async_call_attribute_stays_non_coroutine():
    class Value:
        pass

    value = Value()
    value.__call__ = _AsyncRecorder().__call__
    assert not callable(value)
    assert is_coro_func(value) is False


@pytest.mark.parametrize("wrapping", ["direct", "partial"])
def test_async_callable_class_constructor_stays_sync(wrapping):
    assert is_coro_func(_wrapped(_AsyncRecorder, wrapping)) is False


def _message_manager(kind, callback):
    if kind == "branch":
        from lionagi import Branch

        branch = Branch()
        branch.on_message_added.append(callback)
        return branch.msgs
    return MessageManager(on_message_added=[callback])


@pytest.mark.parametrize("kind", ["manager", "branch"])
@pytest.mark.parametrize("wrapping", ["direct", "partial"])
async def test_message_hook_callable_instance_is_awaited(kind, wrapping, recwarn):
    callback = _UnhashableAsyncRecorder()
    manager = _message_manager(kind, _wrapped(callback, wrapping))

    message = await manager.a_add_message(instruction="hello")

    assert len(callback.seen) == 1
    assert callback.seen[0] is message
    gc.collect()
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


@pytest.mark.parametrize("kind", ["manager", "branch"])
@pytest.mark.parametrize("wrapping", ["direct", "partial"])
def test_sync_message_hook_rejects_callable_instance_before_mutation(kind, wrapping, recwarn):
    callback = _UnhashableAsyncRecorder()
    manager = _message_manager(kind, _wrapped(callback, wrapping))
    before = list(manager.messages.keys())

    with pytest.raises(RuntimeError, match="Use a_add_message"):
        manager.add_message(instruction="hello")

    assert list(manager.messages.keys()) == before
    assert callback.seen == []
    gc.collect()
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_async_callable_hook_error_keeps_identity_and_drains_later_hooks(error_type):
    error = error_type("callback stopped")
    seen = []

    class FailingCallback:
        async def __call__(self, message):
            await asyncio.sleep(0)
            raise error

    manager = MessageManager(on_message_added=[FailingCallback(), seen.append])
    with pytest.raises(error_type) as caught:
        await manager.a_add_message(instruction="hello")

    assert caught.value is error
    assert len(seen) == 1
    assert manager.messages[seen[0].id] is seen[0]


async def test_async_callable_operation_is_registered_and_awaited():
    from lionagi.operations.manager import OperationManager

    callback = _AsyncRecorder()
    manager = OperationManager()
    manager.register("record", callback)
    assert await manager.registry["record"]("value") == "value"
    assert callback.seen == ["value"]


async def test_async_callable_service_hook_returns_the_awaited_value():
    from lionagi.service.hooks._utils import get_handler

    callback = _AsyncRecorder()
    handler = get_handler({"record": callback}, "record")
    assert await handler("value") == "value"
    assert callback.seen == ["value"]


async def test_async_callable_agent_processors_preserve_order():
    from lionagi.agent.factory import _compose_postprocessor, _compose_preprocessor

    order = []

    class Recorder:
        def __init__(self, name):
            self.name = name

        async def __call__(self, value):
            await asyncio.sleep(0)
            order.append((self.name, value))
            return value

    first = Recorder("first")
    second = Recorder("second")
    preprocess = _compose_preprocessor(first, second)
    postprocess = _compose_postprocessor(first, second)

    assert await preprocess("before") == "before"
    assert await postprocess("after") == "after"
    assert order == [
        ("first", "before"),
        ("second", "before"),
        ("second", "after"),
        ("first", "after"),
    ]


async def test_async_callable_tool_and_processors_are_awaited():
    from lionagi.protocols.action.function_calling import FunctionCalling
    from lionagi.protocols.action.tool import Tool
    from lionagi.protocols.generic.event import EventStatus

    calls = []

    class Preprocessor:
        async def __call__(self, arguments):
            calls.append("pre")
            return {"value": arguments["value"] + 1}

    class Target:
        __name__ = "increment"

        async def __call__(self, value):
            calls.append("target")
            return value + 1

    class Postprocessor:
        async def __call__(self, result):
            calls.append("post")
            return result + 1

    tool = Tool(
        func_callable=Target(),
        preprocessor=Preprocessor(),
        postprocessor=Postprocessor(),
        tool_schema={
            "type": "function",
            "function": {
                "name": "increment",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            },
        },
    )
    call = FunctionCalling(func_tool=tool, arguments={"value": 0})
    await call.invoke()

    assert call.status == EventStatus.COMPLETED
    assert call.response == 3
    assert calls == ["pre", "target", "post"]
