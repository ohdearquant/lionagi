# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A Claude agent carries the sandbox policy resolved for its agent profile name.

The exemption key is the agent profile the user passed, never the spec's role name.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli.orchestrate._orchestration import _confine_claude_worker
from lionagi.service.providers import _CLAUDE_PROVIDER_NAMES
from lionhub.agent.factory import create_agent
from lionhub.agent.spec import AgentSpec

child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")

CLAUDE = sorted(_CLAUDE_PROVIDER_NAMES)[0]
EXEMPT = "~/.npmrc"


@pytest.fixture(autouse=True)
def _exemption_for_one_profile(monkeypatch, tmp_path):
    """A throwaway HOME and a global setting that exempts one path for the profile ``builder``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    settings = {"child_sandbox": {"profiles": {"builder": {"allow_read": [EXEMPT]}}}}
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: settings)


def _model(provider, *, is_cli=True):
    config = SimpleNamespace(provider=provider, kwargs={})
    return SimpleNamespace(is_cli=is_cli, endpoint=SimpleNamespace(config=config))


def test_a_named_profile_gets_its_own_exemption():
    model = _model(CLAUDE)
    _confine_claude_worker(model, "builder")
    policy = model.endpoint.config.kwargs["child_sandbox"]
    assert policy["enabled"] is True
    assert EXEMPT not in policy["deny_read"]
    assert set(child_sandbox.DEFAULT_DENY_READ) - set(policy["deny_read"]) == {EXEMPT}


def test_a_bare_worker_and_another_profile_keep_the_full_list():
    for profile in (None, "reviewer"):
        model = _model(CLAUDE)
        _confine_claude_worker(model, profile)
        deny = model.endpoint.config.kwargs["child_sandbox"]["deny_read"]
        assert deny == list(child_sandbox.DEFAULT_DENY_READ)


def test_the_policy_is_a_plain_dict_that_survives_json():
    model = _model(CLAUDE)
    _confine_claude_worker(model, "builder")
    policy = model.endpoint.config.kwargs["child_sandbox"]
    assert isinstance(policy, dict)
    assert child_sandbox.policy_from_dict(json.loads(json.dumps(policy))) == (
        child_sandbox.resolve_child_sandbox("builder")
    )


def test_a_model_that_is_not_a_cli_is_left_alone():
    model = _model(CLAUDE, is_cli=False)
    _confine_claude_worker(model, "builder")
    assert model.endpoint.config.kwargs == {}


def test_a_cli_that_is_not_claude_is_left_alone():
    model = _model("codex")
    _confine_claude_worker(model, "builder")
    assert model.endpoint.config.kwargs == {}


def test_a_bad_setting_refuses_and_sets_no_policy(monkeypatch):
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"surprise": 1}}
    )
    model = _model(CLAUDE)
    with pytest.raises(ConfigurationError, match="unknown keys"):
        _confine_claude_worker(model, "builder")
    assert "child_sandbox" not in model.endpoint.config.kwargs


# --------------------------------------------------------------------------- create_agent


def _two_profile_settings():
    """A global block that exempts one path for the profile ``builder`` and for the role name."""
    entry = {"allow_read": [EXEMPT]}
    return {"child_sandbox": {"profiles": {"builder": entry, "implementer": entry}}}


async def _policy_of_built_agent(monkeypatch, **kwargs):
    monkeypatch.setattr(child_sandbox, "_global_settings", _two_profile_settings)
    spec = AgentSpec.compose("implementer", model="claude_code/sonnet")
    branch = await create_agent(spec, load_settings=False, **kwargs)
    return branch.chat_model.endpoint.config.kwargs["child_sandbox"]


def test_sandbox_profile_is_a_keyword_only_parameter_that_defaults_to_none():
    param = inspect.signature(create_agent).parameters["sandbox_profile"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


@pytest.mark.asyncio
async def test_create_agent_exempts_by_the_agent_profile_name(monkeypatch):
    policy = await _policy_of_built_agent(monkeypatch, sandbox_profile="builder")
    assert EXEMPT not in policy["deny_read"]
    assert set(child_sandbox.DEFAULT_DENY_READ) - set(policy["deny_read"]) == {EXEMPT}


@pytest.mark.asyncio
async def test_create_agent_never_matches_the_role_name(monkeypatch):
    # The spec's role is "implementer" and the global block names it, but only the agent
    # profile name given as sandbox_profile selects an exemption.
    for kwargs in ({"sandbox_profile": None}, {}):
        policy = await _policy_of_built_agent(monkeypatch, **kwargs)
        assert policy["deny_read"] == list(child_sandbox.DEFAULT_DENY_READ)


# --------------------------------------------------------------------------- the call sites

# Each command path that builds an agent and the least number of create_agent calls it holds.
# Every one of them names the agent profile it was given, so the exemption key is never the
# role the profile casts.
_CALL_SITES = {
    "lionagi.cli.agent": 1,
    "lionagi.cli.orchestrate._orchestration": 2,
    "lionagi.cli.orchestrate.fanout": 1,
}


def _create_agent_calls(module_name):
    origin = importlib.util.find_spec(module_name).origin
    tree = ast.parse(Path(origin).read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_agent"
    ]


@pytest.mark.parametrize("module_name", sorted(_CALL_SITES))
def test_every_create_agent_caller_passes_the_agent_profile_name(module_name):
    calls = _create_agent_calls(module_name)
    assert len(calls) >= _CALL_SITES[module_name]
    for call in calls:
        assert "sandbox_profile" in {keyword.arg for keyword in call.keywords}
