# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig


class TestAPICalling:
    @pytest.fixture
    def sample_payload(self):
        return {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
        }

    @pytest.fixture
    def sample_headers(self):
        return {
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        }

    @pytest.fixture
    def mock_endpoint(self):
        config = EndpointConfig(
            name="openai_chat",
            endpoint="chat",
            provider="openai",
            base_url="https://api.openai.com/v1",
            endpoint_params=["chat", "completions"],
            openai_compatible=True,
        )
        return Endpoint(config=config)

    def test_api_calling_initialization(self, sample_payload, sample_headers, mock_endpoint):
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        assert api_call.payload == sample_payload
        assert api_call.headers == sample_headers
        assert api_call.endpoint == mock_endpoint
        assert api_call.status == EventStatus.PENDING
        assert api_call.execution is not None
        assert api_call.execution.status == EventStatus.PENDING
        assert api_call.response is None

    def test_response_property_before_execution(
        self, sample_payload, sample_headers, mock_endpoint
    ):
        """Test response property returns None before execution."""
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        assert api_call.response is None

    def test_response_property_after_execution(self, sample_payload, sample_headers, mock_endpoint):
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        # Mock execution with response
        mock_execution = MagicMock()
        mock_execution.response = {"test": "response"}
        api_call.execution = mock_execution

        assert api_call.response == {"test": "response"}

    @pytest.mark.asyncio
    async def test_successful_execution(
        self, sample_payload, sample_headers, mock_endpoint, mock_response
    ):
        """Test successful API call execution."""
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        with patch.object(mock_endpoint, "call", return_value=mock_response.json.return_value):
            await api_call.invoke()

        assert api_call.status == EventStatus.COMPLETED
        assert api_call.execution is not None
        assert api_call.response is not None

    @pytest.mark.asyncio
    async def test_execution_error_handling(self, sample_payload, sample_headers, mock_endpoint):
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        # invoke() is total: the error is recorded on execution, not re-raised.
        with patch.object(mock_endpoint, "call", side_effect=Exception("API Error")):
            await api_call.invoke()

        assert api_call.status == EventStatus.FAILED
        assert api_call.execution is not None
        assert "API Error" in str(api_call.execution.error)

    @pytest.mark.asyncio
    async def test_timeout_handling(self, sample_payload, sample_headers, mock_endpoint):
        """Test timeout handling during execution — captured as FAILED state (a timeout
        is a business failure, not cancellation)."""
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        with patch.object(
            mock_endpoint,
            "call",
            side_effect=asyncio.TimeoutError("Request timed out"),
        ):
            await api_call.invoke()

        assert api_call.status == EventStatus.FAILED

    @pytest.mark.asyncio
    async def test_streaming_execution(self, sample_payload, sample_headers, mock_endpoint):
        sample_payload["stream"] = True
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        async def mock_stream():
            yield {"choices": [{"delta": {"content": "Hello"}}]}
            yield {"choices": [{"delta": {"content": " world"}}]}
            yield {"choices": [{"delta": {}}]}  # End of stream

        with patch.object(mock_endpoint, "stream", return_value=mock_stream()):
            chunks = []
            async for chunk in api_call.stream():
                chunks.append(chunk)

        assert len(chunks) >= 2
        assert api_call.status == EventStatus.COMPLETED

    def test_cache_control_handling(self, sample_payload, sample_headers, mock_endpoint):
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
            cache_control=True,
        )

        assert api_call.cache_control is True

    @pytest.mark.asyncio
    async def test_concurrent_execution_isolation(self, sample_headers, mock_endpoint):
        payloads = [
            {
                "model": "gpt-4.1-mini",
                "messages": [{"role": "user", "content": f"Message {i}"}],
            }
            for i in range(3)
        ]

        api_calls = [
            APICalling(payload=payload, headers=sample_headers, endpoint=mock_endpoint)
            for payload in payloads
        ]

        responses = [{"response": f"Response {i}"} for i in range(3)]

        async def mock_request(request, cache_control=False, **kwargs):
            # Simulate different response times
            i = int(request["messages"][0]["content"][-1])
            await asyncio.sleep(0.1 * (i + 1))
            return responses[i]

        with patch.object(mock_endpoint, "call", side_effect=mock_request):
            tasks = [api_call.invoke() for api_call in api_calls]
            await asyncio.gather(*tasks)

        # Verify each call got the correct response
        for i, api_call in enumerate(api_calls):
            assert api_call.status == EventStatus.COMPLETED
            assert api_call.response == responses[i]

    @pytest.mark.asyncio
    async def test_error_propagation(self, sample_payload, sample_headers, mock_endpoint):
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        original_error = ValueError("Custom API error")

        # invoke() is total: the error is recorded on execution, not swallowed and
        # not re-raised. The caller inspects status / execution.error.
        with patch.object(mock_endpoint, "call", side_effect=original_error):
            await api_call.invoke()

        assert api_call.status == EventStatus.FAILED
        assert api_call.execution.error is not None
        assert "Custom API error" in str(api_call.execution.error)

    def test_include_token_usage_to_model(self, sample_payload, sample_headers, mock_endpoint):
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
            include_token_usage_to_model=True,
        )

        assert api_call.include_token_usage_to_model is True

    @pytest.mark.asyncio
    async def test_retry_logic(self, sample_payload, sample_headers, mock_endpoint):
        responses = []

        # First call fails
        api_call1 = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        with patch.object(
            mock_endpoint,
            "call",
            side_effect=ConnectionError("Transient error"),
        ):
            await api_call1.invoke()  # total: captured as FAILED, not raised

        assert api_call1.status == EventStatus.FAILED

        # Second call succeeds
        api_call2 = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        with patch.object(mock_endpoint, "call", return_value={"success": True}):
            await api_call2.invoke()

        assert api_call2.status == EventStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_payload_immutability(self, sample_payload, sample_headers, mock_endpoint):
        original_payload = sample_payload.copy()
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        with patch.object(mock_endpoint, "call", return_value={"test": "response"}):
            await api_call.invoke()

        # Verify payload wasn't mutated
        assert api_call.payload == original_payload

    def test_str_representation(self, sample_payload, sample_headers, mock_endpoint):
        api_call = APICalling(
            payload=sample_payload,
            headers=sample_headers,
            endpoint=mock_endpoint,
        )

        str_repr = str(api_call)
        # APICalling inherits from Event, check that basic representation works
        assert "id=" in str_repr
        assert "payload=" in str_repr
        assert api_call.status == EventStatus.PENDING


