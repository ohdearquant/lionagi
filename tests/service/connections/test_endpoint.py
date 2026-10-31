# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import json
import re
import ssl
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import httpx
import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.resilience import APIClientError
from lionagi.service.types.stream_chunk import StreamChunk


def _http_response(status: int = 200, body: dict | None = None, *, headers=None):
    return httpx.Response(status, json=body if body is not None else {}, headers=headers or {})


def _http_client_for(responses):
    pending = iter(responses)

    def handler(request):
        response = next(pending)
        response.request = request
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _http_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _stream_client(body: str):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=body,
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )
        )
    )


class TestEndpoint:
    """Test the Endpoint class for stateless behavior and parallel execution."""

    @pytest.fixture
    def openai_config(self):
        return EndpointConfig(
            name="openai_chat",
            endpoint="chat",
            provider="openai",
            base_url="https://api.openai.com/v1",
            endpoint_params=["chat", "completions"],
            openai_compatible=True,
            api_key="test-key",
        )

    @pytest.fixture
    def anthropic_config(self):
        return EndpointConfig(
            name="anthropic_chat",
            endpoint="chat",
            provider="anthropic",
            base_url="https://api.anthropic.com/v1",
            endpoint_params=["messages"],
            openai_compatible=False,
            auth_type="x-api-key",
            default_headers={"anthropic-version": "2023-06-01"},
            api_key="test-key",
        )

    def test_endpoint_initialization(self, openai_config):
        endpoint = Endpoint(config=openai_config)
        assert endpoint.config == openai_config

    def test_endpoint_initialization_with_dict(self):
        config_dict = {
            "name": "test_endpoint",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "endpoint": "chat",
            "api_key": "test-key",
        }
        endpoint = Endpoint(config=config_dict)
        assert endpoint.config.name == "test_endpoint"
        assert endpoint.config.provider == "openai"

    def test_endpoint_initialization_invalid_config_type(self):
        with pytest.raises(ValueError, match="Config must be a dict, EndpointConfig, or None"):
            Endpoint(config="invalid_config_type")

    @pytest.mark.asyncio
    async def test_http_client_factory_uses_httpx_defaults_and_client_overrides(
        self, openai_config
    ):
        endpoint = Endpoint(config=openai_config)
        client = endpoint._create_http_session(timeout=7)
        try:
            assert isinstance(client, httpx.AsyncClient)
            assert client.timeout.connect == 7
            assert client.timeout.read == 7
            assert client.follow_redirects is True
            assert client._trust_env is False
        finally:
            await client.aclose()

        client = endpoint._create_http_session(
            {"follow_redirects": False, "trust_env": True, "headers": {"X-Test": "yes"}},
            timeout=7,
        )
        try:
            assert isinstance(client, httpx.AsyncClient)
            assert client.timeout.connect == 7
            assert client.follow_redirects is False
            assert client._trust_env is True
            assert client.headers["X-Test"] == "yes"
        finally:
            await (client.aclose() if isinstance(client, httpx.AsyncClient) else client.close())

    @pytest.mark.parametrize(
        ("legacy_name", "new_name"),
        [("_call_aiohttp", "_call_http"), ("_stream_aiohttp", "_stream_http")],
    )
    def test_subclass_rejects_removed_transport_override(self, legacy_name, new_name):
        async def override(*args, **kwargs):
            return None

        with pytest.raises(TypeError, match=re.escape(new_name)):
            type("LegacyTransportEndpoint", (Endpoint,), {legacy_name: override})

    def test_request_options_setter(self, openai_config):
        from pydantic import BaseModel

        class CustomRequest(BaseModel):
            messages: list
            temperature: float = 0.7

        endpoint = Endpoint(config=openai_config)
        endpoint.request_options = CustomRequest
        assert endpoint.request_options == CustomRequest

    def test_create_payload_with_extra_headers(self, openai_config):
        endpoint = Endpoint(config=openai_config)
        request = {"messages": [{"role": "user", "content": "test"}]}
        extra_headers = {"X-Custom-Header": "custom-value"}

        payload, headers = endpoint.create_payload(request, extra_headers=extra_headers)

        assert "X-Custom-Header" in headers
        assert headers["X-Custom-Header"] == "custom-value"

    def test_create_payload_with_kwargs(self, openai_config):
        endpoint = Endpoint(config=openai_config)
        request = {"messages": [{"role": "user", "content": "test"}]}

        payload, headers = endpoint.create_payload(request, temperature=0.9, max_tokens=500)

        assert payload["temperature"] == 0.9
        assert payload["max_tokens"] == 500

    def test_endpoint_stateless_design(self, openai_config):
        endpoint = Endpoint(config=openai_config)

        # First payload creation
        payload1, headers1 = endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "gpt-4.1-mini",
            }
        )

        # Second payload creation with different data
        payload2, headers2 = endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Goodbye"}],
                "model": "gpt-4o",
            }
        )

        # Verify that payloads are independent
        assert payload1["messages"][0]["content"] == "Hello"
        assert payload2["messages"][0]["content"] == "Goodbye"
        assert payload1["model"] == "gpt-4.1-mini"
        assert payload2["model"] == "gpt-4o"

    def test_create_payload_openai(self, openai_config):
        endpoint = Endpoint(config=openai_config)

        request_data = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "gpt-4.1-mini",
            "temperature": 0.7,
            "max_tokens": 100,
        }

        payload, headers = endpoint.create_payload(request_data)

        assert payload["model"] == "gpt-4.1-mini"
        assert payload["messages"] == request_data["messages"]
        assert payload["temperature"] == 0.7
        assert payload["max_tokens"] == 100
        assert "Authorization" in headers
        assert headers["Content-Type"] == "application/json"

    def test_create_payload_anthropic(self, anthropic_config):
        endpoint = Endpoint(config=anthropic_config)

        request_data = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "claude-3-opus-20240229",
            "max_tokens": 100,
            "api_key": "test-key",
        }

        payload, headers = endpoint.create_payload(request_data)

        assert payload["model"] == "claude-3-opus-20240229"
        assert payload["messages"] == request_data["messages"]
        assert payload["max_tokens"] == 100
        assert "api_key" not in payload  # Should be removed from payload
        assert "x-api-key" in headers
        assert headers["anthropic-version"] == "2023-06-01"

    @pytest.mark.asyncio
    async def test_http_request_session_cleanup(self, openai_config):
        # Disable OpenAI compatibility for pure HTTP test
        openai_config.openai_compatible = False
        endpoint = Endpoint(config=openai_config)

        client = _http_client_for([_http_response(body={"success": True})])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=client):
                request = {
                    "messages": [{"role": "user", "content": "test"}],
                    "model": "gpt-4.1-mini",
                }

                await endpoint.call(request)

        assert client.is_closed

    @pytest.mark.asyncio
    async def test_http_client_closes_after_request(self, openai_config):
        """The HTTP client closes after a request completes."""
        # Disable OpenAI compatibility for pure HTTP test
        openai_config.openai_compatible = False
        endpoint = Endpoint(config=openai_config)

        client = _http_client_for([_http_response(body={"released": True})])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=client):
                request = {
                    "messages": [{"role": "user", "content": "test"}],
                    "model": "gpt-4.1-mini",
                }
                result = await endpoint.call(request)

        assert client.is_closed
        assert result is not None

    def test_url_construction(self, openai_config, anthropic_config):
        openai_endpoint = Endpoint(config=openai_config)
        anthropic_endpoint = Endpoint(config=anthropic_config)

        openai_url = openai_endpoint.config.full_url
        anthropic_url = anthropic_endpoint.config.full_url

        assert urlsplit(openai_url).hostname == "api.openai.com"
        assert urlsplit(anthropic_url).hostname == "api.anthropic.com"

    @pytest.mark.asyncio
    async def test_call_with_skip_payload_creation(self, openai_config):
        endpoint = Endpoint(config=openai_config)

        # Prepare pre-created payload
        ready_payload = {
            "messages": [{"role": "user", "content": "test"}],
            "model": "gpt-4o-mini",
        }
        custom_headers = {"X-Custom": "header"}

        with patch.object(endpoint, "_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"response": "success"}
            await endpoint.call(
                ready_payload,
                skip_payload_creation=True,
                extra_headers=custom_headers,
            )
            mock_call.assert_called_once()
            # Verify payload was passed through without create_payload
            call_args = mock_call.call_args
            assert call_args[0][0] == ready_payload

    @pytest.mark.asyncio
    async def test_call_with_retry_config(self, openai_config):
        from lionagi.service.resilience import RetryConfig

        retry_config = RetryConfig(max_retries=3, base_delay=0.01)
        endpoint = Endpoint(config=openai_config, retry_config=retry_config)

        with patch.object(endpoint, "_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"response": "success"}
            request = {"messages": [{"role": "user", "content": "test"}]}
            result = await endpoint.call(request)
            assert result == {"response": "success"}

    @pytest.mark.asyncio
    async def test_call_with_circuit_breaker(self, openai_config):
        from lionagi.service.resilience import CircuitBreaker

        circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_time=1.0)
        endpoint = Endpoint(config=openai_config, circuit_breaker=circuit_breaker)

        with patch.object(endpoint, "_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"response": "success"}
            request = {"messages": [{"role": "user", "content": "test"}]}
            result = await endpoint.call(request)
            assert result == {"response": "success"}

    @pytest.mark.asyncio
    async def test_call_with_circuit_breaker_and_retry(self, openai_config):
        from lionagi.service.resilience import CircuitBreaker, RetryConfig

        circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_time=1.0)
        retry_config = RetryConfig(max_retries=2, base_delay=0.01)
        endpoint = Endpoint(
            config=openai_config,
            circuit_breaker=circuit_breaker,
            retry_config=retry_config,
        )

        with patch.object(endpoint, "_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"response": "success"}
            request = {"messages": [{"role": "user", "content": "test"}]}
            result = await endpoint.call(request)
            assert result == {"response": "success"}

    @pytest.mark.asyncio
    async def test_call_with_cache_control(self, openai_config):
        endpoint = Endpoint(config=openai_config)

        with patch.object(endpoint, "_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"response": "cached"}
            request = {"messages": [{"role": "user", "content": "test"}]}
            # First call - should cache
            result1 = await endpoint.call(request, cache_control=True)
            # Second call - should use cache
            result2 = await endpoint.call(request, cache_control=True)
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_call_with_cache_and_circuit_breaker(self, openai_config):
        from lionagi.service.resilience import CircuitBreaker

        circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_time=1.0)
        endpoint = Endpoint(config=openai_config, circuit_breaker=circuit_breaker)

        with patch.object(endpoint, "_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"response": "success"}
            request = {"messages": [{"role": "user", "content": "test"}]}
            result = await endpoint.call(request, cache_control=True)
            assert result == {"response": "success"}

    @pytest.mark.asyncio
    async def test_call_with_cache_and_retry(self, openai_config):
        from lionagi.service.resilience import RetryConfig

        retry_config = RetryConfig(max_retries=2, base_delay=0.01)
        endpoint = Endpoint(config=openai_config, retry_config=retry_config)

        with patch.object(endpoint, "_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"response": "success"}
            request = {"messages": [{"role": "user", "content": "test"}]}
            result = await endpoint.call(request, cache_control=True)
            assert result == {"response": "success"}

    @pytest.mark.asyncio
    async def test_error_handling_isolation(self, openai_config):
        """One failed HTTP request leaves concurrent requests and their cleanup intact."""
        openai_config.max_retries = 1
        endpoint = Endpoint(config=openai_config)
        entered = []
        all_entered = asyncio.Event()
        clients = []

        async def handler(request):
            payload = json.loads(request.content)
            label = payload["messages"][0]["content"]
            entered.append(label)
            if len(entered) == 3:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), timeout=2)
            if label == "second":
                raise httpx.ConnectError("second request failed", request=request)
            return httpx.Response(200, json={"success": True, "request": label}, request=request)

        def create_client():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            clients.append(client)
            return client

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=create_client):
                results = await asyncio.gather(
                    *(
                        endpoint.call({"messages": [{"role": "user", "content": label}]})
                        for label in ("first", "second", "third")
                    ),
                    return_exceptions=True,
                )

        assert results[0] == {"success": True, "request": "first"}
        assert isinstance(results[1], httpx.RequestError)
        assert str(results[1]) == "second request failed"
        assert results[2] == {"success": True, "request": "third"}
        assert sorted(entered) == ["first", "second", "third"]
        assert len(clients) == 3
        assert len({id(client) for client in clients}) == 3
        assert all(client.is_closed for client in clients)

    @pytest.mark.asyncio
    async def test_http_429_status_code_path(self, openai_config):
        openai_config.openai_compatible = False
        endpoint = Endpoint(config=openai_config)

        # Mock the transport call to test status handling logic.
        with patch.object(endpoint, "_call") as mock_call:
            mock_call.return_value = {"success": True}
            request = {"messages": [{"role": "user", "content": "test"}]}
            result = await endpoint.call(request)
            assert result == {"success": True}
            mock_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_error_handling_paths(self, openai_config):
        openai_config.openai_compatible = False
        endpoint = Endpoint(config=openai_config)

        # Verify error handling paths are exercised
        with patch.object(endpoint, "_call") as mock_call:
            mock_call.return_value = {"success": True}
            request = {"messages": [{"role": "user", "content": "test"}]}
            result = await endpoint.call(request)
            assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_stream_basic(self, openai_config):
        endpoint = Endpoint(config=openai_config)

        async def mock_stream(*args, **kwargs):
            yield b"chunk1"
            yield b"chunk2"
            yield b"chunk3"

        with patch.object(
            endpoint,
            "_stream_http",
            side_effect=lambda *args, **kwargs: mock_stream(),
        ):
            request = {"messages": [{"role": "user", "content": "test"}]}
            chunks = []
            async for chunk in endpoint.stream(request):
                chunks.append(chunk)

            assert len(chunks) == 3
            assert chunks[0] == b"chunk1"

    @pytest.mark.asyncio
    async def test_stream_http(self, openai_config):
        endpoint = Endpoint(config=openai_config)
        client = _stream_client("line1\nline2\n\nline3\n")

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=client):
                request = {"messages": [{"role": "user", "content": "test"}]}
                chunks = []
                async for chunk in endpoint.stream(request):
                    chunks.append(chunk)

        assert len(chunks) == 3
        assert all(isinstance(chunk, StreamChunk) for chunk in chunks)
        assert [chunk.type for chunk in chunks] == ["text", "text", "text"]
        assert [chunk.content for chunk in chunks] == ["line1", "line2", "line3"]

    @pytest.mark.asyncio
    async def test_stream_http_ignores_sse_control_lines(self, openai_config):
        endpoint = Endpoint(config=openai_config)
        client = _stream_client(
            "event: response.output_text.delta\nid: evt_1\n"
            'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            ": keepalive\n\ndata: [DONE]\n\n"
        )

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=client):
                request = {"messages": [{"role": "user", "content": "test"}]}
                chunks = [chunk async for chunk in endpoint.stream(request)]

        assert [chunk.type for chunk in chunks] == ["text", "result"]
        assert chunks[0].content == "hi"
        assert chunks[1].metadata == {"done": True}

    def test_usage_only_event_is_result_chunk(self, openai_config):
        """A Chat Completions usage-only terminal event ({"choices": [],
        "usage": {...}}) must normalize to a result chunk carrying the usage,
        not a system chunk that terminal-accounting consumers skip."""
        endpoint = Endpoint(config=openai_config)
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        chunk = endpoint._event_to_stream_chunk({"choices": [], "usage": usage})
        assert chunk is not None
        assert chunk.type == "result"
        assert chunk.metadata["usage"] == usage

    def test_empty_choices_without_usage_is_not_result(self, openai_config):
        """Empty choices with no usage stays a non-result (unchanged behavior)."""
        endpoint = Endpoint(config=openai_config)
        assert endpoint._event_to_stream_chunk({"choices": []}) is None

    @pytest.mark.asyncio
    async def test_stream_http_usage_only_terminal_event(self, openai_config):
        """End-to-end: the usage-only line surfaces as a result chunk with usage."""
        endpoint = Endpoint(config=openai_config)

        client = _stream_client(
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            'data: {"choices":[],"usage":{"total_tokens":18}}\n\n'
            "data: [DONE]\n\n"
        )

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=client):
                request = {"messages": [{"role": "user", "content": "test"}]}
                chunks = [chunk async for chunk in endpoint.stream(request)]

        assert [c.type for c in chunks] == ["text", "result", "result"]
        # the usage-only result carries the provider usage
        assert chunks[1].metadata["usage"] == {"total_tokens": 18}

    @pytest.mark.asyncio
    async def test_stream_with_extra_headers(self, openai_config):
        endpoint = Endpoint(config=openai_config)

        async def mock_stream(*args, **kwargs):
            yield b"chunk1"

        with patch.object(
            endpoint,
            "_stream_http",
            side_effect=lambda *args, **kwargs: mock_stream(),
        ):
            request = {"messages": [{"role": "user", "content": "test"}]}
            extra_headers = {"X-Stream-Header": "value"}
            chunks = []
            async for chunk in endpoint.stream(request, extra_headers=extra_headers):
                chunks.append(chunk)
            assert len(chunks) == 1

    def test_to_dict(self, openai_config):
        from lionagi.service.resilience import CircuitBreaker, RetryConfig

        retry_config = RetryConfig(max_retries=3, base_delay=0.1)
        circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_time=2.0)
        endpoint = Endpoint(
            config=openai_config,
            retry_config=retry_config,
            circuit_breaker=circuit_breaker,
        )

        result = endpoint.to_dict()

        assert "config" in result
        assert "retry_config" in result
        assert "circuit_breaker" in result
        assert result["retry_config"] is not None
        assert result["circuit_breaker"] is not None

    def test_from_dict(self, openai_config):
        from lionagi.service.resilience import CircuitBreaker, RetryConfig

        # Create endpoint with resilience patterns
        retry_config = RetryConfig(max_retries=3, base_delay=0.1)
        circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_time=2.0)
        original_endpoint = Endpoint(
            config=openai_config,
            retry_config=retry_config,
            circuit_breaker=circuit_breaker,
        )

        # Serialize to dict
        data = original_endpoint.to_dict()

        # Deserialize from dict
        restored_endpoint = Endpoint.from_dict(data)

        assert restored_endpoint.config.name == original_endpoint.config.name
        assert restored_endpoint.retry_config is not None
        assert restored_endpoint.circuit_breaker is not None
        assert (
            restored_endpoint.retry_config.max_retries == original_endpoint.retry_config.max_retries
        )

    def test_endpoint_create_payload_filters_non_api_params_without_request_options(
        self,
    ):
        config = EndpointConfig(
            name="test_chat",
            provider="test_provider",
            endpoint="chat/completions",
            auth_type="bearer",
            content_type="application/json",
            api_key="test-key-123",
        )
        endpoint = Endpoint(config)
        assert endpoint.config.request_options is None

        request = {
            "messages": [{"role": "user", "content": "hi"}],
            "provider": "openai",
            "branch": object(),
            "parse_model": object(),
            "temperature": 0.2,
        }
        payload, headers = endpoint.create_payload(request)

        assert "messages" in payload
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        assert payload.get("temperature") == 0.2
        assert "provider" not in payload
        assert "branch" not in payload
        assert "parse_model" not in payload
        assert any("application/json" in str(v) for v in headers.values())

    @pytest.mark.asyncio
    async def test_endpoint_call_composes_retry_then_circuit_without_cache(self):
        from unittest.mock import patch

        from lionagi.service.resilience import CircuitBreaker, RetryConfig

        retry_config = RetryConfig(max_retries=1)
        circuit_breaker = CircuitBreaker()

        config = EndpointConfig(
            name="test_chat",
            provider="test_provider",
            endpoint="chat/completions",
            auth_type="bearer",
            content_type="application/json",
            api_key="test-key",
        )
        endpoint = Endpoint(config, circuit_breaker=circuit_breaker, retry_config=retry_config)

        async def fake_call(payload, headers, **kwargs):
            return {"result": "ok"}

        async def fake_execute(func, *args, **kwargs):
            return await func(*args, **kwargs)

        with patch.object(endpoint, "_call", fake_call):
            with patch.object(circuit_breaker, "execute", side_effect=fake_execute) as mock_execute:
                result = await endpoint.call({"messages": []}, cache_control=False)

        assert result == {"result": "ok"}
        assert mock_execute.called


