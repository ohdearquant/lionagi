# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Response cache counts belong to the request that observed them."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._api_usage import APIUsageCollector
from lionagi.protocols.types import EventStatus
from lionagi.service._api_telemetry import collect_api_turn
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from tests.service.hooks.test_hooked_event import _fake_hook


def _endpoint(provider, path=None):
    path = path or ("messages" if provider == "anthropic" else "chat/completions")
    return Endpoint(config=EndpointConfig(name="test", endpoint=path, provider=provider))


def _collector():
    return APIUsageCollector(run_id="run", branch_id="branch")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "usage", "expected"),
    [
        (
            "openai",
            {"prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 12}},
            (0, 12),
        ),
        ("openai", {"input_tokens_details": {"cached_tokens": 9}}, (9, None)),
        ("anthropic", {"cache_read_input_tokens": 2, "cache_creation_input_tokens": 3}, (2, 3)),
        (
            "openai",
            {"prompt_tokens_details": {"cached_tokens": True, "cache_write_tokens": -1}},
            (None, None),
        ),
        (
            "anthropic",
            {"cache_read_input_tokens": "7", "cache_creation_input_tokens": 0},
            (None, 0),
        ),
        (
            "openai",
            {"prompt_tokens_details": {"cached_tokens": 1.0, "cache_write_tokens": None}},
            (None, None),
        ),
        ("other", {"cache_read_input_tokens": 1, "cache_creation_input_tokens": 2}, (None, None)),
        ("openai", {}, (None, None)),
    ],
)
async def test_successful_response_records_each_valid_count(monkeypatch, provider, usage, expected):
    path = "responses" if "input_tokens_details" in usage else None
    endpoint = _endpoint(provider, path)
    response = {"usage": usage}
    monkeypatch.setattr(endpoint, "_call", AsyncMock(return_value=response))
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload={"model": "test"})
    await call.invoke()
    assert call.status is EventStatus.COMPLETED
    assert call.response == response
    row = collector.snapshot()["requests"][0]
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["embeddings", "responses"])
async def test_other_openai_shapes_do_not_claim_chat_usage(monkeypatch, path):
    endpoint = _endpoint("openai", path)
    monkeypatch.setattr(
        endpoint,
        "_call",
        AsyncMock(return_value={"usage": {"prompt_tokens_details": {"cached_tokens": 7}}}),
    )
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload={"model": "test"})
    await call.invoke()
    row = collector.snapshot()["requests"][0]
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (None, None)


@pytest.mark.asyncio
async def test_counts_outside_signed_64_bit_range_stay_serializable(monkeypatch, tmp_path):
    from lionagi.cli._runs import RunDir

    endpoint = _endpoint("openai")
    responses = iter([2**63 - 1, 2**63, 10**5000])

    async def call(*args, **kwargs):
        return {"usage": {"prompt_tokens_details": {"cached_tokens": next(responses)}}}

    monkeypatch.setattr(endpoint, "_call", call)
    collector = _collector()
    with collect_api_turn(collector, 1):
        calls = [APICalling(endpoint=endpoint, payload={"model": "test"}) for _ in range(3)]
    for call_event in calls:
        await call_event.invoke()
    manifest = {"api_usage": collector.snapshot()}
    assert [r["cache_read_tokens"] for r in manifest["api_usage"]["requests"]] == [
        2**63 - 1,
        None,
        None,
    ]
    run = RunDir(run_id="run", state_root=tmp_path / "run", artifact_root=tmp_path / "artifacts")
    run.write_manifest(manifest)
    assert run.read_manifest()["api_usage"] == manifest["api_usage"]


@pytest.mark.asyncio
async def test_post_hook_failure_and_cancel_leave_counts_unknown(monkeypatch):
    endpoint = _endpoint("openai")
    monkeypatch.setattr(
        endpoint,
        "_call",
        AsyncMock(return_value={"usage": {"prompt_tokens_details": {"cached_tokens": 5}}}),
    )
    monkeypatch.setattr("lionagi.service.hooks.hooked_event.global_hook_logger.alog", AsyncMock())
    collector = _collector()
    with collect_api_turn(collector, 1):
        failed = APICalling(endpoint=endpoint, payload={"model": "test"})
        cancelled = APICalling(endpoint=endpoint, payload={"model": "test"})
    failed._post_invoke_hook_event = _fake_hook(EventStatus.FAILED)
    cancelled._post_invoke_hook_event = _fake_hook(
        should_exit=True, exit_cause=asyncio.CancelledError()
    )
    await failed.invoke()
    with pytest.raises(asyncio.CancelledError):
        await cancelled.invoke()
    assert failed.status is EventStatus.FAILED
    assert cancelled.status is EventStatus.CANCELLED
    assert all(row["cache_read_tokens"] is None for row in collector.snapshot()["requests"])


