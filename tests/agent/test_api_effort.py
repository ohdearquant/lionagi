# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Agent effort reaches API request payloads."""

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["low", "max"])
async def test_anthropic_effort_merges_existing_output_format(monkeypatch, effort):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    branch = await create_agent(
        AgentSpec.compose("implementer", model="anthropic/claude-haiku-5-5", effort=effort),
        load_settings=False,
    )
    output_format = {"type": "json_schema", "schema": {"type": "object"}}
    payload, _ = branch.chat_model.endpoint.create_payload(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"format": output_format},
        }
    )
    assert payload["output_config"] == {"effort": effort, "format": output_format}


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["high", "xhigh"])
async def test_openai_chat_agent_carries_effort(monkeypatch, effort):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    branch = await create_agent(
        AgentSpec.compose("implementer", model="openai/gpt-6-luna", effort=effort),
        load_settings=False,
    )
    endpoint = branch.chat_model.endpoint
    for _ in range(2):
        payload, _ = endpoint.create_payload({"messages": [{"role": "user", "content": "hi"}]})
        assert payload["reasoning_effort"] == effort


@pytest.mark.asyncio
async def test_unknown_effort_provider_is_refused():
    with pytest.raises(ConfigurationError, match="groq.*model.*high"):
        await create_agent(
            AgentSpec.compose("implementer", model="groq/example-model", effort="high"),
            load_settings=False,
        )


def test_known_reasoning_model_remains_reasoning_across_repeated_requests():
    from lionagi.providers.openai._chat_schemas import OpenAIChatCompletionsRequest

    for model in ("gpt-5", "o3", "gpt-5"):
        payload = OpenAIChatCompletionsRequest(
            model=model, messages=[{"role": "user", "content": "hi"}], reasoning_effort="high"
        )
        assert payload.reasoning_effort == "high"


def test_responses_preserves_reasoning_siblings(monkeypatch):
    from lionagi.service.imodel import iModel

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    model = iModel(
        provider="openai", endpoint="responses", model="gpt-6-luna", reasoning={"effort": "high"}
    )
    reasoning = {"summary": "auto"}
    payload, _ = model.endpoint.create_payload({"input": "hello", "reasoning": reasoning})
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert reasoning == {"summary": "auto"}


@pytest.mark.asyncio
async def test_anthropic_call_layers_keep_effort_and_do_not_mutate_inputs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    branch = await create_agent(
        AgentSpec.compose("implementer", model="anthropic/claude-haiku-5-5", effort="max"),
        load_settings=False,
    )
    endpoint = branch.chat_model.endpoint
    request = {
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"format": {"type": "json_schema"}, "effort": None},
    }
    payload, _ = endpoint.create_payload(request, output_config={"effort": "low"})
    assert payload["output_config"] == {"format": {"type": "json_schema"}, "effort": "low"}
    assert request["output_config"]["effort"] is None
    payload, _ = endpoint.create_payload({**request, "output_config": None})
    assert payload["output_config"]["effort"] == "max"
    assert endpoint.config.kwargs["output_config"] == {"effort": "max"}


def test_responses_null_does_not_erase_default_effort(monkeypatch):
    from lionagi.service.imodel import iModel

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    endpoint = iModel(
        provider="openai", endpoint="responses", model="gpt-6-luna", reasoning={"effort": "high"}
    ).endpoint
    for value in (None, {"effort": None, "summary": "auto"}):
        payload, _ = endpoint.create_payload({"input": "hello", "reasoning": value})
        assert payload["reasoning"]["effort"] == "high"


def test_responses_maps_flat_effort_to_reasoning(monkeypatch):
    from lionagi.service.imodel import iModel

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    endpoint = iModel(
        provider="openai", endpoint="responses", model="gpt-6-luna", reasoning_effort="high"
    ).endpoint
    payload, _ = endpoint.create_payload({"input": "hello", "reasoning": {"summary": "auto"}})
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "reasoning_effort" not in payload


def test_explicit_effort_is_forwarded_for_provider_validation():
    from lionagi.providers.openai._chat_schemas import OpenAIChatCompletionsRequest

    payload = OpenAIChatCompletionsRequest(
        model="gpt-4.1", messages=[{"role": "user", "content": "hi"}], reasoning_effort="high"
    )
    assert payload.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_invalid_anthropic_intermediate_layer_is_refused(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    branch = await create_agent(
        AgentSpec.compose("implementer", model="anthropic/claude-haiku-5-5", effort="high"),
        load_settings=False,
    )
    with pytest.raises(ValueError, match="output_config"):
        branch.chat_model.endpoint.create_payload(
            {"messages": [{"role": "user", "content": "hi"}], "output_config": "bad"},
            output_config={"format": {"type": "json_schema"}},
        )


def test_invalid_responses_intermediate_layer_is_refused(monkeypatch):
    from lionagi.service.imodel import iModel

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    endpoint = iModel(
        provider="openai", endpoint="responses", model="gpt-6-luna", reasoning_effort="high"
    ).endpoint
    with pytest.raises(ValueError, match="reasoning"):
        endpoint.create_payload({"input": "hi", "reasoning": "bad"}, reasoning={"summary": "auto"})