class TestEndpointSSRFGuard:
    """Endpoint._call_http and _stream_http must block SSRF URLs."""

    def _make_endpoint(self, base_url: str) -> Endpoint:
        config = EndpointConfig(
            name="test_chat",
            provider="test_provider",
            endpoint="chat/completions",
            base_url=base_url,
            auth_type="bearer",
            content_type="application/json",
            api_key="test-key",
        )
        return Endpoint(config=config)

    @pytest.mark.asyncio
    async def test_call_http_blocks_private_ip(self):
        """_call_http raises PermissionError for private base_url."""
        endpoint = self._make_endpoint("http://169.254.169.254")
        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                await endpoint._call_http(payload={}, headers={})

    @pytest.mark.asyncio
    async def test_call_http_allows_public_ip(self):
        """_call_http does not raise for a public base_url."""
        endpoint = self._make_endpoint("https://api.openai.com/v1")
        client = _http_client_for([_http_response(body={"ok": True})])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=client):
                result = await endpoint._call_http(payload={}, headers={})
        assert result == {"ok": True}
        assert client.is_closed

    @pytest.mark.asyncio
    async def test_stream_http_blocks_private_ip(self):
        """_stream_http raises PermissionError for private base_url."""
        endpoint = self._make_endpoint("http://192.168.1.1")
        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                # consume the async generator to trigger the check
                async for _ in endpoint._stream_http(payload={}, headers={}):
                    pass

    @pytest.mark.asyncio
    async def test_imodel_base_url_blocked(self):
        """iModel(base_url='http://169.254.169.254') is blocked at transport layer."""
        from lionagi.service.imodel import iModel

        model = iModel(
            provider="openai",
            model="gpt-4o-mini",
            base_url="http://169.254.169.254",
            api_key="test",
        )
        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                await model.endpoint._call_http(payload={}, headers={})

    @pytest.mark.asyncio
    async def test_endpoint_ssrf_blocks_link_local_ipv6(self):
        """Endpoint rejects fe80:: base URLs (IPv6 link-local, HIGH 1 + HIGH 2 combined)."""
        endpoint = self._make_endpoint("http://[fe80::1]")
        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                await endpoint._call_http(payload={}, headers={})


