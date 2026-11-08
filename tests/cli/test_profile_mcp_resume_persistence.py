# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A profile's MCP refusal remains attached to a resumed branch."""

from __future__ import annotations

import json

import pytest

from tests.cli.test_mcp_set_reaches_every_cli_provider import _record_launch, _wire_resume_stubs


@pytest.mark.asyncio
async def test_named_config_with_snapshot_marker_survives_real_branch_resume(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.operations.run.run import _write_branch_snapshot
    from tests.cli.test_agent_preset_mcp_handover import _wire_agent_stubs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    config = tmp_path / "named.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {"selected": {"command": "server"}},
                "lionagiSnapshotOf": str(tmp_path / "another.json"),
            }
        )
    )
    branches_dir = tmp_path / "branches"
    branches_dir.mkdir()
    captured = []

    async def capture(self, instruction=None, **kwargs):
        captured.append(dict(self.chat_model.endpoint.config.kwargs))
        await _write_branch_snapshot(self, branches_dir)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    _, _, branch_id, _, _ = await agent_mod._run_agent(
        "claude_code/sonnet", "start", mcp_config=str(config)
    )
    branch_path = branches_dir / f"{branch_id}.json"
    record = json.loads(branch_path.read_text())["metadata"][agent_mod._MCP_LAUNCH_KEY]
    (tmp_path / ".mcp.json").write_text("{invalid ambient config")
    monkeypatch.setattr(agent_mod, "find_branch", lambda _bid: ("r", branch_path))
    await agent_mod._run_agent(None, "resume", resume=branch_id)
    assert record["mcp_config_origin"] == "explicit"
    assert len(captured) == 2
    assert all(set(call["mcp_servers"]) == {"selected"} for call in captured)
    assert all(call["strict_mcp_config"] is True for call in captured)


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
    # The current directory is not the source of a recorded snapshot.
    (tmp_path / ".mcp.json").write_text('{"mcpServers":' + "[" * 1500 + "0" + "]" * 1500 + "}")
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        {"mcp_config_origin": "submit_snapshot", "mcp_config": None},
        {"mcp_config_origin": "submit_snapshot", "mcp_config": ""},
        {"mcp_config_origin": "submit_snapshot", "mcp_config": 42},
        {"mcp_config_origin": "unexpected", "mcp_config": "snapshot.json"},
        {"mcp_config_origin": [], "mcp_config": "snapshot.json"},
        {"mcp_config_origin": "explicit", "mcp_config": None},
        {"mcp_config_origin": "explicit", "mcp_config": "relative.json"},
        {"mcp_config_origin": "explicit", "mcp_config": "/tmp/config", "no_mcp_config": True},
        {"mcp_config_origin": "submit_snapshot", "mcp_config": "snapshot.json", "launch_dir": None},
        {
            "mcp_config_origin": "submit_snapshot",
            "mcp_config": "snapshot.json",
            "no_mcp_config": True,
        },
        {
            "mcp_config_origin": "submit_snapshot",
            "mcp_config": "snapshot.json",
            "no_mcp_config": None,
        },
        {
            "mcp_config_origin": "submit_snapshot",
            "mcp_config": "snapshot.json",
            "no_mcp_config": "false",
        },
        {
            "mcp_config_origin": "submit_snapshot",
            "mcp_config": "snapshot.json",
            "no_mcp_config": 0,
        },
        {
            "mcp_config_origin": "submit_snapshot",
            "mcp_config": "snapshot.json",
            "no_mcp_config": "__absent__",
        },
    ],
)
async def test_resume_refuses_invalid_recorded_snapshot_origin(monkeypatch, tmp_path, record):
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)

    def no_discovery(*_args, **_kwargs):
        pytest.fail("invalid recorded snapshot must be refused before MCP discovery")

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", no_discovery)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"ambient": {"command": "server"}}})
    )
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    launch = {"mcp_config": None, "no_mcp_config": False, "launch_dir": str(tmp_path), **record}
    if launch["no_mcp_config"] == "__absent__":
        launch.pop("no_mcp_config")
    _record_launch(tmp_path, branch_id, launch)
    with pytest.raises(McpConfigError, match="invalid recorded MCP snapshot origin"):
        await agent_mod._run_agent(None, "continue", resume=branch_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["snapshot_marker", "missing", "malformed", "deep"])
