# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test submission-time MCP handoff and refusal across single-agent CLI paths.

Caller-named sets must fail when the chosen provider cannot forward them.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.console import _HINT_LOGGER_NAME, _WARN_LOGGER_NAME

from ._worker_fixtures import WorkerSetup


@contextlib.contextmanager
def _capture_cli_messages():
    """Collect CLI hints and warnings without relying on log propagation."""
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = _Collect()
    loggers = [logging.getLogger(name) for name in (_HINT_LOGGER_NAME, _WARN_LOGGER_NAME)]
    restore = [(logger, logger.level) for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    try:
        yield messages
    finally:
        for logger, level in restore:
            logger.removeHandler(handler)
            logger.setLevel(level)


SUBMITTED = {"khive": {"command": "kkernel", "args": ["serve"]}}
NEARBY = {"decoy": {"command": "not-the-submitted-server"}}
ANTIGRAVITY_PROVIDERS = ("gemini-cli", "gemini_cli", "gemini-code", "gemini_code")
DEFAULT_CLAUDE_KHIVE_PACKS = (
    "kg,blob,gtd,memory,brain,schedule,knowledge,session,git,code,workspace"
)


def _write_mcp_config(directory: Path, servers: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def _wire_agent_stubs(monkeypatch, tmp_path: Path):
    """Stub everything the spawn does except building the branch itself."""
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    branches_created: list = []
    real_branch_init = Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(Branch, "__init__", spy_branch_init)

    async def fake_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    persistence = WorkerSetup()
    monkeypatch.setattr(agent_mod, "setup_agent_persist", persistence.setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", persistence.teardown)
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
    return branches_created


def _wire_claude_mcp_stubs(monkeypatch, tmp_path: Path):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.orchestration.runs import RunDir
    from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest

    branches = _wire_agent_stubs(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    binary = tmp_path / "bin" / "kkernel"
    binary.parent.mkdir()
    binary.touch(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary.parent), prepend=os.pathsep)
    monkeypatch.chdir(launch)
    # Render the MCP arguments without launching a provider version probe.
    monkeypatch.setattr(ClaudeCodeRequest, "_child_sandbox_settings", lambda _request: None)
    runs = []

    def allocate():
        root = tmp_path / "runs" / f"run-{len(runs)}"
        run = RunDir(run_id=root.name, state_root=root, artifact_root=root / "artifacts")
        run.ensure_state_dirs()
        runs.append(run)
        return run

    monkeypatch.setattr(agent_mod, "allocate_run", allocate)
    return SimpleNamespace(branches=branches, runs=runs, binary=binary)


def _claude_mcp_servers(branch) -> dict:
    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hello"})
    args = payload["request"].as_cmd_args()
    assert args.count("--strict-mcp-config") == 1
    assert args.count("--mcp-config") == 1
    config = json.loads(args[args.index("--mcp-config") + 1])
    return config["mcpServers"]


def _assert_claude_default_mcp(leg) -> None:
    servers = _claude_mcp_servers(leg.branches[-1])
    assert set(servers) == {"khive"}
    khive = servers["khive"]
    assert khive["command"] == str(leg.binary)
    assert khive["args"] == ["mcp"]
    assert khive["env"]["KHIVE_PACKS"] == DEFAULT_CLAUDE_KHIVE_PACKS
    assert khive["env"]["KHIVE_ACTOR"] == "agent:anonymous"
    manifest = leg.runs[-1].read_manifest()
    assert manifest["mcp_config_reason"] == "default_claude_leg"
    assert manifest["mcp_config_explicit"] is False
    assert manifest["mcp_config_source"] is None


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    """A submitting directory holding the config, and a target holding none.

    HOME is redirected too: the factory's own resolution falls back to a
    home-level config, and a real one on the machine running the tests would
    otherwise decide the outcome.
    """
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)

    submit_dir = tmp_path / "submit"
    _write_mcp_config(submit_dir, SUBMITTED)
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    home = tmp_path / "home"
    _write_mcp_config(home / ".lionagi", NEARBY)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(submit_dir)

    return SimpleNamespace(
        branches=branches_created,
        submit_dir=submit_dir,
        target_dir=target_dir,
        home=home,
    )


def _forwarded_servers(branch) -> dict:
    """The server set a built request carries, per transport."""
    kwargs = branch.chat_model.endpoint.config.kwargs
    provider = branch.chat_model.endpoint.config.provider
    if provider == "codex":
        overrides = kwargs.get("config_overrides") or {}
        servers: dict = {}
        for key, value in overrides.items():
            if not key.startswith("mcp_servers."):
                continue
            _, name, field = key.split(".", 2)
            servers.setdefault(name, {})[field] = value
        return servers
    return kwargs.get("mcp_servers") or {}


def _write_cli_branch(directory: Path, provider: str) -> tuple[str, Path]:
    """Persist a CLI-backed branch for a resume-path test."""
    from lionagi import Branch, iModel

    branch = Branch(
        chat_model=iModel(
            provider=provider,
            endpoint="query_cli",
            model="gemini-3.5-flash",
            api_key="dummy",
        )
    )
    branch_id = str(branch.id)
    path = directory / f"{branch_id}.json"
    path.write_text(json.dumps(branch.to_dict()))
    return branch_id, path


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ANTIGRAVITY_PROVIDERS)
async def test_direct_antigravity_leg_rejects_a_named_nonempty_mcp_set(spawn, tmp_path, provider):
    from lionhub.serve.cli.agent import _run_agent

    named = _write_mcp_config(tmp_path / "named", {"named": {"command": "server"}})

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
        await _run_agent(
            f"{provider}/gemini-3.5-flash",
            "go",
            mcp_config=str(named),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True], ids=["direct", "resumed"])
