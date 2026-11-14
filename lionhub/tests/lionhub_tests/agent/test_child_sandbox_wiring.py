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
from lionhub.serve.cli.orchestrate._orchestration import _confine_claude_worker
from lionagi.service.providers import _CLAUDE_PROVIDER_NAMES
from lionhub.agent.factory import create_agent
from lionhub.agent.spec import AgentSpec

child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")

CLAUDE = sorted(_CLAUDE_PROVIDER_NAMES)[0]
EXEMPT = "~/.npmrc"


@pytest.fixture(autouse=True)
def _exemption_for_one_profile(monkeypatch, tmp_path):
    """A throwaway HOME and a global setting that exempts one path for the profile ``builder``,
    whose file sits in the throwaway home's global agents directory."""
    home = tmp_path / "home"
    (home / ".lionagi" / "agents").mkdir(parents=True)
    (home / ".lionagi" / "agents" / "builder.md").write_text("---\nname: builder\n---\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home / ".lionagi")
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
        assert deny == [*child_sandbox.DEFAULT_DENY_READ, "~/.lionagi/.env"]


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
        assert policy["deny_read"] == [*child_sandbox.DEFAULT_DENY_READ, "~/.lionagi/.env"]


@pytest.mark.asyncio
async def test_create_agent_confines_a_caller_supplied_claude_model_under_the_profile_name(
    monkeypatch,
):
    from lionagi import iModel

    monkeypatch.setattr(child_sandbox, "_global_settings", _two_profile_settings)
    model = iModel(provider="claude_code", model="sonnet")
    assert "child_sandbox" not in model.endpoint.config.kwargs
    spec = AgentSpec.compose("implementer")
    branch = await create_agent(
        spec, load_settings=False, chat_model=model, sandbox_profile="builder"
    )
    policy = branch.chat_model.endpoint.config.kwargs["child_sandbox"]
    assert set(child_sandbox.DEFAULT_DENY_READ) - set(policy["deny_read"]) == {EXEMPT}


@pytest.mark.asyncio
async def test_create_agent_leaves_a_caller_supplied_model_that_is_not_claude_code_alone(
    monkeypatch,
):
    from lionagi import iModel

    monkeypatch.setattr(child_sandbox, "_global_settings", _two_profile_settings)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    branch = await create_agent(
        AgentSpec.compose("implementer"),
        load_settings=False,
        chat_model=model,
        sandbox_profile="builder",
    )
    assert "child_sandbox" not in branch.chat_model.endpoint.config.kwargs


# --------------------------------------------------------------------------- the call sites

# Each command path that builds an agent and the least number of create_agent calls it holds.
# Every one of them names the agent profile it was given, so the exemption key is never the
# role the profile casts.
_CALL_SITES = {
    "lionhub.serve.cli.agent": 1,
    "lionhub.serve.cli.orchestrate._orchestration": 2,
    "lionhub.serve.cli.orchestrate.fanout": 1,
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


# --------------------------------------------------------------------------- branches built directly


@pytest.mark.asyncio
async def test_a_flow_orchestrator_with_a_loaded_profile_gets_that_profiles_exemption(
    monkeypatch, tmp_path
):
    """A profile orchestrator is built as a plain Branch, not by create_agent."""
    from lionhub.serve.cli.orchestrate import _orchestration as orch
    from lionhub.orchestration.runs import RunDir

    work = tmp_path / "work"
    work.mkdir()
    # The profile is the global one the exemption was written for.
    agents = tmp_path / "home" / ".lionagi" / "agents"
    (agents / "builder.md").write_text(f"---\nmodel: {CLAUDE}/haiku\n---\nOrchestrate.\n")
    monkeypatch.chdir(work)
    monkeypatch.setattr("lionhub.orchestration.spawn._plugin_agent_profiles", lambda: {})

    def allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="test-run",
            state_root=tmp_path / "state",
            artifact_root=tmp_path / "artifacts",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch, "allocate_run", allocate)
    env = await orch.setup_orchestration(
        pattern_name="Flow",
        model_spec=None,
        agent_name="builder",
        save_dir=None,
        cwd=str(work),
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
        mcp_config=None,
        no_mcp_config=True,
    )
    policy = env.orc_branch.chat_model.endpoint.config.kwargs["child_sandbox"]
    assert set(child_sandbox.DEFAULT_DENY_READ) - set(policy["deny_read"]) == {EXEMPT}


@pytest.mark.asyncio
async def test_a_plain_li_agent_profile_leg_is_confined_under_its_profile_name(
    monkeypatch, tmp_path
):
    """A ``-a <profile>`` leg with no role key is built as a plain Branch, not by create_agent."""
    from unittest.mock import AsyncMock

    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch
    from lionhub.serve.cli.orchestrate import _orchestration as orch
    from lionagi.service.manager import iModelManager
    from lionhub.orchestration.spawn import _parse_profile

    profile = _parse_profile("builder", "---\n---\nBuild.\n")
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )
    monkeypatch.setattr(agent_mod, "setup_agent_persist", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", AsyncMock(return_value="completed"))
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    confined = []
    monkeypatch.setattr(
        orch, "_confine_claude_worker", lambda model, name: confined.append((model, name))
    )

    async def fake_operate(self, instruction=None, **kw):
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)

    await agent_mod._run_agent("codex/model", "do the thing", agent_name="builder")

    assert [name for _, name in confined] == ["builder"]