@pytest.mark.asyncio
async def test_response_updates_its_own_frame_and_not_closed_or_unowned(monkeypatch):
    endpoint = _endpoint("openai")

    async def call(request, headers, **kwargs):
        await asyncio.sleep(0)
        return {"usage": {"prompt_tokens_details": {"cached_tokens": request["number"]}}}

    monkeypatch.setattr(endpoint, "_call", call)
    first, second, closed = _collector(), _collector(), _collector()
    with collect_api_turn(first, 1):
        a = APICalling(endpoint=endpoint, payload={"number": 1})
    with collect_api_turn(second, 2):
        b = APICalling(endpoint=endpoint, payload={"number": 2})
    with collect_api_turn(closed, 1):
        c = APICalling(endpoint=endpoint, payload={"number": 3})
    unowned = APICalling(endpoint=endpoint, payload={"number": 4})
    closed.close()
    await asyncio.gather(b.invoke(), unowned.invoke(), c.invoke(), a.invoke())
    assert first.snapshot()["requests"][0]["cache_read_tokens"] == 1
    assert second.snapshot()["requests"][0]["cache_read_tokens"] == 2
    assert closed.snapshot()["requests"][0]["cache_read_tokens"] is None


@pytest.mark.asyncio
async def test_retry_records_only_the_final_returned_response(monkeypatch):
    from lionagi.service.resilience import RetryConfig

    endpoint = _endpoint("openai")
    endpoint.retry_config = RetryConfig(
        max_retries=1, base_delay=0.001, jitter=False, retry_exceptions=(RuntimeError,)
    )
    transport = AsyncMock(
        side_effect=[
            RuntimeError("retry"),
            {"usage": {"prompt_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 8}}},
        ]
    )
    monkeypatch.setattr(endpoint, "_call", transport)
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload={"model": "test"})
    await call.invoke()
    assert call.status is EventStatus.COMPLETED, call.execution.error
    assert transport.await_count == 2
    row = collector.snapshot()["requests"][0]
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (5, 8)


@pytest.mark.asyncio
async def test_local_cache_miss_records_and_hit_stays_unknown(monkeypatch):
    from aiocache import SimpleMemoryCache, caches
    from aiocache.serializers import JsonSerializer

    from lionagi.config import settings

    monkeypatch.setattr(settings.aiocache_config, "alias", "default")
    cache = SimpleMemoryCache(serializer=JsonSerializer())
    monkeypatch.setattr(caches, "get", lambda alias: cache)
    endpoint = _endpoint("openai")
    response = {"usage": {"prompt_tokens_details": {"cached_tokens": 11, "cache_write_tokens": 4}}}
    transport = AsyncMock(return_value=response)
    monkeypatch.setattr(endpoint, "_call", transport)
    collector = _collector()
    payload = {"model": str(uuid.uuid4())}
    with collect_api_turn(collector, 1):
        first = APICalling(endpoint=endpoint, payload=payload, cache_control=True)
        second = APICalling(endpoint=endpoint, payload=payload, cache_control=True)
    await first.invoke()
    await second.invoke()
    assert first.status is second.status is EventStatus.COMPLETED
    assert second.response == response and isinstance(second.response, dict)
    assert transport.await_count == 1
    rows = collector.snapshot()["requests"]
    assert (rows[0]["cache_read_tokens"], rows[0]["cache_write_tokens"]) == (11, 4)
    assert (rows[1]["cache_read_tokens"], rows[1]["cache_write_tokens"]) == (None, None)


@pytest.mark.asyncio
async def test_nested_endpoint_in_cache_lookup_does_not_claim_outer_hit(monkeypatch):
    import aiocache

    nested = _endpoint("openai")
    monkeypatch.setattr(nested, "_call", AsyncMock(return_value={}))
    cached = {"usage": {"prompt_tokens_details": {"cached_tokens": 9}}}

    def decorator(**kwargs):
        def wrap(fn):
            async def hit(*args, **kwargs):
                await nested.call({"model": "nested"}, skip_payload_creation=True)
                return cached

            return hit

        return wrap

    monkeypatch.setattr(aiocache, "cached", decorator)
    endpoint = _endpoint("openai")
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload={"model": "outer"}, cache_control=True)
    await call.invoke()
    assert call.status is EventStatus.COMPLETED, call.execution.error
    assert call.response == cached
    assert collector.snapshot()["requests"][0]["cache_read_tokens"] is None


