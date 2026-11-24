# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A config named with ``--mcp-config`` is copied into the run directory at launch, and a resume
that names no MCP flag replays that copy rather than reading the named file again."""

from __future__ import annotations

import json
import os
import stat
from fnmatch import fnmatch

import pytest

from .test_mcp_set_reaches_every_cli_provider import _record_launch, _wire_resume_stubs

SELECTED = {"selected": {"command": "server", "args": ["--flag"]}}


def _named(tmp_path, servers=SELECTED):
    config = tmp_path / "named.json"
    config.write_text(json.dumps({"mcpServers": servers}))
    return config


def _launch_with_real_snapshots(monkeypatch, tmp_path):
    """Stub the spawn but keep the branch snapshot a resume reads, and record what each run's
    request carried and what its branch recorded."""
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.operations.run.run import _write_branch_snapshot

    from .test_agent_preset_mcp_handover import _wire_agent_stubs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    branches_dir = tmp_path / "branches"
    branches_dir.mkdir()
    seen: list[tuple[dict, dict]] = []

    async def capture(self, instruction=None, **kwargs):
        seen.append(
            (
                dict(self.chat_model.endpoint.config.kwargs),
                dict(self.metadata.get(agent_mod._MCP_LAUNCH_KEY) or {}),
            )
        )
        await _write_branch_snapshot(self, branches_dir)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    return branches_dir, seen


@pytest.mark.asyncio
async def test_a_named_config_is_copied_owner_only_and_the_record_points_at_the_copy(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.agent as agent_mod

    _, seen = _launch_with_real_snapshots(monkeypatch, tmp_path)
    # A khive server is handed to the run with the run's own actor attributed to it; the copy
    # holds the set as the named file gave it, so a resume attributes its own actor in turn.
    servers = {**SELECTED, "khive": {"command": "kkernel", "args": ["serve"]}}
    config = _named(tmp_path, servers)

    await agent_mod._run_agent("claude_code/sonnet", "start", mcp_config=str(config))

    copy = tmp_path / "run-state" / "r0" / "mcp-config.json"
    assert stat.S_IMODE(os.lstat(copy).st_mode) == 0o600
    assert json.loads(copy.read_text()) == {"mcpServers": servers}
    request, record = seen[0]
    assert request["mcp_servers"]["khive"] != servers["khive"]
    assert record["mcp_config"] == str(copy)
    assert record["mcp_config_named"] == str(config.resolve())
    assert record["mcp_config_origin"] == "explicit"


@pytest.mark.asyncio
async def test_a_resume_hands_over_the_first_runs_set_after_the_named_file_changes(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.agent as agent_mod

    branches_dir, seen = _launch_with_real_snapshots(monkeypatch, tmp_path)
    config = _named(tmp_path)
    _, _, branch_id, _, _ = await agent_mod._run_agent(
        "claude_code/sonnet", "start", mcp_config=str(config)
    )
    # The named file now lists another server, as a leg that could write it would make it.
    config.write_text(json.dumps({"mcpServers": {"planted": {"command": "other"}}}))
    branch_path = branches_dir / f"{branch_id}.json"
    monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("r", branch_path))

    await agent_mod._run_agent(None, "resume", resume=branch_id)

    (first, _), (resumed, record) = seen
    assert resumed["mcp_servers"] == first["mcp_servers"]
    assert set(resumed["mcp_servers"]) == {"selected"}
    # The resume copied the set again into its own run directory and points at that copy.
    second_copy = tmp_path / "run-state" / "r1" / "mcp-config.json"
    assert record["mcp_config"] == str(second_copy)
    assert record["mcp_config_named"] == str(config.resolve())
    assert json.loads(second_copy.read_text()) == {"mcpServers": SELECTED}
    assert stat.S_IMODE(os.lstat(second_copy).st_mode) == 0o600


@pytest.mark.asyncio
async def test_a_resume_whose_copy_is_gone_is_refused_naming_both_flags(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    branches_dir, seen = _launch_with_real_snapshots(monkeypatch, tmp_path)
    config = _named(tmp_path)
    _, _, branch_id, _, _ = await agent_mod._run_agent(
        "claude_code/sonnet", "start", mcp_config=str(config)
    )
    (tmp_path / "run-state" / "r0" / "mcp-config.json").unlink()
    branch_path = branches_dir / f"{branch_id}.json"
    monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("r", branch_path))

    with pytest.raises(McpConfigError) as raised:
        await agent_mod._run_agent(None, "resume", resume=branch_id)

    message = str(raised.value)
    assert "mcp-config.json" in message
    assert "--mcp-config" in message and "--no-mcp-config" in message
    # The named file is still there and is not read in the copy's place.
    assert config.is_file()
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_automatic_resume_replays_the_copy_rather_than_the_named_file(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.agent as agent_mod

    from .test_agent_resume_on_timeout import _wire_agent_stubs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _named(tmp_path)

    def side_effect(index):
        if index == 0:
            config.write_text(json.dumps({"mcpServers": {"planted": {"command": "other"}}}))
            raise TimeoutError("boom")
        return "concluded"

    call_count, _, _ = _wire_agent_stubs(monkeypatch, tmp_path, operate_side_effect=side_effect)
    resolutions = []
    real_resolve = agent_mod.resolve_spawn_mcp_servers

    def capture_resolution(*args, **kwargs):
        result = real_resolve(*args, **kwargs)
        resolutions.append((args, result))
        return result

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", capture_resolution)

    _, _, _, status, _ = await agent_mod._run_agent(
        "claude_code/sonnet",
        "hello",
        timeout=30,
        resume_on_timeout=True,
        mcp_config=str(config),
    )

    assert status == "completed"
    assert call_count["n"] == 2
    first_run, second_run = call_count["runs"]
    (first_args, first), (resumed_args, resumed) = resolutions
    assert first_args[0] == str(config)
    assert resumed_args[0] == str(first_run.state_root / "mcp-config.json")
    assert resumed.servers == first.servers == SELECTED
    assert json.loads((second_run.state_root / "mcp-config.json").read_text()) == {
        "mcpServers": SELECTED
    }


@pytest.mark.asyncio
async def test_an_older_explicit_record_without_the_name_still_reads_the_named_file(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    config = _named(tmp_path)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    recorded = {
        "mcp_config": str(config),
        "mcp_config_origin": "explicit",
        "no_mcp_config": False,
        "launch_dir": str(tmp_path),
    }
    _record_launch(tmp_path, branch_id, recorded)
    seen = []

    async def capture(self, instruction=None, **kwargs):
        seen.append(
            (
                dict(self.chat_model.endpoint.config.kwargs),
                dict(self.metadata[agent_mod._MCP_LAUNCH_KEY]),
            )
        )
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "continue", resume=branch_id)

    [(request, record)] = seen
    assert set(request["mcp_servers"]) == {"selected"}
    assert record == recorded
    assert list((tmp_path / "run-state").glob("*/mcp-config.json")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"mcp_config_named": "relative.json"},
        {"mcp_config_named": 42},
        {"mcp_config_named": None},
        {"mcp_config_named": "/abs/named.json", "mcp_config_origin": "submit_snapshot"},
    ],
)
async def test_an_invalid_record_of_the_named_config_is_refused_before_any_lookup(
    monkeypatch, tmp_path, overrides
):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)

    def no_lookup(*_args, **_kwargs):
        pytest.fail("an invalid record must be refused before any MCP config is read")

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", no_lookup)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    copy = tmp_path / "mcp-config.json"
    copy.write_text(json.dumps({"mcpServers": SELECTED}))
    _record_launch(
        tmp_path,
        branch_id,
        {
            "mcp_config": str(copy),
            "mcp_config_origin": "explicit",
            "no_mcp_config": False,
            "launch_dir": str(tmp_path),
            **overrides,
        },
    )

    with pytest.raises(McpConfigError, match="invalid record of the MCP config it was named"):
        await agent_mod._run_agent(None, "continue", resume=branch_id)


@pytest.mark.asyncio
async def test_a_refusal_before_the_run_leaves_no_copy_and_no_branch(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod
    from lionagi._errors import ConfigurationError

    from .test_agent_scratch import _role, _wire_run, child_sandbox

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {"child_sandbox": {}})
    state = _wire_run(
        monkeypatch, tmp_path, _role("scratch: true\nbypass: true\n", model="codex/gpt-5.5")
    )

    async def no_persist(*_args, **_kwargs):
        pytest.fail("a refused run must persist no branch")

    monkeypatch.setattr(agent_mod, "setup_agent_persist", no_persist)
    config = _named(tmp_path)

    with pytest.raises(ConfigurationError, match="declares scratch"):
        await agent_mod._run_agent(None, "review", agent_name="role", mcp_config=str(config))

    assert state["allocated"] == 0
    assert list(tmp_path.rglob("mcp-config.json")) == []


@pytest.mark.asyncio
async def test_a_branch_on_a_discovered_config_gets_no_copy_and_searches_again(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.agent as agent_mod

    _, seen = _launch_with_real_snapshots(monkeypatch, tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": SELECTED}))

    await agent_mod._run_agent("claude_code/sonnet", "start")

    _, record = seen[0]
    assert record["mcp_config"] is None
    assert "mcp_config_named" not in record
    assert "mcp_config_origin" not in record
    assert list((tmp_path / "run-state").glob("*/mcp-config.json")) == []


def test_the_copy_is_outside_every_write_root_and_denied_to_the_file_tools(monkeypatch, tmp_path):
    from lionhub.agent import child_sandbox

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home / ".lionagi")
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {"child_sandbox": {}})
    workspace = tmp_path / "work"
    workspace.mkdir()
    copy = home / ".lionagi" / "runs" / "20261123-run" / "mcp-config.json"

    policy = child_sandbox.resolve_child_sandbox(None)
    settings = child_sandbox.claude_settings_for(policy, workspace=workspace)

    entry = f"{home}/.lionagi/runs/*/mcp-config.json"
    assert entry in child_sandbox.run_state_paths()
    assert f"Edit(/{entry})" in settings["permissions"]["deny"]
    assert fnmatch(str(copy), entry)
    # Bash writes are held to the working directory and allowWrite, the file tools to the same
    # roots by the hook, and a codex child to the same roots plus its scratch directory.
    roots = [str(workspace), *(os.path.expanduser(root) for root in policy.allow_write)]
    assert not any(
        str(copy) == root.rstrip("/") or str(copy).startswith(root.rstrip("/") + "/")
        for root in roots
    )
