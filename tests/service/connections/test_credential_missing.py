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
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.registry import EndpointMeta, EndpointType
from lionagi.service.imodel import iModel
from lionagi.testing._endpoint import ScriptedEndpoint

ENV_NAME = "ANTHROPIC_API_KEY"
REQUEST = {"model": "credential-test", "messages": [{"role": "user", "content": "hello"}]}


class _Secrets:
    ANTHROPIC_ACCOUNTS = ()
    ANTHROPIC_API_KEY = None

    def get_secret(self, name):
        if not hasattr(self, name):
            if "ollama" in name.lower():
                return "ollama"
            raise AttributeError(f"Secret key '{name}' not found in settings")
        value = getattr(self, name)
        if value is None:
            if "ollama" in name.lower():
                return "ollama"
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
    preview_payload, preview_headers = endpoint.create_payload(REQUEST)
    error = None
    try:
        await endpoint.call(REQUEST)
    except Exception as caught:
        error = caught

    assert endpoint.requests == [], "an unset credential reference must refuse before dispatch"
    assert isinstance(error, ConfigurationError)
    assert getattr(error, "reason", None) == "credential_missing"
    assert ENV_NAME in str(error)
    assert preview_payload["messages"] == REQUEST["messages"]
    assert preview_headers["x-api-key"]
    assert ENV_NAME not in repr({"payload": preview_payload, "headers": preview_headers})


@pytest.mark.asyncio
async def test_model_invoke_preserves_the_missing_credential_refusal(secrets):
    endpoint = _endpoint(ENV_NAME)
    preview_payload, preview_headers = endpoint.create_payload(REQUEST)
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
    assert preview_payload["messages"] == REQUEST["messages"]
    assert preview_headers["x-api-key"]
    assert ENV_NAME not in repr({"payload": preview_payload, "headers": preview_headers})
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


def _direct_endpoint(key):
    return _CapturedEndpoint(
        EndpointConfig(
            name="direct_messages",
            provider="anthropic",
            endpoint="messages",
            base_url="https://credential-test.invalid",
            auth_type="x-api-key",
            api_key=key,
        )
    )


@pytest.mark.asyncio
async def test_direct_declared_credential_name_refuses_before_dispatch(secrets):
    endpoint = _direct_endpoint(ENV_NAME)
    preview_payload, preview_headers = endpoint.create_payload(REQUEST)
    error = None
    try:
        await endpoint.call(REQUEST)
    except Exception as caught:
        error = caught

    assert endpoint.requests == [], "a direct declared credential must refuse before dispatch"
    assert isinstance(error, ConfigurationError)
    assert getattr(error, "reason", None) == "credential_missing"
    assert ENV_NAME in str(error)
    assert preview_payload["messages"] == REQUEST["messages"]
    assert preview_headers["x-api-key"]
    assert ENV_NAME not in repr({"payload": preview_payload, "headers": preview_headers})


@pytest.mark.asyncio
async def test_direct_declared_credential_name_uses_environment_value(secrets, monkeypatch):
    resolved = "synthetic-direct-key"
    monkeypatch.setenv(ENV_NAME, resolved)
    endpoint = _direct_endpoint(ENV_NAME)

    assert await endpoint.call(REQUEST) == {"ok": True}
    assert len(endpoint.requests) == 1
    assert endpoint.requests[0]["headers"]["x-api-key"] == resolved
    assert ENV_NAME not in repr(endpoint.requests[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [None, "synthetic-literal-env"], ids=["literal", "env"])
async def test_direct_undeclared_literal_keeps_existing_environment_fallback(
    secrets, monkeypatch, resolved
):
    literal = "UPPERCASE_LITERAL"
    if resolved is None:
        monkeypatch.delenv(literal, raising=False)
    else:
        monkeypatch.setenv(literal, resolved)
    endpoint = _direct_endpoint(literal)

    assert await endpoint.call(REQUEST) == {"ok": True}
    assert len(endpoint.requests) == 1
    assert endpoint.requests[0]["headers"]["x-api-key"] == (
        literal if resolved is None else resolved
    )


def _scripted_endpoint():
    return ScriptedEndpoint(
        config=_endpoint(ENV_NAME).config,
        api_key=ENV_NAME,
        script=[{"type": "text", "content": "scripted response"}],
    )


async def _read_scripted_stream(endpoint, **kwargs):
    stream = endpoint.stream(REQUEST, **kwargs)
    try:
        return [chunk async for chunk in stream]
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_scripted_stream_refuses_an_unset_credential_reference(secrets):
    endpoint = _scripted_endpoint()
    preview_payload, preview_headers = endpoint.create_payload(REQUEST)
    error = None
    try:
        await asyncio.wait_for(_read_scripted_stream(endpoint), timeout=10)
    except Exception as caught:
        error = caught

    assert endpoint.calls == [], "a scripted stream must refuse an unset credential before dispatch"
    assert isinstance(error, ConfigurationError)
    assert getattr(error, "reason", None) == "credential_missing"
    assert ENV_NAME in str(error)
    assert preview_payload["messages"] == REQUEST["messages"]
    assert preview_headers["x-api-key"]
    assert ENV_NAME not in repr({"payload": preview_payload, "headers": preview_headers})


@pytest.mark.asyncio
async def test_scripted_stream_refreshes_prepared_headers_after_credential_resolution(
    secrets, monkeypatch
):
    endpoint = _scripted_endpoint()
    preview_payload, preview_headers = endpoint.create_payload(REQUEST)
    resolved = "synthetic-late-stream-key"
    monkeypatch.setenv(ENV_NAME, resolved)

    chunks = await asyncio.wait_for(
        _read_scripted_stream(endpoint, extra_headers=preview_headers), timeout=10
    )

    assert any(chunk.content == "scripted response" for chunk in chunks)
    assert len(endpoint.calls) == 1 and endpoint.calls[0].was_streamed
    assert endpoint.calls[0].headers["x-api-key"] == resolved, (
        "a prepared stream must dispatch with the resolved credential"
    )
    assert endpoint.calls[0].payload == preview_payload
    assert ENV_NAME not in repr(endpoint.calls[0].headers)
    assert resolved not in repr(endpoint.calls[0].payload)


@pytest.mark.parametrize("available", [False, True], ids=["unset", "set"])
def test_registered_model_restore_preserves_reference(secrets, monkeypatch, available):
    original = iModel(
        provider="anthropic",
        endpoint="messages",
        api_key="synthetic-snapshot-key",
    )
    stored = original.to_dict()
    resolved = "synthetic-restored-key" if available else None
    if resolved is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(ENV_NAME, resolved)

    restored = iModel.from_dict(stored)

    assert "synthetic-snapshot-key" not in repr(stored)
    assert restored.endpoint.config.api_key_env == ENV_NAME
    assert restored.endpoint.config.api_key == ENV_NAME
    assert restored.endpoint.config._api_key == resolved
    assert restored.calls_sent == 0
