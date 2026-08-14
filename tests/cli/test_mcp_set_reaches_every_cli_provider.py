# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A resolved MCP server set reaches every CLI provider that has a transport for one.

Three spawn shapes build a CLI request — a plain leg, a resumed leg, and a
flow/fanout worker — and each used to decide for itself whether the set could be
carried, by asking whether the provider was the Claude CLI. codex carries a set
too, over config overrides, so a caller who named one got it on one path and
silently lost it on three. These tests are per spawn shape rather than per
provider, because the shape is what differed.

The secret-field checks are the reason each shape is checked separately: `env`
and `http_headers` can hold API keys, and a path that forwards them as `-c`
overrides puts them in `ps` output and in every serialized request record.
"""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import apply_forwarded_mcp_servers

CODEX_SPEC = "codex/gpt-5.3-codex"

SERVERS = {"khive": {"command": "kkernel", "args": ["serve"]}}
SECRET = "sk-live-do-not-put-me-on-argv"
SECRET_SERVERS = {"khive": {"command": "kkernel", "env": {"KHIVE_API_KEY": SECRET}}}

WARN_LOGGER = "lionagi.cli.warn"


async def _cmd_args(imodel) -> list[str]:
    """The argv the CLI child would be spawned with, off the live request."""
    api_call = await imodel.create_event(prompt="hi")
    return api_call.payload["request"].as_cmd_args()


def _override_values(kwargs: dict) -> list:
    return list((kwargs.get("config_overrides") or {}).values())


def _assert_secret_is_off_the_wire(kwargs: dict, args: list[str], codex_home) -> None:
    """The secret reaches the child through a private file, never through argv."""
    assert all(SECRET not in str(value) for value in _override_values(kwargs))
    assert all(SECRET not in arg for arg in args)

    profile = codex_home.path / f"{kwargs['profile']}.config.toml"
    assert profile in codex_home.profile_files()
    assert SECRET in profile.read_text()
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    # The child is pointed at the file; the file is how the value gets there.
    assert args[args.index("-p") + 1] == kwargs["profile"]


# The transports themselves: what an empty set means to each of them.


def test_an_empty_exclusive_set_is_strict_for_the_claude_transport():
    kwargs: dict = {}
    assert apply_forwarded_mcp_servers(kwargs, {}, provider="claude_code", exclusive=True)
    assert kwargs["mcp_servers"] == {}
    assert kwargs["strict_mcp_config"] is True


def test_an_empty_exclusive_set_disables_by_name_for_the_codex_transport(codex_home):
    """codex has no wholesale clear: `-c mcp_servers={}` merges onto the existing
    table rather than replacing it, so each server it would have loaded is
    disabled by name instead."""
    codex_home.write_config({"khive": {"command": "kkernel"}, "docs": {"url": "http://x"}})
    kwargs: dict = {}

    assert apply_forwarded_mcp_servers(kwargs, {}, provider="codex", exclusive=True)

    assert kwargs["config_overrides"] == {
        "mcp_servers.docs.enabled": False,
        "mcp_servers.khive.enabled": False,
    }


def test_a_provider_without_a_transport_reports_that_it_carried_nothing():
    kwargs: dict = {}
    assert apply_forwarded_mcp_servers(kwargs, SERVERS, provider="gemini_code") is False
    assert kwargs == {}


# Site 1 — the plain `li agent` leg (build_chat_model).


def test_plain_leg_hands_a_codex_request_the_resolved_set(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({})
    model = build_chat_model("codex", "gpt-5.3-codex", False, False, None, mcp_servers=SERVERS)

    assert model.endpoint.config.kwargs["config_overrides"] == {
        "mcp_servers.khive.command": "kkernel",
        "mcp_servers.khive.args": ["serve"],
    }


def test_plain_leg_still_hands_a_claude_request_the_resolved_set():
    from lionagi.cli._providers import build_chat_model

    model = build_chat_model(
        "claude_code", "claude-opus-4-5", False, False, None, mcp_servers=SERVERS
    )
    assert model.endpoint.config.kwargs["mcp_servers"] == SERVERS


def test_plain_leg_hands_nothing_to_a_provider_without_a_transport():
    from lionagi.cli._providers import build_chat_model

    model = build_chat_model(
        "gemini_code", "gemini-3.5-flash", False, False, None, mcp_servers=SERVERS
    )
    # No flags and nothing forwarded leaves a bare spec string, not a request.
    assert model == "gemini_code/gemini-3.5-flash"


@pytest.mark.asyncio
async def test_plain_leg_keeps_codex_secret_fields_off_the_command_line(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({})
    model = build_chat_model(
        "codex", "gpt-5.3-codex", False, False, None, mcp_servers=SECRET_SERVERS
    )

    kwargs = model.endpoint.config.kwargs
    assert kwargs["config_overrides"] == {"mcp_servers.khive.command": "kkernel"}
    _assert_secret_is_off_the_wire(kwargs, await _cmd_args(model), codex_home)


def test_plain_leg_empty_set_is_the_whole_set_on_both_transports(codex_home):
    from lionagi.cli._providers import build_chat_model

    codex_home.write_config({"khive": {"command": "kkernel"}})

    codex = build_chat_model("codex", "gpt-5.3-codex", False, False, None, mcp_servers={})
    assert codex.endpoint.config.kwargs["config_overrides"] == {"mcp_servers.khive.enabled": False}

    claude = build_chat_model("claude_code", "claude-opus-4-5", False, False, None, mcp_servers={})
    assert claude.endpoint.config.kwargs["mcp_servers"] == {}
    assert claude.endpoint.config.kwargs["strict_mcp_config"] is True


# Site 2 — the resumed leg.


def _wire_resume_stubs(monkeypatch, tmp_path: Path, provider: str, model: str) -> str:
    """Persist a branch on *provider* and stub every external I/O `_run_agent`
    does, so what the resumed leg's request ends up carrying is all that is
    left to observe."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch, iModel
    from lionagi.service.manager import iModelManager

    branch = Branch(
        chat_model=iModel(provider=provider, endpoint="query_cli", model=model, api_key="dummy")
    )
    branch_path = tmp_path / f"{branch.id}.json"
    branch_path.write_text(json.dumps(branch.to_dict()))

    async def fake_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

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
    return str(branch.id)


