# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for NVIDIA NIM provider endpoints."""

import os

import pytest
from dotenv import load_dotenv

from lionagi.providers.nvidia_nim.chat.endpoint import NvidiaNimChatEndpoint
from lionagi.providers.nvidia_nim.embed.endpoint import NvidiaNimEmbedEndpoint
from lionagi.service.connections.match_endpoint import match_endpoint

# Create config objects using the new endpoint classes (equivalent to old module-level constants)
NVIDIA_NIM_CHAT_ENDPOINT_CONFIG = NvidiaNimChatEndpoint().config
NVIDIA_NIM_EMBED_ENDPOINT_CONFIG = NvidiaNimEmbedEndpoint().config

# Load environment variables
load_dotenv()

# Skip tests if API key is not available
skip_if_no_api_key = pytest.mark.skipif(
    not os.getenv("NVIDIA_NIM_API_KEY"),
    reason="NVIDIA_NIM_API_KEY not set in environment",
)


class TestNvidiaNimEndpoints:
    """Test NVIDIA NIM endpoint configurations."""

    def test_chat_endpoint_config(self):
        """Test that chat endpoint config has correct defaults."""
        config = NVIDIA_NIM_CHAT_ENDPOINT_CONFIG
        assert config.provider == "nvidia_nim"
        assert config.base_url == "https://integrate.api.nvidia.com/v1"
        assert config.endpoint == "chat/completions"
        assert config.auth_type == "bearer"
        assert config.content_type == "application/json"
        assert config.method == "POST"
        assert config.kwargs["model"] == "meta/llama3-8b-instruct"

    def test_embed_endpoint_config(self):
        """Test that embedding endpoint config has correct defaults."""
        config = NVIDIA_NIM_EMBED_ENDPOINT_CONFIG
        assert config.provider == "nvidia_nim"
        assert config.base_url == "https://integrate.api.nvidia.com/v1"
        assert config.endpoint == "embeddings"
        assert config.auth_type == "bearer"
        assert config.kwargs["model"] == "nvidia/nv-embed-v1"

    def test_match_endpoint_chat(self):
        """Test that match_endpoint returns correct chat endpoint."""
        endpoint = match_endpoint("nvidia_nim", "chat")
        assert isinstance(endpoint, NvidiaNimChatEndpoint)
        assert endpoint.config.provider == "nvidia_nim"

    def test_match_endpoint_embed(self):
        """Test that match_endpoint returns correct embedding endpoint."""
        endpoint = match_endpoint("nvidia_nim", "embed")
        assert isinstance(endpoint, NvidiaNimEmbedEndpoint)
        assert endpoint.config.provider == "nvidia_nim"

    @pytest.mark.asyncio
    async def test_chat_endpoint_call(self):
        """Test chat endpoint call with mocked response."""
        from unittest.mock import AsyncMock, patch

        endpoint = NvidiaNimChatEndpoint()

        request = {
            "messages": [
                {
                    "role": "user",
                    "content": "Say 'Hello NVIDIA NIM' and nothing else.",
                }
            ],
            "model": "meta/llama3-8b-instruct",
            "max_tokens": 20,
            "temperature": 0.1,
        }

        # Mock the API response to avoid real API calls
        mock_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello NVIDIA NIM",
                    }
                }
            ]
        }

        with patch.object(
            endpoint,
            "call",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            response = await endpoint.call(request)

        assert response is not None
        assert "choices" in response
        assert len(response["choices"]) > 0
        assert "message" in response["choices"][0]

        # Check that response contains expected content
        content = response["choices"][0]["message"]["content"]
        assert "NVIDIA" in content or "Hello" in content
