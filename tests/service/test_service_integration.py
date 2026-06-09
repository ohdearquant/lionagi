# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.header_factory import HeaderFactory
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.imodel import iModel


class TestServiceIntegration:
    """Integration tests covering core service functionality."""

    def test_endpoint_payload_creation(self, openai_endpoint_config):
        config = openai_endpoint_config

        endpoint = Endpoint(config=config)

        request_data = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "gpt-4.1-mini",
            "temperature": 0.7,
        }

        payload, headers = endpoint.create_payload(request_data)

        assert payload["model"] == "gpt-4.1-mini"
        assert payload["messages"][0]["content"] == "Hello"
        assert payload["temperature"] == 0.7
        assert "Authorization" in headers

    @pytest.mark.parametrize(
        "auth_type,api_key,expected_key,expected_value",
        [
            ("bearer", "test-key", "Authorization", "Bearer test-key"),
            ("x-api-key", "test-key", "x-api-key", "test-key"),
            ("none", None, None, None),  # No auth case
        ],
    )
    def test_header_factory_comprehensive(self, auth_type, api_key, expected_key, expected_value):
        headers = HeaderFactory.get_header(auth_type=auth_type, api_key=api_key)

        if expected_key is None:
            # No auth case
            assert "Authorization" not in headers
            assert "x-api-key" not in headers
        else:
            assert headers[expected_key] == expected_value
            if auth_type == "bearer":
                assert headers["Content-Type"] == "application/json"

    def test_match_endpoint_openai(self):
        endpoint = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        assert endpoint.config.provider == "openai"
        # Note: openai_compatible may be set differently by the match_endpoint function

    def test_match_endpoint_anthropic(self):
        endpoint = match_endpoint(
            provider="anthropic",
            endpoint="chat",
            model="claude-3-opus-20240229",
        )

        assert endpoint.config.provider == "anthropic"
        assert endpoint.config.openai_compatible is False

    def test_api_calling_creation(self, openai_endpoint_config):
        config = openai_endpoint_config

        endpoint = Endpoint(config=config)

        api_call = APICalling(
            payload={
                "model": "gpt-4.1-mini",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers={"Authorization": "Bearer test-key"},
            endpoint=endpoint,
        )

        assert api_call.payload["model"] == "gpt-4.1-mini"
        assert api_call.headers["Authorization"] == "Bearer test-key"
        assert api_call.endpoint == endpoint
        assert api_call.response is None

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_creation(self):
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        assert imodel.endpoint.config.provider == "openai"
        assert imodel.endpoint.config.kwargs["model"] == "gpt-4.1-mini"
        assert imodel.model_name == "gpt-4.1-mini"

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_api_calling_creation(self):
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}], temperature=0.7
        )

        assert isinstance(api_call, APICalling)
        assert api_call.payload["model"] == "gpt-4.1-mini"
        assert api_call.payload["temperature"] == 0.7

    def test_endpoint_url_construction(self, openai_endpoint_config, anthropic_endpoint_config):
        # OpenAI endpoint
        openai_endpoint = Endpoint(config=openai_endpoint_config)
        openai_url = openai_endpoint.config.full_url
        assert "api.openai.com" in openai_url

        # Anthropic endpoint
        anthropic_endpoint = Endpoint(config=anthropic_endpoint_config)
        anthropic_url = anthropic_endpoint.config.full_url
        assert "api.anthropic.com" in anthropic_url

    def test_endpoint_config_update(self, openai_endpoint_config):
        config = openai_endpoint_config

        config.update(timeout=600, custom_param="value")

        assert config.timeout == 600
        assert config.kwargs["custom_param"] == "value"

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_anthropic_integration(self):
        imodel = iModel(provider="anthropic", model="claude-3-opus-20240229")

        assert imodel.endpoint.config.provider == "anthropic"
        assert imodel.endpoint.config.openai_compatible is False

        # Test payload creation
        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}], max_tokens=100
        )

        assert api_call.payload["model"] == "claude-3-opus-20240229"
        assert api_call.payload["max_tokens"] == 100

    def test_endpoint_config_kwargs_handling(self):
        config = EndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            custom_field="custom_value",  # This should go to kwargs
            another_param=123,
        )

        assert config.kwargs["custom_field"] == "custom_value"
        assert config.kwargs["another_param"] == 123


class TestServiceErrorHandling:
    def test_endpoint_config_missing_required_fields(self):
        with pytest.raises(ValidationError):  # Pydantic ValidationError
            EndpointConfig(name="test")  # Missing provider, endpoint, base_url

    def test_endpoint_config_invalid_url(self):
        # Test with various invalid URL formats
        config = EndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            base_url="not-a-valid-url",  # Invalid URL format
            api_key="test-key",
        )
        # Config should be created but URL validation may happen later
        assert config.base_url == "not-a-valid-url"

    def test_header_factory_missing_api_key(self):
        with pytest.raises(ValueError, match="API key is required"):
            HeaderFactory.get_header(auth_type="bearer", api_key=None)

    def test_header_factory_empty_api_key(self):
        with pytest.raises(ValueError, match="API key is required"):
            HeaderFactory.get_header(auth_type="bearer", api_key="")

    def test_header_factory_unknown_auth_type(self):
        with pytest.raises(ValueError, match="Unsupported auth type"):
            HeaderFactory.get_header(auth_type="unknown", api_key="test-key")

    def test_endpoint_payload_creation_with_invalid_data(self, openai_endpoint_config):
        endpoint = Endpoint(config=openai_endpoint_config)

        # Test with missing required fields
        invalid_request = {"model": "gpt-4.1-mini"}  # Missing messages

        payload, headers = endpoint.create_payload(invalid_request)
        # Should still create payload, validation happens at API level
        assert payload["model"] == "gpt-4.1-mini"

    def test_endpoint_payload_with_none_values(self, openai_endpoint_config):
        endpoint = Endpoint(config=openai_endpoint_config)

        request_data = {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "test"}],
            "temperature": None,
            "max_tokens": None,
        }

        payload, headers = endpoint.create_payload(request_data)
        assert payload["model"] == "gpt-4.1-mini"


class TestServiceEdgeCases:
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_with_empty_messages(self):
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        api_call = imodel.create_api_calling(messages=[])
        # Should create payload with empty messages
        assert api_call.payload["messages"] == []
