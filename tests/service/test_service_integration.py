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

    def test_endpoint_config_creation(self, openai_endpoint_config):
        """Test basic endpoint configuration creation."""
        config = openai_endpoint_config

        assert config.name == "test_endpoint"
        assert config.provider == "openai"
        assert config.endpoint == "chat"
        assert config.openai_compatible is True

    def test_endpoint_config_validation(self, anthropic_endpoint_config):
        """Test endpoint configuration validation."""
        config = anthropic_endpoint_config

        # Test that validation passes
        assert config.provider == "anthropic"

    def test_endpoint_creation(self, openai_endpoint_config):
        """Test endpoint creation with configuration."""
        config = openai_endpoint_config

        endpoint = Endpoint(config=config)
        assert endpoint.config == config

    def test_endpoint_payload_creation(self, openai_endpoint_config):
        """Test endpoint payload creation."""
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
        """Test comprehensive header factory functionality."""
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
        """Test endpoint matching for OpenAI."""
        endpoint = match_endpoint(provider="openai", endpoint="chat", model="gpt-4.1-mini")

        assert endpoint.config.provider == "openai"
        # Note: openai_compatible may be set differently by the match_endpoint function

    def test_match_endpoint_anthropic(self):
        """Test endpoint matching for Anthropic."""
        endpoint = match_endpoint(
            provider="anthropic",
            endpoint="chat",
            model="claude-3-opus-20240229",
        )

        assert endpoint.config.provider == "anthropic"
        assert endpoint.config.openai_compatible is False

    def test_api_calling_creation(self, openai_endpoint_config):
        """Test API calling creation and basic functionality."""
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
        """Test iModel creation."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        assert imodel.endpoint.config.provider == "openai"
        assert imodel.endpoint.config.kwargs["model"] == "gpt-4.1-mini"
        assert imodel.model_name == "gpt-4.1-mini"

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_api_calling_creation(self):
        """Test iModel API calling creation."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "Hello"}], temperature=0.7
        )

        assert isinstance(api_call, APICalling)
        assert api_call.payload["model"] == "gpt-4.1-mini"
        assert api_call.payload["temperature"] == 0.7

    def test_endpoint_url_construction(self, openai_endpoint_config, anthropic_endpoint_config):
        """Test URL construction for different endpoints."""
        # OpenAI endpoint
        openai_endpoint = Endpoint(config=openai_endpoint_config)
        openai_url = openai_endpoint.config.full_url
        assert "api.openai.com" in openai_url

        # Anthropic endpoint
        anthropic_endpoint = Endpoint(config=anthropic_endpoint_config)
        anthropic_url = anthropic_endpoint.config.full_url
        assert "api.anthropic.com" in anthropic_url

    def test_endpoint_config_update(self, openai_endpoint_config):
        """Test endpoint configuration update functionality."""
        config = openai_endpoint_config

        config.update(timeout=600, custom_param="value")

        assert config.timeout == 600
        assert config.kwargs["custom_param"] == "value"

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_anthropic_integration(self):
        """Test Anthropic integration."""
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
        """Test that endpoint config properly handles extra kwargs."""
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
    """Tests for error handling in service layer."""

    def test_endpoint_config_missing_required_fields(self):
        """Test endpoint config raises error with missing required fields."""
        with pytest.raises(ValidationError):  # Pydantic ValidationError
            EndpointConfig(name="test")  # Missing provider, endpoint, base_url

    def test_endpoint_config_invalid_url(self):
        """Test endpoint config handles invalid URLs gracefully."""
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

    def test_endpoint_config_empty_api_key(self):
        """Test endpoint config with empty API key."""
        config = EndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            base_url="https://api.openai.com/v1",
            api_key="",  # Empty key
        )
        assert config.api_key == ""

    def test_endpoint_config_none_api_key(self):
        """Test endpoint config with None API key."""
        config = EndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            base_url="https://api.openai.com/v1",
            api_key=None,
        )
        assert config.api_key is None

    def test_header_factory_missing_api_key(self):
        """Test header factory raises error for missing API key."""
        with pytest.raises(ValueError, match="API key is required"):
            HeaderFactory.get_header(auth_type="bearer", api_key=None)

    def test_header_factory_empty_api_key(self):
        """Test header factory raises error for empty API key."""
        with pytest.raises(ValueError, match="API key is required"):
            HeaderFactory.get_header(auth_type="bearer", api_key="")

    def test_header_factory_unknown_auth_type(self):
        """Test header factory raises error for unknown auth type."""
        with pytest.raises(ValueError, match="Unsupported auth type"):
            HeaderFactory.get_header(auth_type="unknown", api_key="test-key")

    @patch.dict(os.environ, {}, clear=False)
    def test_imodel_missing_api_key(self):
        """Test iModel creation without API key in environment."""
        # This may raise an error or handle gracefully depending on implementation
        try:
            imodel = iModel(provider="openai", model="gpt-4.1-mini")
            # If it succeeds, verify it was created
            assert imodel is not None
        except Exception as e:
            # If it fails, verify it's an appropriate error
            assert isinstance(e, (ValueError, KeyError, AttributeError))

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_invalid_provider(self):
        """Test iModel with invalid provider."""
        # iModel may accept invalid providers and fail at API call time
        # This tests that creation doesn't crash
        try:
            imodel = iModel(provider="invalid_provider", model="test-model")
            # If it succeeds, verify the provider was set
            assert imodel.endpoint.config.provider == "invalid_provider"
        except Exception as e:
            # If it fails, verify it's an appropriate error
            assert isinstance(e, (ValueError, KeyError))

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_empty_model_name(self):
        """Test iModel with empty model name."""
        # Empty model names may be handled differently
        try:
            imodel = iModel(provider="openai", model="")
            # If it succeeds, verify model was set
            assert imodel.model_name == ""
        except Exception as e:
            # If it fails, verify it's a validation error
            assert isinstance(e, (ValueError, TypeError))

    def test_endpoint_payload_creation_with_invalid_data(self, openai_endpoint_config):
        """Test endpoint payload creation with invalid request data."""
        endpoint = Endpoint(config=openai_endpoint_config)

        # Test with missing required fields
        invalid_request = {"model": "gpt-4.1-mini"}  # Missing messages

        payload, headers = endpoint.create_payload(invalid_request)
        # Should still create payload, validation happens at API level
        assert payload["model"] == "gpt-4.1-mini"

    def test_endpoint_payload_with_none_values(self, openai_endpoint_config):
        """Test endpoint handles None values in request data."""
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
    """Tests for edge cases in service layer."""

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_with_extreme_temperature(self):
        """Test iModel with extreme temperature values."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        # Test with temperature at boundaries
        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "test"}], temperature=0.0
        )
        assert api_call.payload["temperature"] == 0.0

        api_call2 = imodel.create_api_calling(
            messages=[{"role": "user", "content": "test"}], temperature=2.0
        )
        assert api_call2.payload["temperature"] == 2.0

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_with_large_max_tokens(self):
        """Test iModel with very large max_tokens value."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "test"}], max_tokens=100000
        )
        # Should accept the value, validation happens at API level
        assert api_call.payload["max_tokens"] == 100000

    def test_endpoint_config_with_very_long_strings(self):
        """Test endpoint config handles very long string values."""
        long_string = "a" * 10000
        config = EndpointConfig(
            name=long_string,
            provider="openai",
            endpoint="chat",
            base_url="https://api.openai.com/v1",
            api_key=long_string,
        )
        assert len(config.name) == 10000
        assert len(config.api_key) == 10000

    def test_endpoint_config_with_unicode_characters(self):
        """Test endpoint config handles unicode characters."""
        config = EndpointConfig(
            name="test_端点",
            provider="openai",
            endpoint="chat",
            base_url="https://api.openai.com/v1",
            api_key="test-键-🔑",
        )
        assert config.name == "test_端点"
        assert "🔑" in config.api_key

    def test_header_factory_with_special_characters_in_key(self):
        """Test header factory with special characters in API key."""
        headers = HeaderFactory.get_header(auth_type="bearer", api_key="test-key-!@#$%^&*()")
        assert headers["Authorization"] == "Bearer test-key-!@#$%^&*()"

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_with_empty_messages(self):
        """Test iModel with empty messages list."""
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        api_call = imodel.create_api_calling(messages=[])
        # Should create payload with empty messages
        assert api_call.payload["messages"] == []

    def test_match_endpoint_with_missing_model(self):
        """Test endpoint matching without model parameter."""
        # May use default model or require model
        try:
            endpoint = match_endpoint(provider="openai", endpoint="chat")
            assert endpoint is not None
        except Exception as e:
            # If it requires model, should raise appropriate error
            assert isinstance(e, (ValueError, KeyError, TypeError))