class TestTokenUsageContentInjection:
    """Cover lines 89-97 — token-usage message injected into last message content."""

    @pytest.fixture
    def token_endpoint(self):
        config = EndpointConfig(
            name="openai_token",
            endpoint="chat/completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            requires_tokens=True,
        )
        return Endpoint(config=config)

    def _make_api_call(self, content, token_endpoint):
        from lionagi.service.token_calculator import TokenCalculator

        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": content}],
        }
        with patch.object(TokenCalculator, "calculate_message_tokens", return_value=99):
            return APICalling(
                payload=payload,
                endpoint=token_endpoint,
                include_token_usage_to_model=True,
            )

    def test_string_content_gets_token_msg_appended(self, token_endpoint):
        api_call = self._make_api_call("Hello", token_endpoint)
        result_content = api_call.payload["messages"][-1]["content"]
        assert "Estimated Current Token Usage" in result_content
        assert "99" in result_content

    def test_dict_content_with_text_key_gets_appended(self, token_endpoint):
        api_call = self._make_api_call({"type": "text", "text": "Hello"}, token_endpoint)
        result_content = api_call.payload["messages"][-1]["content"]
        assert isinstance(result_content, dict)
        assert "Estimated Current Token Usage" in result_content["text"]

    def test_list_content_finds_last_text_item(self, token_endpoint):
        content = [
            {"type": "text", "text": "First"},
            {"type": "image_url", "image_url": "..."},
            {"type": "text", "text": "Last"},
        ]
        api_call = self._make_api_call(content, token_endpoint)
        result_content = api_call.payload["messages"][-1]["content"]
        # Last text item (reversed search) should have the token message
        assert "Estimated Current Token Usage" in result_content[-1]["text"]
        # Earlier text item is unchanged
        assert "Estimated Current Token Usage" not in result_content[0]["text"]

    def test_model_in_token_limit_map_appends_limit(self, token_endpoint):
        from lionagi.service.token_calculator import TokenCalculator

        payload = {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
        }
        with patch.object(TokenCalculator, "calculate_message_tokens", return_value=10):
            api_call = APICalling(
                payload=payload,
                endpoint=token_endpoint,
                include_token_usage_to_model=True,
            )
        content = api_call.payload["messages"][-1]["content"]
        # gpt-4 prefix matches → limit appended
        assert "/" in content


