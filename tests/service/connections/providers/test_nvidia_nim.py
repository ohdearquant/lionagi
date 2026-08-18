# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for NVIDIA NIM provider endpoints."""

import os

import pytest
from dotenv import load_dotenv

from lionagi.providers.nvidia_nim.chat import NvidiaNimChatEndpoint
from lionagi.providers.nvidia_nim.embed import NvidiaNimEmbedEndpoint
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
        config = NVIDIA_NIM_CHAT_ENDPOINT_CONFIG
        assert config.provider == "nvidia_nim"
        assert config.base_url == "https://integrate.api.nvidia.com/v1"
        assert config.endpoint == "chat/completions"
        assert config.auth_type == "bearer"
        assert config.content_type == "application/json"
        assert config.method == "POST"
        assert config.kwargs["model"] == "meta/llama3-8b-instruct"

    def test_embed_endpoint_config(self):
        config = NVIDIA_NIM_EMBED_ENDPOINT_CONFIG
        assert config.provider == "nvidia_nim"
        assert config.base_url == "https://integrate.api.nvidia.com/v1"
        assert config.endpoint == "embeddings"
        assert config.auth_type == "bearer"
        assert config.kwargs["model"] == "nvidia/nv-embed-v1"

    def test_chat_endpoint_initialization(self):
        endpoint = NvidiaNimChatEndpoint()
        assert endpoint.config.provider == "nvidia_nim"
        assert endpoint.config.endpoint == "chat/completions"

    def test_embed_endpoint_initialization(self):
        endpoint = NvidiaNimEmbedEndpoint()
        assert endpoint.config.provider == "nvidia_nim"
        assert endpoint.config.endpoint == "embeddings"

    def test_match_endpoint_chat(self):
        endpoint = match_endpoint("nvidia_nim", "chat")
        assert isinstance(endpoint, NvidiaNimChatEndpoint)
        assert endpoint.config.provider == "nvidia_nim"

    def test_match_endpoint_embed(self):
        endpoint = match_endpoint("nvidia_nim", "embed")
        assert isinstance(endpoint, NvidiaNimEmbedEndpoint)
        assert endpoint.config.provider == "nvidia_nim"

    def test_custom_model_override(self):
        endpoint = NvidiaNimChatEndpoint()
        endpoint.config.kwargs["model"] = "meta/llama3-70b-instruct"
        assert endpoint.config.kwargs["model"] == "meta/llama3-70b-instruct"