class TestProviderCallOverrideSSRFGuard:
    """Provider endpoints that override _call() must invoke _assert_ssrf_safe_url() before any network I/O."""

    @pytest.mark.asyncio
    async def test_openai_tts_blocks_private_base_url(self):
        """OpenaiAudioSpeechEndpoint._call must reject private base_url."""
        from lionagi.service.connections.endpoint_config import EndpointConfig
        from lionhub.providers.openai.audio import OpenaiAudioSpeechEndpoint

        config = EndpointConfig(
            name="openai_audio_speech",
            provider="openai",
            endpoint="audio/speech",
            base_url="http://169.254.169.254",
            api_key="test-key",
        )
        endpoint = OpenaiAudioSpeechEndpoint(config=config)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                await endpoint._call(payload={"input": "hi", "voice": "nova"}, headers={})

    @pytest.mark.asyncio
    async def test_openai_stt_blocks_private_base_url(self):
        """OpenaiAudioTranscriptionEndpoint._call must reject private base_url."""
        from lionagi.service.connections.endpoint_config import EndpointConfig
        from lionhub.providers.openai.audio import (
            OpenaiAudioTranscriptionEndpoint,
        )

        config = EndpointConfig(
            name="openai_audio_transcription",
            provider="openai",
            endpoint="audio/transcriptions",
            base_url="http://10.0.0.1",
            api_key="test-key",
        )
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                await endpoint._call(payload={"model": "whisper-1"}, headers={})

    @pytest.mark.asyncio
    async def test_openai_image_edit_blocks_private_base_url(self):
        """OpenaiImageEditEndpoint._call must reject private base_url."""
        from lionagi.service.connections.endpoint_config import EndpointConfig
        from lionhub.providers.openai.images import OpenaiImageEditEndpoint

        config = EndpointConfig(
            name="openai_image_edit",
            provider="openai",
            endpoint="images/edits",
            base_url="http://192.168.1.100",
            api_key="test-key",
        )
        endpoint = OpenaiImageEditEndpoint(config=config)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                await endpoint._call(
                    payload={"prompt": "add rainbow"}, headers={}, image=b"\x89PNG"
                )

    @pytest.mark.asyncio
    async def test_groq_stt_blocks_private_base_url(self):
        """GroqAudioTranscriptionEndpoint._call must reject private base_url."""
        from lionagi.service.connections.endpoint_config import EndpointConfig
        from lionhub.providers.groq.audio_transcription import (
            GroqAudioTranscriptionEndpoint,
        )

        config = EndpointConfig(
            name="groq_audio_transcription",
            provider="groq",
            endpoint="audio/transcriptions",
            base_url="http://127.0.0.1",
            api_key="test-key",
        )
        endpoint = GroqAudioTranscriptionEndpoint(config=config)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                await endpoint._call(payload={"model": "whisper-large-v3"}, headers={})


