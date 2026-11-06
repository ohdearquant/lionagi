"""A planned worker's profile MCP restriction survives checkpoint resume."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli._runs import RunDir
from lionagi.cli.orchestrate import _checkpoint, flow
from lionagi.cli.orchestrate import _orchestration as orch
from lionagi.cli.orchestrate._checkpoint import CheckpointWriter, load_checkpoint
from lionhub.orchestration import spawn as _providers


@pytest.mark.parametrize("initial,updated", [("none", None), (None, "none")])
async def test_planned_worker_profile_restriction_survives_failed_retry(
    tmp_path, monkeypatch, initial, updated
):
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)
    (launch / ".mcp.json").write_text(json.dumps({"mcpServers": {"example": {"command": "x"}}}))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def allocate(save_dir=None, run_id=None):
        run = RunDir(
            run_id="checkpoint-test",
            state_root=tmp_path / "state",
            artifact_root=tmp_path / "artifacts",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        return run

    monkeypatch.setattr(orch, "allocate_run", allocate)
    agents = launch / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    profile_path = agents / "worker.md"
    monkeypatch.setattr(_providers, "_plugin_agent_profiles", lambda: {})

    def write_profile(mcp):
        policy = "mcp: none\n" if mcp == "none" else ""
        profile_path.write_text(f"---\nmodel: codex/gpt-5\n{policy}---\nWorker.\n")

    write_profile(initial)

    async def make_env(*, mcp_config=None):
        return await orch.setup_orchestration(
            pattern_name="Flow",
            model_spec="claude_code/sonnet",
            agent_name=None,
            save_dir=None,
            cwd=None,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            no_mcp_config=False,
            mcp_config=mcp_config,
        )

    monkeypatch.setattr(
        flow, "plan", AsyncMock(return_value=[TaskAssignment(task="work", assignee="worker")])
    )
    monkeypatch.setattr(flow, "_persist_session_phase", AsyncMock())
    monkeypatch.setattr(flow, "_finalize_flow", lambda *a, **kw: "ok")
    captures = []

    async def execute(env, plan_result, dag, **kwargs):
        worker = dag.worker_branches[plan_result.agent_ids[0]]
        captures.append((worker, kwargs["checkpoint_plan"]))
        writer = CheckpointWriter(
            env.run.checkpoint_path,
            "session",
            kwargs["checkpoint_prompt"],
            kwargs["checkpoint_plan"],
            kwargs["checkpoint_config"],
        )
        await writer.record(plan_result.agent_ids[0], status="failed", response="failed")
        return flow._ExecResult(agent_results=[], n_spawned=0, t_exec_elapsed=0)

    monkeypatch.setattr(flow, "_execute_dag", execute)
    first_env = await make_env()
    await flow._run_flow_inner(
        "claude_code/sonnet",
        "work",
        env=first_env,
        reactive_spec="off",
        checkpoint_config={"no_mcp_config": False},
    )
    checkpoint = load_checkpoint(first_env.run.checkpoint_path)
    original = checkpoint["plan"][0]
    assert (original.get("profile_mcp_disabled") is True) is (initial == "none")
    if initial == "none":
        assert original["profile_mcp_source"] == "worker"

    write_profile(updated)
    second_env = await make_env()
    monkeypatch.setattr(_checkpoint, "_find_run_dir_by_id", lambda target: first_env.run)

    async def run_flow_proxy(*, prompt, resume_checkpoint, retry_failed, **kwargs):
        assert kwargs["no_mcp_config"] is False
        assert retry_failed is True
        output = await flow._run_flow_inner(
            "claude_code/sonnet",
            prompt,
            env=second_env,
            reactive_spec="off",
            checkpoint_config={"no_mcp_config": False},
            resume_checkpoint=resume_checkpoint,
            retry_failed=retry_failed,
        )
        return output, "completed"

    monkeypatch.setattr(flow, "_run_flow", run_flow_proxy)
    from lionagi.cli.orchestrate import (
        add_orchestrate_subparser,
        inject_playbook_schema_into_parser,
    )

    parser = argparse.ArgumentParser(prog="li")
    parsers = add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    argv = ["o", "flow", "--resume", "checkpoint-test", "--retry-failed"]
    inject_playbook_schema_into_parser(parsers["flow"], argv)
    args = parser.parse_args(argv)
    assert args.retry_failed is True
    assert await flow._resume_flow(args.resume, retry_failed=args.retry_failed) == (
        "ok",
        "completed",
    )
    worker, replayed_plan = captures[-1]
    assert worker.metadata["mcp_launch"]["no_mcp_config"] is True
    assert worker.metadata["mcp_launch"]["no_mcp_config_source"] == "profile"
    assert worker.chat_model.endpoint.config.kwargs["ignore_user_config"] is True
    assert not any(
        key.startswith("mcp_servers.")
        for key in worker.chat_model.endpoint.config.kwargs.get("config_overrides", {})
    )
    assert replayed_plan[0]["profile_mcp_disabled"] is True
    assert replayed_plan[0]["profile_mcp_source"] == "worker"
    assert load_checkpoint(second_env.run.checkpoint_path)["plan"] == replayed_plan

    if initial == "none":
        # The recorded source can differ from the currently resolved profile.
        checkpoint["plan"][0]["profile_mcp_source"] = "previous-worker"
        expected = r"Recorded.*profile 'previous-worker'.*mcp: none.*--mcp-config"
    else:
        expected = r"Agent profile 'worker' declares mcp: none.*--mcp-config"
    # Explicit config may override neither a recorded nor a live refusal.
    explicit = launch / "explicit.json"
    explicit.write_text(json.dumps({"mcpServers": {"example": {"command": "x"}}}))
    explicit_env = await make_env(mcp_config=str(explicit))
    with pytest.raises(orch.WorkerBuildError, match=expected):
        await flow._run_flow_inner(
            "claude_code/sonnet",
            "work",
            env=explicit_env,
            reactive_spec="off",
            checkpoint_config={},
            resume_checkpoint=checkpoint,
            retry_failed=True,
        )
    for invalid in (
        {"profile_mcp_disabled": "true", "profile_mcp_source": "worker"},
        {"profile_mcp_disabled": True},
        {"profile_mcp_source": "worker"},
    ):
        bad = dict(checkpoint)
        bad["plan"] = [{**original, **invalid}]
        if invalid == {"profile_mcp_disabled": True}:
            bad["plan"][0].pop("profile_mcp_source", None)
        if invalid == {"profile_mcp_source": "worker"}:
            bad["plan"][0].pop("profile_mcp_disabled", None)
        with pytest.raises(flow.FlowResumeError, match="invalid planned-worker MCP"):
            await flow._run_flow_inner(
                "claude_code/sonnet",
                "work",
                env=second_env,
                reactive_spec="off",
                checkpoint_config={},
                resume_checkpoint=bad,
                retry_failed=True,
            )
