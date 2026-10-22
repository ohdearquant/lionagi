# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A 200 response whose body is a provider error fails the call like the status it names."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.resilience import APIClientError

_NO_SLEEP = patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock())


@pytest.fixture
def run_server(monkeypatch):
    async def _run(handler):
        received = []

        def transport_handler(request):
            received.append(request.content)
            return handler(request)

        def create_client(self, *args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))

        monkeypatch.setattr(Endpoint, "_create_http_session", create_client)
        monkeypatch.setattr(Endpoint, "_assert_ssrf_safe_url", lambda self: None)
        return "https://api.example.test", received

    return _run


def _json_response(request: httpx.Request, body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=request)


def _endpoint(base_url: str) -> Endpoint:
    return Endpoint(
        config=EndpointConfig(
            name="openai_chat",
            provider="openai",
            endpoint="chat/completions",
            base_url=base_url,
            auth_type="bearer",
            api_key="test-key",
            allow_local_network=True,
            max_retries=3,
            timeout=5,
        )
    )


async def _call(endpoint: Endpoint):
    with _NO_SLEEP:
        return await endpoint._call(
            payload={"model": "m", "messages": []},
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        )


@pytest.mark.asyncio
async def test_http_error_keeps_status_headers_and_parsed_body(monkeypatch):
    body = {"error": {"message": "invalid input"}}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                422,
                json=body,
                headers={"X-Provider-Error": "invalid"},
                request=request,
            )
        )
    )
    endpoint = _endpoint("https://api.example.test")
    monkeypatch.setattr(endpoint, "_create_http_session", lambda *args, **kwargs: client)
    monkeypatch.setattr("lionagi.ln._ssrf.is_ssrf_safe", lambda *args, **kwargs: True)

    with pytest.raises(APIClientError) as caught:
        await _call(endpoint)

    assert caught.value.status_code == 422
    assert isinstance(caught.value.headers, httpx.Headers)
    assert caught.value.headers["x-provider-error"] == "invalid"
    assert caught.value.response_data == body


@pytest.mark.asyncio
async def test_an_error_naming_a_4xx_is_raised_and_sent_once(run_server):
    body = {"id": "gen-1", "error": {"message": "input exceeds the context window", "code": 400}}
    base_url, received = await run_server(lambda request: _json_response(request, body))

    with pytest.raises(APIClientError) as exc_info:
        await _call(_endpoint(base_url))

    assert exc_info.value.status_code == 400
    assert "context window" in str(exc_info.value)
    assert len(received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [429, 502])
async def test_an_error_naming_a_retryable_status_is_retried_to_the_cap(run_server, code):
    body = {"error": {"message": "upstream", "code": code}}
    base_url, received = await run_server(lambda request: _json_response(request, body))

    with pytest.raises(APIClientError) as exc_info:
        await _call(_endpoint(base_url))

    assert exc_info.value.status_code == code
    assert exc_info.value.response_data == body
    assert len(received) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        {"message": "rate limited", "code": "rate_limit_exceeded"},
        {"message": "no code"},
        "bad request",
    ],
)
async def test_an_error_without_a_status_code_is_raised_once_under_the_http_status(
    run_server, error
):
    body = {"error": error}
    base_url, received = await run_server(lambda request: _json_response(request, body))

    with pytest.raises(APIClientError) as exc_info:
        await _call(_endpoint(base_url))

    assert exc_info.value.status_code == 200
    assert exc_info.value.response_data == body
    assert len(received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"id": "resp-1", "status": "completed", "error": None, "output": []},
        {"choices": [{"message": {"content": "hi"}}], "error": {"message": "partial"}},
        {
            "id": "resp-2",
            "object": "response",
            "status": "failed",
            "error": {"code": "server_error", "message": "The model failed."},
            "output": [],
        },
        {"id": "gen-1", "error": {}},
    ],
    ids=["null error", "choices", "typed object", "empty error"],
)
async def test_a_result_is_returned_as_is(run_server, body):
    base_url, received = await run_server(lambda request: _json_response(request, body))

    assert await _call(_endpoint(base_url)) == body
    assert len(received) == 1


@pytest.mark.asyncio
async def test_an_error_answering_a_request_that_is_not_a_chat_is_returned_as_is(run_server):
    body = {"success": False, "code": "SCRAPE_DNS_RESOLUTION_ERROR", "error": "DNS lookup failed"}
    base_url, received = await run_server(lambda request: _json_response(request, body))
    with _NO_SLEEP:
        result = await _endpoint(base_url)._call(
            payload={"url": "https://example.com"},
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        )

    assert result == body
    assert len(received) == 1