class TestCLIEndpointDeprecation:
    """CLIEndpoint is a deprecated alias for AgenticEndpoint."""

    def test_cli_endpoint_emits_deprecation_warning(self):
        import sys

        import lionagi.service.connections as _conn_mod

        # Clear the cached attribute so __getattr__ fires fresh.
        _conn_mod.__dict__.pop("CLIEndpoint", None)
        try:
            with pytest.warns(DeprecationWarning, match="CLIEndpoint is deprecated"):
                from lionagi.service.connections import CLIEndpoint  # noqa: F401
        finally:
            _conn_mod.__dict__.pop("CLIEndpoint", None)

    def test_cli_endpoint_resolves_to_agentic_endpoint(self):
        from lionagi.service.connections import AgenticEndpoint, CLIEndpoint

        assert CLIEndpoint is AgenticEndpoint


class TestSingleRetryPathParity:
    """Verify the native-only retry path preserves the giveup-except-429 rule."""

    def _make_endpoint(self, max_retries: int = 3) -> Endpoint:
        config = EndpointConfig(
            name="test",
            provider="test",
            endpoint="v1/chat",
            base_url="https://api.test.com",
            auth_type="bearer",
            api_key="test-key",
            max_retries=max_retries,
        )
        return Endpoint(config=config)

    def _make_mock_session(self, responses):
        """Build an HTTPX client with a mock transport returning responses in sequence."""
        return _http_client_for(responses)

    def _make_ok_response(self, body: dict | None = None):
        return _http_response(body=body or {"ok": True})

    def _make_error_response(self, status: int):
        return _http_response(status, {"error": f"status {status}"})

    @pytest.mark.asyncio
    async def test_200_ok_no_retry(self):
        endpoint = self._make_endpoint()
        ok = self._make_ok_response({"choices": []})
        session = self._make_mock_session([ok])
        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=session):
                result = await endpoint._call_http({}, {})
        assert result == {"choices": []}
        assert session.is_closed

    @pytest.mark.asyncio
    async def test_429_is_retried(self):
        """429 must be retried; succeeds on second attempt."""
        endpoint = self._make_endpoint(max_retries=3)
        r429 = self._make_error_response(429)
        ok = self._make_ok_response({"retried": True})

        attempt = 0

        def _make_new_session(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                return self._make_mock_session([r429])
            return self._make_mock_session([ok])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    result = await endpoint._call_http({}, {})
        assert result == {"retried": True}
        assert attempt == 2

    @pytest.mark.asyncio
    async def test_400_gives_up_immediately(self):
        """400 response errors must not be retried."""
        endpoint = self._make_endpoint(max_retries=3)
        r400 = self._make_error_response(400)

        call_count = 0

        def _make_new_session(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return self._make_mock_session([r400])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with pytest.raises(APIClientError) as exc_info:
                    await endpoint._call_http({}, {})
        assert exc_info.value.status_code == 400
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_401_gives_up_immediately(self):
        """401 must not be retried."""
        endpoint = self._make_endpoint(max_retries=3)
        r401 = self._make_error_response(401)
        call_count = 0

        def _make_new_session(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return self._make_mock_session([r401])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with pytest.raises(APIClientError) as exc_info:
                    await endpoint._call_http({}, {})
        assert exc_info.value.status_code == 401
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_403_gives_up_immediately(self):
        """403 must not be retried."""
        endpoint = self._make_endpoint(max_retries=3)
        r403 = self._make_error_response(403)
        call_count = 0

        def _make_new_session(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return self._make_mock_session([r403])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with pytest.raises(APIClientError) as exc_info:
                    await endpoint._call_http({}, {})
        assert exc_info.value.status_code == 403
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_500_is_retried_up_to_max(self):
        """500 server error must be retried up to max_retries attempts."""
        endpoint = self._make_endpoint(max_retries=2)

        call_count = 0

        def _make_new_session(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return self._make_mock_session([self._make_error_response(500)])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    with pytest.raises(APIClientError) as exc_info:
                        await endpoint._call_http({}, {})
        assert exc_info.value.status_code == 500
        # max_retries is a total-attempt cap (was backoff's max_tries): 2 → 2 attempts
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_network_timeout_is_retried(self):
        """asyncio.TimeoutError must be retried."""
        endpoint = self._make_endpoint(max_retries=2)

        call_count = 0

        async def _raise_timeout(request):
            nonlocal call_count
            call_count += 1
            raise asyncio.TimeoutError()

        def create_client():
            return httpx.AsyncClient(transport=httpx.MockTransport(_raise_timeout))

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=create_client):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    with pytest.raises(asyncio.TimeoutError):
                        await endpoint._call_http({}, {})
        # max_retries is a total-attempt cap (was backoff's max_tries): 2 → 2 attempts
        assert call_count == 2


class TestRetryConfigDefaultRetriesRealErrors:
    """RetryConfig() with unmodified defaults, exercised through endpoint.call()
    (the opt-in retry_config path), must retry the errors production code
    actually raises — not just APIClientError, which nothing ever constructs."""

    def _make_endpoint(self, max_retries: int = 3) -> Endpoint:
        from lionagi.service.resilience import RetryConfig

        config = EndpointConfig(
            name="test",
            provider="test",
            endpoint="v1/chat",
            base_url="https://api.test.com",
            auth_type="bearer",
            api_key="test-key",
        )
        return Endpoint(
            config=config,
            retry_config=RetryConfig(max_retries=max_retries, base_delay=0.01),
        )

    def _make_mock_session(self, responses):
        return _http_client_for(responses)

    def _make_ok_response(self, body: dict | None = None):
        return _http_response(body=body or {"ok": True})

    def _make_error_response(self, status: int):
        return _http_response(status, {"error": f"status {status}"})

    @pytest.mark.asyncio
    async def test_429_is_retried_with_default_retry_config(self):
        endpoint = self._make_endpoint(max_retries=3)
        r429 = self._make_error_response(429)
        ok = self._make_ok_response({"retried": True})

        attempt = 0

        def _make_new_session(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                return self._make_mock_session([r429])
            return self._make_mock_session([ok])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    result = await endpoint.call({}, skip_payload_creation=True)
        assert result == {"retried": True}
        assert attempt == 2

    @pytest.mark.asyncio
    async def test_500_is_retried_with_default_retry_config(self):
        endpoint = self._make_endpoint(max_retries=3)
        r500 = self._make_error_response(500)
        ok = self._make_ok_response({"retried": True})

        attempt = 0

        def _make_new_session(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                return self._make_mock_session([r500])
            return self._make_mock_session([ok])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    result = await endpoint.call({}, skip_payload_creation=True)
        assert result == {"retried": True}
        assert attempt == 2

    @pytest.mark.asyncio
    async def test_404_is_not_retried_with_default_retry_config(self):
        endpoint = self._make_endpoint(max_retries=3)
        r404 = self._make_error_response(404)

        call_count = 0

        def _make_new_session(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return self._make_mock_session([r404])

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with pytest.raises(APIClientError) as exc_info:
                    await endpoint.call({}, skip_payload_creation=True)
        assert exc_info.value.status_code == 404
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_asyncio_timeout_is_retried_with_default_retry_config(self):
        # asyncio.TimeoutError != builtin TimeoutError before Python 3.11;
        # the defaults must retry the asyncio class the same as the native
        # endpoint fallback does.
        import asyncio as _asyncio

        endpoint = self._make_endpoint(max_retries=2)

        call_count = 0

        async def _raise_timeout(request):
            nonlocal call_count
            call_count += 1
            raise _asyncio.TimeoutError()

        def create_client():
            return _http_client(_raise_timeout)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=create_client):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    with pytest.raises(_asyncio.TimeoutError):
                        await endpoint.call({}, skip_payload_creation=True)
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_connection_error_is_retried_with_default_retry_config(self):
        endpoint = self._make_endpoint(max_retries=2)

        call_count = 0

        async def _raise_connection_error(request):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("boom")

        def create_client():
            return _http_client(_raise_connection_error)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=create_client):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    with pytest.raises(ConnectionError):
                        await endpoint.call({}, skip_payload_creation=True)
        assert call_count == 3


# A retried POST to a non-idempotent creation endpoint must carry a stable
# Idempotency-Key reused across every attempt, so an ambiguous lost response
# cannot be replayed into a second billable job.


class TestIdempotencyKeyOnRetriedPosts:
    def _make_endpoint(self, *, idempotent_retries: bool, max_retries: int = 3) -> Endpoint:
        config = EndpointConfig(
            name="test",
            provider="test",
            endpoint="v1/images/generations",
            base_url="https://api.test.com",
            auth_type="bearer",
            api_key="test-key",
            method="POST",
            max_retries=max_retries,
            idempotent_retries=idempotent_retries,
        )
        return Endpoint(config=config)

    def _make_ok_response(self, body: dict | None = None):
        return _http_response(body=body or {"ok": True})

    def _make_error_response(self, status: int):
        return _http_response(status, {"error": f"status {status}"})

    @pytest.mark.asyncio
    async def test_same_idempotency_key_reused_across_retries(self):
        """A 500 on attempt 1 followed by a 200 on attempt 2 must reach the
        server with the SAME Idempotency-Key both times -- a fresh key per
        attempt would defeat server-side dedup of the ambiguous first try."""
        endpoint = self._make_endpoint(idempotent_retries=True, max_retries=3)
        sent_headers: list[httpx.Headers] = []

        attempt = 0

        def _make_new_session(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            resp = self._make_error_response(500) if attempt == 1 else self._make_ok_response()

            async def handler(request):
                sent_headers.append(request.headers.copy())
                return resp

            return _http_client(handler)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    await endpoint.call({"prompt": "a cat"})

        assert attempt == 2
        assert len(sent_headers) == 2
        assert "Idempotency-Key" in sent_headers[0]
        assert sent_headers[0]["Idempotency-Key"] == sent_headers[1]["Idempotency-Key"]

    @pytest.mark.asyncio
    async def test_two_separate_calls_get_different_keys(self):
        """Two distinct logical requests must not collide on the same key."""
        endpoint = self._make_endpoint(idempotent_retries=True, max_retries=1)
        sent_headers: list[httpx.Headers] = []

        def _make_new_session(*args, **kwargs):
            async def handler(request):
                sent_headers.append(request.headers.copy())
                return self._make_ok_response()

            return _http_client(handler)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                await endpoint.call({"prompt": "a cat"})
                await endpoint.call({"prompt": "a dog"})

        assert len(sent_headers) == 2
        assert sent_headers[0]["Idempotency-Key"] != sent_headers[1]["Idempotency-Key"]

    @pytest.mark.asyncio
    async def test_idempotency_key_absent_when_not_opted_in(self):
        endpoint = self._make_endpoint(idempotent_retries=False, max_retries=1)
        sent_headers: list[httpx.Headers] = []

        def _make_new_session(*args, **kwargs):
            async def handler(request):
                sent_headers.append(request.headers.copy())
                return self._make_ok_response()

            return _http_client(handler)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                await endpoint.call({"prompt": "a cat"})

        assert "Idempotency-Key" not in sent_headers[0]

    @pytest.mark.asyncio
    async def test_same_key_reused_across_retries_via_retry_config(self):
        """The opt-in RetryConfig path re-invokes ``self._call`` (and therefore
        ``_call_http``) fresh on every attempt from outside -- the key must
        be minted in ``call()``, before that wrapper is entered, or each
        attempt would silently mint its own."""
        from lionagi.service.resilience import RetryConfig

        config = EndpointConfig(
            name="test",
            provider="test",
            endpoint="v1/images/generations",
            base_url="https://api.test.com",
            auth_type="bearer",
            api_key="test-key",
            method="POST",
            idempotent_retries=True,
        )
        endpoint = Endpoint(config=config, retry_config=RetryConfig(max_retries=2, base_delay=0.01))
        sent_headers: list[httpx.Headers] = []
        attempt = 0

        def _make_new_session(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            resp = self._make_error_response(500) if attempt == 1 else self._make_ok_response()

            async def handler(request):
                sent_headers.append(request.headers.copy())
                return resp

            return _http_client(handler)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=_make_new_session):
                await endpoint.call({"prompt": "a cat"})

        assert attempt == 2
        assert len(sent_headers) == 2
        assert sent_headers[0]["Idempotency-Key"] == sent_headers[1]["Idempotency-Key"]


def _mock_session_returning(*responses):
    return _http_client_for(responses)


def _response_with_status(status: int):
    return _http_response(status, {"error": f"status {status}"})


def _session_per_attempt(status: int):
    # Every attempt gets a client of its own, as _create_http_session gives one.
    return lambda *args, **kwargs: _mock_session_returning(_response_with_status(status))


class TestThe4xxSentinelUnwrapLeavesAnAcyclicCause:
    """A 4xx answer is wrapped in a sentinel the retry layer excludes, then unwrapped.

    The unwrap must not re-raise the original from the sentinel's ``__cause__`` —
    which is that original — because the resulting self-cause makes a walk of
    ``__cause__``, the engine judge's among them, never reach an end.
    """

    def _make_endpoint(self, retry_config=None) -> Endpoint:
        config = EndpointConfig(
            name="test",
            provider="test",
            endpoint="v1/chat",
            base_url="https://api.test.com",
            auth_type="bearer",
            api_key="test-key",
            max_retries=3,
        )
        return Endpoint(config=config, retry_config=retry_config)

    @pytest.mark.asyncio
    async def test_the_native_path_reraise_is_not_its_own_cause(self):
        endpoint = self._make_endpoint()

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(
                endpoint, "_create_http_session", side_effect=_session_per_attempt(401)
            ):
                with pytest.raises(APIClientError) as info:
                    await endpoint._call(payload={"model": "m", "messages": []}, headers={})

        assert info.value.status_code == 401
        assert info.value.__cause__ is not info.value
        assert info.value.__cause__ is None

    @pytest.mark.asyncio
    async def test_the_retry_config_path_reraise_is_not_its_own_cause(self):
        from lionagi.service.resilience import RetryConfig

        endpoint = self._make_endpoint(RetryConfig(max_retries=3, base_delay=0.01))

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(
                endpoint, "_create_http_session", side_effect=_session_per_attempt(400)
            ):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    with pytest.raises(APIClientError) as info:
                        await endpoint.call({"messages": []})

        assert info.value.status_code == 400
        assert info.value.__cause__ is not info.value

    @pytest.mark.asyncio
    async def test_the_walk_from_the_unwrapped_error_ends(self):
        endpoint = self._make_endpoint()

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(
                endpoint, "_create_http_session", side_effect=_session_per_attempt(403)
            ):
                with pytest.raises(APIClientError) as info:
                    await endpoint._call(payload={"model": "m", "messages": []}, headers={})

        seen, cause = set(), info.value
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            assert cause.__cause__ is not cause
            cause = cause.__cause__
        assert len(seen) <= 8


_JSON = {"Content-Type": "application/json"}


def _raw_response(status: str, body: bytes = b"", headers: dict[str, str] | None = None) -> bytes:
    lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", "Connection: close"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class _LocalServer:
    """An HTTP/1.1 server on a loopback socket.

    ``respond(server, request_line)`` returns the raw response for each request; every
    request line the server read is kept in ``request_lines``.
    """

    def __init__(self, respond, ssl_context: ssl.SSLContext | None = None):
        self._respond = respond
        self._ssl_context = ssl_context
        self.request_lines: list[str] = []
        self.closing = asyncio.Event()

    async def __aenter__(self):
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self._ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info):
        self.closing.set()
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            length = 0
            for line in head[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value)
            await reader.readexactly(length)
            self.request_lines.append(head[0])
            writer.write(await self._respond(self, head[0]))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


def _local_endpoint(port: int, scheme: str = "http", **config) -> Endpoint:
    return Endpoint(
        config=EndpointConfig(
            name="test",
            provider="test",
            endpoint="v1/chat",
            base_url=f"{scheme}://127.0.0.1:{port}",
            auth_type="bearer",
            api_key="test-key",
            **{"max_retries": 1, **config},
        )
    )


def _ssl_context_of(client: httpx.AsyncClient) -> ssl.SSLContext:
    # httpx keeps the context on the transport's connection pool and has no public accessor.
    return client._transport._pool._ssl_context


class TestTransportFailuresAreRetried:
    @pytest.mark.asyncio
    async def test_a_connect_error_is_retried_on_the_native_path(self):
        endpoint = _local_endpoint(1, max_retries=2)
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"ok": True}, request=request)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(
                endpoint, "_create_http_session", side_effect=lambda *a, **k: _http_client(handler)
            ):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    result = await endpoint._call_http({}, {})

        assert result == {"ok": True}
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_a_connect_error_is_retried_with_default_retry_config(self):
        from lionagi.service.resilience import RetryConfig

        endpoint = _local_endpoint(1)
        endpoint.retry_config = RetryConfig(max_retries=2, base_delay=0.01)
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("connection refused", request=request)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(
                endpoint, "_create_http_session", side_effect=lambda *a, **k: _http_client(handler)
            ):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    with pytest.raises(httpx.ConnectError):
                        await endpoint.call({}, skip_payload_creation=True)

        assert attempts == 3

    @pytest.mark.asyncio
    async def test_a_200_whose_body_is_not_json_is_not_retried(self):
        endpoint = _local_endpoint(1, max_retries=3)
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200, text="<html></html>", headers={"Content-Type": "text/html"}, request=request
            )

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(
                endpoint, "_create_http_session", side_effect=lambda *a, **k: _http_client(handler)
            ):
                with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
                    with pytest.raises(json.JSONDecodeError):
                        await endpoint._call_http({}, {})

        assert attempts == 1


class TestRequestsOverALoopbackSocket:
    @staticmethod
    async def _redirect_once(server, line):
        if line.startswith("POST /v1/chat "):
            return _raw_response("302 Found", headers={"Location": "/v1/final"})
        return _raw_response("200 OK", b'{"ok": true}', _JSON)

    @pytest.mark.asyncio
    async def test_a_redirect_is_followed(self):
        async with _LocalServer(self._redirect_once) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                result = await _local_endpoint(server.port)._call_http({}, {})

        assert result == {"ok": True}
        assert server.request_lines == ["POST /v1/chat HTTP/1.1", "GET /v1/final HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_allow_redirects_false_answers_with_the_redirect(self):
        async with _LocalServer(self._redirect_once) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                with pytest.raises(APIClientError) as info:
                    await _local_endpoint(server.port)._call_http({}, {}, allow_redirects=False)

        assert info.value.status_code == 302
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_request_proxy_carries_the_request_and_the_environment_proxy_does_not(
        self, monkeypatch
    ):
        async def direct(server, line):
            return _raw_response("200 OK", b'{"via": "direct"}', _JSON)

        async def forward(server, line):
            return _raw_response("200 OK", b'{"via": "proxy"}', _JSON)

        async with _LocalServer(direct) as target, _LocalServer(forward) as proxy:
            proxy_url = f"http://127.0.0.1:{proxy.port}"
            for name in ("NO_PROXY", "no_proxy"):
                monkeypatch.delenv(name, raising=False)
            for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                monkeypatch.setenv(name, proxy_url)
            endpoint = _local_endpoint(target.port)
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                from_environment = await endpoint._call_http({}, {})
                from_request = await endpoint._call_http({}, {}, proxy=proxy_url)

        assert from_environment == {"via": "direct"}
        assert from_request == {"via": "proxy"}
        assert target.request_lines == ["POST /v1/chat HTTP/1.1"]
        assert proxy.request_lines == [f"POST http://127.0.0.1:{target.port}/v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_stalled_read_raises_read_timeout(self):
        async def stall(server, line):
            # Bounded, so a client that never times out fails the test instead of hanging it.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.closing.wait(), 10)
            return _raw_response("200 OK", b"{}", _JSON)

        async with _LocalServer(stall) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                started = time.monotonic()
                with pytest.raises(httpx.ReadTimeout) as info:
                    await _local_endpoint(server.port, timeout=1)._call_http({}, {})
                elapsed = time.monotonic() - started

        # The configured second, well short of httpx's own five.
        assert elapsed < 4
        assert not isinstance(info.value, TimeoutError)
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [b"", b" \r\n"])
    async def test_an_empty_body_reads_as_none(self, body):
        async def empty(server, line):
            return _raw_response("200 OK", body, _JSON)

        async with _LocalServer(empty) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                result = await _local_endpoint(server.port)._call_http({}, {})

        assert result is None
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]


class TestRequestClientOptions:
    @pytest.mark.asyncio
    async def test_ssl_false_gives_a_client_that_does_not_verify(self):
        client, rest = _local_endpoint(1)._client_for_request({"ssl": False, "params": {"q": "1"}})
        try:
            assert rest == {"params": {"q": "1"}}
            assert _ssl_context_of(client).verify_mode == ssl.CERT_NONE
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_an_ssl_context_is_the_client_context(self):
        context = ssl.create_default_context()
        client, rest = _local_endpoint(1)._client_for_request({"ssl": context})
        try:
            assert rest == {}
            assert _ssl_context_of(client) is context
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_ssl_true_and_proxy_none_keep_the_default_client(self):
        from lionagi.service.connections import endpoint as endpoint_module

        client, rest = _local_endpoint(1)._client_for_request({"ssl": True, "proxy": None})
        try:
            assert rest == {}
            assert _ssl_context_of(client) is endpoint_module._verified_ssl_context()
        finally:
            await client.aclose()

    def test_ssl_of_another_type_is_refused(self):
        with pytest.raises(TypeError, match="ssl takes True, False, None, an ssl.SSLContext"):
            _local_endpoint(1)._client_for_request({"ssl": object()})

    @pytest.mark.asyncio
    async def test_allow_redirects_becomes_follow_redirects(self):
        client, rest = _local_endpoint(1)._client_for_request({"allow_redirects": False})
        await client.aclose()
        assert rest == {"follow_redirects": False}

        with pytest.raises(TypeError, match="not both"):
            _local_endpoint(1)._client_for_request(
                {"allow_redirects": False, "follow_redirects": True}
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="Windows also loads its system store")
    async def test_the_default_client_trusts_ssl_cert_file(self, monkeypatch, tmp_path):
        import certifi

        from lionagi.service.connections import endpoint as endpoint_module

        bundle = Path(certifi.where()).read_text(encoding="utf-8")
        begin = bundle.index("-----BEGIN CERTIFICATE-----")
        end = bundle.index("-----END CERTIFICATE-----", begin) + len("-----END CERTIFICATE-----")
        one_root = tmp_path / "one-root.pem"
        one_root.write_text(bundle[begin:end] + "\n", encoding="ascii")
        monkeypatch.setenv("SSL_CERT_FILE", str(one_root))
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        monkeypatch.setattr(endpoint_module, "_VERIFIED_SSL_CONTEXT", None)

        client = _local_endpoint(1)._create_http_session()
        try:
            assert len(_ssl_context_of(client).get_ca_certs()) == 1
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, None])
    async def test_ssl_true_or_none_verifies_under_a_client_that_does_not(self, value):
        from lionagi.service.connections import endpoint as endpoint_module

        endpoint = _local_endpoint(1, client_kwargs={"verify": False})
        default = endpoint._create_http_session()
        client, rest = endpoint._client_for_request({"ssl": value})
        try:
            assert _ssl_context_of(default).verify_mode == ssl.CERT_NONE
            assert rest == {}
            assert _ssl_context_of(client) is endpoint_module._verified_ssl_context()
        finally:
            await default.aclose()
            await client.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, None])
    async def test_ssl_true_or_none_keeps_a_configured_context(self, value):
        context = ssl.create_default_context()
        # A dict config: an EndpointConfig is deep-copied, and an SSLContext cannot be.
        endpoint = Endpoint(
            config={
                "name": "test",
                "provider": "test",
                "endpoint": "v1/chat",
                "base_url": "https://127.0.0.1:1",
                "auth_type": "bearer",
                "api_key": "test-key",
                "client_kwargs": {"verify": context},
            }
        )
        client, rest = endpoint._client_for_request({"ssl": value})
        try:
            assert rest == {}
            assert _ssl_context_of(client) is context
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_ssl_none_keeps_the_default_client(self):
        from lionagi.service.connections import endpoint as endpoint_module

        client, rest = _local_endpoint(1)._client_for_request({"ssl": None})
        try:
            assert rest == {}
            assert _ssl_context_of(client) is endpoint_module._verified_ssl_context()
        finally:
            await client.aclose()


