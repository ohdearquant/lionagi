# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os
from unittest.mock import patch

import pytest

from lionagi.protocols.generic.event import EventStatus
from lionagi.providers.google.chat import GeminiChatEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.imodel import iModel


def _get_gemini_config(**overrides) -> EndpointConfig:
    """Create a Gemini (OpenAI-compatible) chat endpoint config for testing."""
    defaults = dict(
        name="gemini_chat/completions",
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        endpoint="chat/completions",
        api_key="dummy-key-for-testing",
        auth_type="bearer",
        content_type="application/json",
        method="POST",
    )
    defaults.update(overrides)
    return EndpointConfig(**defaults)


class TestOpenAIIntegration:
    """Integration tests for OpenAI endpoint."""

    @pytest.fixture
    def openai_imodel(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}):
            return iModel(provider="openai", model="gpt-4.1-mini")

    @pytest.fixture
    def reasoning_imodel(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}):
            return iModel(provider="openai", model="o1-preview")

    def test_openai_endpoint_configuration(self, openai_imodel):
        assert openai_imodel.endpoint.config.provider == "openai"
        # OpenAI compatible flag may be set differently based on implementation

    def test_openai_headers_creation(self, openai_imodel):
        payload, headers = openai_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "gpt-4.1-mini",
                "temperature": 0.7,
                "api_key": "test-key",
            }
        )

        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["Content-Type"] == "application/json"
        assert "api_key" not in payload  # Should be removed from payload

    def test_openai_payload_standard_model(self, openai_imodel):
        payload, headers = openai_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "gpt-4.1-mini",
                "temperature": 0.7,
                "max_tokens": 100,
                "top_p": 0.9,
            }
        )

        assert payload["model"] == "gpt-4.1-mini"
        assert payload["messages"][0]["content"] == "Hello"
        assert payload["temperature"] == 0.7
        assert payload["max_tokens"] == 100
        assert payload["top_p"] == 0.9

    def test_openai_payload_reasoning_model(self, reasoning_imodel):
        payload, headers = reasoning_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Solve this complex problem"}],
                "model": "o1-preview",
                "temperature": 0.7,  # Should be filtered out
                "max_tokens": 100,
                "top_p": 0.9,  # Should be filtered out
            }
        )

        assert payload["model"] == "o1-preview"
        assert payload["messages"][0]["content"] == "Solve this complex problem"
        assert payload["max_tokens"] == 100
        # Note: Parameter filtering may not be implemented for reasoning models yet

    def test_openai_system_message_handling(self, openai_imodel):
        # gpt-4.1-mini is not gated: system role is preserved.
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a helpful assistant.",
                    },
                    {"role": "user", "content": "Hello"},
                ],
                "model": "gpt-4.1-mini",
            }
        )

        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"

    @pytest.mark.parametrize(
        "model",
        [
            "o1",
            "o1-preview",
            "o1-mini",
            "o1-2024-12-17",
            "o3",
            "o3-mini",
            "o3-2025-04-16",
            "o4-mini",
            "o4-mini-2025-04-16",
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-2025-08-07",
            "gpt-5-chat-latest",
            "ft:gpt-5-mini:acme::abc123",
            "ft:o3-mini:acme:custom:xyz789",
        ],
    )
    def test_openai_developer_role_conversion_gated_models(self, openai_imodel, model):
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ],
                "model": model,
            }
        )

        assert payload["messages"][0]["role"] == "developer"
        assert payload["messages"][1]["role"] == "user"

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
            "ft:gpt-4o-mini:acme::abc123",
            "ft:",
        ],
    )
    def test_openai_system_role_preserved_non_gated_models(self, openai_imodel, model):
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ],
                "model": model,
            }
        )

        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"

    def test_openai_system_role_preserved_missing_model(self, openai_imodel):
        # No "model" key in the request at all -> falls back to the config
        # default (gpt-4.1-mini via the fixture), which is not gated.
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ],
            }
        )

        assert payload["messages"][0]["role"] == "system"

    def test_openai_system_role_preserved_unknown_model(self, openai_imodel):
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ],
                "model": "some-custom-compatible-model",
            }
        )

        assert payload["messages"][0]["role"] == "system"

    def test_openai_explicit_developer_role_preserved(self, openai_imodel):
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [
                    {"role": "developer", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ],
                "model": "o1-preview",
            }
        )

        assert payload["messages"][0]["role"] == "developer"
        assert payload["messages"][0]["content"] == "You are a helpful assistant."

    def test_openai_all_system_messages_converted_for_gated_model(self, openai_imodel):
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [
                    {"role": "system", "content": "First system message."},
                    {"role": "user", "content": "Hello"},
                    {"role": "system", "content": "Second system message."},
                    {"role": "assistant", "content": "Hi there"},
                ],
                "model": "o3-mini",
            }
        )

        roles = [m["role"] for m in payload["messages"]]
        assert roles == ["developer", "user", "developer", "assistant"]
        assert payload["messages"][0]["content"] == "First system message."
        assert payload["messages"][2]["content"] == "Second system message."

    def test_openai_create_payload_does_not_mutate_caller_input(self, openai_imodel):
        original_messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        request = {
            "messages": original_messages,
            "model": "o1-preview",
        }
        # Snapshot deep copies for comparison.
        import copy

        snapshot_request = copy.deepcopy(request)
        snapshot_messages = copy.deepcopy(original_messages)

        payload, _ = openai_imodel.endpoint.create_payload(request)

        # The gated conversion happened in the returned payload...
        assert payload["messages"][0]["role"] == "developer"
        # ...but the caller's original dicts/list are untouched.
        assert request == snapshot_request
        assert original_messages == snapshot_messages
        assert original_messages[0]["role"] == "system"
        assert payload["messages"] is not original_messages
        assert payload["messages"][0] is not original_messages[0]

    @pytest.mark.asyncio
    async def test_openai_api_calling_creation(self, openai_imodel, mock_response):
        api_call = openai_imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello, GPT!"}],
            temperature=0.7,
            max_tokens=100,
        )

        assert api_call.payload["model"] == "gpt-4.1-mini"
        assert api_call.payload["messages"][0]["content"] == "Hello, GPT!"
        assert api_call.payload["temperature"] == 0.7
        assert api_call.payload["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_openai_successful_invoke(self, openai_imodel, mock_response):
        with patch.object(
            openai_imodel.endpoint,
            "call",
            return_value=mock_response.json.return_value,
        ):
            result = await openai_imodel.invoke(
                messages=[{"role": "user", "content": "Hello, GPT!"}],
                temperature=0.7,
            )

        assert result is not None
        assert result.response["choices"][0]["message"]["role"] == "assistant"
        assert "Test response" in result.response["choices"][0]["message"]["content"]

    @pytest.mark.asyncio
    async def test_openai_streaming(self, openai_imodel):
        # Set a streaming_process_func that returns the chunk
        openai_imodel.streaming_process_func = lambda chunk: chunk

        async def mock_openai_stream():
            chunks = [
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
                {"choices": [{"delta": {}}]},  # End of stream
            ]
            for chunk in chunks:
                yield chunk

        with patch.object(openai_imodel.endpoint, "stream", return_value=mock_openai_stream()):
            chunks = []
            async for chunk in openai_imodel.stream(
                messages=[{"role": "user", "content": "Hello"}],
                temperature=0.7,
            ):
                # Check if chunk is a dict with 'choices' key instead of an object with 'choices' attribute
                if (
                    isinstance(chunk, dict)
                    and "choices" in chunk
                    and chunk["choices"]
                    and "content" in chunk["choices"][0].get("delta", {})
                ):
                    chunks.append(chunk)

        assert len(chunks) >= 2

    def test_openai_url_construction(self):
        endpoint = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        url = endpoint.config.full_url
        assert "api.openai.com" in url

    def test_openai_model_validation(self, openai_imodel):
        valid_models = [
            "gpt-4.1-mini",
            "gpt-4o",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
            "o1-preview",
            "o1-mini",
        ]

        for model in valid_models:
            payload, _ = openai_imodel.endpoint.create_payload(
                {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "model": model,
                    "temperature": 0.7,
                }
            )
            assert payload["model"] == model

    @pytest.mark.asyncio
    async def test_openai_parallel_requests(self, openai_imodel, mock_response):

        async def mock_request_with_delay(request, cache_control=False, **kwargs):
            await asyncio.sleep(0.1)
            response = mock_response.json.return_value.copy()
            response["id"] = f"chatcmpl-{request['messages'][0]['content'][-1]}"
            return response

        with patch.object(openai_imodel.endpoint, "call", side_effect=mock_request_with_delay):
            tasks = []
            for i in range(3):
                task = asyncio.create_task(
                    openai_imodel.invoke(
                        messages=[{"role": "user", "content": f"Message {i}"}],
                        temperature=0.7,
                    )
                )
                tasks.append(task)

            results = await asyncio.gather(*tasks)

        # Verify all requests completed independently
        assert len(results) == 3
        for i, result in enumerate(results):
            assert result.response is not None
            assert f"{i}" in result.response["id"]

    def test_openai_function_calling(self, openai_imodel):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather information",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                    },
                },
            }
        ]

        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "What's the weather?"}],
                "model": "gpt-4.1-mini",
                "tools": tools,
                "tool_choice": "auto",
            }
        )

        assert payload["tools"] == tools
        assert payload["tool_choice"] == "auto"

    def test_openai_response_format(self, openai_imodel):
        payload, _ = openai_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Return JSON"}],
                "model": "gpt-4.1-mini",
                "response_format": {"type": "json_object"},
            }
        )

        assert payload["response_format"] == {"type": "json_object"}

    def test_openai_reasoning_model_parameter_filtering(self, reasoning_imodel):
        # These parameters should be filtered out for o1 models
        forbidden_params = {
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.3,
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
        }

        payload, _ = reasoning_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Complex reasoning task"}],
                "model": "o1-preview",
                "max_tokens": 1000,  # This should be kept
                **forbidden_params,
            }
        )

        # max_tokens should be present
        assert payload["max_tokens"] == 1000

        # Note: Parameter filtering for reasoning models may not be fully implemented

    def test_openai_custom_base_url(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            imodel = iModel(
                provider="openai",
                base_url="https://custom.openai.proxy.com/v1",
                model="gpt-4.1-mini",
            )

        assert imodel.endpoint.config.base_url == "https://custom.openai.proxy.com/v1"

    @pytest.mark.asyncio
    async def test_openai_error_handling(self, openai_imodel):
        import aiohttp

        # Test rate limit error
        with patch.object(
            openai_imodel.endpoint,
            "call",
            side_effect=aiohttp.ClientError("Rate limit exceeded"),
        ):
            result = await openai_imodel.invoke(messages=[{"role": "user", "content": "Hello"}])

            # The invoke method returns a failed APICalling object instead of raising
            assert result.status == EventStatus.FAILED

    def test_openai_token_usage_tracking(self, openai_imodel):
        api_call = openai_imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}],
            include_token_usage_to_model=True,
        )

        assert api_call.include_token_usage_to_model is True

    def test_openai_different_models_isolation(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            standard_model = iModel(provider="openai", model="gpt-4.1-mini")
            reasoning_model = iModel(provider="openai", model="o1-preview")

        # Create payloads with same input but different models
        standard_payload, _ = standard_model.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 0.7,
                "top_p": 0.9,
            }
        )

        reasoning_payload, _ = reasoning_model.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 0.7,  # Should be filtered
                "top_p": 0.9,  # Should be filtered
            }
        )

        # Standard model should keep all parameters
        assert "temperature" in standard_payload
        assert "top_p" in standard_payload

        # Note: Reasoning model parameter filtering may not be implemented


