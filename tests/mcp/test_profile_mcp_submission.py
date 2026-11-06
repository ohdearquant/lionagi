# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Profile containment applies before the MCP submission snapshots servers."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

from lionhub.serve.mcp import config, dispatch, jobs


class _FakeProcess:
    pid = 4242

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def submission(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(config, "child_actor_suffix", lambda: None)
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _run_id: None)
    monkeypatch.setattr("lionhub.orchestration.spawn._plugin_agent_profiles", lambda: {})
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    agents = cwd / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "isolated.md").write_text(
        "---\nmodel: codex/gpt-5\nmcp: none\n---\nIsolated profile."
    )
    (agents / "ordinary.md").write_text("---\nmodel: codex/gpt-5\n---\nOrdinary profile.")
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "echo"}}}))
    spawned = []

    def popen(argv, **kwargs):
        if argv[0] == "echo":
            spawned.append(argv)
        return _FakeProcess()

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    return cwd, spawned


def _submit(op, args):
    fingerprint = asyncio.run(dispatch.request(help=op))["schema_fingerprint"]
    response = asyncio.run(
        dispatch.request(ops=[{"op": op, "args": args, "schema_fingerprint": fingerprint}])
    )
    return response["ops"][0]


@pytest.mark.parametrize("op", ["agent.submit", "flow.submit", "fanout.submit"])
def test_contained_profile_disables_ambient_snapshot_at_submit(submission, op):
    cwd, spawned = submission
    result = _submit(op, {"agent": "isolated", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"], result
    handle = result["result"]
    assert handle["mcp_config"] is None
    assert handle["mcp_config_source"] is None
    assert handle["declared_mcp_servers"] == []
    assert handle["mcp_config_servers"] == []
    assert handle["mcp_config_reason"] == "mcp_disabled_by_profile:isolated"
    record = jobs._read_job(handle["run_id"])
    assert record["mcp_config_reason"] == "mcp_disabled_by_profile:isolated"
    assert record["declared_mcp_servers"] == []
    argv = spawned[0]
    assert "--no-mcp-config" in argv
    assert not any(arg == "--mcp-config" or arg.startswith("--mcp-config=") for arg in argv)
    if "--" in argv:
        assert argv.index("--no-mcp-config") < argv.index("--")
    assert not list(config.JOBS_DIR.rglob("mcp-servers.json"))


@pytest.mark.parametrize("op", ["agent.submit", "flow.submit", "fanout.submit"])
def test_profile_and_caller_no_config_do_not_duplicate_flag(submission, op):
    cwd, spawned = submission
    result = _submit(
        op,
        {"agent": "isolated", "prompt": "do work", "cwd": str(cwd), "no_mcp_config": True},
    )
    assert result["ok"], result
    assert result["result"]["mcp_config_reason"] == "mcp_disabled_by_profile:isolated"
    assert spawned[0].count("--no-mcp-config") == 1


def test_profile_none_rejects_exclusions_at_submit(submission):
    cwd, spawned = submission
    result = _submit(
        "agent.submit",
        {
            "agent": "isolated",
            "prompt": "do work",
            "cwd": str(cwd),
            "exclude_mcp_servers": ["ambient"],
        },
    )
    assert not result["ok"]
    assert result["error"]["kind"] == "invalid_input"
    assert spawned == []


@pytest.mark.parametrize("op", ["agent.submit", "flow.submit", "fanout.submit"])
def test_contained_profile_rejects_explicit_config_before_spawn(submission, op):
    cwd, spawned = submission
    chosen = cwd / "chosen.json"
    chosen.write_text(json.dumps({"mcpServers": {"chosen": {"command": "echo"}}}))
    result = _submit(
        op,
        {"agent": "isolated", "prompt": "do work", "cwd": str(cwd), "mcp_config": str(chosen)},
    )
    assert not result["ok"]
    assert result["error"]["kind"] == "invalid_input"
    assert "isolated" in result["error"]["message"]
    assert "mcp: none" in result["error"]["message"]
    assert "--mcp-config" in result["error"]["message"]
    assert spawned == []
    assert not config.JOBS_DIR.exists() or list(config.JOBS_DIR.iterdir()) == []


@pytest.mark.parametrize("op", ["agent.submit", "flow.submit", "fanout.submit"])
def test_noncontained_profile_still_receives_ambient_snapshot(submission, op):
    cwd, spawned = submission
    result = _submit(op, {"agent": "ordinary", "prompt": "do work", "cwd": str(cwd)})
    assert result["ok"], result
    handle = result["result"]
    assert handle["declared_mcp_servers"] == ["ambient"]
    assert handle["mcp_config_source"] == str(cwd / ".mcp.json")
    assert "--mcp-submit-snapshot" in spawned[0]


def test_flow_contained_orchestrator_matches_cli_worker_servers(submission, monkeypatch, tmp_path):
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration as orch
    from lionhub.orchestration.flow.parsing import add_orchestrate_subparser

    cwd, spawned = submission
    result = _submit("flow.submit", {"agent": "isolated", "prompt": "work", "cwd": str(cwd)})
    assert result["ok"], result
    argv = spawned[0]
    assert not any(arg == "--mcp-config" or arg.startswith("--mcp-config=") for arg in argv)
    parser = argparse.ArgumentParser()
    add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(argv[1:])
    assert parsed.no_mcp_config is True

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    count = 0

    def allocate(save_dir=None, run_id=None):
        nonlocal count
        count += 1
        run = RunDir(
            run_id=f"test-{count}",
            state_root=tmp_path / f"state-{count}",
            artifact_root=tmp_path / f"artifacts-{count}",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch, "allocate_run", allocate)

    async def setup_and_build(*, no_mcp_config):
        env = await orch.setup_orchestration(
            pattern_name="Flow",
            model_spec=None,
            agent_name=parsed.agent,
            save_dir=None,
            cwd=parsed.cwd,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            mcp_config=None,
            no_mcp_config=no_mcp_config,
        )
        worker, _, _, _ = await orch.build_worker_branch(env, agent_id="worker", role="ordinary")
        overrides = worker.chat_model.endpoint.config.kwargs.get("config_overrides", {})
        return env.mcp_servers, {k: v for k, v in overrides.items() if k.startswith("mcp_servers.")}

    submitted = asyncio.run(setup_and_build(no_mcp_config=parsed.no_mcp_config))
    direct_cli = asyncio.run(setup_and_build(no_mcp_config=False))
    assert submitted == direct_cli == ({}, {})


def test_flow_default_contained_orchestrator_matches_cli(submission, monkeypatch, tmp_path):
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration as orch
    from lionhub.orchestration.flow.parsing import add_orchestrate_subparser

    cwd, spawned = submission
    (cwd / ".lionagi" / "agents" / "orchestrator.md").write_text(
        "---\nmodel: codex/gpt-5\nmcp: none\n---\nDefault orchestrator."
    )
    result = _submit("flow.submit", {"prompt": "work", "cwd": str(cwd)})
    assert result["ok"], result
    argv = spawned[0]
    assert not any(arg == "--mcp-config" or arg.startswith("--mcp-config=") for arg in argv)
    assert result["result"]["declared_mcp_servers"] == []
    assert result["result"]["mcp_config_reason"] == "mcp_disabled_by_profile:orchestrator"
    parser = argparse.ArgumentParser()
    add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(argv[1:])
    assert parsed.agent is None
    assert parsed.no_mcp_config is True

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    count = 0

    def allocate(save_dir=None, run_id=None):
        nonlocal count
        count += 1
        run = RunDir(
            run_id=f"test-{count}",
            state_root=tmp_path / f"state-{count}",
            artifact_root=tmp_path / f"artifacts-{count}",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch, "allocate_run", allocate)

    async def setup_and_build(no_mcp_config):
        env = await orch.setup_orchestration(
            pattern_name="Flow",
            model_spec=None,
            agent_name=None,
            save_dir=None,
            cwd=parsed.cwd,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            mcp_config=None,
            no_mcp_config=no_mcp_config,
        )
        worker, _, _, _ = await orch.build_worker_branch(env, agent_id="worker", role="ordinary")
        overrides = worker.chat_model.endpoint.config.kwargs.get("config_overrides", {})
        return env.mcp_servers, {k: v for k, v in overrides.items() if k.startswith("mcp_servers.")}

    assert (
        asyncio.run(setup_and_build(parsed.no_mcp_config))
        == asyncio.run(setup_and_build(False))
        == ({}, {})
    )


def test_flow_model_skips_default_contained_orchestrator(submission):
    cwd, spawned = submission
    (cwd / ".lionagi" / "agents" / "orchestrator.md").write_text(
        "---\nmodel: codex/gpt-5\nmcp: none\n---\nDefault orchestrator."
    )
    result = _submit("flow.submit", {"query": ["codex/gpt-5"], "prompt": "work", "cwd": str(cwd)})
    assert result["ok"], result
    assert result["result"]["declared_mcp_servers"] == ["ambient"]
    assert "--mcp-submit-snapshot" in spawned[0]


@pytest.mark.parametrize("op", ["flow.submit", "fanout.submit"])
@pytest.mark.parametrize(
    ("query", "expected_model", "expected_servers"),
    [(["work"], None, []), (["codex/gpt-5", "work"], "codex/gpt-5", ["ambient"])],
)
def test_positional_query_selects_default_or_explicit_model(
    submission, op, query, expected_model, expected_servers
):
    from lionagi.cli.orchestrate import _resolve_model_and_prompt, add_orchestrate_subparser

    cwd, spawned = submission
    (cwd / ".lionagi" / "agents" / "orchestrator.md").write_text(
        "---\nmodel: codex/gpt-5\nmcp: none\n---\nDefault orchestrator."
    )
    result = _submit(op, {"query": query, "cwd": str(cwd)})
    assert result["ok"], result
    assert result["result"]["declared_mcp_servers"] == expected_servers
    parser = argparse.ArgumentParser()
    add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(spawned[0][1:])
    assert _resolve_model_and_prompt(parsed.query) == (expected_model, "work")
    assert parsed.no_mcp_config is (expected_model is None)
    assert ("--mcp-submit-snapshot" in spawned[0]) is (expected_model is not None)


def test_fanout_uses_contained_default_profile(submission):
    cwd, spawned = submission
    (cwd / ".lionagi" / "agents" / "orchestrator.md").write_text(
        "---\nmodel: codex/gpt-5\nmcp: none\n---\nDefault orchestrator."
    )
    result = _submit("fanout.submit", {"prompt": "work", "cwd": str(cwd)})
    assert result["ok"], result
    assert result["result"]["declared_mcp_servers"] == []
    assert "--mcp-config" not in spawned[0]
    assert "--no-mcp-config" in spawned[0]


@pytest.mark.parametrize("op", ["flow.submit", "fanout.submit"])
def test_contained_default_refuses_explicit_config(submission, op):
    cwd, spawned = submission
    (cwd / ".lionagi" / "agents" / "orchestrator.md").write_text(
        "---\nmodel: codex/gpt-5\nmcp: none\n---\nDefault orchestrator."
    )
    result = _submit(op, {"prompt": "work", "cwd": str(cwd), "mcp_config": str(cwd / ".mcp.json")})
    assert not result["ok"]
    assert result["error"]["kind"] == "invalid_input"
    assert "orchestrator" in result["error"]["message"]
    assert "--mcp-config" in result["error"]["message"]
    assert not spawned


def test_missing_default_profile_keeps_existing_submit_behavior(submission, monkeypatch, tmp_path):
    cwd, spawned = submission
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    result = _submit("flow.submit", {"prompt": "work", "cwd": str(cwd)})
    assert result["ok"], result
    assert result["result"]["declared_mcp_servers"] == ["ambient"]
    assert "--mcp-submit-snapshot" in spawned[0]


def test_flow_spec_does_not_assume_default_profile(submission):
    cwd, spawned = submission
    (cwd / ".lionagi" / "agents" / "orchestrator.md").write_text(
        "---\nmodel: codex/gpt-5\nmcp: none\n---\nDefault orchestrator."
    )
    spec = cwd / "flow.yaml"
    spec.write_text("model: codex/gpt-5\nprompt: work\n")
    result = _submit("flow.submit", {"file": str(spec), "cwd": str(cwd)})
    assert result["ok"], result
    assert result["result"]["declared_mcp_servers"] == ["ambient"]
    assert "--mcp-submit-snapshot" in spawned[0]


def test_flow_spec_contained_orchestrator_discards_ambient_snapshot(
    submission, monkeypatch, tmp_path
):
    from lionagi.cli import orchestrate
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration as orch

    cwd, spawned = submission
    spec = cwd / "flow.yaml"
    spec.write_text("agent: isolated\nprompt: work\n")
    result = _submit("flow.submit", {"file": str(spec), "cwd": str(cwd)})
    assert result["ok"], result
    assert result["result"]["declared_mcp_servers"] == ["ambient"]
    assert "--mcp-submit-snapshot" in spawned[0]

    parser = argparse.ArgumentParser()
    orchestrate.add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(spawned[0][1:])
    captured = []

    def allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="test", state_root=tmp_path / "state", artifact_root=tmp_path / "artifacts"
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch, "allocate_run", allocate)

    async def capture_flow(**kwargs):
        env = await orch.setup_orchestration(
            pattern_name="Flow",
            model_spec=kwargs["model_spec"],
            agent_name=kwargs["agent_name"],
            save_dir=None,
            cwd=kwargs["cwd"],
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            mcp_config=kwargs["mcp_config"],
            no_mcp_config=kwargs["no_mcp_config"],
            mcp_submit_snapshot=kwargs["mcp_submit_snapshot"],
        )
        captured.append(env)
        return "ok", "completed"

    monkeypatch.setattr(orchestrate, "_run_flow", capture_flow)
    assert orchestrate.run_orchestrate(parsed) == 0
    assert len(captured) == 1
    assert captured[0].orc_profile.name == "isolated"
    assert captured[0].mcp_servers == {}


@pytest.mark.parametrize("op", ["flow.submit", "fanout.submit"])
@pytest.mark.parametrize("named_config", [False, True])
def test_submitted_worker_profile_distinguishes_named_and_snapshotted_config(
    submission, monkeypatch, tmp_path, op, named_config
):
    from lionagi._errors import ConfigurationError
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration as orch
    from lionhub.orchestration.flow.parsing import add_orchestrate_subparser

    cwd, spawned = submission
    args = {"agent": "ordinary", "prompt": "do work", "cwd": str(cwd)}
    if named_config:
        args["mcp_config"] = str(cwd / ".mcp.json")
    result = _submit(op, args)
    assert result["ok"], result
    parser = argparse.ArgumentParser()
    add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(spawned[0][1:])

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="test", state_root=tmp_path / "state", artifact_root=tmp_path / "artifacts"
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch, "allocate_run", allocate)

    async def run():
        env = await orch.setup_orchestration(
            pattern_name="Flow" if op == "flow.submit" else "Fanout",
            model_spec=None,
            agent_name=parsed.agent,
            save_dir=None,
            cwd=parsed.cwd,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            mcp_config=parsed.mcp_config,
            mcp_submit_snapshot=parsed.mcp_submit_snapshot,
            no_mcp_config=parsed.no_mcp_config,
        )
        assert "ambient" in env.mcp_servers
        ordinary, _, _, _ = await orch.build_worker_branch(
            env, agent_id="ordinary", role="ordinary"
        )
        assert (
            ordinary.chat_model.endpoint.config.kwargs["config_overrides"][
                "mcp_servers.ambient.command"
            ]
            == "echo"
        )
        if named_config:
            with pytest.raises(ConfigurationError, match="mcp: none.*--mcp-config"):
                await orch.build_worker_branch(env, agent_id="isolated", role="isolated")
        else:
            worker, _, _, _ = await orch.build_worker_branch(
                env, agent_id="isolated", role="isolated"
            )
            assert worker.metadata["mcp_launch"]["no_mcp_config_source"] == "profile"
            assert not any(
                key.startswith("mcp_servers.")
                for key in worker.chat_model.endpoint.config.kwargs.get("config_overrides", {})
            )

    asyncio.run(run())