async def test_named_snapshot_marker_cannot_hide_explicit_antigravity_request(
    spawn, tmp_path, monkeypatch, resumed
):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.mcp.jobs import _write_mcp_server_snapshot

    snapshot = tmp_path / "named.json"
    _write_mcp_server_snapshot(
        snapshot,
        {"named": {"command": "server"}},
        source=tmp_path / ".mcp.json",
        searched_from=tmp_path,
    )
    kwargs = {"mcp_config": str(snapshot)}
    model = "gemini-cli/gemini-3.5-flash"
    if resumed:
        branch_id, branch_path = _write_cli_branch(tmp_path, "gemini-cli")
        monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("run-x", branch_path))
        kwargs["resume"] = branch_id
        model = None

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP") as error:
        await agent_mod._run_agent(model, "go", **kwargs)
    message = str(error.value).lower()
    assert "submit snapshot" in message
    assert "mcp server" in message
    assert "restart" in message


@pytest.mark.parametrize("marked", [False, True], ids=["named", "submit-snapshot"])
def test_antigravity_old_submit_argv_reports_snapshot_restart_only_when_marked(
    spawn, tmp_path, monkeypatch, marked
):
    """A pre-upgrade server names its generated snapshot with --mcp-config."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.mcp.jobs import _write_mcp_server_snapshot

    config = tmp_path / "mcp-servers.json"
    if marked:
        _write_mcp_server_snapshot(
            config, SUBMITTED, source=spawn.submit_dir / ".mcp.json", searched_from=tmp_path
        )
    else:
        config.write_text(json.dumps({"mcpServers": SUBMITTED}))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("go")
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)

    parser = argparse.ArgumentParser()
    agent_mod.add_agent_subparser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(
        [
            "agent",
            "--notify",
            "python -m lionhub.serve.mcp._notify_hook --run-id run-x --status {status}",
            "--mcp-config",
            str(config),
            f"--cwd={spawn.submit_dir}",
            "--prompt-file",
            str(prompt),
            "--",
            "gemini-cli/gemini-3.5-flash",
        ]
    )
    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP") as error:
        agent_mod.run_agent(args)
    assert not spawn.branches
    message = str(error.value).lower()
    assert ("submit snapshot" in message) is marked
    assert ("restart" in message) is marked


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True], ids=["direct", "recorded-explicit"])
@pytest.mark.parametrize("field", ["lionagiSnapshotOf", "lionagiSnapshotSearchedFrom"])
@pytest.mark.parametrize("bad_value", [None, "", "relative/path", 123])
async def test_named_marked_antigravity_config_refuses_malformed_snapshot_metadata(
    spawn, tmp_path, monkeypatch, resumed, field, bad_value
):
    """The snapshot metadata is validated even when its carrier is explicit."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    snapshot = tmp_path / "marked.json"
    config = {
        "mcpServers": SUBMITTED,
        "lionagiSnapshotOf": str(spawn.submit_dir / ".mcp.json"),
        "lionagiSnapshotSearchedFrom": str(spawn.submit_dir),
    }
    config[field] = bad_value
    snapshot.write_text(json.dumps(config))
    model = "gemini-cli/gemini-3.5-flash"
    kwargs = {"mcp_config": str(snapshot)}
    if resumed:
        branch_id, branch_path = _write_cli_branch(tmp_path, "gemini-cli")
        stored = json.loads(branch_path.read_text())
        stored["metadata"][agent_mod._MCP_LAUNCH_KEY] = {
            "mcp_config": str(snapshot),
            "mcp_config_origin": "explicit",
            "no_mcp_config": False,
            "launch_dir": str(spawn.submit_dir),
        }
        branch_path.write_text(json.dumps(stored))
        monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("run-x", branch_path))
        kwargs = {"resume": branch_id}
        model = None
    with pytest.raises(McpConfigError):
        await agent_mod._run_agent(model, "go", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True], ids=["direct", "recorded-explicit"])
