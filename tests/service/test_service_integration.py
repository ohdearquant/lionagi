# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import os
from unittest.mock import patch

import pytest
from pydantic import Field, ValidationError

from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.header_factory import HeaderFactory
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.imodel import iModel


class TestServiceIntegration:
    def test_endpoint_config_creation(self, openai_endpoint_config):
        config = openai_endpoint_config

        assert config.name == "test_endpoint"
        assert config.provider == "openai"
        assert config.endpoint == "chat"
        assert config.openai_compatible is True

    def test_endpoint_config_validation(self, anthropic_endpoint_config):
        config = anthropic_endpoint_config

        # Test that validation passes
        assert config.provider == "anthropic"

    def test_endpoint_creation(self, openai_endpoint_config):
        config = openai_endpoint_config

        endpoint = Endpoint(config=config)
        assert endpoint.config == config

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

    def test_endpoint_config_kwargs_only_collect_unknown_fields(self):
        config = EndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            timeout=42,
            kwargs={"preserved": "value"},
            unknown_option="kept",
        )

        assert config.timeout == 42
        assert config.kwargs == {"preserved": "value", "unknown_option": "kept"}

    def test_endpoint_config_subclass_accepts_aliased_field_key(self):
        class AliasedEndpointConfig(EndpointConfig):
            wire_timeout: int = Field(alias="wireTimeout")

        config = AliasedEndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            wireTimeout=7,
        )

        assert config.wire_timeout == 7
        assert "wireTimeout" not in config.kwargs

    def test_endpoint_config_field_key_cache_is_per_class_identity(self):
        from lionagi.service.connections.endpoint_config import (
            _FIELD_KEYS_BY_CLASS,
        )

        class ExtendedEndpointConfig(EndpointConfig):
            extra_knob: int = 0

        EndpointConfig(name="a", provider="openai", endpoint="chat")
        ExtendedEndpointConfig(name="b", provider="openai", endpoint="chat", extra_knob=1)

        base_cls, base_keys = _FIELD_KEYS_BY_CLASS[id(EndpointConfig)]
        sub_cls, sub_keys = _FIELD_KEYS_BY_CLASS[id(ExtendedEndpointConfig)]
        assert base_cls is EndpointConfig
        assert sub_cls is ExtendedEndpointConfig
        assert base_keys is not sub_keys
        assert "extra_knob" in sub_keys
        assert "extra_knob" not in base_keys

    def test_endpoint_config_cache_ignores_metaclass_equality(self):
        class EqualModelMeta(type(EndpointConfig)):
            def __eq__(cls, other):
                return isinstance(other, EqualModelMeta)

            def __hash__(cls):
                return 1

        class First(EndpointConfig, metaclass=EqualModelMeta):
            first_only: int = 0

        class Second(EndpointConfig, metaclass=EqualModelMeta):
            second_only: int = 0

        assert First is not Second and First == Second

        First(name="first", provider="openai", endpoint="chat", first_only=1)
        second = Second(name="second", provider="openai", endpoint="chat", second_only=2)

        assert second.second_only == 2
        assert "second_only" not in second.kwargs


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

    def test_endpoint_config_empty_api_key(self):
        config = EndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            base_url="https://api.openai.com/v1",
            api_key="",  # Empty key
        )
        assert config.api_key == ""

    def test_endpoint_config_none_api_key(self):
        config = EndpointConfig(
            name="test",
            provider="openai",
            endpoint="chat",
            base_url="https://api.openai.com/v1",
            api_key=None,
        )
        assert config.api_key is None

    def test_header_factory_missing_api_key(self):
        with pytest.raises(ValueError, match="API key is required"):
            HeaderFactory.get_header(auth_type="bearer", api_key=None)

    def test_header_factory_empty_api_key(self):
        with pytest.raises(ValueError, match="API key is required"):
            HeaderFactory.get_header(auth_type="bearer", api_key="")

    def test_header_factory_unknown_auth_type(self):
        with pytest.raises(ValueError, match="Unsupported auth type"):
            HeaderFactory.get_header(auth_type="unknown", api_key="test-key")

    @patch.dict(os.environ, {}, clear=False)
    def test_imodel_missing_api_key(self):
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
        # Empty model names may be handled differently
        try:
            imodel = iModel(provider="openai", model="")
            # If it succeeds, verify model was set
            assert imodel.model_name == ""
        except Exception as e:
            # If it fails, verify it's a validation error
            assert isinstance(e, (ValueError, TypeError))

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
    def test_imodel_with_extreme_temperature(self):
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
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        api_call = imodel.create_api_calling(
            messages=[{"role": "user", "content": "test"}], max_tokens=100000
        )
        # Should accept the value, validation happens at API level
        assert api_call.payload["max_tokens"] == 100000

    def test_endpoint_config_with_very_long_strings(self):
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
        headers = HeaderFactory.get_header(auth_type="bearer", api_key="test-key-!@#$%^&*()")
        assert headers["Authorization"] == "Bearer test-key-!@#$%^&*()"

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_imodel_with_empty_messages(self):
        imodel = iModel(provider="openai", model="gpt-4.1-mini")

        api_call = imodel.create_api_calling(messages=[])
        # Should create payload with empty messages
        assert api_call.payload["messages"] == []

    def test_match_endpoint_with_missing_model(self):
        # May use default model or require model
        try:
            endpoint = match_endpoint(provider="openai", endpoint="chat")
            assert endpoint is not None
        except Exception as e:
            # If it requires model, should raise appropriate error
            assert isinstance(e, (ValueError, KeyError, TypeError))
