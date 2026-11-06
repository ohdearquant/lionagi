# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A submit snapshot carries discovery origin across the CLI boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

from lionhub.serve.mcp import config, dispatch, jobs


class _Process:
    pid = 4242

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def submitted(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _orchestration as orch
    from lionhub.orchestration.runs import RunDir

    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(config, "child_actor_suffix", lambda: None)
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _: None)
    monkeypatch.setattr("lionhub.orchestration.spawn._plugin_agent_profiles", lambda: {})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    agents = cwd / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "ordinary.md").write_text("---\nmodel: codex/gpt-5\n---\nOrdinary.")
    (agents / "isolated.md").write_text("---\nmodel: codex/gpt-5\nmcp: none\n---\nIsolated.")
    source = cwd / ".mcp.json"
    source.write_text(json.dumps({"mcpServers": {"ambient": {"command": "echo"}}}))
    spawned = []

    def popen(argv, **kwargs):
        if argv[0] == "echo":
            spawned.append(argv)
        return _Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    serial = 0

    def allocate(save_dir=None, run_id=None):
        nonlocal serial
        serial += 1
        run = RunDir(
            run_id=f"run-{serial}",
            state_root=tmp_path / f"state-{serial}",
            artifact_root=tmp_path / f"artifacts-{serial}",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch, "allocate_run", allocate)
    return cwd, source, spawned


def _submit(op, args, *, playbook=None):
    help_target = op if playbook is None else {"verb": op, "playbook": playbook}
    fingerprint = asyncio.run(dispatch.request(help=help_target))["schema_fingerprint"]
    result = asyncio.run(
        dispatch.request(ops=[{"op": op, "args": args, "schema_fingerprint": fingerprint}])
    )["ops"][0]
    assert result["ok"], result
    return result["result"]


def _parse(argv):
    from lionagi.cli.orchestrate import add_orchestrate_subparser

    parser = argparse.ArgumentParser()
    add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(argv[1:])


async def _setup(parsed, *, agent_name=None):
    from lionagi.cli.orchestrate import _orchestration as orch

    return await orch.setup_orchestration(
        pattern_name="Flow" if parsed.orch_command == "flow" else "Fanout",
        model_spec=None,
        agent_name=agent_name or parsed.agent,
        save_dir=None,
        cwd=parsed.cwd,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
        mcp_config=parsed.mcp_config,
        no_mcp_config=parsed.no_mcp_config,
        mcp_submit_snapshot=parsed.mcp_submit_snapshot,
    )


@pytest.mark.parametrize("op", ["flow.submit", "fanout.submit"])
def test_snapshot_survives_source_drift_and_only_contained_worker_drops_it(submitted, op):
    from lionagi.cli.orchestrate import _orchestration as orch

    cwd, source, spawned = submitted
    handle = _submit(op, {"agent": "ordinary", "prompt": "work", "cwd": str(cwd)})
    argv = spawned[0]
    assert "--mcp-submit-snapshot" in argv
    assert "--mcp-config" not in argv
    assert argv.index("--mcp-submit-snapshot") < argv.index("--")
    parsed = _parse(argv)
    assert parsed.mcp_submit_snapshot == handle["mcp_config"]
    assert parsed.mcp_config is None
    source.write_text(json.dumps({"mcpServers": {"replacement": {"command": "other"}}}))

    async def check():
        env = await _setup(parsed)
        assert set(env.mcp_servers) == {"ambient"}
        ordinary, _, _, _ = await orch.build_worker_branch(env, agent_id="a", role="ordinary")
        isolated, _, _, _ = await orch.build_worker_branch(env, agent_id="b", role="isolated")
        ordinary_opts = ordinary.chat_model.endpoint.config.kwargs.get("config_overrides", {})
        isolated_opts = isolated.chat_model.endpoint.config.kwargs.get("config_overrides", {})
        assert ordinary_opts["mcp_servers.ambient.command"] == "echo"
        assert not any(k.startswith("mcp_servers.") for k in isolated_opts)
        assert isolated.metadata["mcp_launch"]["no_mcp_config_source"] == "profile"

    asyncio.run(check())