async def test_named_snapshot_without_optional_search_directory_reaches_antigravity_refusal(
    spawn, tmp_path, monkeypatch, resumed
):
    import lionhub.serve.cli.agent as agent_mod

    snapshot = tmp_path / "marked.json"
    snapshot.write_text(
        json.dumps(
            {
                "mcpServers": SUBMITTED,
                "lionagiSnapshotOf": str(spawn.submit_dir / ".mcp.json"),
            }
        )
    )
    model = "gemini-cli/gemini-3.5-flash"
    kwargs = {"mcp_config": str(snapshot)}
    if resumed:
        branch_id, branch_path = _write_cli_branch(tmp_path, "gemini-cli")
        stored = json.loads(branch_path.read_text())
        stored["metadata"][agent_mod._MCP_LAUNCH_KEY] = {
            "mcp_config": str(snapshot),
            "mcp_config_origin": "explicit",
            "no_mcp_config": False,
            "launch_dir": str(spawn.submit_dir),
        }
        branch_path.write_text(json.dumps(stored))
        monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("run-x", branch_path))
        kwargs = {"resume": branch_id}
        model = None
    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP") as error:
        await agent_mod._run_agent(model, "go", **kwargs)
    assert "submit snapshot" in str(error.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["GEMINI-CLI", "Gemini_Code", "gemini-Cli", " gemini-cli "])
async def test_a_provider_spelling_that_resolves_is_a_spelling_the_guard_recognises(
    spawn, tmp_path, provider
):
    """Endpoint resolution case-folds the provider, so the guard must too.

    A spelling that differs only in case still resolves to the Antigravity CLI and
    still runs, so matching the exact lowercase spellings would let the very
    omission this rejection exists to prevent through under a capitalised name.
    """
    from lionhub.serve.cli.agent import _run_agent

    named = _write_mcp_config(tmp_path / "named", {"named": {"command": "server"}})

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
        await _run_agent(
            f"{provider}/gemini-3.5-flash",
            "go",
            mcp_config=str(named),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ANTIGRAVITY_PROVIDERS)
