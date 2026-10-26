# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Credential references are resolved before a scripted request is dispatched."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from pydantic import SecretStr

from lionagi._errors import ConfigurationError
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.registry import EndpointMeta, EndpointType
from lionagi.service.imodel import iModel

ENV_NAME = "ANTHROPIC_API_KEY"
REQUEST = {"model": "credential-test", "messages": [{"role": "user", "content": "hello"}]}


class _Secrets:
    ANTHROPIC_ACCOUNTS = ()
    ANTHROPIC_API_KEY = None

    def get_secret(self, name):
        value = getattr(self, name, None)
        if value is None:
            raise ValueError(f"Secret key '{name}' is not set")
        return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


@pytest.fixture
def secrets(monkeypatch):
    # Every value is synthetic; no user settings file or provider is queried.
    import lionagi.config

    values = _Secrets()
    monkeypatch.setattr(lionagi.config, "settings", values)
    for name in (ENV_NAME, "UPPERCASE_LITERAL", "sk-synthetic-literal"):
        monkeypatch.delenv(name, raising=False)
    return values


class _CapturedEndpoint(Endpoint):
    """Run the real payload/header path with an in-process scripted transport."""

    def __init__(self, config):
        super().__init__(config=config)
        self.requests = []

    async def _call(self, payload, headers, **kwargs):
        self.requests.append({"payload": deepcopy(payload), "headers": dict(headers)})
        return {"ok": True}


def _endpoint(key, *, provider="anthropic", auth_type="x-api-key"):
    meta = EndpointMeta(
        provider=provider,
        endpoint="messages",
        endpoint_type=EndpointType.API,
        base_url="https://credential-test.invalid",
        auth_type=auth_type,
        api_key_env=ENV_NAME if provider == "anthropic" else None,
    )
    return _CapturedEndpoint(meta.create_config(api_key=key))


@pytest.mark.asyncio
async def test_unset_credential_reference_refuses_before_dispatch(secrets):
    endpoint = _endpoint(ENV_NAME)
    error = None
    try:
        await endpoint.call(REQUEST)
    except Exception as caught:
        error = caught

    assert endpoint.requests == [], "an unset credential reference must refuse before dispatch"
    assert isinstance(error, ConfigurationError)
    assert getattr(error, "reason", None) == "credential_missing"
    assert ENV_NAME in str(error)
    assert ENV_NAME not in repr(endpoint.requests)


@pytest.mark.asyncio
async def test_model_invoke_preserves_the_missing_credential_refusal(secrets):
    endpoint = _endpoint(ENV_NAME)
    model = iModel(endpoint=endpoint)
    error = None
    try:
        try:
            await asyncio.wait_for(model.invoke(**REQUEST), timeout=10)
        except Exception as caught:
            error = caught
    finally:
        await asyncio.wait_for(model.close(), timeout=10)

    assert endpoint.requests == [], "an unset model credential must refuse before dispatch"
    assert isinstance(error, ConfigurationError)
    assert getattr(error, "reason", None) == "credential_missing"
    assert ENV_NAME in str(error)
    assert ENV_NAME not in repr(endpoint.requests)
    assert model.calls_sent == 0


@pytest.mark.asyncio
async def test_credential_reference_uses_the_environment_value(secrets, monkeypatch):
    resolved = "synthetic-environment-key"
    monkeypatch.setenv(ENV_NAME, resolved)
    endpoint = _endpoint(ENV_NAME)

    assert await endpoint.call(REQUEST) == {"ok": True}
    assert len(endpoint.requests) == 1
    request = endpoint.requests[0]
    assert request["headers"]["x-api-key"] == resolved
    assert ENV_NAME not in repr(request)
    assert resolved not in repr(request["payload"])


@pytest.mark.asyncio
async def test_credential_reference_uses_the_settings_secret(secrets):
    resolved = "synthetic-settings-key"
    secrets.ANTHROPIC_API_KEY = SecretStr(resolved)
    endpoint = _endpoint(ENV_NAME)

    assert await endpoint.call(REQUEST) == {"ok": True}
    assert endpoint.requests[0]["headers"]["x-api-key"] == resolved
    assert ENV_NAME not in repr(endpoint.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        pytest.param("sk-synthetic-literal", id="literal"),
        pytest.param("UPPERCASE_LITERAL", id="uppercase-literal"),
        pytest.param(SecretStr(ENV_NAME), id="explicit-secret"),
    ],
)
async def test_explicit_literal_credential_is_byte_exact(secrets, key):
    endpoint = _endpoint(key)
    expected = key.get_secret_value() if isinstance(key, SecretStr) else key

    assert await endpoint.call(REQUEST) == {"ok": True}
    assert len(endpoint.requests) == 1
    assert endpoint.requests[0]["headers"]["x-api-key"] == expected
    assert expected not in repr(endpoint.requests[0]["payload"])


def test_unset_credential_reference_allows_config_endpoint_and_model_construction(secrets):
    endpoint = _endpoint(ENV_NAME)
    config = endpoint.config
    model = iModel(endpoint=endpoint)

    assert config.api_key == ENV_NAME
    assert model.endpoint is endpoint
    assert endpoint.requests == []
    assert model.calls_sent == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,key,auth_type",
    [
        pytest.param("ollama", "ollama_key", "none", id="ollama"),
        pytest.param("anthropic", "dummy-key", "x-api-key", id="dummy"),
        pytest.param("anthropic", "dummy-key-for-testing", "x-api-key", id="dummy-testing"),
    ],
)
async def test_credential_sentinels_preserve_dispatch(secrets, provider, key, auth_type):
    endpoint = _endpoint(key, provider=provider, auth_type=auth_type)

    assert endpoint.config._api_key == key
    assert await endpoint.call(REQUEST) == {"ok": True}
    assert len(endpoint.requests) == 1
    if auth_type == "none":
        assert "Authorization" not in endpoint.requests[0]["headers"]
        assert "x-api-key" not in endpoint.requests[0]["headers"]
    else:
        assert endpoint.requests[0]["headers"]["x-api-key"] == key
