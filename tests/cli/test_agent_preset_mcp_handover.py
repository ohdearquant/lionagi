# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A coding-preset leg must be handed the MCP servers resolved at submission.

The whole point of resolving the server set from the submitting directory is
that a leg pointed at a checkout without a config of its own still gets the
servers its instructions assume. The coding preset builds its agent through
the factory, which resolves an MCP config of its own unless it is handed one,
so these spawn with a config that exists only where the command was submitted
and assert the leg's request carries exactly that set — for both CLI
transports, since they carry it in different places.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._logging import _HINT_LOGGER_NAME, _WARN_LOGGER_NAME


@contextlib.contextmanager
def _capture_cli_messages():
    """Collect what the spawn tells its caller.

    Handlers go on the CLI's own channels rather than through caplog: those
    loggers stop propagating once CLI logging is configured, and whether that
    has happened depends on what else ran in the session.
    """
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

    async def fake_setup(*a, **kw):
        return {"session_id": "sess-0"}

    async def fake_teardown(ctx, **kw):
        return kw.get("status", "completed")

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
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
async def test_explicit_mcp_config_beats_a_config_at_the_target_directory(spawn, tmp_path, model):
    """--mcp-config names the set; a config sitting at --cwd does not win."""
    from lionagi.cli.agent import _run_agent

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
async def test_no_mcp_config_hands_nothing_over(spawn, model):
    """--no-mcp-config is a choice: the leg's request carries no server set."""
    from lionagi.cli.agent import _run_agent

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
async def test_no_mcp_config_is_not_reported_as_servers_handed_over(spawn, model):
    from lionagi.cli.agent import _run_agent

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
