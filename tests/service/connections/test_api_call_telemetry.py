"""Real APICalling construction/core paths preserve decision and run ownership."""

import asyncio
import json
import math

import pytest

from lionagi._errors import InputTokenCapError
from lionagi.cli._api_usage import APIUsageCollector
from lionagi.protocols.generic.processor import Processor
from lionagi.service._api_telemetry import (
    activate_request,
    collect_api_turn,
    record_selected_attempt,
)
from lionagi.service.connections import api_calling as api_module
from lionagi.service.connections.api_calling import APICalling, estimate_payload_input_tokens
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel


@pytest.fixture
def endpoint():
    return Endpoint(
        config=EndpointConfig(
            name="openai_chat",
            endpoint="chat",
            provider="openai",
            base_url="https://invalid.example/v1",
            endpoint_params=["chat", "completions"],
            openai_compatible=True,
        )
    )


def _collector(label="a"):
    return APIUsageCollector(run_id=f"run-{label}", branch_id=f"branch-{label}")


def _payload():
    return {
        "model": "requested-model",
        "system": "é🦁",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"name": "read", "description": "read safely"}],
        "response_format": {"type": "json_object"},
    }


def _chars(payload):
    keys = ("system", "instructions", "messages", "input", "prompt", "tools", "response_format")
    parts = [payload[key] for key in keys if payload.get(key) is not None]
    return len(json.dumps(parts, ensure_ascii=False, default=str)) if parts else 0


def test_actual_scaled_decision_and_unicode_chars_are_immutable(endpoint):
    payload = _payload()
    counted = estimate_payload_input_tokens(payload)
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(
            endpoint=endpoint, payload=payload, max_input_tokens=10000, input_token_scale=2.5
        )
    call.payload["messages"].clear()
    call.payload["model"] = "later-model"
    call.input_token_scale = 9
    row = collector.snapshot()["requests"][0]
    assert row["provider"] == "openai"
    assert row["model"] == "requested-model"
    assert row["model_semantics"] == "requested"
    assert row["prompt_chars"] == _chars(_payload())
    assert row["input_decision"] == dict(
        kind="checked", counted=counted, scale=2.5, estimate=math.ceil(counted * 2.5), cap=10000
    )
    assert row["api_call_id"] == str(call.id)
    assert row["dispatch_started"] is False


def test_cap_measurement_serializes_stateful_payload_only_once(endpoint):
    class StatefulText:
        calls = 0

        def __str__(self):
            self.calls += 1
            return "first" if self.calls == 1 else "different"

    text = StatefulText()
    collector = _collector()
    with collect_api_turn(collector, 1):
        APICalling(endpoint=endpoint, payload={"model": "m", "prompt": text}, max_input_tokens=100)
    assert text.calls == 1
    assert collector.snapshot()["requests"][0]["prompt_chars"] == len('["first"]')


def test_uncapped_advisory_chars_never_count_or_calibrate(endpoint, monkeypatch):
    def forbidden_count(self):
        raise AssertionError("uncapped telemetry must not tokenize")

    monkeypatch.setattr(APICalling, "input_token_estimate", property(forbidden_count))
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload=_payload())
    assert call.counted_input_tokens is None
    row = collector.snapshot()["requests"][0]
    assert row["prompt_chars"] == _chars(_payload())
    assert row["input_decision"] == dict(
        kind="uncapped", counted=None, scale=None, estimate=None, cap=None
    )
    model = iModel(
        provider="anthropic", model="claude-haiku-5-5", api_key="test-not-a-key", max_tokens=64
    )
    model._calibrate_input_scale(call, {"input_tokens": 100000})
    assert model.input_token_scale == 1.0


def test_nontelemetry_uncapped_construction_does_no_new_serialization(endpoint, monkeypatch):
    def forbidden(*args):
        raise AssertionError("no optional work without telemetry")

    monkeypatch.setattr(api_module, "_input_payload_text", forbidden)
    call = APICalling(endpoint=endpoint, payload=_payload())
    assert call.counted_input_tokens is None


def test_overridden_estimate_property_keeps_its_existing_decision(endpoint, monkeypatch):
    monkeypatch.setattr(APICalling, "input_token_estimate", property(lambda self: 7))
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(
            endpoint=endpoint, payload=_payload(), max_input_tokens=20, input_token_scale=2
        )
    assert call.counted_input_tokens == 7
    assert collector.snapshot()["requests"][0]["input_decision"]["estimate"] == 14


def test_refused_constructor_records_once_and_sticky_rethrow_adds_no_request():
    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        max_input_tokens=1,
    )
    collector = _collector()
    with collect_api_turn(collector, 1):
        with pytest.raises(InputTokenCapError) as first:
            model.create_api_calling(messages=[{"role": "user", "content": "many words " * 20}])
        with pytest.raises(InputTokenCapError) as second:
            model.create_api_calling(messages=[{"role": "user", "content": "different"}])
    assert first.value is second.value
    (row,) = collector.snapshot()["requests"]
    assert row["api_call_id"] is None
    assert row["input_decision"] == {"kind": "refused", **first.value.details}
    assert row["dispatch_started"] is False
    assert row["selected_attempts"] == []


def test_shared_estimator_probes_are_not_requests():
    collector = _collector()
    with collect_api_turn(collector, 1):
        for _ in range(3):
            assert estimate_payload_input_tokens(_payload()) > 0
    assert collector.snapshot()["requests"] == []


