# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""Restored API models retain the request schema used to filter their payloads."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lionagi.service.accounts._pricing import request_ttls
from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode
from lionagi.service.imodel import iModel
from lionhub.providers.anthropic.messages import CreateMessageRequest
from lionhub.providers.openai.chat import OpenAIChatCompletionsRequest


class CustomRequest(BaseModel):
    model: str
    messages: list[dict]
    custom_field: str


@pytest.mark.parametrize(
    ("provider", "model_name", "request_model"),
    [
        ("anthropic", "claude-sonnet-5-5", CreateMessageRequest),
        ("openai", "gpt-4.1-mini", OpenAIChatCompletionsRequest),
    ],
)
def test_default_round_trip_keeps_registered_request_model_and_filters_unknown_fields(
    provider, model_name, request_model
):
    model = iModel(provider=provider, model=model_name, api_key="test-key")
    stored = model.to_dict()
    assert "request_options" not in stored["endpoint"]["config"]
    restored = iModel.from_dict(stored)

    assert restored.request_options is request_model
    payload, _ = restored.endpoint.create_payload(
        {
            "model": model_name,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
            "unrecognized_option": "not sent",
        }
    )
    assert "unrecognized_option" not in payload
    assert payload["model"] == model_name
    assert payload["messages"]


def test_stored_non_null_request_schema_wins_over_registered_model():
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    model.endpoint.request_options = CustomRequest
    stored = model.to_dict(include_request_options=True)
    assert stored["endpoint"]["config"]["request_options"] is not None

    restored = iModel.from_dict(stored)
    assert set(restored.request_options.model_fields) == {"model", "messages", "custom_field"}
    payload, _ = restored.endpoint.create_payload(
        {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "custom_field": "kept",
            "unrecognized_option": "not sent",
        }
    )
    assert payload["custom_field"] == "kept"
    assert "unrecognized_option" not in payload


def test_stored_null_recovers_the_registered_request_model():
    model = iModel(provider="anthropic", model="claude-sonnet-5-5", api_key="test-key")
    stored = model.to_dict()
    stored["endpoint"]["config"]["request_options"] = None

    restored = iModel.from_dict(stored)
    assert restored.request_options is CreateMessageRequest
    payload, _ = restored.endpoint.create_payload(
        {
            "model": "claude-sonnet-5-5",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
            "unrecognized_option": "not sent",
        }
    )
    assert "unrecognized_option" not in payload


def test_unregistered_provider_fallback_remains_schema_less():
    model = iModel(
        provider="unregistered-provider",
        model="external-model",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        openai_compatible=True,
    )
    restored = iModel.from_dict(model.to_dict(), api_key="test-key")
    assert restored.endpoint.config.provider == "unregistered-provider"
    assert restored.request_options is None
    payload, _ = restored.endpoint.create_payload(
        {"model": "external-model", "messages": [], "provider_specific_option": "kept"}
    )
    assert payload["provider_specific_option"] == "kept"


def test_restored_routed_payload_can_be_priced_and_unknown_key_is_not_disclosed(monkeypatch):
    model = iModel(provider="anthropic", model="claude-sonnet-5-5")
    restored = iModel.from_dict(model.to_dict())
    monkeypatch.setattr(restored.endpoint, "_account_routing_enabled", lambda: True)
    payload, _ = restored.endpoint.create_payload(
        {
            "model": "claude-sonnet-5-5",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
            "unrecognized_option": "not sent",
        }
    )
    assert "unrecognized_option" not in payload
    priced = {**payload, "service_tier": "standard_only"}
    assert request_ttls(priced) == frozenset()

    key = "unknown_pricing_option"
    value = "private-option-value"
    with pytest.raises(AccountRoutingError) as caught:
        request_ttls({**priced, key: value})
    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING
    assert key not in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    ("role", "content"),
    [
        ("assistant", [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}]),
        ("user", [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]),
    ],
)
def test_restored_and_fresh_models_refuse_the_same_native_blocks(role, content):
    fresh = iModel(provider="anthropic", model="synthetic-model")
    restored = iModel.from_dict(fresh.to_dict())
    request = {
        "model": "synthetic-model",
        "messages": [{"role": role, "content": content}],
        "max_tokens": 32,
    }
    with pytest.raises(ValueError) as fresh_error:
        fresh.endpoint.create_payload(request)
    with pytest.raises(ValueError) as restored_error:
        restored.endpoint.create_payload(request)
    assert str(restored_error.value) == str(fresh_error.value)


def test_restored_and_fresh_models_build_the_same_payload_with_cache_and_tier_options():
    fresh = iModel(provider="anthropic", model="synthetic-model")
    restored = iModel.from_dict(fresh.to_dict())
    request = {
        "model": "synthetic-model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 32,
        "service_tier": "standard_only",
        "cache_control": {"type": "ephemeral"},
    }
    fresh_payload, _ = fresh.endpoint.create_payload(request)
    restored_payload, _ = restored.endpoint.create_payload(request)
    assert restored_payload == fresh_payload