async def test_resumed_antigravity_leg_rejects_a_named_nonempty_mcp_set(
    spawn, tmp_path, monkeypatch, provider
):
    import lionhub.serve.cli.agent as agent_mod

    branch_id, branch_path = _write_cli_branch(tmp_path, provider)
    monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("run-x", branch_path))
    named = _write_mcp_config(tmp_path / "named", {"named": {"command": "server"}})

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
        await agent_mod._run_agent(
            None,
            "go",
            resume=branch_id,
            mcp_config=str(named),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ANTIGRAVITY_PROVIDERS)
@pytest.mark.parametrize("resumed", [False, True], ids=["direct", "resumed"])
@pytest.mark.parametrize(
    "config_case",
    ["discovered", "submitted-snapshot", "explicit-empty", "disabled"],
)
async def test_antigravity_leg_keeps_non_requested_mcp_sets_non_failing(
    spawn,
    tmp_path,
    monkeypatch,
    provider,
    resumed,
    config_case,
):
    import lionhub.serve.cli.agent as agent_mod

    model = f"{provider}/gemini-3.5-flash"
    kwargs = {}
    if resumed:
        branch_id, branch_path = _write_cli_branch(tmp_path, provider)
        monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("run-x", branch_path))
        model = None
        kwargs["resume"] = branch_id

    if config_case == "explicit-empty":
        kwargs["mcp_config"] = str(_write_mcp_config(tmp_path / "empty", {}))
    elif config_case == "submitted-snapshot":
        # What a leg submitted through the MCP server is pointed at: the set the
        # submission found, which nobody named.
        from lionhub.serve.mcp.jobs import _write_mcp_server_snapshot

        snapshot = tmp_path / "job" / "mcp.json"
        snapshot.parent.mkdir()
        _write_mcp_server_snapshot(
            snapshot,
            {"found": {"command": "server"}},
            source=tmp_path / ".mcp.json",
            searched_from=tmp_path,
        )
        if resumed:
            # A resume replays the persisted choice, not the new submission's
            # snapshot. Record the carrier that the first run saved.
            stored = json.loads(branch_path.read_text())
            stored["metadata"][agent_mod._MCP_LAUNCH_KEY] = {
                "mcp_config": str(snapshot),
                "mcp_config_origin": "submit_snapshot",
                "no_mcp_config": False,
                "launch_dir": str(spawn.submit_dir),
            }
            branch_path.write_text(json.dumps(stored))
        else:
            kwargs["mcp_submit_snapshot"] = str(snapshot)
    elif config_case == "disabled":
        kwargs["no_mcp_config"] = True

    result, _provider, _branch_id, status, _session_id = await agent_mod._run_agent(
        model,
        "go",
        **kwargs,
    )

    assert result == "done"
    assert status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_preset_leg_gets_the_submitted_servers_not_the_target_directory(spawn, model):
    """The config exists only where the command was submitted; --cwd has none."""
    from lionhub.serve.cli.agent import _run_agent

    await _run_agent(model, "go", preset="coding", cwd=str(spawn.target_dir), yolo=True)

    branch = spawn.branches[-1]
    forwarded = _forwarded_servers(branch)
    assert set(forwarded) == {"khive"}
    assert forwarded["khive"]["command"] == "kkernel"
    if model.startswith("claude_code"):
        assert branch.chat_model.endpoint.config.kwargs["strict_mcp_config"] is True


