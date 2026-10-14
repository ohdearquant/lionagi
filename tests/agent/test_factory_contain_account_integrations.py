# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AgentSpec.contain_account_integrations through create_agent.

Codex attaches the signed-in account's app connectors and plugins whatever MCP
servers a request carries, and a connector acts as the account. A library
caller could not turn them off: the switch existed on the request and on CLI
profiles only. The assertions read the argv the child is spawned with.
"""

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import create_agent
from lionagi.agent.spec import AgentSpec
from lionagi.service.imodel import iModel


def _features_switched(args: list[str]) -> list[tuple[str, str]]:
    return [
        (args[i], args[i + 1])
        for i in range(len(args) - 1)
        if args[i] in ("--enable", "--disable") and args[i + 1] in ("apps", "plugins")
    ]


def _argv(model: iModel) -> list[str]:
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    return payload["request"].as_cmd_args()


async def _branch(model: str, **spec_kwargs):
    spec = AgentSpec.compose("reviewer", model=model, **spec_kwargs)
    return await create_agent(spec, load_settings=False)


async def test_a_contained_codex_agent_turns_apps_and_plugins_off():
    branch = await _branch("codex/gpt-5.5", contain_account_integrations=True)
    assert _features_switched(_argv(branch.chat_model)) == [
        ("--disable", "apps"),
        ("--disable", "plugins"),
    ]


@pytest.mark.parametrize("model", ["openai-codex/gpt-5.5", "Codex/gpt-5.5", "CODEX/gpt-5.5"])
async def test_every_spelling_of_codex_is_contained(model):
    branch = await _branch(model, contain_account_integrations=True)
    assert _features_switched(_argv(branch.chat_model)) == [
        ("--disable", "apps"),
        ("--disable", "plugins"),
    ]


async def test_a_codex_model_named_without_a_provider_is_contained(monkeypatch):
    """A model with no provider prefix takes the configured chat provider."""
    from lionagi import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "settings",
        config_mod.settings.model_copy(update={"LIONAGI_CHAT_PROVIDER": "codex"}),
    )
    branch = await _branch("gpt-5.5", contain_account_integrations=True)
    assert _features_switched(_argv(branch.chat_model)) == [
        ("--disable", "apps"),
        ("--disable", "plugins"),
    ]


async def test_containment_without_a_spec_model_raises():
    """With no model the branch builds its default one, which would run uncontained."""
    spec = AgentSpec.compose("reviewer", contain_account_integrations=True)
    spec.model = None
    with pytest.raises(ConfigurationError, match="requires spec.model"):
        await create_agent(spec, load_settings=False)


async def test_an_uncontained_codex_agent_leaves_them_as_the_account_sets_them():
    """Control: the default argv names neither feature."""
    branch = await _branch("codex/gpt-5.5")
    assert _features_switched(_argv(branch.chat_model)) == []


async def test_containment_survives_a_restored_model():
    """It is a restriction, so it is kept with the endpoint's config: a model
    restored from its serialized form is contained without being asked again."""
    branch = await _branch("codex/gpt-5.5", contain_account_integrations=True)
    restored = iModel.from_dict(branch.chat_model.to_dict())
    assert _features_switched(_argv(restored)) == [
        ("--disable", "apps"),
        ("--disable", "plugins"),
    ]


async def test_a_provider_without_account_integrations_is_built_unchanged():
    contained = await _branch("claude_code/haiku", contain_account_integrations=True)
    plain = await _branch("claude_code/haiku")
    assert _argv(contained.chat_model) == _argv(plain.chat_model)


async def test_containment_with_a_caller_supplied_chat_model_raises():
    """The factory applies it while building the endpoint, so a model built
    elsewhere would run uncontained; that is refused, not ignored."""
    spec = AgentSpec.compose("reviewer", model="codex/gpt-5.5", contain_account_integrations=True)
    model = iModel(provider="codex", model="gpt-5.5", api_key="dummy")
    with pytest.raises(ConfigurationError, match="contain_account_integrations"):
        await create_agent(spec, load_settings=False, chat_model=model)


def test_the_setting_round_trips_through_yaml(tmp_path):
    path = tmp_path / "spec.yaml"
    AgentSpec.compose("reviewer", contain_account_integrations=True).to_yaml(path)
    assert AgentSpec.from_yaml(path).contain_account_integrations is True


def test_a_setting_that_is_not_a_boolean_is_refused_from_yaml(tmp_path):
    """bool("false") is True, so a quoted value is refused rather than read as set."""
    path = tmp_path / "spec.yaml"
    AgentSpec.compose("reviewer").to_yaml(path)
    path.write_text(path.read_text() + 'contain_account_integrations: "false"\n')
    with pytest.raises(ValueError, match="true or false"):
        AgentSpec.from_yaml(path)


def test_an_unset_setting_is_not_written_to_yaml(tmp_path):
    """A spec saved without it reads exactly as one saved before it existed."""
    path = tmp_path / "spec.yaml"
    AgentSpec.compose("reviewer").to_yaml(path)
    assert "contain_account_integrations" not in path.read_text()
    assert AgentSpec.from_yaml(path).contain_account_integrations is False