def _self_signed_server_context(tmp_path) -> tuple[ssl.SSLContext, bytes]:
    """A server context with a fresh self-signed certificate for 127.0.0.1, and that
    certificate's DER bytes."""
    import datetime
    import ipaddress

    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "server-cert.pem"
    key_file = tmp_path / "server-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    return context, cert.public_bytes(serialization.Encoding.DER)


async def _ok(server, line):
    return _raw_response("200 OK", b'{"ok": true}', _JSON)


class TestAiohttpRequestValues:
    """The request values aiohttp took and httpx takes in another form, or not at all."""

    @pytest.mark.asyncio
    async def test_a_fingerprint_pins_the_server_certificate(self, tmp_path):
        aiohttp = pytest.importorskip("aiohttp")
        import hashlib

        context, der = _self_signed_server_context(tmp_path)
        pin = aiohttp.Fingerprint(hashlib.sha256(der).digest())
        async with _LocalServer(_ok, context) as server:
            endpoint = _local_endpoint(server.port, scheme="https")
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                # The certificate is self-signed, so the default client refuses it.
                with pytest.raises(httpx.ConnectError):
                    await endpoint._call_http({}, {})
                result = await endpoint._call_http({}, {}, ssl=pin)

        assert result == {"ok": True}
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_fingerprint_mismatch_fails_before_the_request_is_sent(self, tmp_path):
        aiohttp = pytest.importorskip("aiohttp")

        context, _ = _self_signed_server_context(tmp_path)
        async with _LocalServer(_ok, context) as server:
            endpoint = _local_endpoint(server.port, scheme="https")
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                with pytest.raises(httpx.ConnectError, match="fingerprint"):
                    await endpoint._call_http({}, {}, ssl=aiohttp.Fingerprint(b"\0" * 32))

        assert server.request_lines == []

    @pytest.mark.asyncio
    async def test_a_fingerprint_pins_a_stream(self, tmp_path):
        aiohttp = pytest.importorskip("aiohttp")
        import hashlib

        async def lines(server, line):
            return _raw_response("200 OK", b"pinned\n", {"Content-Type": "text/event-stream"})

        context, der = _self_signed_server_context(tmp_path)
        async with _LocalServer(lines, context) as server:
            endpoint = _local_endpoint(server.port, scheme="https")
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                pin = aiohttp.Fingerprint(hashlib.sha256(der).digest())
                chunks = [c async for c in endpoint._stream_http({}, {}, ssl=pin)]
                with pytest.raises(httpx.ConnectError, match="fingerprint"):
                    wrong = aiohttp.Fingerprint(b"\0" * 32)
                    async for _ in endpoint._stream_http({}, {}, ssl=wrong):
                        pass

        assert [c.content for c in chunks] == ["pinned"]
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_pinned_request_is_not_redirected_off_https(self, tmp_path):
        aiohttp = pytest.importorskip("aiohttp")
        import hashlib

        context, der = _self_signed_server_context(tmp_path)
        pin = aiohttp.Fingerprint(hashlib.sha256(der).digest())
        async with _LocalServer(_ok) as plain:

            async def downgrade(server, line):
                location = f"http://127.0.0.1:{plain.port}/v1/final"
                return _raw_response("307 Temporary Redirect", headers={"Location": location})

            async with _LocalServer(downgrade, context) as server:
                # Retries allowed: the refusal is not one, so the pinned server is asked once.
                endpoint = _local_endpoint(server.port, scheme="https", max_retries=3)
                with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                    with pytest.raises(httpx.ConnectError, match="redirect from https to http"):
                        await endpoint._call_http({}, {}, ssl=pin)

        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]
        assert plain.request_lines == []

    @pytest.mark.asyncio
    async def test_a_pinned_request_is_not_redirected_off_https_after_starting_on_http(
        self, tmp_path
    ):
        aiohttp = pytest.importorskip("aiohttp")
        import hashlib

        context, der = _self_signed_server_context(tmp_path)
        pin = aiohttp.Fingerprint(hashlib.sha256(der).digest())
        pinned_port = None

        async def plain_hops(server, line):
            if "/v1/final" in line:
                return await _ok(server, line)
            location = f"https://127.0.0.1:{pinned_port}/v1/chat"
            return _raw_response("307 Temporary Redirect", headers={"Location": location})

        async with _LocalServer(plain_hops) as plain:

            async def downgrade(server, line):
                location = f"http://127.0.0.1:{plain.port}/v1/final"
                return _raw_response("307 Temporary Redirect", headers={"Location": location})

            async with _LocalServer(downgrade, context) as pinned:
                pinned_port = pinned.port
                with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                    with pytest.raises(httpx.ConnectError, match="redirect from https to http"):
                        await _local_endpoint(plain.port)._call_http({}, {}, ssl=pin)

        assert plain.request_lines == ["POST /v1/chat HTTP/1.1"]
        assert pinned.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_pinned_stream_is_not_redirected_off_https(self, tmp_path):
        aiohttp = pytest.importorskip("aiohttp")
        import hashlib

        context, der = _self_signed_server_context(tmp_path)
        pin = aiohttp.Fingerprint(hashlib.sha256(der).digest())
        async with _LocalServer(_ok) as plain:

            async def downgrade(server, line):
                location = f"http://127.0.0.1:{plain.port}/v1/final"
                return _raw_response("307 Temporary Redirect", headers={"Location": location})

            async with _LocalServer(downgrade, context) as server:
                endpoint = _local_endpoint(server.port, scheme="https")
                with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                    with pytest.raises(httpx.ConnectError, match="redirect from https to http"):
                        async for _ in endpoint._stream_http({}, {}, ssl=pin):
                            pass

        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]
        assert plain.request_lines == []

    @pytest.mark.asyncio
    async def test_a_pinned_request_follows_a_redirect_that_stays_on_https(self, tmp_path):
        aiohttp = pytest.importorskip("aiohttp")
        import hashlib

        context, der = _self_signed_server_context(tmp_path)
        pin = aiohttp.Fingerprint(hashlib.sha256(der).digest())
        async with _LocalServer(TestRequestsOverALoopbackSocket._redirect_once, context) as server:
            endpoint = _local_endpoint(server.port, scheme="https")
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                result = await endpoint._call_http({}, {}, ssl=pin)

        assert result == {"ok": True}
        assert server.request_lines == ["POST /v1/chat HTTP/1.1", "GET /v1/final HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_pin_leaves_a_plain_http_request_alone(self):
        aiohttp = pytest.importorskip("aiohttp")

        async with _LocalServer(_ok) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                result = await _local_endpoint(server.port)._call_http(
                    {}, {}, ssl=aiohttp.Fingerprint(b"\0" * 32)
                )

        assert result == {"ok": True}
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_pin_keeps_the_configured_request_hooks(self):
        aiohttp = pytest.importorskip("aiohttp")

        async def record(request):
            pass

        endpoint = _local_endpoint(1, client_kwargs={"event_hooks": {"request": [record]}})
        client, _ = endpoint._client_for_request({"ssl": aiohttp.Fingerprint(b"\0" * 32)})
        try:
            hooks = client.event_hooks["request"]
            assert len(hooks) == 2
            assert hooks[0] is record
        finally:
            await client.aclose()
        assert endpoint.config.client_kwargs["event_hooks"]["request"] == [record]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"total": 10, "sock_read": 2}, (10, 2, 10, 10)),
            ({"total": 5, "connect": 30, "sock_connect": 2}, (2, 5, 5, 5)),
            ({"connect": 2, "sock_connect": 30}, (2, None, None, 2)),
            ({"connect": 3}, (3, None, None, 3)),
            ({}, (None, None, None, None)),
            ({"total": 2, "connect": 10, "sock_connect": 10, "sock_read": 10}, (2, 2, 2, 2)),
            ({"total": 0}, (None, None, None, None)),
            ({"total": 2, "sock_read": 0}, (2, 2, 2, 2)),
            ({"total": 0, "sock_read": 3, "connect": -1}, (None, 3, None, None)),
        ],
    )
    def test_a_client_timeout_becomes_an_httpx_timeout(self, fields, expected):
        aiohttp = pytest.importorskip("aiohttp")

        _, rest = _local_endpoint(1)._client_for_request(
            {"timeout": aiohttp.ClientTimeout(**fields)}
        )
        timeout = rest["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == expected

    @pytest.mark.parametrize("sock_read", [-1, -0.5])
    def test_a_negative_sock_read_is_refused(self, sock_read):
        aiohttp = pytest.importorskip("aiohttp")

        with pytest.raises(ValueError, match="sock_read"):
            _local_endpoint(1)._client_for_request(
                {"timeout": aiohttp.ClientTimeout(total=5, sock_read=sock_read)}
            )

    @pytest.mark.asyncio
    async def test_a_client_timeout_bounds_a_stalled_read(self):
        aiohttp = pytest.importorskip("aiohttp")

        async def stall(server, line):
            # Bounded, so a client that never times out fails the test instead of hanging it.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.closing.wait(), 10)
            return _raw_response("200 OK", b"{}", _JSON)

        async with _LocalServer(stall) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                started = time.monotonic()
                with pytest.raises(httpx.ReadTimeout):
                    await _local_endpoint(server.port)._call_http(
                        {}, {}, timeout=aiohttp.ClientTimeout(total=1)
                    )
                elapsed = time.monotonic() - started

        assert elapsed < 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "where", ["request", "float", "client timeout", "config", "client kwargs"]
    )
    async def test_a_zero_timeout_is_no_timeout(self, where):
        aiohttp = pytest.importorskip("aiohttp")

        async def late(server, line):
            await asyncio.sleep(0.3)
            return _raw_response("200 OK", b'{"ok": true}', _JSON)

        options = {
            "request": {"timeout": 0},
            "float": {"timeout": 0.0},
            "client timeout": {"timeout": aiohttp.ClientTimeout(total=0)},
            "config": {},
            "client kwargs": {},
        }[where]
        config = {
            "config": {"timeout": 0},
            "client kwargs": {"client_kwargs": {"timeout": 0}},
        }.get(where, {})
        async with _LocalServer(late) as server:
            endpoint = _local_endpoint(server.port, **config)
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                result = await endpoint._call_http({}, {}, **options)

        assert result == {"ok": True}
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_an_empty_request_proxy_goes_direct(self):
        async with _LocalServer(_ok) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                result = await _local_endpoint(server.port)._call_http({}, {}, proxy="")

        assert result == {"ok": True}
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]