@pytest.mark.asyncio
async def test_queue_driver_never_owns_another_constructors_call(endpoint, monkeypatch):
    async def respond(**kwargs):
        record_selected_attempt(
            attempt_id=kwargs["request"]["model"], provider="openai", account_label="primary"
        )
        return {"model": "served-model"}

    monkeypatch.setattr(endpoint, "call", respond)
    a, b = _collector("a"), _collector("b")
    with collect_api_turn(b, 1):
        owned_b = APICalling(endpoint=endpoint, payload={"model": "b-attempt"})
    unowned = APICalling(endpoint=endpoint, payload={"model": "unowned-attempt"})
    processor = Processor(queue_capacity=2, capacity_refresh_time=0.01, concurrency_limit=2)
    await processor.enqueue(owned_b)
    await processor.enqueue(unowned)
    with collect_api_turn(a, 1):
        APICalling(endpoint=endpoint, payload={"model": "a-prepared"})
        await processor.process()
    assert a.snapshot()["requests"][0]["dispatch_started"] is False
    assert a.snapshot()["account_labels"] == []
    (row,) = b.snapshot()["requests"]
    assert row["dispatch_started"] is True
    assert row["model"] == "b-attempt"
    assert [x["attempt_id"] for x in row["selected_attempts"]] == ["b-attempt"]


@pytest.mark.asyncio
async def test_terminal_reuse_no_dispatch_and_fresh_event_has_new_decision(endpoint, monkeypatch):
    sends = []

    async def respond(**kwargs):
        sends.append(kwargs)
        return {"model": "served-model"}

    monkeypatch.setattr(endpoint, "call", respond)
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload=_payload())
        await call.invoke()
        await call.invoke()
        assert [chunk async for chunk in call.stream()] == []
        fresh = call.as_fresh_event()
        await fresh.invoke()
    rows = collector.snapshot()["requests"]
    assert len(sends) == len(rows) == 2
    assert rows[0]["api_call_id"] != rows[1]["api_call_id"]
    assert all(row["dispatch_started"] for row in rows)
    assert all(row["model"] == "requested-model" for row in rows)


@pytest.mark.asyncio
async def test_stream_frame_resets_between_chunks_and_applies_during_close(endpoint, monkeypatch):
    async def stream(**kwargs):
        record_selected_attempt(
            attempt_id="inside-first", provider="openai", account_label="primary"
        )
        try:
            yield "first"
            record_selected_attempt(
                attempt_id="inside-second", provider="openai", account_label="primary"
            )
            yield "second"
        finally:
            record_selected_attempt(
                attempt_id="inside-close", provider="openai", account_label="primary"
            )

    monkeypatch.setattr(endpoint, "stream", stream)
    a, b = _collector("a"), _collector("b")
    with collect_api_turn(a, 1):
        call = APICalling(endpoint=endpoint, payload=_payload())
    with collect_api_turn(b, 1):
        other = APICalling(endpoint=endpoint, payload={"model": "b"})
    with activate_request(other._telemetry_frame):
        source = call._core_stream()
        assert await source.__anext__() == "first"
        record_selected_attempt(
            attempt_id="consumer-b", provider="openai", account_label="secondary"
        )
        await source.aclose()
    a_ids = [x["attempt_id"] for x in a.snapshot()["requests"][0]["selected_attempts"]]
    b_ids = [x["attempt_id"] for x in b.snapshot()["requests"][0]["selected_attempts"]]
    assert a_ids == ["inside-first", "inside-close"]
    assert b_ids == ["consumer-b"]


@pytest.mark.asyncio
async def test_closed_owner_and_cancelled_endpoint_do_not_borrow_later_run(endpoint, monkeypatch):
    cancellation = asyncio.CancelledError("original cancellation")

    async def respond(**kwargs):
        record_selected_attempt(attempt_id="late", provider="openai", account_label="primary")
        raise cancellation

    monkeypatch.setattr(endpoint, "call", respond)
    a, b = _collector("a"), _collector("b")
    with collect_api_turn(a, 1):
        call = APICalling(endpoint=endpoint, payload=_payload())
    sealed = a.close()
    with collect_api_turn(b, 1):
        with pytest.raises(asyncio.CancelledError) as caught:
            await call.invoke()
    assert caught.value is cancellation
    assert a.snapshot() == sealed
    assert b.snapshot()["requests"] == []


@pytest.mark.asyncio
async def test_cancellation_during_stream_close_reaches_consumer(endpoint, monkeypatch):
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class Source:
        yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.yielded:
                raise StopAsyncIteration
            self.yielded = True
            return "first"

        async def aclose(self):
            close_started.set()
            await release_close.wait()

    iterator = Source()
    monkeypatch.setattr(endpoint, "stream", lambda **kwargs: iterator)
    call = APICalling(endpoint=endpoint, payload=_payload())
    stream = call._core_stream()
    after_close = False
    cancel_seen = False

    async def consume():
        nonlocal after_close, cancel_seen
        try:
            assert await stream.__anext__() == "first"
            await stream.aclose()
            after_close = True
        except asyncio.CancelledError:
            cancel_seen = True
            raise

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(close_started.wait(), timeout=5)
        consumer.cancel()
        outcomes = await asyncio.wait_for(
            asyncio.gather(consumer, return_exceptions=True), timeout=5
        )
        assert cancel_seen is True
        assert after_close is False
        assert isinstance(outcomes[0], asyncio.CancelledError)
    finally:
        release_close.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.wait_for(asyncio.gather(consumer, return_exceptions=True), timeout=5)
        await asyncio.wait_for(stream.aclose(), timeout=5)
