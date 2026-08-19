"""Edge case tests for Processor/Executor from ReAct coverage."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from lionagi.protocols.generic.event import Event, EventStatus
from lionagi.protocols.generic.processor import Executor, Processor


class _OkEvent(Event):
    async def _invoke(self):
        return "ok"


class _StreamEvent(Event):
    streaming: bool = True

    async def _stream(self):
        for chunk in ["a", "b", "c"]:
            yield chunk


class _FailEvent(Event):
    async def _invoke(self):
        raise ValueError("intentional failure")


class _Proc(Processor):
    event_type: ClassVar[type[Event]] = _OkEvent


def _proc(**kw) -> _Proc:
    defaults = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
    defaults.update(kw)
    return _Proc(**defaults)


class _MyExecutor(Executor):
    processor_type: ClassVar[type[Processor]] = _Proc


class TestProcessorStreamingPath:
    async def test_process_streaming_event_completes(self):
        class _StreamProc(Processor):
            event_type: ClassVar[type[Event]] = _StreamEvent

        proc = _StreamProc(queue_capacity=5, capacity_refresh_time=0.01, concurrency_limit=1)
        event = _StreamEvent()
        await proc.enqueue(event)
        await asyncio.wait_for(proc.process(), timeout=3.0)
        assert event.status == EventStatus.COMPLETED

    async def test_process_streaming_with_semaphore(self):
        class _StreamProc2(Processor):
            event_type: ClassVar[type[Event]] = _StreamEvent

        proc = _StreamProc2(queue_capacity=5, capacity_refresh_time=0.01, concurrency_limit=2)
        events = [_StreamEvent() for _ in range(2)]
        for e in events:
            await proc.enqueue(e)
        await asyncio.wait_for(proc.process(), timeout=3.0)
        for e in events:
            assert e.status == EventStatus.COMPLETED

    async def test_process_streaming_no_semaphore(self):
        class _StreamProc3(Processor):
            event_type: ClassVar[type[Event]] = _StreamEvent

        proc = _StreamProc3(queue_capacity=5, capacity_refresh_time=0.01, concurrency_limit=0)
        event = _StreamEvent()
        await proc.enqueue(event)
        await asyncio.wait_for(proc.process(), timeout=3.0)
        assert event.status == EventStatus.COMPLETED

    async def test_process_non_streaming_no_semaphore(self):
        proc = _Proc(queue_capacity=5, capacity_refresh_time=0.01, concurrency_limit=0)
        event = _OkEvent()
        await proc.enqueue(event)
        await asyncio.wait_for(proc.process(), timeout=3.0)
        assert event.status == EventStatus.COMPLETED


class TestExecutorInit:
    def test_default_init(self):
        ex = _MyExecutor()
        assert ex.processor is None
        assert ex.processor_config == {}
        assert len(ex.pending) == 0
        assert ex.pile is not None

    def test_with_config(self):
        cfg = dict(queue_capacity=5, capacity_refresh_time=0.05, concurrency_limit=1)
        ex = _MyExecutor(processor_config=cfg)
        assert ex.processor_config == cfg

    def test_strict_event_type_false(self):
        ex = _MyExecutor(strict_event_type=False)
        assert ex.strict_event_type is False

    def test_event_type_property(self):
        ex = _MyExecutor()
        assert ex.event_type is _OkEvent


class TestExecutorAppend:
    async def test_append_adds_to_pile_and_pending(self):
        ex = _MyExecutor()
        event = _OkEvent()
        await ex.append(event)
        assert event in ex.pile
        assert len(ex.pending) == 1

    async def test_append_multiple_events(self):
        ex = _MyExecutor()
        events = [_OkEvent() for _ in range(3)]
        for e in events:
            await ex.append(e)
        assert len(ex.pile) == 3
        assert len(ex.pending) == 3


class TestExecutorStartStop:
    async def test_start_creates_processor(self):
        cfg = dict(queue_capacity=5, capacity_refresh_time=0.05, concurrency_limit=1)
        ex = _MyExecutor(processor_config=cfg)
        assert ex.processor is None
        await ex.start()
        assert ex.processor is not None
        assert isinstance(ex.processor, _Proc)

    async def test_start_twice_does_not_create_new_processor(self):
        cfg = dict(queue_capacity=5, capacity_refresh_time=0.05, concurrency_limit=1)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()
        first = ex.processor
        await ex.start()
        assert ex.processor is first

    async def test_stop_stops_processor(self):
        cfg = dict(queue_capacity=5, capacity_refresh_time=0.05, concurrency_limit=1)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()
        await ex.stop()
        assert ex.processor.is_stopped()

    async def test_stop_without_processor_is_noop(self):
        ex = _MyExecutor()
        assert ex.processor is None
        await ex.stop()


class TestExecutorForward:
    async def test_forward_processes_pending_events(self):
        cfg = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()

        events = [_OkEvent() for _ in range(3)]
        for e in events:
            await ex.append(e)

        await asyncio.wait_for(ex.forward(), timeout=3.0)

        for e in events:
            assert e.status == EventStatus.COMPLETED

    async def test_forward_drains_pending(self):
        cfg = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()

        event = _OkEvent()
        await ex.append(event)
        await asyncio.wait_for(ex.forward(), timeout=3.0)
        assert len(ex.pending) == 0


class TestExecutorEventProperties:
    async def test_completed_events(self):
        cfg = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()

        event = _OkEvent()
        await ex.append(event)
        await asyncio.wait_for(ex.forward(), timeout=3.0)

        completed = ex.completed_events
        assert len(completed) == 1

    async def test_failed_events(self):
        cfg = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)

        class _FailProc(Processor):
            event_type: ClassVar[type[Event]] = _FailEvent

        class _FailExecutor(Executor):
            processor_type: ClassVar[type[Processor]] = _FailProc

        ex = _FailExecutor(processor_config=cfg)
        await ex.start()

        event = _FailEvent()
        await ex.append(event)
        await asyncio.wait_for(ex.forward(), timeout=3.0)

        failed = ex.failed_events
        assert len(failed) == 1

    async def test_pending_events_before_process(self):
        ex = _MyExecutor()
        event = _OkEvent()
        await ex.append(event)
        pending = ex.pending_events
        assert len(pending) == 1

    async def test_cancelled_events_property(self):
        ex = _MyExecutor()
        event = _OkEvent()
        await ex.append(event)
        event.status = EventStatus.CANCELLED
        cancelled = ex.cancelled_events
        assert len(cancelled) == 1

    async def test_skipped_events_property(self):
        ex = _MyExecutor()
        event = _OkEvent()
        await ex.append(event)
        event.status = EventStatus.SKIPPED
        skipped = ex.skipped_events
        assert len(skipped) == 1


class TestExecutorStatusCounts:
    async def test_status_counts_after_complete(self):
        cfg = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()

        for _ in range(2):
            await ex.append(_OkEvent())
        await asyncio.wait_for(ex.forward(), timeout=3.0)

        counts = ex.status_counts()
        assert counts.get("completed", 0) == 2

    def test_status_counts_empty(self):
        ex = _MyExecutor()
        assert ex.status_counts() == {}


class TestExecutorCleanup:
    async def test_cleanup_completed_removes_events(self):
        cfg = dict(queue_capacity=10, capacity_refresh_time=0.01, concurrency_limit=2)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()

        events = [_OkEvent() for _ in range(3)]
        for e in events:
            await ex.append(e)
        await asyncio.wait_for(ex.forward(), timeout=3.0)

        removed = ex.cleanup_completed()
        assert removed == 3
        assert len(ex.pile) == 0

    def test_cleanup_completed_empty_pile(self):
        ex = _MyExecutor()
        assert ex.cleanup_completed() == 0


class TestExecutorInspectState:
    def test_inspect_state_no_processor(self):
        ex = _MyExecutor()
        state = ex.inspect_state()
        assert state["total_events"] == 0
        assert state["processor_running"] is False
        assert state["processor_stopped"] is True

    async def test_inspect_state_with_processor(self):
        cfg = dict(queue_capacity=5, capacity_refresh_time=0.05, concurrency_limit=1)
        ex = _MyExecutor(processor_config=cfg)
        await ex.start()
        state = ex.inspect_state()
        assert state["processor_running"] is False
        assert state["processor_stopped"] is False


class TestExecutorContains:
    async def test_contains_appended_event(self):
        ex = _MyExecutor()
        event = _OkEvent()
        await ex.append(event)
        assert event in ex

    async def test_not_contains_unrelated_event(self):
        ex = _MyExecutor()
        event = _OkEvent()
        assert event not in ex


class TestHandleFieldModels:
    def test_no_intermediate_returns_empty(self):
        from lionagi.operations.ReAct.ReAct import handle_field_models

        result = handle_field_models(None, None)
        assert result == []

    def test_with_field_models_only(self):
        from lionagi.models.field_model import FieldModel
        from lionagi.operations.ReAct.ReAct import handle_field_models

        fm = FieldModel(name="test_field")
        result = handle_field_models([fm], None)
        assert len(result) == 1
        assert result[0] is fm

    def test_intermediate_options_single_model(self):
        from pydantic import BaseModel

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class MyOutput(BaseModel):
            value: str = ""

        fms = handle_field_models(None, MyOutput)
        assert len(fms) == 1
        assert fms[0].name == "intermediate_response_options"

    def test_intermediate_options_list_of_models(self):
        from pydantic import BaseModel

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class ModelA(BaseModel):
            a: str = ""

        class ModelB(BaseModel):
            b: int = 0

        fms = handle_field_models(None, [ModelA, ModelB])
        assert len(fms) == 1
        assert fms[0].name == "intermediate_response_options"

    def test_intermediate_options_listable(self):
        from pydantic import BaseModel

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class MyOut(BaseModel):
            v: str = ""

        fms = handle_field_models(None, MyOut, intermediate_listable=True)
        assert len(fms) == 1

    def test_intermediate_options_nullable(self):
        from pydantic import BaseModel

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class MyOut(BaseModel):
            v: str = ""

        fms = handle_field_models(None, MyOut, intermediate_nullable=True)
        assert len(fms) == 1

    def test_intermediate_options_with_existing_field_models(self):
        from pydantic import BaseModel

        from lionagi.models.field_model import FieldModel
        from lionagi.operations.ReAct.ReAct import handle_field_models

        class Extra(BaseModel):
            x: int = 0

        fm = FieldModel(name="existing")
        fms = handle_field_models([fm], Extra)
        assert len(fms) == 2

    def test_generated_intermediate_field_is_a_neutral_spec(self):
        from pydantic import BaseModel

        from lionagi.ln.types import Spec
        from lionagi.operations.ReAct.ReAct import handle_field_models

        class Intermediate(BaseModel):
            value: str

        generated = handle_field_models(None, Intermediate)[0]

        assert isinstance(generated, Spec)
        assert generated.name == "intermediate_response_options"

    def test_intermediate_generation_does_not_mutate_the_callers_list(self):
        from pydantic import BaseModel

        from lionagi.models.field_model import FieldModel
        from lionagi.operations.ReAct.ReAct import handle_field_models

        class Intermediate(BaseModel):
            value: str

        existing = FieldModel(str, name="existing")
        caller_fields = [existing]

        result = handle_field_models(caller_fields, Intermediate)

        assert caller_fields == [existing]
        assert result is not caller_fields
        assert result[0] is existing

    def test_intermediate_inner_model_preserves_declaration_order_and_requiredness(self):
        from pydantic import BaseModel

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class FirstIntermediate(BaseModel):
            first: str

        class SecondIntermediate(BaseModel):
            second: int

        generated = handle_field_models(
            None,
            [FirstIntermediate, SecondIntermediate],
        )[0]
        inner_model = generated.base_type

        assert tuple(inner_model.model_fields) == (
            "firstintermediate",
            "secondintermediate",
        )
        assert all(field.is_required() for field in inner_model.model_fields.values())
        assert inner_model.model_json_schema()["required"] == [
            "firstintermediate",
            "secondintermediate",
        ]

    def test_intermediate_inner_model_fields_are_required_but_nullable(self):
        from pydantic import BaseModel, ValidationError

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class FirstIntermediate(BaseModel):
            first: str

        class SecondIntermediate(BaseModel):
            second: int

        generated = handle_field_models(
            None,
            [FirstIntermediate, SecondIntermediate],
        )[0]
        inner_model = generated.base_type

        instance = inner_model.model_validate(
            {"firstintermediate": None, "secondintermediate": None}
        )
        assert instance.firstintermediate is None
        assert instance.secondintermediate is None
        with pytest.raises(ValidationError):
            inner_model.model_validate({"firstintermediate": None})

    def test_intermediate_inner_model_schema_has_no_declaration_name_extension(self):
        from pydantic import BaseModel

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class FirstIntermediate(BaseModel):
            first: str

        class SecondIntermediate(BaseModel):
            second: int

        generated = handle_field_models(
            None,
            [FirstIntermediate, SecondIntermediate],
        )[0]
        properties = generated.base_type.model_json_schema()["properties"]

        assert properties == {
            "firstintermediate": {
                "anyOf": [
                    {"$ref": "#/$defs/FirstIntermediate"},
                    {"type": "null"},
                ]
            },
            "secondintermediate": {
                "anyOf": [
                    {"$ref": "#/$defs/SecondIntermediate"},
                    {"type": "null"},
                ]
            },
        }

    def test_duplicate_normalized_intermediate_names_fail_deterministically(self):
        from pydantic import BaseModel

        from lionagi.operations.ReAct.ReAct import handle_field_models

        class Choice(BaseModel):
            first: str

        class CHOICE(BaseModel):
            second: int

        messages: list[str] = []
        for _ in range(2):
            with pytest.raises(ValueError) as exc_info:
                handle_field_models(None, [Choice, CHOICE])
            messages.append(str(exc_info.value))

        assert messages[0] == messages[1]
        assert "choice" in messages[0].lower()
