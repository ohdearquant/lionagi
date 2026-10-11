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
from pathlib import Path

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
    test_home = tmp_path / "home"
    test_home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))
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


async def _setup(
    *, no_mcp_config: bool, mcp_config: str | None = None, model_spec: str = "claude_code/sonnet"
):
    return await setup_orchestration(
        pattern_name="Fanout",
        model_spec=model_spec,
        agent_name=None,
        save_dir=None,
        cwd=None,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
        mcp_config=mcp_config,
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


@pytest.mark.asyncio
async def test_a_run_resolves_its_set_from_the_launch_dir_it_is_given(tmp_run, tmp_path):
    """A resumed flow hands setup the directory the run was launched from, and
    the set must come from there rather than from where the resume runs."""
    launched_from = tmp_path / "launched-from"
    launched_from.mkdir()
    (launched_from / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"from-launch-dir": {"command": "a"}}})
    )
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": {"from-here": {"command": "b"}}}))

    # Control: without a launch dir the config beside this process is the one found.
    here = await _setup(no_mcp_config=False)
    assert set(here.mcp_servers) == {"from-here"}

    there = await setup_orchestration(
        pattern_name="Fanout",
        model_spec="claude_code/sonnet",
        agent_name=None,
        save_dir=None,
        cwd=None,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
        mcp_launch_dir=str(launched_from),
    )
    assert set(there.mcp_servers) == {"from-launch-dir"}


@pytest.mark.asyncio
async def test_ambient_job_control_is_withheld_from_leaf_workers(tmp_run):
    """A config found by walking up the tree is not consent to hand every worker
    lionagi's own job-control server, whatever alias it is registered under."""
    ambient = {
        "control-under-an-alias": {"command": "/usr/local/bin/li", "args": ["mcp"]},
        "knowledge": {"command": "kkernel", "args": ["mcp"]},
    }
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": ambient}))

    env = await _setup(no_mcp_config=False)
    branch, _, _, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="implementer",
        model_override="claude_code/sonnet",
    )
    args = await _cli_args(branch.chat_model)
    forwarded = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]

    assert forwarded == {"knowledge": ambient["knowledge"]}
    assert "--strict-mcp-config" in args
    assert env.withheld_mcp_servers == ("control-under-an-alias",)
    assert "control-under-an-alias" in branch.system.rendered
    assert "withheld" in branch.system.rendered


@pytest.mark.asyncio
async def test_explicit_mcp_config_is_the_job_control_opt_in(tmp_run, tmp_path):
    granted = {
        "nested-control": {"command": "python3", "args": ["-m", "lionagi.mcp"]},
        "knowledge": {"command": "kkernel", "args": ["mcp"]},
    }
    named = tmp_path / "nested.json"
    named.write_text(json.dumps({"mcpServers": granted}))

    env = await _setup(no_mcp_config=False, mcp_config=str(named))
    branch, _, _, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="implementer",
        model_override="claude_code/sonnet",
    )
    args = await _cli_args(branch.chat_model)
    forwarded = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]

    assert forwarded == granted
    assert "--strict-mcp-config" in args
    assert env.withheld_mcp_servers == ()
    assert "withheld" not in branch.system.rendered


@pytest.mark.asyncio
async def test_codex_worker_disables_withheld_job_control_by_name(tmp_run, codex_home):
    servers = {
        "lion-control": {"command": "li", "args": ["mcp"]},
        "knowledge": {"command": "kkernel", "args": ["mcp"]},
    }
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    codex_home.write_config(servers)

    env = await _setup(no_mcp_config=False)
    branch, _, _, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="implementer",
        model_override="codex/gpt-5",
    )

    overrides = branch.chat_model.endpoint.config.kwargs["config_overrides"]
    assert overrides["mcp_servers.knowledge.command"] == "kkernel"
    assert overrides["mcp_servers.lion-control.enabled"] is False


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


