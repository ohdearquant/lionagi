# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Replay HTTP-success streams through real loopback transport and public consumers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from lionagi.protocols.generic import EventStatus
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel

pytestmark = pytest.mark.asyncio

_REQUESTED = "requested-alias"
_SERVED = "provider-resolved-model"
_CREATED = {"type": "response.created", "response": {"id": "r1", "model": _SERVED}}
_COMPLETED = {"type": "response.completed", "response": {"id": "r1", "status": "completed"}}
_FAILURES = [
    pytest.param(
        {"choices": [{"delta": {}, "finish_reason": "error"}], "error": {"message": "upstream"}},
        id="chat-error",
    ),
    pytest.param({"type": "error", "code": "server_error", "message": "upstream"}, id="flat-error"),
    pytest.param(
        {"type": "response.failed", "response": {"id": "r1", "error": {"message": "upstream"}}},
        id="response-failed-nested-error",
    ),
    pytest.param(
        {"type": "response.failed", "response": {"id": "r1"}}, id="response-failed-no-error"
    ),
]
_ENTRIES = ["imodel", "imodel-limited", "api-call-to-endpoint"]


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


@dataclass
class _Replay:
    url: str
    requests: list[dict] = field(default_factory=list)
    eof: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@pytest_asyncio.fixture
async def replay_server():
    servers = []
    replays = []

    async def start(wire, *, hold_open=False):
        replay = _Replay(url="")
        replays.append(replay)

        async def handler(request):
            payload = await request.json()
            replay.requests.append(payload)
            response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            data = wire(payload) if callable(wire) else wire
            await response.write(data.encode())
            if hold_open:
                await replay.release.wait()
            try:
                await response.write_eof()
            except ConnectionResetError:
                # A close/cancel control deliberately disconnects before EOF.
                pass
            replay.eof.set()
            return response

        app = web.Application()
        app.router.add_post("/{tail:.*}", handler)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        servers.append(server)
        replay.url = str(server.make_url("/")).rstrip("/")
        assert replay.url.startswith("http://127.0.0.1:")
        return replay

    yield start
    for replay in replays:
        replay.release.set()
    for server in servers:
        await server.close()


def _endpoint(replay):
    return Endpoint(
        config=EndpointConfig(
            name="stream_replay",
            provider="openai",
            endpoint="replay",
            base_url=replay.url,
            api_key="dummy-key-replay",
            allow_local_network=True,
            timeout=5,
            max_retries=1,
            kwargs={"model": _REQUESTED},
        )
    )


def _payload(message="hello"):
    return {"model": _REQUESTED, "messages": [{"role": "user", "content": message}], "stream": True}


async def _observe(replay, entry, record_property):
    endpoint = _endpoint(replay)
    model = None
    if entry == "api-call-to-endpoint":
        call = APICalling(endpoint=endpoint, payload=_payload())
        source = call.stream()
    else:
        model = iModel(
            endpoint=endpoint, concurrency_limit=2 if entry == "imodel-limited" else None
        )
        call = await model.create_event(**_payload())
        source = model.stream(api_call=call)
    chunks = []
    error = None
    try:
        async for chunk in source:
            chunks.append(chunk)
    except ValueError as exc:
        error = str(exc)
    finally:
        if model:
            await model.close()
    await asyncio.wait_for(replay.eof.wait(), timeout=2)
    assert len(replay.requests) == 1, "replay must reach the real local HTTP server exactly once"
    assert replay.requests[0]["model"] == _REQUESTED
    record_property("entry", entry)
    record_property("execution_status", call.status.value)
    record_property("served_model", str(call.metadata.get("served_model")))
    record_property("public_error", str(error))
    record_property("chunks", json.dumps([asdict(c) for c in chunks]))
    return call, chunks, model


@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("failure", _FAILURES)
async def test_call_error_then_normal_eof_is_failed(replay_server, entry, failure, record_property):
    replay = await replay_server(_sse(_CREATED, failure))
    call, _chunks, _model = await _observe(replay, entry, record_property)
    assert call.status == EventStatus.FAILED, (
        "a provider call error followed by EOF is not completion"
    )


async def test_endpoint_already_marks_a_flat_error_chunk(replay_server, record_property):
    replay = await replay_server(
        _sse({"type": "error", "code": "server_error", "message": "upstream"})
    )
    chunks = [chunk async for chunk in _endpoint(replay).stream(_payload())]
    await asyncio.wait_for(replay.eof.wait(), timeout=2)
    assert len(replay.requests) == 1
    record_property("chunks", json.dumps([asdict(c) for c in chunks]))
    assert any(chunk.type == "error" and chunk.is_error for chunk in chunks)


@pytest.mark.parametrize("entry", _ENTRIES)
async def test_sse_event_name_only_error_is_a_failed_call(replay_server, entry, record_property):
    replay = await replay_server(
        'event: error\nid: err-1\nretry: 1000\ndata: {"message":"upstream"}\n\n'
    )
    call, _chunks, _model = await _observe(replay, entry, record_property)
    assert call.status == EventStatus.FAILED, "the SSE event name must not lose the call error"