class TestStreamRequestOptions:
    """A stream takes the request options a call takes."""

    @pytest.mark.asyncio
    async def test_allow_redirects_false_answers_a_stream_with_the_redirect(self):
        async with _LocalServer(TestRequestsOverALoopbackSocket._redirect_once) as server:
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                with pytest.raises(APIClientError) as info:
                    async for _ in _local_endpoint(server.port)._stream_http(
                        {}, {}, allow_redirects=False
                    ):
                        pass

        assert info.value.status_code == 302
        assert server.request_lines == ["POST /v1/chat HTTP/1.1"]

    @pytest.mark.asyncio
    async def test_a_stream_goes_through_a_request_proxy_and_direct_for_an_empty_one(self):
        async def direct(server, line):
            return _raw_response("200 OK", b"direct\n", {"Content-Type": "text/event-stream"})

        async def forward(server, line):
            return _raw_response("200 OK", b"proxy\n", {"Content-Type": "text/event-stream"})

        async with _LocalServer(direct) as target, _LocalServer(forward) as proxy:
            endpoint = _local_endpoint(target.port)
            with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
                proxied = [
                    c.content
                    async for c in endpoint._stream_http(
                        {}, {}, proxy=f"http://127.0.0.1:{proxy.port}"
                    )
                ]
                empty = [c.content async for c in endpoint._stream_http({}, {}, proxy="")]

        assert proxied == ["proxy"]
        assert empty == ["direct"]
        assert proxy.request_lines == [f"POST http://127.0.0.1:{target.port}/v1/chat HTTP/1.1"]
        assert target.request_lines == ["POST /v1/chat HTTP/1.1"]
