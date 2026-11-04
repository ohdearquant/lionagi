# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A profile's MCP refusal remains attached to a resumed branch."""

from __future__ import annotations

import json

import pytest

from tests.cli.test_mcp_set_reaches_every_cli_provider import _record_launch, _wire_resume_stubs


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude_code"])
async def test_profile_containment_survives_three_resumes(
    monkeypatch, tmp_path, codex_home, provider
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.operations.run.run import _write_branch_snapshot

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    codex_home.write_config({"ambient": {"command": "synthetic-server"}})
    agent_dir = tmp_path / ".lionagi" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "contained.md").write_text("---\nmcp: none\n---\nReview.\n")
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"mcpServers": {"selected": {"command": "server"}}}))
    model = "gpt-5.3-codex" if provider == "codex" else "sonnet"
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, provider, model)
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": str(config), "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )
    branch_path = tmp_path / f"{branch_id}.json"
    captured = []

    async def capture(self, instruction=None, **kwargs):
        cfg = self.chat_model.endpoint.config.kwargs
        captured.append((dict(cfg), dict(self.metadata[agent_mod._MCP_LAUNCH_KEY])))
        # Exercise the production branch-snapshot serialization between turns.
        await _write_branch_snapshot(self, tmp_path)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "first", resume=branch_id)
    assert captured[-1][1]["no_mcp_config"] is False

    await agent_mod._run_agent(None, "second", resume=branch_id, agent_name="contained")
    contained = json.loads(branch_path.read_text())["metadata"][agent_mod._MCP_LAUNCH_KEY]
    assert contained["no_mcp_config"] is True
    assert contained["no_mcp_config_source"] == "profile"

    await agent_mod._run_agent(None, "third", resume=branch_id)
    cfg, recorded = captured[-1]
    assert recorded["no_mcp_config_source"] == "profile"
    if provider == "codex":
        assert cfg["ignore_user_config"] is True
        assert not any(key.startswith("mcp_servers.") for key in cfg.get("config_overrides", {}))
    else:
        assert cfg["mcp_servers"] == {}
        assert cfg["strict_mcp_config"] is True

    with pytest.raises(ValueError, match="profile mcp: none.*--mcp-config"):
        await agent_mod._run_agent(None, "override", resume=branch_id, mcp_config=str(config))
    assert len(captured) == 3


@pytest.mark.asyncio
async def test_agent_resume_replays_submitted_snapshot_origin(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.chdir(tmp_path)
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"mcpServers": {"original": {"command": "server"}}}))
    newer = tmp_path / "new.json"
    newer.write_text(json.dumps({"mcpServers": {"new": {"command": "server"}}}))
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {
            "mcp_config": str(old),
            "mcp_config_origin": "submit_snapshot",
            "no_mcp_config": False,
            "launch_dir": str(tmp_path),
        },
    )
    calls = []
    real_resolve = agent_mod.resolve_spawn_mcp_servers

    def capture_resolution(*args, **kwargs):
        result = real_resolve(*args, **kwargs)
        calls.append((args, kwargs, result))
        return result

    seen = {}

    async def capture_branch(self, instruction=None, **kwargs):
        seen.update(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", capture_resolution)
    monkeypatch.setattr(Branch, "operate", capture_branch)
    await agent_mod._run_agent(None, "continue", resume=branch_id, mcp_submit_snapshot=str(newer))
    assert calls[-1][1]["submit_snapshot"] == str(old)
    assert calls[-1][2].explicit is False
    assert set(seen["mcp_servers"]) == {"original"}