def _mcp_config_file(tmp_path: Path, servers: dict) -> str:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return str(path)


@pytest.mark.asyncio
async def test_resumed_codex_leg_gets_the_set_the_resume_command_resolved(
    monkeypatch, tmp_path, codex_home, caplog
):
    """A resumed leg re-spawns a CLI child, so it needs the set as much as a new
    one does; the persisted branch carries the model, not the caller's directory.
    Having been given it, the leg is not also told it was not."""
    from lionagi.cli.agent import _run_agent

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "codex", "gpt-5.3-codex")

    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        await _run_agent(
            None,
            "follow up",
            resume=branch_id,
            mcp_config=_mcp_config_file(tmp_path, SERVERS),
        )

    assert "not carried" not in caplog.text


@pytest.mark.asyncio
async def test_resumed_leg_request_carries_the_codex_overrides(monkeypatch, tmp_path, codex_home):
    """Read the built request, not the report: the overrides are what the child
    is spawned with."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "codex", "gpt-5.3-codex")

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        seen["args"] = await _cmd_args(self.chat_model)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(
        None,
        "follow up",
        resume=branch_id,
        mcp_config=_mcp_config_file(tmp_path, SERVERS),
    )

    assert seen["kwargs"]["config_overrides"] == {
        "mcp_servers.khive.command": "kkernel",
        "mcp_servers.khive.args": ["serve"],
    }
    assert "-c" in seen["args"]
    assert 'mcp_servers.khive.command="kkernel"' in seen["args"]


@pytest.mark.asyncio
async def test_resumed_leg_keeps_codex_secret_fields_off_the_command_line(
    monkeypatch, tmp_path, codex_home
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    codex_home.write_config({})
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "codex", "gpt-5.3-codex")

    seen: dict = {}

    async def capture(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        seen["args"] = await _cmd_args(self.chat_model)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(
        None,
        "follow up",
        resume=branch_id,
        mcp_config=_mcp_config_file(tmp_path, SECRET_SERVERS),
    )

    _assert_secret_is_off_the_wire(seen["kwargs"], seen["args"], codex_home)


@pytest.mark.asyncio
async def test_resumed_antigravity_leg_rejects_an_explicit_server_set(monkeypatch, tmp_path):
    from lionagi.cli.agent import _run_agent

    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "gemini_code", "gemini-3.5-flash")

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
        await _run_agent(
            None,
            "follow up",
            resume=branch_id,
            mcp_config=_mcp_config_file(tmp_path, SERVERS),
        )


# Site 3 — the flow / fanout worker.


@pytest.fixture
def flow_run(monkeypatch, tmp_path):
    """A run launched from a directory whose .mcp.json is the run's server set."""
    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli._runs import RunDir

    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)

    def _allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="test-run",
            state_root=tmp_path / "state",
            artifact_root=tmp_path / "artifacts",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch_mod, "allocate_run", _allocate)
    return launch_dir


