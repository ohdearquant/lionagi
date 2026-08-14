# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o flow` / `li o fanout` (and `li play`, which expands into `li o flow`)
share `setup_orchestration()`. Naming neither an agent nor a model there is a
request to orchestrate, not an incomplete command, so it resolves to the
orchestrator profile instead of refusing.

The refusal survives for the case it was written for: a caller who did name an
agent, whose profile carries no model.

Because that resolution happens inside `setup_orchestration`, the record of the
run has to be told about it: a defaulted run orchestrates under a profile its
caller never named, and a record that repeats the caller's own (empty) argument
cannot tell such a run from one that used no profile at all. The second half of
this module follows the resolved name out to what lands on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli._providers import AgentProfile, AgentProfileNotFoundError
from lionagi.cli._runs import RunDir
from lionagi.cli.orchestrate._orchestration import (
    DEFAULT_ORCHESTRATOR_AGENT,
    setup_orchestration,
)


class _Reached(Exception):
    """Raised in place of building an imodel, to stop the call once the
    agent/model resolution under test has already happened."""


@pytest.fixture
def resolution_probe(monkeypatch, tmp_path):
    """Capture which profile name setup_orchestration loads, then stop the call
    before it builds anything. Returns the list of names passed to
    load_agent_profile."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    loaded: list[str] = []

    def _fake_load_agent_profile(name, *a, **kw):
        loaded.append(name)
        return SimpleNamespace(model="claude", effort=None, yolo=False, fast_mode=False)

    def _stop(*a, **kw):
        raise _Reached

    monkeypatch.setattr(orch_mod, "load_agent_profile", _fake_load_agent_profile)
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", _stop)
    return loaded


async def _run(**overrides):
    kwargs = dict(
        pattern_name="Fanout",
        model_spec=None,
        agent_name=None,
        save_dir=None,
        cwd=None,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
    )
    kwargs.update(overrides)
    return await setup_orchestration(**kwargs)


@pytest.mark.asyncio
async def test_naming_neither_agent_nor_model_defaults_to_the_orchestrator(resolution_probe):
    """The bare case is the one the directive is about: no agent, no model."""
    with pytest.raises(_Reached):
        await _run()

    assert resolution_probe == [DEFAULT_ORCHESTRATOR_AGENT]


@pytest.mark.asyncio
async def test_a_named_model_is_honoured_and_loads_no_profile(resolution_probe):
    """Naming compute is still naming compute — the default must not override
    it, and must not drag a profile in behind it."""
    with pytest.raises(_Reached):
        await _run(model_spec="claude")

    assert resolution_probe == []


@pytest.mark.asyncio
async def test_a_named_agent_is_honoured_over_the_default(resolution_probe):
    with pytest.raises(_Reached):
        await _run(agent_name="reviewer")

    assert resolution_probe == ["reviewer"]


@pytest.mark.asyncio
async def test_a_named_agent_with_no_model_still_refuses_and_names_itself(monkeypatch):
    """The refusal is not deleted, it is narrowed. It now fires only where the
    caller chose an agent we could not resolve to a model, so it says which."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    def _modelless_profile(name, *a, **kw):
        return SimpleNamespace(model=None, effort=None, yolo=False, fast_mode=False)

    def _boom(*a, **kw):
        raise AssertionError("must refuse before building an imodel")

    monkeypatch.setattr(orch_mod, "load_agent_profile", _modelless_profile)
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", _boom)

    with pytest.raises(ConfigurationError) as exc_info:
        await _run(agent_name="profile-without-a-model")

    assert "profile-without-a-model" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a_missing_orchestrator_profile_says_what_was_assumed(monkeypatch):
    """The default reaches for a profile the caller never mentioned, so if it is
    not there the raw loader error names something they did not ask for. Explain
    the assumption instead."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    def _absent(name, *a, **kw):
        raise AgentProfileNotFoundError(f"Agent profile '{name}' not found")

    monkeypatch.setattr(orch_mod, "load_agent_profile", _absent)

    with pytest.raises(ConfigurationError) as exc_info:
        await _run()

    message = str(exc_info.value)
    assert DEFAULT_ORCHESTRATOR_AGENT in message
    assert "name an agent or a model" in message


@pytest.mark.asyncio
async def test_a_named_agent_that_is_missing_still_raises_the_loader_error(monkeypatch):
    """The explanation above is for the assumption we made. A caller who named
    the profile themselves gets the loader's own error, which lists what is
    available."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    def _absent(name, *a, **kw):
        raise AgentProfileNotFoundError(f"Agent profile '{name}' not found")

    monkeypatch.setattr(orch_mod, "load_agent_profile", _absent)

    with pytest.raises(AgentProfileNotFoundError) as exc_info:
        await _run(agent_name="no-such-agent")

    assert "no-such-agent" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a_profile_that_cannot_be_read_is_not_reported_as_a_missing_default(monkeypatch):
    """The loader finds the file and then reads it, and a file that disappears
    between those two steps raises the same builtin type as a missing profile.
    Calling that "no orchestrator profile was found" sends the reader to create
    a profile that is already there."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    def _found_then_vanished(name, *a, **kw):
        raise FileNotFoundError(f"[Errno 2] No such file or directory: '{name}.md'")

    monkeypatch.setattr(orch_mod, "load_agent_profile", _found_then_vanished)

    with pytest.raises(FileNotFoundError) as exc_info:
        await _run()

    assert not isinstance(exc_info.value, ConfigurationError)
    assert "No such file or directory" in str(exc_info.value)


@pytest.mark.asyncio
async def test_the_default_does_not_fire_when_a_modelless_agent_was_named(monkeypatch):
    """Guards the interaction between the two behaviours: a modelless named
    agent must reach the refusal, never silently fall through to the default
    and orchestrate under compute the caller did not ask for."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    loaded: list[str] = []

    def _modelless_profile(name, *a, **kw):
        loaded.append(name)
        return SimpleNamespace(model=None, effort=None, yolo=False, fast_mode=False)

    monkeypatch.setattr(orch_mod, "load_agent_profile", _modelless_profile)

    with pytest.raises(ConfigurationError):
        await _run(agent_name="profile-without-a-model")

    assert loaded == ["profile-without-a-model"]
    assert DEFAULT_ORCHESTRATOR_AGENT not in loaded


