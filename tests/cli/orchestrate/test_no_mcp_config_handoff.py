# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li o flow` / `li o fanout` with `--no-mcp-config` must hand workers an empty
MCP server set, not nothing at all.

Handing over nothing is what the run does when it found no config: the worker
then falls back to whatever its CLI discovers for itself. That is the opposite
of what the flag asked for, and it is silent, so the two states are checked
here on the command line the worker would actually be spawned with rather than
on whether a helper was called.
"""

from __future__ import annotations

import json

import pytest

import lionagi.cli.orchestrate._orchestration as orch_mod
from lionagi.cli._providers import AgentProfile
from lionagi.cli._runs import RunDir
from lionagi.cli.orchestrate._orchestration import build_worker_branch, setup_orchestration
from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest


@pytest.fixture
def tmp_run(monkeypatch, tmp_path):
    """Isolate state while simulating host routing for implementers."""
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)

    def _load_profile(name):
        if name == "implementer":
            body = "Use the configured provider."
            return AgentProfile(
                name=name,
                model="codex/gpt-5",
                system_prompt=body,
                raw_body=body,
            )
        raise FileNotFoundError(name)

    def _allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="test-run",
            state_root=tmp_path / "state",
            artifact_root=tmp_path / "artifacts",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch_mod, "load_agent_profile", _load_profile)
    monkeypatch.setattr(orch_mod, "allocate_run", _allocate)
    return launch_dir


async def _setup(*, no_mcp_config: bool):
    return await setup_orchestration(
        pattern_name="Fanout",
        model_spec="claude_code/sonnet",
        agent_name=None,
        save_dir=None,
        cwd=None,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
        no_mcp_config=no_mcp_config,
    )


async def _cli_args(imodel) -> list[str]:
    """The argv the CLI child would be spawned with, off the live request."""
    api_call = await imodel.create_event(prompt="hi")
    request = api_call.payload["request"]
    assert isinstance(request, ClaudeCodeRequest)
    return request.as_cmd_args()


@pytest.mark.asyncio
async def test_no_mcp_config_reaches_the_worker_as_an_empty_server_set(tmp_run):
    env = await _setup(no_mcp_config=True)

    # The empty set is a decision and survives to the workers as one.
    assert env.mcp_servers == {}

    branch, _, _, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="implementer",
        model_override="claude_code/sonnet",
    )
    args = await _cli_args(branch.chat_model)

    assert "--mcp-config" in args
    assert args[args.index("--mcp-config") + 1] == json.dumps({"mcpServers": {}})
    # Without this the empty set is merged with whatever the CLI finds by
    # itself, which leaves the discovered servers in place.
    assert "--strict-mcp-config" in args


@pytest.mark.asyncio
async def test_orchestrator_gets_the_same_empty_server_set(tmp_run):
    env = await _setup(no_mcp_config=True)
    args = await _cli_args(env.orc_branch.chat_model)

    assert args[args.index("--mcp-config") + 1] == json.dumps({"mcpServers": {}})
    assert "--strict-mcp-config" in args


@pytest.mark.asyncio
async def test_no_config_found_hands_over_nothing_and_says_nothing(tmp_run):
    """The contrast case: no flag and no config to find. The worker is handed
    no set at all and keeps its own discovery, so no MCP argument is emitted."""
    env = await _setup(no_mcp_config=False)

    assert env.mcp_servers is None

    branch, _, _, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="implementer",
        model_override="claude_code/sonnet",
    )
    args = await _cli_args(branch.chat_model)

    assert "--mcp-config" not in args
    assert "--strict-mcp-config" not in args


def test_refusal_reaches_a_codex_worker_as_a_disabled_server(caplog, codex_home):
    """codex has no per-request server set and no wholesale clear, so a refusal
    reaches it as every server it would have loaded, disabled by name."""
    import logging

    from lionagi.cli._providers import build_imodel_from_spec

    codex_home.write_config({"khive": {"command": "kkernel"}})
    imodel = build_imodel_from_spec("codex/gpt-5")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        orch_mod._hand_mcp_servers(imodel, {}, label="reviewer-2")

    assert imodel.endpoint.config.kwargs["config_overrides"] == {"mcp_servers.khive.enabled": False}
    assert caplog.text == ""


def test_a_resolved_set_reaches_a_codex_worker(caplog, codex_home):
    """The run's set is what the worker gets, over whichever transport its
    provider has for one."""
    import logging

    from lionagi.cli._providers import build_imodel_from_spec

    codex_home.write_config({})
    imodel = build_imodel_from_spec("codex/gpt-5")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        orch_mod._hand_mcp_servers(imodel, {"khive": {"command": "kkernel"}}, label="reviewer-2")

    assert imodel.endpoint.config.kwargs["config_overrides"] == {
        "mcp_servers.khive.command": "kkernel"
    }
    assert caplog.text == ""


def test_refusal_is_reported_when_the_provider_cannot_carry_it(caplog):
    """A provider with no transport for a caller-resolved set cannot be told the
    set is empty. Saying so is the point — the caller passed a flag."""
    import logging

    from lionagi.cli._providers import build_imodel_from_spec

    imodel = build_imodel_from_spec("gemini-cli/gemini-3.5-flash")
    provider = imodel.endpoint.config.provider
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        orch_mod._hand_mcp_servers(imodel, {}, label="reviewer-2")

    assert "mcp_servers" not in imodel.endpoint.config.kwargs
    assert "reviewer-2" in caplog.text
    assert "--no-mcp-config" in caplog.text
    assert provider in caplog.text


def test_a_dropped_grant_is_reported_as_well_as_a_dropped_refusal(caplog):
    """A set the provider cannot carry leaves the worker without servers its
    instructions assume, which is worth at least as much of a word as a refusal."""
    import logging

    from lionagi.cli._providers import build_imodel_from_spec

    imodel = build_imodel_from_spec("gemini-cli/gemini-3.5-flash")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        orch_mod._hand_mcp_servers(imodel, {"khive": {"command": "kkernel"}}, label="reviewer-2")

    assert "mcp_servers" not in imodel.endpoint.config.kwargs
    assert "reviewer-2" in caplog.text
    assert "khive" in caplog.text


# The factory has a discovery of its own: handed no set, ``create_agent`` reads
# ``~/.lionagi/.mcp.json``, starts the servers it names as lionagi tools, and
# writes them into the CLI request over whatever the run already handed this
# worker. The cases below put a server in that file and check it reaches no
# worker the run resolved a set for.
_HOME_SERVER = {"home-only": {"command": "home-only-server"}}
_RUN_SERVER = {"run-set": {"command": "run-set-server"}}


@pytest.fixture
def home_mcp_config(monkeypatch, tmp_path):
    """A home directory whose ``.lionagi/.mcp.json`` names one server, and a
    record of every time the factory goes looking for it."""
    import lionagi.agent.factory as factory_mod

    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    (home / ".lionagi" / ".mcp.json").write_text(json.dumps({"mcpServers": _HOME_SERVER}))
    monkeypatch.setenv("HOME", str(home))

    discovered: list[str] = []

    async def _record_discovery(branch, spec, *, trust_project_settings=False):
        # Recorded instead of run: running it starts the servers the file names.
        discovered.append(spec.profile.role.name)
        return frozenset()

    monkeypatch.setattr(factory_mod, "_load_mcp", _record_discovery)
    return discovered


def _mcp_config_arg(args: list[str]) -> dict:
    assert "--mcp-config" in args
    return json.loads(args[args.index("--mcp-config") + 1])


async def _casts_worker_args(env) -> list[str]:
    # A built-in casts role with no profile of its own is the worker that goes
    # through create_agent; a profile with a body of its own does not.
    branch, _, profile, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="critic",
        model_override="claude_code/sonnet",
    )
    assert profile is None
    return await _cli_args(branch.chat_model)


@pytest.mark.asyncio
async def test_no_mcp_config_keeps_the_home_config_off_a_casts_worker(tmp_run, home_mcp_config):
    env = await _setup(no_mcp_config=True)
    args = await _casts_worker_args(env)

    assert _mcp_config_arg(args) == {"mcpServers": {}}
    assert "--strict-mcp-config" in args
    assert home_mcp_config == []


@pytest.mark.asyncio
async def test_a_found_run_set_is_the_set_a_casts_worker_gets(tmp_run, home_mcp_config):
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": _RUN_SERVER}))
    env = await _setup(no_mcp_config=False)
    assert env.mcp_servers == _RUN_SERVER

    args = await _casts_worker_args(env)

    assert _mcp_config_arg(args) == {"mcpServers": _RUN_SERVER}
    assert home_mcp_config == []


@pytest.mark.asyncio
async def test_a_found_run_set_is_the_set_the_orchestrator_gets(tmp_run, home_mcp_config):
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": _RUN_SERVER}))
    env = await _setup(no_mcp_config=False)
    args = await _cli_args(env.orc_branch.chat_model)

    assert _mcp_config_arg(args) == {"mcpServers": _RUN_SERVER}
    assert home_mcp_config == []


@pytest.mark.asyncio
async def test_no_mcp_config_keeps_the_home_config_off_the_synthesis_branch(
    tmp_run, home_mcp_config
):
    from lionagi.cli.orchestrate.fanout import _fresh_synthesis_branch

    env = await _setup(no_mcp_config=True)
    branch = await _fresh_synthesis_branch(env)
    args = await _cli_args(branch.chat_model)

    assert _mcp_config_arg(args) == {"mcpServers": {}}
    assert "--strict-mcp-config" in args
    assert home_mcp_config == []


@pytest.mark.asyncio
async def test_a_found_run_set_is_the_set_the_synthesis_branch_gets(tmp_run, home_mcp_config):
    from lionagi.cli.orchestrate.fanout import _fresh_synthesis_branch

    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": _RUN_SERVER}))
    env = await _setup(no_mcp_config=False)
    branch = await _fresh_synthesis_branch(env)
    args = await _cli_args(branch.chat_model)

    assert _mcp_config_arg(args) == {"mcpServers": _RUN_SERVER}
    assert home_mcp_config == []


@pytest.mark.asyncio
async def test_with_no_run_set_a_casts_worker_still_finds_the_home_config(tmp_run, home_mcp_config):
    """The contrast case: the run found nothing and was told nothing, so the
    worker keeps the factory's own discovery, home config included."""
    env = await _setup(no_mcp_config=False)
    assert env.mcp_servers is None

    args = await _casts_worker_args(env)

    assert _mcp_config_arg(args) == {"mcpServers": _HOME_SERVER}
    assert "critic" in home_mcp_config
