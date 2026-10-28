# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AgentSpec.max_tokens through create_agent.

The Anthropic endpoint sends its own 8192 when no layer names an output limit, and an
agent this factory builds had no way to name one, so a reviewer that spent the whole
limit on thinking produced no text. The limit is read off the payload the endpoint
would send, not off the spec.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.providers.anthropic.messages import ANTHROPIC_DEFAULT_MAX_TOKENS
from lionagi.service.imodel import iModel

MODEL = "anthropic/claude-haiku-5-5"


async def _branch(model: str | None, **spec_kwargs):
    spec = AgentSpec.compose("reviewer", model=model, **spec_kwargs)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        return await create_agent(spec, load_settings=False)


def _sent_max_tokens(branch) -> int:
    endpoint = branch.chat_model.endpoint
    request = {"messages": [{"role": "user", "content": "x"}], "model": "claude-haiku-5-5"}
    payload, _ = endpoint.create_payload(request)
    return payload["max_tokens"]


async def test_an_anthropic_agent_sends_the_limit_the_spec_names():
    branch = await _branch(MODEL, max_tokens=16384)
    assert branch.chat_model.endpoint.config.kwargs["max_tokens"] == 16384
    assert _sent_max_tokens(branch) == 16384


async def test_an_agent_without_it_sends_the_providers_default():
    """Control: the same build with the field unset sends the endpoint's 8192, so the
    assertion above reads the limit and not a default that already matched."""
    branch = await _branch(MODEL)
    assert "max_tokens" not in branch.chat_model.endpoint.config.kwargs
    assert _sent_max_tokens(branch) == ANTHROPIC_DEFAULT_MAX_TOKENS == 8192


async def test_it_stays_on_the_model_it_was_set_for():
    limited = await _branch(MODEL, max_tokens=16384)
    plain = await _branch(MODEL)
    assert _sent_max_tokens(limited) == 16384
    assert _sent_max_tokens(plain) == 8192


async def test_a_clone_of_the_agent_sends_it_too():
    branch = await _branch(MODEL, max_tokens=16384)
    assert _sent_max_tokens(branch.clone()) == 16384


async def test_an_effort_suffix_does_not_displace_it():
    """The suffix is dropped for this provider; the limit still reaches the request."""
    branch = await _branch(f"{MODEL}-high", max_tokens=16384)
    assert _sent_max_tokens(branch) == 16384


async def test_a_cli_agent_refuses_it():
    with pytest.raises(ConfigurationError, match="no output-token limit to set"):
        await _branch("claude_code/haiku", max_tokens=16384)


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-5.5",
        "openrouter/z-ai/glm-5.2",
        "groq/llama-4-70b",
        "gemini/gemini-4-argon",
        "deepseek/deepseek-v4-flash",
        "ollama/llama4",
    ],
)
async def test_an_api_provider_without_a_tested_mapping_refuses_it(model):
    """OpenAI chat names the field max_completion_tokens and Responses max_output_tokens: a
    provider not in the tested set is refused, never handed a key it may drop or reject."""
    with pytest.raises(ConfigurationError, match="no tested mapping"):
        await _branch(model, max_tokens=16384)


async def test_an_alias_of_a_provider_without_a_mapping_refuses_it_too():
    with pytest.raises(ConfigurationError, match="no tested mapping"):
        await _branch("OpenAI/gpt-5.5", max_tokens=16384)


async def test_a_caller_supplied_chat_model_refuses_it():
    spec = AgentSpec.compose("reviewer", model=MODEL, max_tokens=16384)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model="claude-haiku-5-5")
        with pytest.raises(ConfigurationError, match="caller-supplied chat_model"):
            await create_agent(spec, load_settings=False, chat_model=model)


async def test_no_spec_model_refuses_it():
    spec = AgentSpec.compose("reviewer", max_tokens=16384)
    spec.model = None
    with pytest.raises(ConfigurationError, match="requires spec.model"):
        await create_agent(spec, load_settings=False)


async def test_an_endpoint_that_does_not_keep_the_limit_refuses_it():
    """An endpoint whose request settings drop the limit would send the provider's default
    while the spec read as set; the factory asks the endpoint it built."""

    def drops_it(*, provider, model, **extra):
        return SimpleNamespace(
            endpoint=SimpleNamespace(
                config=SimpleNamespace(kwargs={}, prompt_caching=False), _runtime_state={}
            )
        )

    spec = AgentSpec.compose("reviewer", model=MODEL, max_tokens=16384)
    with patch("lionagi.service.imodel.iModel", drops_it):
        with pytest.raises(ConfigurationError, match="did not keep it"):
            await create_agent(spec, load_settings=False)


def test_the_yaml_round_trip_keeps_it(tmp_path):
    path = tmp_path / "agent.yaml"
    AgentSpec.compose("reviewer", model=MODEL, max_tokens=16384).to_yaml(path)
    assert "max_tokens: 16384" in path.read_text()
    assert AgentSpec.from_yaml(path).max_tokens == 16384


def test_the_yaml_of_a_spec_without_it_names_nothing(tmp_path):
    path = tmp_path / "agent.yaml"
    AgentSpec.compose("reviewer", model=MODEL).to_yaml(path)
    assert "max_tokens" not in path.read_text()
    assert AgentSpec.from_yaml(path).max_tokens is None


@pytest.mark.parametrize("raw", ["'16384'", "''", "true", "0", "-5", "1.5"])
def test_a_yaml_value_that_is_not_a_positive_integer_is_refused(tmp_path, raw):
    path = tmp_path / "agent.yaml"
    path.write_text(f"role: reviewer\nmax_tokens: {raw}\n")
    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        AgentSpec.from_yaml(path)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "16384"])
def test_a_spec_built_with_a_limit_that_is_not_a_positive_integer_is_refused(bad):
    """compose and the constructor take the same refusal as the YAML reader."""
    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        AgentSpec.compose("reviewer", model=MODEL, max_tokens=bad)
    good = AgentSpec.compose("reviewer", model=MODEL)
    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        AgentSpec(profile=good.profile, model=MODEL, max_tokens=bad)
