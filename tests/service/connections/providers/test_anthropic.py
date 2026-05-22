# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import os
from unittest.mock import patch

import pytest

from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.imodel import iModel


class TestAnthropicIntegration:
    """Integration tests for Anthropic endpoint."""

    @pytest.fixture
    def anthropic_imodel(self):
        """Create an iModel instance for Anthropic."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
            return iModel(provider="anthropic", model="claude-3-opus-20240229")

    def test_anthropic_endpoint_configuration(self, anthropic_imodel):
        """Test that Anthropic endpoint is configured correctly."""
        assert anthropic_imodel.endpoint.config.provider == "anthropic"
        assert anthropic_imodel.endpoint.config.openai_compatible is False
        if anthropic_imodel.endpoint.config.endpoint_params:
            assert "messages" in anthropic_imodel.endpoint.config.endpoint_params
        assert anthropic_imodel.endpoint.config.default_headers["anthropic-version"] == "2023-06-01"

    def test_anthropic_headers_creation(self, anthropic_imodel):
        """Test that Anthropic headers are created correctly."""
        payload, headers = anthropic_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "claude-3-opus-20240229",
                "max_tokens": 100,
                "api_key": "test-key",
            }
        )

        assert "x-api-key" in headers
        # API key should be present but may not match input exactly
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["Content-Type"] == "application/json"
        assert "api_key" not in payload  # Should be removed from payload

    def test_anthropic_payload_validation(self, anthropic_imodel):
        """Test Anthropic payload validation with Pydantic models."""
        # Valid payload should work
        payload, headers = anthropic_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "claude-3-opus-20240229",
                "max_tokens": 100,
            }
        )

        assert payload["model"] == "claude-3-opus-20240229"
        assert payload["messages"][0]["content"] == "Hello"
        assert payload["max_tokens"] == 100

    def test_anthropic_message_format(self, anthropic_imodel):
        """Test Anthropic message format requirements."""
        # Test with system message
        payload, _ = anthropic_imodel.endpoint.create_payload(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a helpful assistant.",
                    },
                    {"role": "user", "content": "Hello"},
                ],
                "model": "claude-3-opus-20240229",
                "max_tokens": 100,
            }
        )

        # Anthropic extracts system messages to a separate field
        assert "system" in payload
        assert len(payload["messages"]) == 1  # System message removed from messages
        assert payload["messages"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_anthropic_api_calling_creation(self, anthropic_imodel, mock_anthropic_response):
        """Test creating APICalling for Anthropic."""
        api_call = anthropic_imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello, Claude!"}],
            max_tokens=100,
            temperature=0.7,
        )

        assert api_call.payload["model"] == "claude-3-opus-20240229"
        assert api_call.payload["messages"][0]["content"] == "Hello, Claude!"
        assert api_call.payload["max_tokens"] == 100
        assert api_call.payload["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_anthropic_successful_invoke(self, anthropic_imodel, mock_anthropic_response):
        """Test successful Anthropic API invocation."""
        with patch.object(
            anthropic_imodel.endpoint,
            "call",
            return_value=mock_anthropic_response.json.return_value,
        ):
            result = await anthropic_imodel.invoke(
                messages=[{"role": "user", "content": "Hello, Claude!"}],
                max_tokens=100,
            )

        assert result is not None
        assert result.response["role"] == "assistant"
        assert "Test Anthropic response" in result.response["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_anthropic_streaming(self, anthropic_imodel):
        """Test Anthropic streaming responses."""
        # Set a streaming_process_func that returns the chunk
        anthropic_imodel.streaming_process_func = lambda chunk: chunk

        async def mock_anthropic_stream():
            chunks = [
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": " world"},
                },
                {"type": "message_stop"},
            ]
            for chunk in chunks:
                yield chunk

        with patch.object(
            anthropic_imodel.endpoint,
            "stream",
            return_value=mock_anthropic_stream(),
        ):
            chunks = []
            async for chunk in anthropic_imodel.stream(
                messages=[{"role": "user", "content": "Hello"}], max_tokens=100
            ):
                # Check if chunk is a dict with 'type' key instead of an object with 'type' attribute
                if isinstance(chunk, dict) and chunk.get("type") == "content_block_delta":
                    chunks.append(chunk)

        assert len(chunks) >= 2

    def test_anthropic_url_construction(self):
        """Test Anthropic URL construction."""
        endpoint = match_endpoint(
            provider="anthropic",
            endpoint="chat",
            model="claude-3-opus-20240229",
        )

        url = endpoint.config.full_url
        assert "api.anthropic.com" in url

    def test_anthropic_model_validation(self, anthropic_imodel):
        """Test that Anthropic models are validated correctly."""
        valid_models = [
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307",
        ]

        for model in valid_models:
            payload, _ = anthropic_imodel.endpoint.create_payload(
                {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "model": model,
                    "max_tokens": 100,
                }
            )
            assert payload["model"] == model

    def test_anthropic_error_handling(self, anthropic_imodel):
        """Test Anthropic-specific error handling."""
        # Test with missing max_tokens (required for Anthropic)
        with pytest.raises(
            ValueError
        ):  # endpoint wraps pydantic ValidationError as ValueError("Invalid payload")
            anthropic_imodel.endpoint.create_payload(
                {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "model": "claude-3-opus-20240229",
                    # Missing max_tokens
                }
            )

    def test_anthropic_reasoning_models(self):
        """Test configuration for Anthropic reasoning models if any."""
        # Anthropic doesn't have reasoning models like OpenAI's o1 series yet,
        # but this test ensures the system can handle them if introduced
        endpoint = match_endpoint(
            provider="anthropic",
            endpoint="chat",
            model="claude-3-opus-20240229",  # Current model
        )

        # Standard Anthropic models should use normal configuration
        assert endpoint.config.openai_compatible is False
        if endpoint.config.endpoint_params:
            assert "messages" in endpoint.config.endpoint_params

    @pytest.mark.asyncio
    async def test_anthropic_parallel_requests(self, anthropic_imodel, mock_anthropic_response):
        """Test parallel requests to Anthropic API."""
        import asyncio

        async def mock_request_with_delay(request, cache_control=False, **kwargs):
            await asyncio.sleep(0.1)
            response = mock_anthropic_response.json.return_value.copy()
            response["id"] = f"msg_{request['messages'][0]['content'][-1]}"
            return response

        with patch.object(
            anthropic_imodel.endpoint,
            "call",
            side_effect=mock_request_with_delay,
        ):
            tasks = []
            for i in range(3):
                task = asyncio.create_task(
                    anthropic_imodel.invoke(
                        messages=[{"role": "user", "content": f"Message {i}"}],
                        max_tokens=100,
                    )
                )
                tasks.append(task)

            results = await asyncio.gather(*tasks)

        # Verify all requests completed independently
        assert len(results) == 3
        for i, result in enumerate(results):
            assert result.response is not None
            assert f"{i}" in result.response["id"]

    def test_anthropic_cache_control(self, anthropic_imodel):
        """Test Anthropic cache control feature."""
        api_call = anthropic_imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=100,
            cache_control=True,
        )

        assert api_call.cache_control is True