@pytest.mark.parametrize("op", ["flow.submit", "fanout.submit"])
def test_named_snapshot_shaped_config_remains_explicit(submitted, op):
    from lionagi._errors import ConfigurationError
    from lionagi.cli.orchestrate import _orchestration as orch

    cwd, source, spawned = submitted
    named = cwd / "caller.json"
    named.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ambient": {"command": "echo"},
                    "jobs": {"command": "li", "args": ["mcp"]},
                },
                "lionagiSnapshotOf": str(source),
                "lionagiSnapshotSearchedFrom": str(cwd),
            }
        )
    )
    handle = _submit(
        op, {"agent": "ordinary", "prompt": "work", "cwd": str(cwd), "mcp_config": str(named)}
    )
    argv = spawned[0]
    assert handle["mcp_config"] == str(named)
    assert "--mcp-submit-snapshot" not in argv
    parsed = _parse(argv)
    assert parsed.mcp_config == str(named)
    assert parsed.mcp_submit_snapshot is None

    async def check():
        env = await _setup(parsed)
        assert env.mcp_config_explicit is True
        assert set(env.mcp_servers) == {"ambient"}
        assert env.withheld_mcp_servers == ("jobs",)
        with pytest.raises(ConfigurationError, match="isolated.*mcp: none.*--mcp-config"):
            await orch.build_worker_branch(env, agent_id="b", role="isolated")

    asyncio.run(check())


def test_manually_named_submit_snapshot_has_ambient_origin(submitted):
    from lionagi.cli.orchestrate import _orchestration as orch

    cwd, _, _ = submitted
    snapshot = cwd / "manual.json"
    snapshot.write_text(json.dumps({"mcpServers": {"manual": {"command": "echo"}}}))
    parsed = _parse(
        [
            "echo",
            "orchestrate",
            "flow",
            "--agent=ordinary",
            f"--cwd={cwd}",
            f"--mcp-submit-snapshot={snapshot}",
            "--",
            "work",
        ]
    )

    async def check():
        env = await _setup(parsed)
        assert env.mcp_config_explicit is False
        assert set(env.mcp_servers) == {"manual"}
        worker, _, _, _ = await orch.build_worker_branch(env, agent_id="isolated", role="isolated")
        assert worker.metadata["mcp_launch"]["no_mcp_config_source"] == "profile"

    asyncio.run(check())


def test_playbook_contained_orchestrator_discards_submit_snapshot(submitted, monkeypatch):
    from lionagi.cli import orchestrate

    cwd, _, spawned = submitted
    books = cwd / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    (books / "isolated.playbook.yaml").write_text("name: isolated\nagent: isolated\nprompt: work\n")
    _submit("play.submit", {"playbook": "isolated", "cwd": str(cwd)}, playbook="isolated")
    argv = spawned[0]
    assert "--mcp-submit-snapshot" in argv
    parsed = _parse(argv)
    captured = []

    async def capture_flow(**kwargs):
        assert kwargs["agent_name"] == "isolated"
        env = await _setup(parsed, agent_name=kwargs["agent_name"])
        captured.append(env)
        return "ok", "completed"

    monkeypatch.setattr(orchestrate, "_run_flow", capture_flow)
    assert orchestrate.run_orchestrate(parsed) == 0
    assert len(captured) == 1
    assert captured[0].mcp_servers == {}