# The resolved name has to leave the function


@pytest.fixture
def completing_setup(monkeypatch, tmp_path):
    """Let `setup_orchestration` run to its return without building a provider,
    a branch or a session, so the env it hands back can be inspected.

    Only profile resolution is left live — that is what these tests are about.
    """
    import lionagi.cli.orchestrate._orchestration as orch_mod

    def _profile(name, *a, **kw):
        return AgentProfile(name=name, system_prompt="be an orchestrator", model="claude")

    imodel = MagicMock()
    imodel.endpoint.config.provider = "claude_code"
    imodel.endpoint.config.kwargs = {}

    def _allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="setup-run",
            state_root=tmp_path / "state",
            artifact_root=tmp_path / "artifacts",
        )
        run.ensure_state_dirs()
        return run

    monkeypatch.setattr(orch_mod, "load_agent_profile", _profile)
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", lambda *a, **kw: imodel)
    monkeypatch.setattr(orch_mod, "allocate_run", _allocate)
    monkeypatch.setattr(orch_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(orch_mod, "Branch", MagicMock())
    monkeypatch.setattr(orch_mod, "Session", MagicMock())
    monkeypatch.setattr(orch_mod, "OperationGraphBuilder", MagicMock())
    monkeypatch.setattr(orch_mod, "register_profile_injection", lambda *a, **kw: None)
    monkeypatch.setattr(orch_mod, "create_agent", AsyncMock(return_value=MagicMock()))


@pytest.mark.asyncio
async def test_a_defaulted_run_carries_out_the_name_it_resolved(completing_setup):
    """The caller named nothing, so nothing they passed can name the profile.
    The env has to, or the run is unattributable from here on."""
    env = await _run()

    assert env.orc_profile_name == DEFAULT_ORCHESTRATOR_AGENT


@pytest.mark.asyncio
async def test_a_named_agent_carries_out_its_own_name(completing_setup):
    env = await _run(agent_name="reviewer")

    assert env.orc_profile_name == "reviewer"


@pytest.mark.asyncio
async def test_naming_only_a_model_carries_out_no_name(completing_setup):
    """No profile was loaded, so there is no name to record. Recording one
    would claim a profile shaped this run when none did."""
    env = await _run(model_spec="claude_code/sonnet")

    assert env.orc_profile is None
    assert env.orc_profile_name is None


@pytest.mark.asyncio
async def test_the_carried_name_is_the_loaded_profiles_own(completing_setup, monkeypatch):
    """Read off the profile, not off the argument: the two can differ, and it is
    the profile that shaped the run."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    monkeypatch.setattr(
        orch_mod,
        "load_agent_profile",
        lambda name, *a, **kw: AgentProfile(name="orchestrator", system_prompt="s", model="claude"),
    )

    env = await _run(agent_name="orchestrator-alias")

    assert env.orc_profile_name == "orchestrator"


# …and reach the record a later reader has


@pytest.fixture
def temp_db_path(tmp_path, monkeypatch):
    """Keep the state DB the persist layer opens inside tmp_path."""
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")


def _persisting_env(tmp_path, *, orc_profile_name):
    """An env already past setup_orchestration, with a real run directory so the
    manifest it writes can be read back off disk."""
    from lionagi import Branch, Session
    from lionagi.cli.orchestrate._orchestration import OrchestrationEnv
    from lionagi.operations.builder import OperationGraphBuilder

    orc_branch = Branch(name="orchestrator")
    run = RunDir(
        run_id="record-run",
        state_root=tmp_path / "run-state",
        artifact_root=tmp_path / "run-artifacts",
    )
    run.ensure_state_dirs()
    run.ensure_artifact_root()
    profile = (
        None
        if orc_profile_name is None
        else AgentProfile(name=orc_profile_name, system_prompt="s", model="codex/gpt-5.6-sol")
    )
    return OrchestrationEnv(
        run=run,
        session=Session(default_branch=orc_branch),
        orc_branch=orc_branch,
        builder=OperationGraphBuilder(),
        orc_profile=profile,
        orc_profile_name=orc_profile_name,
        default_model_spec="codex/gpt-5.6-sol",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )


def _manifest(env) -> dict:
    return json.loads(Path(env.run.manifest_path).read_text())


@pytest.mark.asyncio
async def test_a_bare_fanout_run_names_the_profile_it_used_on_disk(temp_db_path, tmp_path):
    """`li o fanout` with no agent and no model: the manifest a later reader
    opens must say which profile the run orchestrated under. `agent_name=None`
    is passed here on purpose — it is what the bare command line gives."""
    from lionagi.cli.orchestrate import fanout as fanout_module

    env = _persisting_env(tmp_path, orc_profile_name=DEFAULT_ORCHESTRATOR_AGENT)

    with (
        patch.object(fanout_module, "setup_orchestration", AsyncMock(return_value=env)),
        # An empty assignment list is the shortest path from start_live_persist
        # to a clean terminal status — no worker or DAG machinery runs.
        patch.object(fanout_module, "plan", AsyncMock(return_value=[])),
    ):
        await fanout_module._run_fanout("codex/gpt-5.6-sol", "prompt", agent_name=None)

    assert _manifest(env)["agent_name"] == DEFAULT_ORCHESTRATOR_AGENT

    # The manifest and the session row are two records of the same run, and a
    # reader may open either. Both have to name the profile.
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        row = await db.get_session(str(env.session.id))
    assert row is not None
    assert row["agent_name"] == DEFAULT_ORCHESTRATOR_AGENT


@pytest.mark.asyncio
async def test_a_fanout_run_that_named_only_a_model_names_no_profile_on_disk(
    temp_db_path, tmp_path
):
    """No profile was used, so the record must not name one."""
    from lionagi.cli.orchestrate import fanout as fanout_module

    env = _persisting_env(tmp_path, orc_profile_name=None)

    with (
        patch.object(fanout_module, "setup_orchestration", AsyncMock(return_value=env)),
        patch.object(fanout_module, "plan", AsyncMock(return_value=[])),
    ):
        await fanout_module._run_fanout("codex/gpt-5.6-sol", "prompt", agent_name=None)

    assert _manifest(env)["agent_name"] is None


@pytest.mark.asyncio
async def test_a_bare_flow_run_names_the_profile_it_used_on_disk(temp_db_path, tmp_path):
    """The same for `li o flow`, which `li play` also expands into."""
    from lionagi.cli.orchestrate import flow as flow_module

    env = _persisting_env(tmp_path, orc_profile_name=DEFAULT_ORCHESTRATOR_AGENT)

    with (
        patch.object(flow_module, "setup_orchestration", AsyncMock(return_value=env)),
        patch.object(flow_module, "_run_flow_inner", AsyncMock(return_value="ok")),
    ):
        await flow_module._run_flow("codex/gpt-5.6-sol", "prompt", agent_name=None)

    assert _manifest(env)["agent_name"] == DEFAULT_ORCHESTRATOR_AGENT


@pytest.mark.asyncio
async def test_a_flow_run_that_named_an_agent_still_names_that_agent_on_disk(
    temp_db_path, tmp_path
):
    """A caller who did name an agent is recorded exactly as before."""
    from lionagi.cli.orchestrate import flow as flow_module

    env = _persisting_env(tmp_path, orc_profile_name="reviewer")

    with (
        patch.object(flow_module, "setup_orchestration", AsyncMock(return_value=env)),
        patch.object(flow_module, "_run_flow_inner", AsyncMock(return_value="ok")),
    ):
        await flow_module._run_flow("codex/gpt-5.6-sol", "prompt", agent_name="reviewer")

    assert _manifest(env)["agent_name"] == "reviewer"
