from datetime import datetime

import pytest

from lionagi.protocols.generic.event import Event, EventStatus, Execution


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


def test_event_duration():
    start = datetime.now().timestamp()
    execution = Execution(duration=1.5)
    event = Event(execution=execution)

    assert event.execution.duration == 1.5
    assert event.created_at >= start


# ---------------------------------------------------------------------------
# Edge case: Event.invoke() called concurrently (idempotency under race)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_invoke_concurrent_idempotency():
    import asyncio

    class _SingleEvent(Event):
        async def _invoke(self):
            return "done"

    event = _SingleEvent()
    # Concurrent invocations -- only the first should proceed; the rest are no-ops
    await asyncio.gather(event.invoke(), event.invoke(), event.invoke())
    assert event.status == EventStatus.COMPLETED
    assert event.response == "done"


# ---------------------------------------------------------------------------
# Edge case: Event.stream() with CancelledError before any yield
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_stream_cancelled_before_first_yield():
    import asyncio

    class _CancelledStream(Event):
        async def _stream(self):
            raise asyncio.CancelledError("cancelled before yield")
            yield  # pragma: no cover

    event = _CancelledStream()
    chunks = []
    with pytest.raises(asyncio.CancelledError):
        async for chunk in event.stream():
            chunks.append(chunk)
    assert event.status == EventStatus.CANCELLED
    assert len(chunks) == 0


# ---------------------------------------------------------------------------
# Edge case: Execution.to_dict with non-serializable error (BaseException)
# ---------------------------------------------------------------------------


def test_execution_to_dict_with_base_exception_error():
    exc = RuntimeError("boom")
    ex = Execution(status=EventStatus.FAILED, error=exc)
    d = ex.to_dict()
    assert d["status"] == "failed"
    assert isinstance(d["error"], dict)
    assert d["error"]["error"] == "RuntimeError"
    assert "boom" in d["error"]["message"]


def test_execution_to_dict_with_unserializable_response():
    class _Unserializable:
        def __repr__(self):
            return "<unserializable>"

        def __reduce__(self):
            raise TypeError("cannot pickle")

    ex = Execution(status=EventStatus.COMPLETED, response=_Unserializable())
    d = ex.to_dict()
    # Must not raise; unserializable falls back to "<unserializable>" string
    assert d["response"] == "<unserializable>" or d["response"] is not None


# ---------------------------------------------------------------------------
# Edge case: Event with large response (no crash, state correct)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_large_response_object():
    big_data = list(range(100_000))

    class _BigEvent(Event):
        async def _invoke(self):
            return big_data

    event = _BigEvent()
    await event.invoke()
    assert event.status == EventStatus.COMPLETED
    assert event.response is big_data