async def test_endpoint_recognizes_an_error_named_only_by_sse(replay_server, record_property):
    replay = await replay_server('event: error\ndata: {"message":"upstream"}\n\n')
    chunks = []
    error = None
    try:
        async for chunk in _endpoint(replay).stream(_payload()):
            chunks.append(chunk)
    except Exception as exc:
        # Recognition may use a normalized chunk or surface the provider cause.
        # A transport/setup/programming error is not evidence of recognition.
        if "upstream" not in str(exc):
            raise
        error = exc
    await asyncio.wait_for(replay.eof.wait(), timeout=2)
    assert len(replay.requests) == 1
    record_property("chunks", json.dumps([asdict(c) for c in chunks]))
    record_property("public_error", str(error))
    assert error is not None or any(chunk.is_error for chunk in chunks), (
        "an event:error discriminator must reach conversion"
    )


@pytest.mark.parametrize("entry", ["imodel", "imodel-limited"])
@pytest.mark.parametrize("nested_error", [False, True], ids=["no-error", "nested-error"])
async def test_item_failure_does_not_fail_successful_call(
    replay_server, entry, nested_error, record_property
):
    item = {"type": "response.mcp_call.failed", "item_id": "item-1"}
    if nested_error:
        item["error"] = {"message": "tool unavailable"}
    replay = await replay_server(_sse(_CREATED, item, _COMPLETED))
    call, _chunks, _model = await _observe(replay, entry, record_property)
    assert call.status == EventStatus.COMPLETED
    assert call.metadata.get("served_model") == _SERVED


@pytest.mark.parametrize("entry", ["imodel", "imodel-limited"])
async def test_model_named_only_on_response_completed_is_attributed(
    replay_server, entry, record_property
):
    terminal = {
        "type": "response.completed",
        "response": {"id": "r1", "status": "completed", "model": _SERVED},
    }
    replay = await replay_server(_sse(terminal))
    call, chunks, _model = await _observe(replay, entry, record_property)
    assert call.status == EventStatus.COMPLETED
    assert any(c.type == "result" for c in chunks)
    assert call.payload["model"] == _REQUESTED
    assert call.metadata.get("served_model") == _SERVED, (
        "terminal HTTP model evidence is not a request echo"
    )


@pytest.mark.parametrize("entry", ["imodel", "imodel-limited"])
async def test_later_model_cannot_erase_a_call_failure(replay_server, entry, record_property):
    failure = {"type": "response.failed", "response": {"id": "r1"}}
    replay = await replay_server(
        _sse(_CREATED, failure, {"type": "response.created", "response": {"model": "later-model"}})
    )
    call, _chunks, model = await _observe(replay, entry, record_property)
    assert "served_model" not in call.metadata, (
        "a later model must not restore attribution after a call failure"
    )
    assert "served_model" not in model.provider_metadata
    assert call.status == EventStatus.FAILED


@pytest.mark.parametrize("entry", ["imodel", "imodel-limited"])
@pytest.mark.parametrize("reported", [_SERVED, None], ids=["returned-model", "no-returned-model"])
@pytest.mark.parametrize("done", [False, True], ids=["eof-without-terminal", "done-marker"])
async def test_requested_alias_and_reported_model_stay_distinct(
    replay_server, entry, reported, done, record_property
):
    event = {"choices": [{"delta": {"content": "hello"}}]}
    if reported is not None:
        event["model"] = reported
    replay = await replay_server(_sse(event) + ("data: [DONE]\n\n" if done else ""))
    call, _chunks, _model = await _observe(replay, entry, record_property)
    assert call.payload["model"] == _REQUESTED
    assert call.metadata.get("served_model") == reported
    assert call.metadata.get("served_model") != _REQUESTED


@pytest.mark.parametrize("concurrency_limit", [None, 2])
async def test_concurrent_calls_keep_their_own_attribution(
    replay_server, concurrency_limit, record_property
):
    def wire(payload):
        name = payload["messages"][0]["content"]
        return _sse({"choices": [{"delta": {"content": name}}], "model": f"served-{name}"})

    replay = await replay_server(wire)
    model = iModel(endpoint=_endpoint(replay), concurrency_limit=concurrency_limit)
    calls = [await model.create_event(**_payload(name)) for name in ["one", "two"]]

    async def consume(call):
        return [chunk async for chunk in model.stream(api_call=call)]

    try:
        await asyncio.gather(*(consume(call) for call in calls))
    finally:
        await model.close()
    assert len(replay.requests) == 2
    record_property("call_models", json.dumps([c.metadata.get("served_model") for c in calls]))
    assert [c.status for c in calls] == [EventStatus.COMPLETED, EventStatus.COMPLETED]
    assert [c.payload["model"] for c in calls] == [_REQUESTED, _REQUESTED]
    assert [c.metadata.get("served_model") for c in calls] == ["served-one", "served-two"]


@pytest.mark.parametrize("concurrency_limit", [None, 2])
@pytest.mark.parametrize("how", ["close", "cancel"])
async def test_interrupted_stream_keeps_attribution_without_completing(
    replay_server, concurrency_limit, how, record_property
):
    replay = await replay_server(_sse(_CREATED), hold_open=True)
    model = iModel(endpoint=_endpoint(replay), concurrency_limit=concurrency_limit)
    call = await model.create_event(**_payload())
    source = model.stream(api_call=call)
    try:
        first = await anext(source)
        assert first.metadata["raw"] == _CREATED
        if how == "close":
            await source.aclose()
        else:
            task = asyncio.create_task(anext(source))
            # Let the consumer enter the next HTTP read before cancellation.
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        record_property("execution_status", call.status.value)
        record_property("served_model", str(call.metadata.get("served_model")))
        assert call.status != EventStatus.COMPLETED
        assert call.metadata.get("served_model") == _SERVED
    finally:
        replay.release.set()
        await source.aclose()
        await model.close()