async def test_resume_refuses_ambiguous_legacy_snapshot_before_discovery(
    monkeypatch, tmp_path, kind
):
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    (tmp_path / ".mcp.json").write_text('{"mcpServers":' + "[" * 1500 + "0" + "]" * 1500 + "}")
    config = tmp_path / "recorded.json"
    if kind == "snapshot_marker":
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {"old": {"command": "server"}},
                    "lionagiSnapshotOf": str(tmp_path / "original.json"),
                }
            )
        )
    elif kind == "malformed":
        config.write_text("{broken")
    elif kind == "deep":
        config.write_text('{"mcpServers":' + "[" * 1500 + "0" + "]" * 1500 + "}")
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": str(config), "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )
    with pytest.raises(McpConfigError) as raised:
        await agent_mod._run_agent(None, "continue", resume=branch_id)
    assert str(config) in str(raised.value)
    assert "fresh run" in str(raised.value)
    if kind == "snapshot_marker":
        assert "ambiguous" in str(raised.value)


@pytest.mark.asyncio
async def test_resume_keeps_unmarked_legacy_config_explicit_without_ambient_read(
    monkeypatch, tmp_path
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    (tmp_path / ".mcp.json").write_text('{"mcpServers":' + "[" * 1500 + "0" + "]" * 1500 + "}")
    config = tmp_path / "recorded.json"
    config.write_text(json.dumps({"mcpServers": {"old": {"command": "server"}}}))
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": str(config), "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )
    reads = []
    read_bytes = type(config).read_bytes

    def replace_after_read(path):
        raw = read_bytes(path)
        if path == config:
            reads.append(path)
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {"changed": {"command": "server"}},
                        "lionagiSnapshotOf": str(tmp_path / "original.json"),
                    }
                )
            )
        return raw

    monkeypatch.setattr(type(config), "read_bytes", replace_after_read)
    seen = {}

    async def capture(self, instruction=None, **kwargs):
        seen.update(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "continue", resume=branch_id)
    assert set(seen["mcp_servers"]) == {"old"}
    assert seen["strict_mcp_config"] is True
    assert reads == [config]


@pytest.mark.asyncio
async def test_explicit_resume_config_overrides_ambiguous_legacy_record(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    (tmp_path / ".mcp.json").write_text('{"mcpServers":' + "[" * 1500 + "0" + "]" * 1500 + "}")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "mcpServers": {"old": {"command": "server"}},
                "lionagiSnapshotOf": str(tmp_path / "original.json"),
            }
        )
    )
    chosen = tmp_path / "chosen.json"
    chosen.write_text(json.dumps({"mcpServers": {"new": {"command": "server"}}}))
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": str(legacy), "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )
    seen = {}

    async def capture(self, instruction=None, **kwargs):
        seen.update(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "continue", resume=branch_id, mcp_config=str(chosen))
    assert set(seen["mcp_servers"]) == {"new"}
    assert seen["strict_mcp_config"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("record_kind", ["recorded_search", "recorded_disabled", "no_record"])
async def test_agent_replay_never_preloads_current_ambient(monkeypatch, tmp_path, record_kind):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    (tmp_path / ".mcp.json").write_text('{"mcpServers":' + "[" * 1500 + "0" + "]" * 1500 + "}")
    old_dir = tmp_path / "original-launch"
    old_dir.mkdir()
    (old_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"old": {"command": "old-server"}}})
    )
    branch_id = _wire_resume_stubs(
        monkeypatch, tmp_path, "claude_code", "sonnet", mcp_servers={"persisted": {"command": "x"}}
    )
    if record_kind != "no_record":
        _record_launch(
            tmp_path,
            branch_id,
            {
                "mcp_config": None,
                "no_mcp_config": record_kind == "recorded_disabled",
                "launch_dir": str(old_dir),
            },
        )
    seen = {}

    async def capture(self, instruction=None, **kwargs):
        seen.update(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "continue", resume=branch_id)
    expected = {"recorded_search": {"old"}, "recorded_disabled": set(), "no_record": {"persisted"}}
    assert set(seen["mcp_servers"]) == expected[record_kind]
    if record_kind == "recorded_disabled":
        assert seen["strict_mcp_config"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "submit_snapshot"])
async def test_replay_rejects_relative_recorded_launch_dir_before_any_lookup(
    monkeypatch, tmp_path, origin
):
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)

    def no_lookup(*_args, **_kwargs):
        pytest.fail("invalid recorded launch directory must be refused before MCP lookup")

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", no_lookup)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    launch = {
        "mcp_config": "recorded.json",
        "no_mcp_config": False,
        "launch_dir": "relative/launch",
    }
    if origin is not None:
        launch["mcp_config_origin"] = origin
        launch["mcp_config"] = str(tmp_path / "recorded.json")
    _record_launch(tmp_path, branch_id, launch)
    with pytest.raises(McpConfigError, match="invalid recorded MCP launch directory"):
        await agent_mod._run_agent(None, "continue", resume=branch_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [{"secret": "do-not-echo-secret"}, "bad\nline\u202eevil"])
