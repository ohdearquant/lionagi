# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A 200 response whose body is a provider error fails the call like the status it names."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

_NO_SLEEP = patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock())


@pytest.fixture
async def run_server():
    servers = []

    async def _run(body: dict) -> tuple[str, list[int]]:
        received: list[int] = []

        async def handler(request: web.Request):
            await request.read()
            received.append(1)
            return web.json_response(body)

        app = web.Application()
        app.router.add_route("POST", "/{tail:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return str(server.make_url("/")).rstrip("/"), received

    yield _run

    for server in servers:
        await server.close()


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
async def test_an_error_naming_a_4xx_is_raised_and_sent_once(run_server):
    base_url, received = await run_server(
        {"id": "gen-1", "error": {"message": "input exceeds the context window", "code": 400}}
    )

    with pytest.raises(aiohttp.ClientResponseError) as exc_info:
        await _call(_endpoint(base_url))

    assert exc_info.value.status == 400
    assert "context window" in exc_info.value.message
    assert len(received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [429, 502])
async def test_an_error_naming_a_retryable_status_is_retried_to_the_cap(run_server, code):
    base_url, received = await run_server({"error": {"message": "upstream", "code": code}})

    with pytest.raises(aiohttp.ClientResponseError) as exc_info:
        await _call(_endpoint(base_url))

    assert exc_info.value.status == code
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
    base_url, received = await run_server({"error": error})

    with pytest.raises(aiohttp.ClientResponseError) as exc_info:
        await _call(_endpoint(base_url))

    assert exc_info.value.status == 200
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
    base_url, received = await run_server(body)

    assert await _call(_endpoint(base_url)) == body
    assert len(received) == 1


@pytest.mark.asyncio
async def test_an_error_answering_a_request_that_is_not_a_chat_is_returned_as_is(run_server):
    body = {"success": False, "code": "SCRAPE_DNS_RESOLUTION_ERROR", "error": "DNS lookup failed"}
    base_url, received = await run_server(body)

    with _NO_SLEEP:
        result = await _endpoint(base_url)._call(
            payload={"url": "https://example.com"},
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        )

    assert result == body
    assert len(received) == 1
