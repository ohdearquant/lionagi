from datetime import datetime, timezone

import pytest

from lionagi.ln.types import Unset
from lionagi.protocols.generic import element as element_mod
from lionagi.protocols.generic.event import Event, EventStatus, Execution


def test_event_status_enum():
    assert EventStatus.PENDING == "pending"
    assert EventStatus.PROCESSING == "processing"
    assert EventStatus.COMPLETED == "completed"
    assert EventStatus.FAILED == "failed"


def test_execution_initialization():
    execution = Execution()
    assert execution.status == EventStatus.PENDING
    assert execution.duration is Unset
    assert execution.response is None
    assert execution.error is None


def test_execution_str_representation():
    execution = Execution(
        duration=1.23,
        response="test",
        status=EventStatus.COMPLETED,
        error=None,
    )
    assert "Execution(status=completed" in str(execution)
    assert "duration=1.23" in str(execution)
    assert "response=test" in str(execution)


def test_event_initialization():
    event = Event()
    assert event.status == EventStatus.PENDING
    assert event.response is None
    assert isinstance(event.execution, Execution)


def test_event_properties():
    event = Event()
    event.status = EventStatus.PROCESSING
    event.response = "test response"

    assert event.status == EventStatus.PROCESSING
    assert event.response == "test response"
    assert event.request == {}


def test_event_serialization():
    event = Event()
    event.status = EventStatus.COMPLETED
    event.response = {"result": "success"}

    serialized = event.model_dump()
    assert serialized["execution"]["status"] == "completed"
    assert serialized["execution"]["response"] == {"result": "success"}


@pytest.mark.asyncio
async def test_event_invoke_not_implemented():
    event = Event()
    await event.invoke()  # total: NotImplementedError captured, not raised
    assert event.status == EventStatus.FAILED
    assert isinstance(event.execution.error, NotImplementedError)


def test_event_from_dict_not_implemented():
    with pytest.raises(NotImplementedError):
        Event.from_dict({})


def test_event_with_error():
    execution = Execution(status=EventStatus.FAILED, error="Test error")
    event = Event(execution=execution)

    assert event.status == EventStatus.FAILED
    assert event.execution.error == "Test error"


def test_event_duration(monkeypatch):
    t0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2024, 6, 1, 12, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(element_mod, "now_utc", lambda: t0)
    first = Event(execution=Execution(duration=1.5))
    monkeypatch.setattr(element_mod, "now_utc", lambda: t1)
    second = Event(execution=Execution(duration=2.5))

    assert first.execution.duration == 1.5
    # created_at is stamped fresh at construction and advances between events.
    assert first.created_at == t0.timestamp()
    assert second.created_at == t1.timestamp()
    assert second.created_at > first.created_at