class TestRequiredTokensProperty:
    """Cover lines 117-139 — required_tokens with 'input' and 'embed' formats."""

    @pytest.fixture
    def token_endpoint(self):
        config = EndpointConfig(
            name="oai_responses",
            endpoint="responses",
            provider="openai",
            base_url="https://api.openai.com/v1",
            requires_tokens=True,
        )
        return Endpoint(config=config)

    @pytest.fixture
    def embed_endpoint(self):
        config = EndpointConfig(
            name="oai_embed",
            endpoint="embeddings",
            provider="openai",
            base_url="https://api.openai.com/v1",
            requires_tokens=True,
        )
        return Endpoint(config=config)

    @pytest.fixture
    def no_token_endpoint(self):
        config = EndpointConfig(
            name="simple",
            endpoint="completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            requires_tokens=False,
        )
        return Endpoint(config=config)

    def test_required_tokens_none_when_requires_tokens_false(self, no_token_endpoint):
        api_call = APICalling(
            payload={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            endpoint=no_token_endpoint,
        )
        assert api_call.required_tokens is None

    def test_required_tokens_input_string_format(self, token_endpoint):
        from lionagi.service.token_calculator import TokenCalculator

        api_call = APICalling(
            payload={"input": "hello world", "model": "gpt-4"},
            endpoint=token_endpoint,
        )
        with patch.object(TokenCalculator, "calculate_message_tokens", return_value=7) as mock_calc:
            count = api_call.required_tokens
        assert count == 7
        mock_calc.assert_called_once()
        # The messages passed should have a user message with the string content
        call_args = mock_calc.call_args[0][0]
        assert call_args[0]["role"] == "user"
        assert call_args[0]["content"] == "hello world"

    def test_required_tokens_input_list_with_strings(self, token_endpoint):
        from lionagi.service.token_calculator import TokenCalculator

        api_call = APICalling(
            payload={"input": ["hello", "world"], "model": "gpt-4"},
            endpoint=token_endpoint,
        )
        with patch.object(TokenCalculator, "calculate_message_tokens", return_value=5) as mock_calc:
            count = api_call.required_tokens
        assert count == 5
        call_args = mock_calc.call_args[0][0]
        assert len(call_args) == 2

    def test_required_tokens_input_list_with_message_dicts(self, token_endpoint):
        from lionagi.service.token_calculator import TokenCalculator

        msg = {"type": "message", "role": "user", "content": "Hello"}
        api_call = APICalling(
            payload={"input": [msg], "model": "gpt-4"},
            endpoint=token_endpoint,
        )
        with patch.object(TokenCalculator, "calculate_message_tokens", return_value=4) as mock_calc:
            count = api_call.required_tokens
        assert count == 4

    def test_required_tokens_input_non_string_non_list_returns_none(self, token_endpoint):
        api_call = APICalling(
            payload={"input": 12345, "model": "gpt-4"},
            endpoint=token_endpoint,
        )
        assert api_call.required_tokens is None

    def test_required_tokens_embed_endpoint(self, embed_endpoint):
        """Embed endpoint calls calculate_embed_token (lines 136-137).

        The embed branch is reached when neither 'messages' nor 'input' keys
        are present in the payload, but the endpoint name contains 'embed'.
        """
        from lionagi.service.token_calculator import TokenCalculator

        # Payload without "messages" or "input" keys triggers the embed branch
        api_call = APICalling(
            payload={"texts": ["text1", "text2"], "model": "text-embedding-3-small"},
            endpoint=embed_endpoint,
        )
        with patch.object(TokenCalculator, "calculate_embed_token", return_value=8) as mock_calc:
            count = api_call.required_tokens
        assert count == 8
        mock_calc.assert_called_once()