@pytest.mark.parametrize("spec", ["gemini-cli/gemini-3.5-flash", "openai/gpt-4.1-mini"])
def test_the_unforwardable_warning_says_only_what_holds_for_each_provider(caplog, spec):
    """It says only what holds for every provider that reaches it: the request
    cannot carry a set, so the refusal cannot be applied."""
    import logging

    from lionagi.cli._providers import build_imodel_from_spec

    imodel = build_imodel_from_spec(spec)
    provider = imodel.endpoint.config.provider
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        orch_mod._hand_mcp_servers(imodel, {}, label="reviewer-2")

    assert (
        f"reviewer-2: the {provider} provider takes no MCP server set through its "
        "request, so --no-mcp-config cannot be applied to it." in caplog.text
    )
    assert "own configuration" not in caplog.text
    assert "by itself" not in caplog.text


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


_CONTROL_AND_KNOWLEDGE = {
    "control": {"command": "li", "args": ["mcp"]},
    "knowledge": {"command": "kkernel", "args": ["mcp"]},
}


@pytest.mark.asyncio
async def test_job_control_in_a_submitted_runs_snapshot_is_still_withheld(tmp_run, tmp_path):
    """A run submitted through the MCP server is pointed at a snapshot of the
    config the submission found, with the same --mcp-config a caller names a
    file with. Found is still found: the snapshot grants nothing by name."""
    from lionagi.mcp.jobs import _write_mcp_server_snapshot

    snapshot = tmp_path / "job" / "mcp.json"
    snapshot.parent.mkdir()
    _write_mcp_server_snapshot(snapshot, _CONTROL_AND_KNOWLEDGE, source=tmp_run / ".mcp.json")

    env = await _setup(no_mcp_config=False, mcp_config=str(snapshot))
    branch, _, _, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="implementer",
        model_override="claude_code/sonnet",
    )
    args = await _cli_args(branch.chat_model)

    assert _mcp_config_arg(args) == {
        "mcpServers": {"knowledge": _CONTROL_AND_KNOWLEDGE["knowledge"]}
    }
    assert env.withheld_mcp_servers == ("control",)


@pytest.mark.asyncio
async def test_the_synthesis_branch_gets_the_worker_set_as_its_whole_set(tmp_run):
    """The synthesis branch starts from a copy of the orchestrator's model, whose
    request carries the run's whole set. It is handed the worker set the way a
    worker is, so the copy's job-control server does not ride along."""
    from lionagi.cli.orchestrate.fanout import _fresh_synthesis_branch

    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": _CONTROL_AND_KNOWLEDGE}))
    env = await _setup(no_mcp_config=False)
    branch = await _fresh_synthesis_branch(env)
    args = await _cli_args(branch.chat_model)
    orchestrator_args = await _cli_args(env.orc_branch.chat_model)

    assert _mcp_config_arg(args) == {
        "mcpServers": {"knowledge": _CONTROL_AND_KNOWLEDGE["knowledge"]}
    }
    assert "--strict-mcp-config" in args
    assert _mcp_config_arg(orchestrator_args) == {"mcpServers": _CONTROL_AND_KNOWLEDGE}


@pytest.mark.asyncio
async def test_a_codex_synthesis_branch_drops_the_orchestrators_job_control(tmp_run, codex_home):
    from lionagi.cli.orchestrate.fanout import _fresh_synthesis_branch

    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": _CONTROL_AND_KNOWLEDGE}))
    codex_home.write_config({})
    env = await _setup(no_mcp_config=False, model_spec="codex/gpt-5")
    branch = await _fresh_synthesis_branch(env)

    overrides = branch.chat_model.endpoint.config.kwargs["config_overrides"]
    orchestrator = env.orc_branch.chat_model.endpoint.config.kwargs["config_overrides"]
    assert overrides["mcp_servers.knowledge.command"] == "kkernel"
    assert not [key for key in overrides if key.startswith("mcp_servers.control.")]
    assert orchestrator["mcp_servers.control.command"] == "li"