async def _codex_worker(flow_run, servers: dict):
    from lionagi.cli.orchestrate._orchestration import build_worker_branch, setup_orchestration

    (flow_run / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    env = await setup_orchestration(
        pattern_name="Fanout",
        model_spec=CODEX_SPEC,
        agent_name=None,
        save_dir=None,
        cwd=None,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
    )
    # A worker's role resolves through the agent profiles installed on the
    # machine, and a profile's model beats the run's default spec. Without a
    # pin, whichever model `implementer` happens to name locally is the one
    # these codex assertions run against, and the file silently tests another
    # provider. `model_override` is the only input that wins unconditionally.
    branch, _, _, _ = await build_worker_branch(
        env, agent_id="w1", role="implementer", model_override=CODEX_SPEC
    )
    return env, branch


@pytest.mark.asyncio
async def test_the_flow_worker_these_tests_build_is_a_codex_worker(flow_run, codex_home):
    """The worker the two cases below build resolves to the codex provider.

    Agent profiles live outside the repository, so an unpinned worker makes
    this file's provider a property of the machine running it. Asserting the
    provider directly means removing the pin fails here, saying which provider
    was built, rather than further down on an argv shape that no longer applies.
    """
    codex_home.write_config({})
    _env, branch = await _codex_worker(flow_run, SERVERS)

    assert branch.chat_model.endpoint.config.provider == "codex"


@pytest.mark.asyncio
async def test_a_codex_worker_is_spawned_with_the_runs_server_set(flow_run, codex_home):
    """The acceptance case: the `-c mcp_servers.*` arguments on the command line
    a codex worker of a flow run would actually be spawned with."""
    codex_home.write_config({})
    _env, branch = await _codex_worker(flow_run, SERVERS)

    args = await _cmd_args(branch.chat_model)
    forwarded = [args[i + 1] for i, a in enumerate(args) if a == "-c" and i + 1 < len(args)]
    assert 'mcp_servers.khive.command="kkernel"' in forwarded
    assert 'mcp_servers.khive.args=["serve"]' in forwarded


@pytest.mark.asyncio
async def test_a_codex_worker_keeps_secret_fields_off_the_command_line(flow_run, codex_home):
    codex_home.write_config({})
    _env, branch = await _codex_worker(flow_run, SECRET_SERVERS)

    kwargs = branch.chat_model.endpoint.config.kwargs
    _assert_secret_is_off_the_wire(kwargs, await _cmd_args(branch.chat_model), codex_home)