@pytest.mark.asyncio
async def test_leg_without_a_profile_gets_khive_acting_as_the_anonymous_child(spawn):
    """The knowledge server a leg is handed names the leg's own actor."""
    from lionhub.serve.cli.agent import _run_agent

    await _run_agent(
        "claude_code/sonnet", "go", preset="coding", cwd=str(spawn.target_dir), yolo=True
    )

    env = _forwarded_servers(spawn.branches[-1])["khive"]["env"]
    assert env["KHIVE_ACTOR"] == "agent:anonymous"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_explicit_mcp_config_beats_a_config_at_the_target_directory(
    spawn, tmp_path, model, codex_home
):
    """--mcp-config names the set; a config sitting at --cwd does not win."""
    from lionhub.serve.cli.agent import _run_agent

    # A named set on codex disables every server codex declares on its own, read from its
    # config; with no config there it asks the `codex` binary. A config with no servers
    # keeps the test off the binary.
    codex_home.write_config({})
    named = _write_mcp_config(tmp_path / "named", {"named": {"command": "named-server"}})
    _write_mcp_config(spawn.target_dir, NEARBY)

    await _run_agent(
        model,
        "go",
        preset="coding",
        cwd=str(spawn.target_dir),
        yolo=True,
        mcp_config=str(named),
    )

    assert set(_forwarded_servers(spawn.branches[-1])) == {"named"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_no_mcp_config_hands_nothing_over(spawn, codex_home, model):
    """--no-mcp-config is a choice: the leg's request carries no server set."""
    from lionhub.serve.cli.agent import _run_agent

    codex_home.write_config({})

    await _run_agent(
        model,
        "go",
        preset="coding",
        cwd=str(spawn.target_dir),
        yolo=True,
        no_mcp_config=True,
    )

    assert _forwarded_servers(spawn.branches[-1]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "coding"], ids=["plain", "preset"])
async def test_a_new_branch_records_the_mcp_choice_a_resume_replays(spawn, tmp_path, preset):
    """The record holds the launch directory, the named config as an absolute
    path and the refusal flag: what a resume that names no flag replays."""
    from lionhub.serve.cli.agent import _MCP_LAUNCH_KEY, _run_agent

    named = _write_mcp_config(tmp_path / "named", {"named": {"command": "named-server"}})

    await _run_agent(
        "claude_code/sonnet",
        "go",
        preset=preset,
        cwd=str(spawn.target_dir),
        yolo=True,
        mcp_config=str(named),
    )

    assert spawn.branches[-1].metadata[_MCP_LAUNCH_KEY] == {
        "mcp_config": str(named.resolve()),
        "mcp_config_origin": "explicit",
        "no_mcp_config": False,
        "launch_dir": str(spawn.submit_dir.resolve()),
    }


@pytest.mark.asyncio
async def test_a_submitted_legs_snapshot_is_recorded_for_its_resume(spawn, tmp_path):
    """A leg submitted through the MCP server is handed a snapshot of the config
    the submission found. The snapshot is what a resume must read again, even
    though it is not a file anyone named."""
    from lionhub.serve.cli.agent import _MCP_LAUNCH_KEY, _run_agent
    from lionhub.serve.mcp.jobs import _write_mcp_server_snapshot

    server_dir = tmp_path / "server"
    server_dir.mkdir()
    snapshot = tmp_path / "job" / "mcp.json"
    snapshot.parent.mkdir()
    _write_mcp_server_snapshot(
        snapshot,
        {"found": {"command": "found-server"}},
        source=server_dir / ".mcp.json",
        searched_from=server_dir,
    )

    await _run_agent(
        "claude_code/sonnet",
        "go",
        cwd=str(spawn.target_dir),
        yolo=True,
        mcp_submit_snapshot=str(snapshot),
    )

    launched = spawn.branches[-1].metadata[_MCP_LAUNCH_KEY]
    assert launched["mcp_config"] == str(snapshot.resolve())
    assert launched["mcp_config_origin"] == "submit_snapshot"
    # The search directory is provenance; a resume reads from this process's own directory.
    assert launched["launch_dir"] == str(Path.cwd().resolve())
    assert launched["launch_dir"] != str(server_dir)
    assert _forwarded_servers(spawn.branches[-1]) == {"found": {"command": "found-server"}}
    assert spawn.branches[-1].chat_model.endpoint.config.kwargs["strict_mcp_config"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "coding"], ids=["plain", "preset"])
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_no_mcp_config_leaves_the_leg_no_servers_of_its_own_either(
    spawn, codex_home, model, preset
):
    """Handing nothing over is half of it: a CLI left to itself loads every
    server its own config declares, so the request has to say the set is empty."""
    from lionhub.serve.cli.agent import _run_agent

    codex_home.write_config({"khive": {"command": "kkernel"}})

    await _run_agent(
        model,
        "go",
        preset=preset,
        cwd=str(spawn.target_dir),
        yolo=True,
        no_mcp_config=True,
    )

    kwargs = spawn.branches[-1].chat_model.endpoint.config.kwargs
    if model.startswith("codex"):
        assert kwargs["ignore_user_config"] is True
        assert "config_overrides" not in kwargs
    else:
        assert kwargs["mcp_servers"] == {}
        assert kwargs["strict_mcp_config"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "coding"], ids=["plain", "preset"])
