# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AgentSpec.prompt_caching through create_agent.

The endpoint option (ADR-0127) was reachable from an iModel a caller built and
from CLI profiles, but not from an agent this factory builds, so an Engine's
agents sent every request uncached.
"""

import os
from unittest.mock import patch

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.service.imodel import iModel

MODEL = "anthropic/claude-haiku-5-5"


async def _branch(model: str | None, **spec_kwargs):
    spec = AgentSpec.compose("reviewer", model=model, **spec_kwargs)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        return await create_agent(spec, load_settings=False)


async def test_an_anthropic_agent_places_the_prompt_cache():
    branch = await _branch(MODEL, prompt_caching=True)
    assert branch.chat_model.endpoint.config.prompt_caching is True
    assert branch.chat_model.endpoint.places_prompt_cache is True


async def test_an_agent_without_it_does_not():
    """Control: the default spec leaves the option off."""
    branch = await _branch(MODEL)
    assert branch.chat_model.endpoint.config.prompt_caching is False
    assert branch.chat_model.endpoint.places_prompt_cache is False


async def test_it_stays_on_the_model_it_was_set_for():
    cached = await _branch(MODEL, prompt_caching=True)
    plain = await _branch(MODEL)
    assert cached.chat_model.endpoint.config is not plain.chat_model.endpoint.config
    assert plain.chat_model.endpoint.places_prompt_cache is False


async def test_a_clone_of_the_agent_places_it_too():
    """A worker cloned from the agent shares its API model, so it caches as well."""
    branch = await _branch(MODEL, prompt_caching=True)
    assert branch.clone().chat_model.endpoint.places_prompt_cache is True


async def test_a_cli_agent_with_it_is_built_and_places_nothing():
    """An endpoint that does not declare prefix caching sends its requests unchanged."""
    branch = await _branch("claude_code/haiku", prompt_caching=True)
    assert branch.chat_model.endpoint.places_prompt_cache is False


async def test_a_caller_supplied_chat_model_refuses_it():
    spec = AgentSpec.compose("reviewer", model=MODEL, prompt_caching=True)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model="claude-haiku-5-5")
        with pytest.raises(ConfigurationError, match="caller-supplied chat_model"):
            await create_agent(spec, load_settings=False, chat_model=model)


async def test_no_spec_model_refuses_it():
    """With no model the branch builds its default one, which would not cache."""
    spec = AgentSpec.compose("reviewer", prompt_caching=True)
    spec.model = None
    with pytest.raises(ConfigurationError, match="requires spec.model"):
        await create_agent(spec, load_settings=False)


def test_the_yaml_round_trip_keeps_it(tmp_path):
    path = tmp_path / "agent.yaml"
    AgentSpec.compose("reviewer", model=MODEL, prompt_caching=True).to_yaml(path)
    assert "prompt_caching: true" in path.read_text()
    assert AgentSpec.from_yaml(path).prompt_caching is True


def test_the_yaml_of_a_spec_without_it_names_nothing(tmp_path):
    path = tmp_path / "agent.yaml"
    AgentSpec.compose("reviewer", model=MODEL).to_yaml(path)
    assert "prompt_caching" not in path.read_text()
    assert AgentSpec.from_yaml(path).prompt_caching is False


@pytest.mark.parametrize("raw", ["'false'", "''", "1"])
def test_a_yaml_value_that_is_not_a_boolean_is_refused(tmp_path, raw):
    path = tmp_path / "agent.yaml"
    path.write_text(f"role: reviewer\nprompt_caching: {raw}\n")
    with pytest.raises(ValueError, match="prompt_caching must be true or false"):
        AgentSpec.from_yaml(path)