@pytest.mark.parametrize("named", [False, True])
def test_flow_checkpoint_replays_snapshot_origin_and_refusal(submitted, monkeypatch, named):
    from unittest.mock import AsyncMock

    from lionagi._errors import ConfigurationError
    from lionagi.cli.orchestrate import _orchestration as orch
    from lionagi.cli.orchestrate import flow
    from lionhub.orchestration.flow.checkpoint import CheckpointWriter, load_checkpoint

    cwd, source, spawned = submitted
    args = {"agent": "ordinary", "prompt": "work", "cwd": str(cwd)}
    if named:
        args["mcp_config"] = str(source)
    handle = _submit("flow.submit", args)
    parsed = _parse(spawned[0])
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    monkeypatch.setattr(flow, "start_live_persist", AsyncMock())
    monkeypatch.setattr(flow, "stop_live_persist", AsyncMock(return_value="completed"))
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_run_notify_outcome_scope",
        lambda *args, **kwargs: None,
    )
    calls = []

    async def inner(model_spec, prompt, *, env, checkpoint_config, resume_checkpoint, **kwargs):
        assert set(env.mcp_servers) == {"ambient"}
        if named:
            assert env.mcp_config_explicit is True
            with pytest.raises(ConfigurationError, match="mcp: none.*--mcp-config"):
                await orch.build_worker_branch(env, agent_id="isolated", role="isolated")
        else:
            assert env.mcp_config_explicit is False
            worker, _, _, _ = await orch.build_worker_branch(
                env, agent_id="isolated", role="isolated"
            )
            assert worker.metadata["mcp_launch"]["no_mcp_config_source"] == "profile"
        writer = CheckpointWriter(env.run.checkpoint_path, "session", prompt, [], checkpoint_config)
        await writer.flush()
        calls.append((env.run, dict(checkpoint_config), resume_checkpoint))
        return "ok"

    monkeypatch.setattr(flow, "_run_flow_inner", inner)

    async def exercise():
        result = await flow._run_flow(
            model_spec="",
            prompt="work",
            agent_name="ordinary",
            cwd=str(cwd),
            mcp_config=parsed.mcp_config,
            mcp_submit_snapshot=parsed.mcp_submit_snapshot,
        )
        assert result == ("ok", "completed")
        first_run, first_config, _ = calls[0]
        checkpoint = load_checkpoint(first_run.checkpoint_path)
        if named:
            assert checkpoint["config"]["mcp_config"] == str(source)
            assert checkpoint["config"].get("mcp_config_origin") != "submit_snapshot"
        else:
            assert checkpoint["config"]["mcp_config_origin"] == "submit_snapshot"
            assert checkpoint["config"]["mcp_submit_snapshot"] == handle["mcp_config"]
            source.write_text(json.dumps({"mcpServers": {"replacement": {"command": "other"}}}))

        async def resolve(target):
            return first_run, load_checkpoint(first_run.checkpoint_path)

        monkeypatch.setattr(flow, "resolve_checkpoint_target", resolve)
        assert await flow._resume_flow("original", retry_failed=True) == ("ok", "completed")
        _, second_config, resumed = calls[1]
        assert resumed == checkpoint
        if not named:
            assert second_config["mcp_config_origin"] == "submit_snapshot"
            assert second_config["mcp_submit_snapshot"] == handle["mcp_config"]
            Path(handle["mcp_config"]).unlink()
            from lionhub.orchestration.mcp_config import McpConfigError

            with pytest.raises(McpConfigError, match="mcp-submit-snapshot"):
                await flow._resume_flow("original", retry_failed=True)
        else:
            assert second_config["mcp_config"] == str(source)
            assert second_config.get("mcp_config_origin") != "submit_snapshot"

    asyncio.run(exercise())


def test_submitted_agent_carries_ambient_snapshot_as_discovered(submitted):
    from lionagi.cli.agent import add_agent_subparser
    from lionhub.orchestration.mcp_config import resolve_spawn_mcp_servers

    cwd, source, spawned = submitted
    handle = _submit("agent.submit", {"agent": "ordinary", "prompt": "work", "cwd": str(cwd)})
    argv = spawned[0]
    assert "--mcp-submit-snapshot" in argv
    assert "--mcp-config" not in argv
    parser = argparse.ArgumentParser()
    add_agent_subparser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(argv[1:])
    assert parsed.mcp_submit_snapshot == handle["mcp_config"]
    assert parsed.mcp_config is None
    source.write_text(json.dumps({"mcpServers": {"replacement": {"command": "other"}}}))
    resolution = resolve_spawn_mcp_servers(
        parsed.mcp_config, launch_dir=cwd, submit_snapshot=parsed.mcp_submit_snapshot
    )
    assert resolution.explicit is False
    assert set(resolution.servers) == {"ambient"}


def test_agent_contained_profile_refuses_caller_named_snapshot_shaped_config(submitted):
    cwd, source, spawned = submitted
    named = cwd / "caller.json"
    named.write_text(
        json.dumps(
            {
                "mcpServers": {"ambient": {"command": "echo"}},
                "lionagiSnapshotOf": str(source),
                "lionagiSnapshotSearchedFrom": str(cwd),
            }
        )
    )
    fingerprint = asyncio.run(dispatch.request(help="agent.submit"))["schema_fingerprint"]
    result = asyncio.run(
        dispatch.request(
            ops=[
                {
                    "op": "agent.submit",
                    "schema_fingerprint": fingerprint,
                    "args": {
                        "agent": "isolated",
                        "prompt": "work",
                        "cwd": str(cwd),
                        "mcp_config": str(named),
                    },
                }
            ]
        )
    )["ops"][0]
    assert not result["ok"]
    assert result["error"]["kind"] == "invalid_input"
    assert "isolated" in result["error"]["message"]
    assert "--mcp-config" in result["error"]["message"]
    assert spawned == []
