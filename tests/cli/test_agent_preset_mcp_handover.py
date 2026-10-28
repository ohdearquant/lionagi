# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test submission-time MCP handoff and refusal across single-agent CLI paths.

Caller-named sets must fail when the chosen provider cannot forward them.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli._logging import _HINT_LOGGER_NAME, _WARN_LOGGER_NAME
from tests.cli._worker_fixtures import WorkerSetup


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


def _write_mcp_config(directory: Path, servers: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def _wire_agent_stubs(monkeypatch, tmp_path: Path):
    """Stub everything the spawn does except building the branch itself."""
    import lionagi.cli.agent as agent_mod
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
    from lionagi.cli.agent import _run_agent

    named = _write_mcp_config(tmp_path / "named", {"named": {"command": "server"}})

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
        await _run_agent(
            f"{provider}/gemini-3.5-flash",
            "go",
            mcp_config=str(named),
        )


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
    from lionagi.cli.agent import _run_agent

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
    import lionagi.cli.agent as agent_mod

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
    import lionagi.cli.agent as agent_mod

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
        kwargs["mcp_config"] = str(snapshot)
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
    from lionagi.cli.agent import _run_agent

    await _run_agent(model, "go", preset="coding", cwd=str(spawn.target_dir), yolo=True)

    branch = spawn.branches[-1]
    forwarded = _forwarded_servers(branch)
    assert set(forwarded) == {"khive"}
    assert forwarded["khive"]["command"] == "kkernel"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_explicit_mcp_config_beats_a_config_at_the_target_directory(
    spawn, tmp_path, model, codex_home
):
    """--mcp-config names the set; a config sitting at --cwd does not win."""
    from lionagi.cli.agent import _run_agent

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
    from lionagi.cli.agent import _run_agent

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
    from lionagi.cli.agent import _MCP_LAUNCH_KEY, _run_agent

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
        "no_mcp_config": False,
        "launch_dir": str(spawn.submit_dir.resolve()),
    }


@pytest.mark.asyncio
async def test_a_submitted_legs_snapshot_is_recorded_for_its_resume(spawn, tmp_path):
    """A leg submitted through the MCP server is handed a snapshot of the config
    the submission found. The snapshot is what a resume must read again, even
    though it is not a file anyone named."""
    from lionagi.cli.agent import _MCP_LAUNCH_KEY, _run_agent
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
        mcp_config=str(snapshot),
    )

    launched = spawn.branches[-1].metadata[_MCP_LAUNCH_KEY]
    assert launched["mcp_config"] == str(snapshot.resolve())
    # The search directory is provenance; a resume reads from this process's own directory.
    assert launched["launch_dir"] == str(Path.cwd().resolve())
    assert launched["launch_dir"] != str(server_dir)
    assert _forwarded_servers(spawn.branches[-1]) == {"found": {"command": "found-server"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "coding"], ids=["plain", "preset"])
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_no_mcp_config_leaves_the_leg_no_servers_of_its_own_either(
    spawn, codex_home, model, preset
):
    """Handing nothing over is half of it: a CLI left to itself loads every
    server its own config declares, so the request has to say the set is empty."""
    from lionagi.cli.agent import _run_agent

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
async def test_a_leg_with_no_config_found_is_told_it_starts_without_servers(
    monkeypatch, tmp_path, preset
):
    """A search that finds nothing leaves the leg no server set of its own.

    `--cwd` points at a directory with no `.mcp.json` at or above it and no
    config is named, so the leg starts with only what its provider CLI finds
    for itself. That is the case this warning exists for, on the plain path and
    on the path that builds the branch through `create_agent` alike.
    """
    from lionagi.cli.agent import _run_agent

    _wire_agent_stubs(monkeypatch, tmp_path)
    barren = tmp_path / "barren"
    barren.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(barren)

    with _capture_cli_messages() as messages:
        await _run_agent("claude_code/sonnet", "go", preset=preset, yolo=True)

    reported = "\n".join(messages)
    assert "no MCP servers are being handed to this leg" in reported
    assert "this leg" in reported.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "coding"], ids=["plain", "preset"])
async def test_a_home_config_does_not_reach_a_leg_that_found_none(monkeypatch, tmp_path, preset):
    """This command resolves its servers once, from where it was run, and hands
    that result to the branch: a search that finds nothing hands over nothing,
    and the branch does not go looking on its own. The home-level config is the
    one such a search would otherwise pick up, so it is the one checked here."""
    from lionagi.cli.agent import _run_agent

    branches = _wire_agent_stubs(monkeypatch, tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    home = tmp_path / "home"
    _write_mcp_config(home / ".lionagi", {"home-only": {"command": "home-server"}})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(launch)

    await _run_agent("claude_code/sonnet", "go", preset=preset, yolo=True)

    assert "mcp_servers" not in branches[-1].chat_model.endpoint.config.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude_code/sonnet", "codex/gpt-5.3-codex"])
async def test_spawn_message_names_what_the_request_carries(spawn, model):
    """What the caller is told and what the leg gets are the same servers."""
    from lionagi.cli.agent import _run_agent

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
    from lionagi.cli.agent import _run_agent

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
    from lionagi.cli._mcp_resolve import McpConfigError
    from lionagi.cli.agent import _run_agent

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
    from lionagi.cli._mcp_resolve import McpConfigError
    from lionagi.cli.agent import _run_agent

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