async def test_claude_leg_defaults_to_an_exclusive_mcp_server(monkeypatch, tmp_path, preset):
    from lionhub.serve.cli.agent import _run_agent

    leg = _wire_claude_mcp_stubs(monkeypatch, tmp_path)
    _write_mcp_config(tmp_path / "home" / ".lionagi", NEARBY)
    _, _, _, status, _ = await _run_agent("claude_code/sonnet", "go", preset=preset)

    assert status == "completed"
    _assert_claude_default_mcp(leg)


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "coding"], ids=["plain", "preset"])
@pytest.mark.parametrize("disabled", [False, True], ids=["named", "disabled"])
async def test_claude_leg_honors_a_caller_mcp_choice(monkeypatch, tmp_path, preset, disabled):
    from lionhub.serve.cli.agent import _run_agent

    leg = _wire_claude_mcp_stubs(monkeypatch, tmp_path)
    selected = {"chosen": {"command": "chosen-server"}}
    named = None if disabled else _write_mcp_config(tmp_path / "chosen", selected)
    _, _, _, status, _ = await _run_agent(
        "claude_code/sonnet",
        "go",
        preset=preset,
        mcp_config=str(named) if named is not None else None,
        no_mcp_config=disabled,
    )

    assert status == "completed"
    assert _claude_mcp_servers(leg.branches[-1]) == ({} if disabled else selected)
    manifest = leg.runs[-1].read_manifest()
    assert manifest["mcp_config_reason"] == ("mcp_disabled_by_caller" if disabled else None)
    assert manifest["mcp_config_explicit"] is (not disabled)
    assert manifest["mcp_config_source"] == (None if named is None else str(named.resolve()))


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "coding"], ids=["plain", "preset"])
async def test_claude_leg_without_kkernel_stays_strict_with_no_servers(
    monkeypatch, tmp_path, preset
):
    from lionhub.serve.cli.agent import _run_agent

    leg = _wire_claude_mcp_stubs(monkeypatch, tmp_path)
    which = shutil.which

    def unavailable(command, *args, **kwargs):
        return None if command == "kkernel" else which(command, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", unavailable)
    with _capture_cli_messages() as messages:
        _, _, _, status, _ = await _run_agent("claude_code/sonnet", "go", preset=preset)

    assert status == "completed"
    assert _claude_mcp_servers(leg.branches[-1]) == {}
    manifest = leg.runs[-1].read_manifest()
    assert manifest["mcp_config_reason"] == "default_claude_leg_kkernel_unavailable"
    assert manifest["mcp_config_explicit"] is False
    assert manifest["mcp_config_source"] is None
    assert len([message for message in messages if "kkernel" in message]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_text", "reason", "warns"),
    [
        ('{"mcpServers": {}}', "mcp_config_declares_no_servers", False),
        ("{", "mcp_config_unusable:", True),
    ],
    ids=["empty", "unusable"],
)
async def test_claude_leg_uses_strict_empty_for_a_discovered_config_without_servers(
    monkeypatch, tmp_path, config_text, reason, warns
):
    from lionhub.serve.cli.agent import _run_agent

    leg = _wire_claude_mcp_stubs(monkeypatch, tmp_path)
    config = Path.cwd() / ".mcp.json"
    config.write_text(config_text)
    with _capture_cli_messages() as messages:
        _, _, _, status, _ = await _run_agent("claude_code/sonnet", "go")

    assert status == "completed"
    assert _claude_mcp_servers(leg.branches[-1]) == {}
    manifest = leg.runs[-1].read_manifest()
    assert manifest["mcp_config_reason"].startswith(reason)
    assert manifest["mcp_config_source"] == str(config.resolve())
    assert manifest["mcp_config_explicit"] is False
    assert any("MCP configuration could not be used" in message for message in messages) is warns


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_spawn_message_names_what_the_request_carries(spawn, model):
    """What the caller is told and what the leg gets are the same servers."""
    from lionhub.serve.cli.agent import _run_agent

    with _capture_cli_messages() as messages:
        await _run_agent(model, "go", preset="coding", cwd=str(spawn.target_dir), yolo=True)

    reported = "\n".join(messages)
    assert "[mcp]" in reported
    for name in _forwarded_servers(spawn.branches[-1]):
        assert name in reported
    assert "decoy" not in reported
    assert "not carried" not in reported


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_no_mcp_config_is_not_reported_as_servers_handed_over(spawn, codex_home, model):
    from lionhub.serve.cli.agent import _run_agent

    codex_home.write_config({})
    with _capture_cli_messages() as messages:
        await _run_agent(
            model,
            "go",
            preset="coding",
            cwd=str(spawn.target_dir),
            yolo=True,
            no_mcp_config=True,
        )

    assert not [m for m in messages if "[mcp]" in m or "not carried" in m]


@pytest.mark.asyncio
async def test_named_mcp_config_that_does_not_exist_still_refuses_the_spawn(spawn, tmp_path):
    """An explicitly named config that cannot be used stays a loud failure."""
    from lionhub.orchestration.mcp_config import McpConfigError
    from lionhub.serve.cli.agent import _run_agent

    with pytest.raises(McpConfigError):
        await _run_agent(
            "claude_code/sonnet",
            "go",
            preset="coding",
            cwd=str(spawn.target_dir),
            yolo=True,
            mcp_config=str(tmp_path / "nowhere" / ".mcp.json"),
        )


@pytest.mark.asyncio
async def test_named_mcp_config_that_does_not_parse_still_refuses_the_spawn(spawn, tmp_path):
    from lionhub.orchestration.mcp_config import McpConfigError
    from lionhub.serve.cli.agent import _run_agent

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")

    with pytest.raises(McpConfigError):
        await _run_agent(
            "claude_code/sonnet",
            "go",
            preset="coding",
            cwd=str(spawn.target_dir),
            yolo=True,
            mcp_config=str(broken),
        )


@pytest.mark.asyncio
async def test_a_role_profile_leg_runs_its_pre_turn_khive_client_as_the_same_actor(
    spawn, monkeypatch
):
    """A profile with a role builds its branch through create_agent; the khive client
    its recall opt-in registers acts as the same actor as the khive server it is handed."""
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.spawn import _parse_profile
    from lionhub.tools.khive_injection import KhiveInjectionProvider

    profile = _parse_profile(
        "reviewer", "---\nrole: reviewer\nkhive_injection: true\n---\nReview.\n"
    )
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)

    await agent_mod._run_agent(
        "claude_code/sonnet",
        "go",
        agent_name="reviewer",
        cwd=str(spawn.target_dir),
        yolo=True,
    )

    branch = spawn.branches[-1]
    providers = [
        entry.provider
        for entry in branch.providers._entries
        if isinstance(entry.provider, KhiveInjectionProvider)
    ]
    assert len(providers) == 1
    assert providers[0]._mcp_config["env"]["KHIVE_ACTOR"] == "agent:reviewer"
    assert _forwarded_servers(branch)["khive"]["env"]["KHIVE_ACTOR"] == "agent:reviewer"