@pytest.mark.asyncio
async def test_override_nested_call_cannot_mark_outer_cache_hit(monkeypatch):
    import aiocache

    nested = _endpoint("openai")
    monkeypatch.setattr(nested, "_call", AsyncMock(return_value={}))
    cached = {"usage": {"prompt_tokens_details": {"cached_tokens": 9}}}

    class DelegatingEndpoint(Endpoint):
        async def call(self, *args, **kwargs):
            await nested.call({"model": "nested"}, skip_payload_creation=True)
            return await super().call(*args, **kwargs)

    def decorator(**kwargs):
        def wrap(fn):
            async def hit(*args, **kwargs):
                return cached

            return hit

        return wrap

    monkeypatch.setattr(aiocache, "cached", decorator)
    endpoint = DelegatingEndpoint(config=_endpoint("openai").config)
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload={"model": "outer"}, cache_control=True)
    await call.invoke()
    assert call.status is EventStatus.COMPLETED and call.response == cached
    assert collector.snapshot()["requests"][0]["cache_read_tokens"] is None


@pytest.mark.asyncio
async def test_instance_override_nested_call_cannot_mark_outer_cache_hit(monkeypatch):
    from aiocache import SimpleMemoryCache, caches
    from aiocache.serializers import JsonSerializer

    from lionagi.config import settings

    nested = _endpoint("openai")
    nested_transport = AsyncMock(return_value={})
    monkeypatch.setattr(nested, "_call", nested_transport)
    cached = {"usage": {"prompt_tokens_details": {"cached_tokens": 9}}}
    monkeypatch.setattr(settings.aiocache_config, "alias", "default")
    cache = SimpleMemoryCache(serializer=JsonSerializer())
    monkeypatch.setattr(caches, "get", lambda alias: cache)
    endpoint = _endpoint("openai")
    transport = AsyncMock(return_value=cached)
    monkeypatch.setattr(endpoint, "_call", transport)
    original_call = endpoint.call
    collector = _collector()
    payload = {"model": str(uuid.uuid4())}
    with collect_api_turn(collector, 1):
        first = APICalling(endpoint=endpoint, payload=payload, cache_control=True)
        second = APICalling(endpoint=endpoint, payload=payload, cache_control=True)
    await first.invoke()

    async def wrapped_call(*args, **kwargs):
        await nested.call({"model": "nested"}, skip_payload_creation=True)
        return await original_call(*args, **kwargs)

    monkeypatch.setattr(endpoint, "call", wrapped_call)
    await second.invoke()
    assert first.status is second.status is EventStatus.COMPLETED
    assert second.response == cached and isinstance(second.response, dict)
    assert transport.await_count == nested_transport.await_count == 1
    rows = collector.snapshot()["requests"]
    assert rows[0]["cache_read_tokens"] == 9
    assert rows[1]["cache_read_tokens"] is None


@pytest.mark.asyncio
async def test_streamed_usage_is_not_reported_as_a_final_response(monkeypatch):
    endpoint = _endpoint("openai")

    async def stream(**kwargs):
        yield {"usage": {"prompt_tokens_details": {"cached_tokens": 9}}}

    monkeypatch.setattr(endpoint, "stream", stream)
    collector = _collector()
    with collect_api_turn(collector, 1):
        call = APICalling(endpoint=endpoint, payload={"model": "test", "stream": True})
    assert [item async for item in call.stream()]
    row = collector.snapshot()["requests"][0]
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (None, None)
