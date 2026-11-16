# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Apply declared agent effort to the API endpoint the branch actually uses."""

from copy import deepcopy

import pytest

from lionagi.config import settings
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.imodel import iModel
from lionhub.agent.factory import _AgentSettingError, create_agent
from lionhub.agent.spec import AgentSpec

ANTHROPIC_MODEL = "claude-haiku-5-5"
OPENAI_MODEL = "gpt-6-luna"
CHAT_REQUEST = {"messages": [{"role": "user", "content": "hello"}]}
RESPONSES_REQUEST = {"input": "hello"}
OUTPUT_FORMAT = {"type": "json_schema", "schema": {"type": "object"}}


def _configure_model_defaults(monkeypatch, provider, model):
    configured = settings.model_copy(
        update={
            "ANTHROPIC_ACCOUNTS": (),
            "LIONAGI_CHAT_PROVIDER": provider,
            "LIONAGI_CHAT_MODEL": model,
        }
    )
    monkeypatch.setattr("lionagi.config.settings", configured)
    monkeypatch.setattr("lionagi.session.branch.settings", configured)


@pytest.fixture
def sent_payloads(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    _configure_model_defaults(monkeypatch, "anthropic", ANTHROPIC_MODEL)
    payloads = []

    async def send(self, payload, headers, **kwargs):
        payloads.append(deepcopy(payload))
        return {}

    monkeypatch.setattr(Endpoint, "_call", send)
    return payloads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "endpoint", "model", "origin", "request_kwargs", "key", "original", "expected"),
    [
        pytest.param(
            "anthropic",
            "messages",
            ANTHROPIC_MODEL,
            "explicit",
            CHAT_REQUEST,
            "output_config",
            {"effort": "low", "format": OUTPUT_FORMAT},
            {"effort": "high", "format": OUTPUT_FORMAT},
            id="anthropic-output-config",
        ),
        pytest.param(
            "openai",
            "chat",
            OPENAI_MODEL,
            "explicit",
            CHAT_REQUEST,
            "reasoning_effort",
            "low",
            "high",
            id="openai-chat",
        ),
        pytest.param(
            "openai",
            "responses",
            OPENAI_MODEL,
            "profile",
            RESPONSES_REQUEST,
            "reasoning",
            {"effort": "low", "summary": "auto"},
            {"effort": "high", "summary": "auto"},
            id="openai-responses-profile",
        ),
    ],
)
async def test_supplied_model_effort_reaches_payload_and_preserves_caller(
    sent_payloads,
    tmp_path,
    provider,
    endpoint,
    model,
    origin,
    request_kwargs,
    key,
    original,
    expected,
):
    supplied = iModel(
        provider=provider,
        endpoint=endpoint,
        model=model,
        provider_metadata={"session_id": "caller-session"},
        **{key: deepcopy(original)},
    )
    supplied_config = deepcopy(supplied.endpoint.config.kwargs)
    supplied_executor = supplied.executor
    spec = AgentSpec.compose("implementer", effort="high")
    if origin == "profile":
        path = tmp_path / "agent.yaml"
        spec.to_yaml(path)
        spec = AgentSpec.from_yaml(path)

    branch = await create_agent(spec, load_settings=False, chat_model=supplied)
    await branch.chat_model.endpoint.call(request_kwargs)
    await supplied.endpoint.call(request_kwargs)

    assert [payload["model"] for payload in sent_payloads] == [model, model]
    assert [payload[key] for payload in sent_payloads] == [expected, original]
    if endpoint == "responses":
        assert "reasoning_effort" not in sent_payloads[0]
    assert branch.chat_model is not supplied
    assert branch.chat_model.executor is supplied.executor is supplied_executor
    assert branch.chat_model.provider_session_id == supplied.provider_session_id == "caller-session"
    assert supplied.endpoint.config.kwargs == supplied_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "origin", "expected"),
    [
        pytest.param(None, "explicit", "high", id="configured-explicit"),
        pytest.param(None, "profile", "high", id="configured-profile"),
        pytest.param(None, "default", None, id="configured-default-origin"),
        pytest.param(f"anthropic/{ANTHROPIC_MODEL}", "explicit", "high", id="named-control"),
    ],
)
async def test_configured_agent_effort_reaches_payload(
    sent_payloads, tmp_path, model, origin, expected
):
    if origin == "default":
        spec = AgentSpec.coding(secure=False, context_management=False)
    else:
        spec = AgentSpec.compose("implementer", model=model, effort="high")
        if origin == "profile":
            path = tmp_path / "agent.yaml"
            spec.to_yaml(path)
            spec = AgentSpec.from_yaml(path)

    branch = await create_agent(spec, load_settings=False)
    await branch.chat_model.endpoint.call(CHAT_REQUEST)

    (payload,) = sent_payloads
    assert payload["model"] == ANTHROPIC_MODEL
    assert (payload.get("output_config") or {}).get("effort") == expected


@pytest.mark.asyncio
async def test_unset_effort_keeps_supplied_model_and_its_prepared_effort(sent_payloads):
    supplied = iModel(provider="openai", model=OPENAI_MODEL, reasoning_effort="low")
    spec = AgentSpec.coding(secure=False, context_management=False)

    branch = await create_agent(spec, load_settings=False, chat_model=supplied)
    await branch.chat_model.endpoint.call(CHAT_REQUEST)

    (payload,) = sent_payloads
    assert payload["reasoning_effort"] == "low"
    assert branch.chat_model is supplied


@pytest.mark.asyncio
async def test_assigning_effort_replaces_the_prepared_effort(sent_payloads):
    supplied = iModel(provider="openai", model=OPENAI_MODEL, reasoning_effort="low")
    spec = AgentSpec.compose("implementer", effort="low")
    object.__setattr__(spec, "_effort_in_supplied_model", True)
    spec.effort = "high"

    branch = await create_agent(spec, load_settings=False, chat_model=supplied)
    await branch.chat_model.endpoint.call(CHAT_REQUEST)

    (payload,) = sent_payloads
    assert payload["reasoning_effort"] == "high"
    assert supplied.endpoint.config.kwargs["reasoning_effort"] == "low"


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [True, False], ids=["supplied-model", "configured-model"])
async def test_unsupported_explicit_effort_is_refused_before_request(
    sent_payloads, monkeypatch, supplied
):
    _configure_model_defaults(monkeypatch, "openai", "gpt-4.1")
    chat_model = iModel(provider="openai", model="gpt-4.1") if supplied else None
    spec = AgentSpec.compose("implementer", effort="high")

    with pytest.raises(_AgentSettingError, match=r"openai.*gpt-4\.1.*high"):
        await create_agent(spec, load_settings=False, chat_model=chat_model)

    assert sent_payloads == []


@pytest.mark.asyncio
async def test_effort_on_supplied_model_mapping_keeps_serialized_config(sent_payloads):
    supplied = iModel(provider="openai", model=OPENAI_MODEL, reasoning_effort="low").to_dict()
    original = deepcopy(supplied)
    spec = AgentSpec.compose("implementer", effort="high")

    branch = await create_agent(spec, load_settings=False, chat_model=supplied)
    await branch.chat_model.endpoint.call(CHAT_REQUEST)

    (payload,) = sent_payloads
    assert payload["reasoning_effort"] == "high"
    assert supplied == original