async def test_legacy_record_error_safely_formats_malformed_path(monkeypatch, tmp_path, recorded):
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": recorded, "no_mcp_config": False, "launch_dir": str(tmp_path)},
    )
    with pytest.raises(McpConfigError) as raised:
        await agent_mod._run_agent(None, "continue", resume=branch_id)
    message = str(raised.value)
    assert "start a fresh run" in message
    assert "\n" not in message and "\u202e" not in message
    assert "do-not-echo-secret" not in message
    if not isinstance(recorded, str):
        assert "<invalid path>" in message


@pytest.mark.asyncio
async def test_replay_refuses_relative_submit_snapshot_before_lookup(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)

    def no_lookup(*_args, **_kwargs):
        pytest.fail("relative recorded submit snapshot must be refused before MCP lookup")

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", no_lookup)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {
            "mcp_config": "relative-snapshot.json",
            "mcp_config_origin": "submit_snapshot",
            "no_mcp_config": False,
            "launch_dir": str(tmp_path),
        },
    )
    with pytest.raises(McpConfigError, match="invalid recorded MCP snapshot origin"):
        await agent_mod._run_agent(None, "continue", resume=branch_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [None, "false", 0, 1, []])
async def test_replay_refuses_non_boolean_legacy_mcp_disabled_before_lookup(
    monkeypatch, tmp_path, disabled
):
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)

    def no_lookup(*_args, **_kwargs):
        pytest.fail("invalid legacy MCP flag must be refused before MCP lookup")

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", no_lookup)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": None, "no_mcp_config": disabled, "launch_dir": str(tmp_path)},
    )
    with pytest.raises(McpConfigError, match="invalid recorded MCP configuration flag"):
        await agent_mod._run_agent(None, "continue", resume=branch_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [{}, {"mcp_config": None, "no_mcp_config": False}])
async def test_empty_legacy_mcp_record_keeps_stored_branch_request(monkeypatch, tmp_path, legacy):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"current": {"command": "current-server"}}})
    )
    branch_id = _wire_resume_stubs(
        monkeypatch, tmp_path, "claude_code", "sonnet", mcp_servers={"persisted": {"command": "x"}}
    )
    _record_launch(tmp_path, branch_id, legacy)
    seen = {}

    async def capture(self, instruction=None, **kwargs):
        seen.update(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "continue", resume=branch_id)
    assert set(seen["mcp_servers"]) == {"persisted"}


@pytest.mark.asyncio
async def test_legacy_recorded_search_uses_recorded_dir_not_current_dir(monkeypatch, tmp_path):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)
    current = tmp_path / "current"
    recorded = tmp_path / "recorded"
    current.mkdir()
    recorded.mkdir()
    (current / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"current": {"command": "current-server"}}})
    )
    (recorded / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"recorded": {"command": "recorded-server"}}})
    )
    monkeypatch.chdir(current)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(
        tmp_path,
        branch_id,
        {"mcp_config": None, "no_mcp_config": False, "launch_dir": str(recorded)},
    )
    seen = {}

    async def capture(self, instruction=None, **kwargs):
        seen.update(self.chat_model.endpoint.config.kwargs)
        return "done"

    monkeypatch.setattr(Branch, "operate", capture)
    await agent_mod._run_agent(None, "continue", resume=branch_id)
    assert set(seen["mcp_servers"]) == {"recorded"}


@pytest.mark.asyncio
@pytest.mark.parametrize("config", ["relative.json", "/tmp/recorded.json"])
async def test_legacy_record_with_path_but_no_launch_dir_is_refused_before_lookup(
    monkeypatch, tmp_path, config
):
    import lionagi.cli.agent as agent_mod
    from lionhub.orchestration.mcp_config import McpConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "stamp_agent_depth", lambda _name: None)

    def no_lookup(*_args, **_kwargs):
        pytest.fail("missing recorded launch directory must be refused before MCP lookup")

    monkeypatch.setattr(agent_mod, "resolve_spawn_mcp_servers", no_lookup)
    branch_id = _wire_resume_stubs(monkeypatch, tmp_path, "claude_code", "sonnet")
    _record_launch(tmp_path, branch_id, {"mcp_config": config, "no_mcp_config": False})
    with pytest.raises(McpConfigError, match="invalid recorded MCP launch directory"):
        await agent_mod._run_agent(None, "continue", resume=branch_id)