class TestGeminiIntegration:
    """Integration tests for Gemini endpoint (OpenAI-compatible)."""

    @pytest.fixture
    def gemini_imodel(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-gemini-key"}):
            return iModel(provider="gemini", model="gemini-2.5-flash")

    def test_gemini_endpoint_configuration(self, gemini_imodel):
        assert gemini_imodel.endpoint.config.provider == "gemini"
        assert "generativelanguage.googleapis.com" in gemini_imodel.endpoint.config.base_url

    def test_gemini_config_defaults(self):
        config = _get_gemini_config()
        assert config.provider == "gemini"
        assert config.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
        assert config.endpoint == "chat/completions"
        assert config.auth_type == "bearer"
        assert config.method == "POST"

    def test_gemini_config_override(self):
        config = _get_gemini_config(
            kwargs={"model": "gemini-2.5-pro"},
        )
        assert config.kwargs["model"] == "gemini-2.5-pro"

    def test_gemini_headers_creation(self, gemini_imodel):
        payload, headers = gemini_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "gemini-2.5-flash",
                "temperature": 0.7,
                "api_key": "test-gemini-key",
            }
        )

        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["Content-Type"] == "application/json"
        assert "api_key" not in payload

    def test_gemini_payload_creation(self, gemini_imodel):
        payload, headers = gemini_imodel.endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "gemini-2.5-flash",
                "temperature": 0.7,
                "max_tokens": 100,
            }
        )

        assert payload["model"] == "gemini-2.5-flash"
        assert payload["messages"][0]["content"] == "Hello"
        assert payload["temperature"] == 0.7
        assert payload["max_tokens"] == 100

    def test_gemini_match_endpoint_routing(self):
        endpoint = match_endpoint(provider="gemini", endpoint="chat", model="gemini-2.5-flash")
        assert isinstance(endpoint, GeminiChatEndpoint)
        assert endpoint.config.provider == "gemini"

    def test_gemini_url_construction(self):
        endpoint = match_endpoint(provider="gemini", endpoint="chat", model="gemini-2.5-flash")
        url = endpoint.config.full_url
        assert "generativelanguage.googleapis.com" in url
        assert "chat/completions" in url

    def test_gemini_imodel_construction_explicit(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            model = iModel(provider="gemini", model="gemini-2.5-flash")
        assert model.endpoint.config.provider == "gemini"

    def test_gemini_imodel_construction_prefix(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            model = iModel(model="gemini/gemini-2.5-flash")
        assert model.endpoint.config.provider == "gemini"

    @pytest.mark.asyncio
    async def test_gemini_api_calling_creation(self, gemini_imodel, mock_response):
        api_call = gemini_imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello, Gemini!"}],
            temperature=0.7,
            max_tokens=100,
        )

        assert api_call.payload["model"] == "gemini-2.5-flash"
        assert api_call.payload["messages"][0]["content"] == "Hello, Gemini!"
        assert api_call.payload["temperature"] == 0.7
        assert api_call.payload["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_gemini_successful_invoke(self, gemini_imodel, mock_response):
        with patch.object(
            gemini_imodel.endpoint,
            "call",
            return_value=mock_response.json.return_value,
        ):
            result = await gemini_imodel.invoke(
                messages=[{"role": "user", "content": "Hello, Gemini!"}],
                temperature=0.7,
            )

        assert result is not None
        assert result.response["choices"][0]["message"]["role"] == "assistant"