@pytest.mark.asyncio
async def test_a_casts_worker_is_told_which_servers_were_withheld(tmp_run):
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": _CONTROL_AND_KNOWLEDGE}))
    env = await _setup(no_mcp_config=False)
    branch, _, profile, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="critic",
        model_override="claude_code/sonnet",
    )

    assert profile is None
    # The alias in place: the surrounding text says "job-control" whatever it names.
    assert "job-control server(s) control were withheld" in branch.system.rendered


@pytest.mark.asyncio
async def test_a_worker_with_no_spawn_grant_is_not_told_to_spawn(tmp_run):
    (tmp_run / ".mcp.json").write_text(json.dumps({"mcpServers": _CONTROL_AND_KNOWLEDGE}))
    env = await _setup(no_mcp_config=False)
    branch, _, _, _ = await build_worker_branch(
        env,
        agent_id="w1",
        role="implementer",
        model_override="claude_code/sonnet",
        grant_spawn=False,
    )

    assert "withheld" in branch.system.rendered
    assert "spawn" not in branch.system.rendered.split("MCP CAPABILITY POLICY", 1)[1]


@pytest.mark.parametrize(
    "config",
    [
        {"command": "li", "args": ["mcp"]},
        {"command": "/opt/venv/bin/li", "args": ["mcp"]},
        {"command": "LI.EXE", "args": ["mcp"]},
        {"command": "li", "args": ["--verbose", "mcp"]},
        {"command": "uv", "args": ["run", "li", "mcp"]},
        {"command": "uvx", "args": ["--from", "lionagi", "li", "mcp"]},
        {"command": "env", "args": ["LIONAGI_HOME=/x", "li", "mcp"]},
        {"command": "python3.12", "args": ["-m", "lionagi.mcp"]},
        {"command": "py", "args": ["-m", "lionagi.mcp"]},
        {"command": "uv", "args": ["run", "python", "-m", "lionagi.mcp"]},
        {"command": "python", "args": ["-mlionagi.mcp"]},
        {"command": "python", "args": ["-m", "lionagi.cli", "mcp"]},
        {"command": "python3", "args": ["-mlionagi.cli", "--verbose", "mcp"]},
        {"command": "uv", "args": ["run", "python", "-m", "lionagi.cli.main", "mcp"]},
        {"command": "python", "args": ["-m", "lionagi.mcp.server"]},
        {"command": "python", "args": ["-m", "lionagi.cli.__main__", "mcp"]},
        {"command": "python", "args": ["-Im", "lionagi.mcp"]},
        {"command": "python", "args": ["-Imlionagi.cli", "mcp"]},
        {"command": "python", "args": ["-X", "dev", "-m", "lionagi.cli", "mcp"]},
        {"command": "python", "args": ["/opt/venv/site-packages/lionagi/mcp/__main__.py"]},
        {"command": "python3", "args": ["/opt/venv/site-packages/lionagi/cli/main.py", "mcp"]},
    ],
)
def test_job_control_is_recognised_through_launchers(config):
    assert orch_mod._is_job_control_server(config)


@pytest.mark.parametrize(
    "config",
    [
        {"command": "li", "args": ["agent", "mcp"]},
        {"command": "li", "args": []},
        {"command": "uv", "args": ["run", "li", "agent"]},
        {"command": "python", "args": ["-m", "lionagi.cli"]},
        {"command": "python", "args": ["-m", "lionagi.cli", "agent", "mcp"]},
        {"command": "python", "args": ["-m", "other.cli", "mcp"]},
        {"command": "python", "args": ["-m", "lionagi.mcp._notify_hook"]},
        {"command": "python", "args": ["-Xm", "lionagi.mcp"]},
        {"command": "python", "args": ["/opt/venv/site-packages/lionagi/cli/main.py", "agent"]},
        {"command": "kkernel", "args": ["mcp"]},
        {"command": "uvx", "args": ["some-mcp-server"]},
        {"url": "https://example.invalid/mcp"},
        "li mcp",
    ],
)
def test_other_servers_are_not_taken_for_job_control(config):
    assert not orch_mod._is_job_control_server(config)
