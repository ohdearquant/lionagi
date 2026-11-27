# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A resumed flow runs the MCP servers its first run was named, not the file as it is now.

`li o flow --mcp-config <file>` copies the set it read into its run directory, and the
checkpoint points at that copy (ADR-0041 Amendment 8). A resume that names no MCP flag
replays the copy, so editing the named file after the first run changes nothing.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import lionhub.serve.cli.orchestrate._orchestration as orch_mod
from lionhub.orchestration import mcp_config
from lionhub.orchestration.flow import checkpoint as _checkpoint
from lionhub.orchestration.flow.checkpoint import (
    CheckpointWriter,
    FlowResumeError,
    load_checkpoint,
)
from lionhub.orchestration.runs import RunDir
from lionhub.serve.cli.orchestrate import flow

from .test_no_mcp_config_handoff import tmp_run  # noqa: F401


def _servers(name: str) -> dict:
    return {"mcpServers": {name: {"command": "echo", "args": [name]}}}


@pytest.fixture
def runs(tmp_run, tmp_path, monkeypatch):  # noqa: F811
    """Each run the flow allocates gets its own run directory, as a real resume does."""
    allocated: list[RunDir] = []

    def _allocate(save_dir=None, run_id=None):
        index = len(allocated)
        run = RunDir(
            run_id=f"run-{index}",
            state_root=tmp_path / "state" / f"run-{index}",
            artifact_root=tmp_path / "artifacts" / f"run-{index}",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        allocated.append(run)
        return run

    monkeypatch.setattr(orch_mod, "allocate_run", _allocate)
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    monkeypatch.setattr(flow, "start_live_persist", AsyncMock())
    monkeypatch.setattr(flow, "stop_live_persist", AsyncMock(return_value="completed"))
    monkeypatch.setattr(
        mcp_config, "discover_mcp_config", lambda _: pytest.fail("ambient discovery")
    )
    return SimpleNamespace(launch_dir=tmp_run, allocated=allocated)


def _record_runs(monkeypatch):
    """Stand in for the flow body: note the servers each run was handed, write its checkpoint."""
    seen: list[set[str]] = []

    async def execute(_model, prompt, **kwargs):
        env = kwargs["env"]
        seen.append(set(env.mcp_servers))
        writer = CheckpointWriter(
            path=env.run.checkpoint_path,
            session_id=str(env.session.id),
            prompt=prompt,
            plan=[],
            config=kwargs["checkpoint_config"],
        )
        await writer.flush()
        return "ok"

    monkeypatch.setattr(flow, "_run_flow_inner", execute)
    return seen


def _resume_from(monkeypatch, run: RunDir) -> None:
    monkeypatch.setattr(
        _checkpoint,
        "_find_run_dir_by_id",
        lambda _: SimpleNamespace(checkpoint_path=run.checkpoint_path, state_root=run.state_root),
    )


@pytest.mark.asyncio
async def test_resume_runs_the_first_runs_servers_after_the_named_file_changes(runs, monkeypatch):
    named = runs.launch_dir / "named.json"
    named.write_text(json.dumps(_servers("first")))
    seen = _record_runs(monkeypatch)

    await flow._run_flow("claude_code/sonnet", "work", mcp_config="named.json")
    named.write_text(json.dumps(_servers("second")))
    _resume_from(monkeypatch, runs.allocated[0])
    await flow._resume_flow("run-0")

    assert seen == [{"first"}, {"first"}]


@pytest.mark.asyncio
async def test_the_checkpoint_points_at_a_private_copy_and_keeps_the_named_path(runs, monkeypatch):
    named = runs.launch_dir / "named.json"
    named.write_text(json.dumps(_servers("first")))
    _record_runs(monkeypatch)

    await flow._run_flow("claude_code/sonnet", "work", mcp_config="named.json")
    first = runs.allocated[0]
    config = load_checkpoint(first.checkpoint_path)["config"]
    copy = first.state_root / "mcp-config.json"
    assert config["mcp_config"] == str(copy)
    assert config["mcp_config_origin"] == "explicit"
    assert config["mcp_config_named"] == str(named.resolve())
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert json.loads(copy.read_text()) == _servers("first")

    # A resume copies the set again into its own run directory and keeps the named path.
    named.write_text(json.dumps(_servers("second")))
    _resume_from(monkeypatch, first)
    await flow._resume_flow("run-0")
    second = runs.allocated[1]
    resumed = load_checkpoint(second.checkpoint_path)["config"]
    assert resumed["mcp_config"] == str(second.state_root / "mcp-config.json")
    assert resumed["mcp_config_named"] == str(named.resolve())
    assert resumed["mcp_config_origin"] == "explicit"
    assert json.loads(Path(resumed["mcp_config"]).read_text()) == _servers("first")


@pytest.mark.asyncio
async def test_the_copy_holds_the_servers_before_the_runs_actor_is_attributed(runs, monkeypatch):
    khive = {"mcpServers": {"khive": {"command": "/opt/bin/kkernel", "args": ["mcp"]}}}
    (runs.launch_dir / "named.json").write_text(json.dumps(khive))
    _record_runs(monkeypatch)

    await flow._run_flow("claude_code/sonnet", "work", mcp_config="named.json")

    copy = runs.allocated[0].state_root / "mcp-config.json"
    assert json.loads(copy.read_text()) == khive


@pytest.mark.asyncio
async def test_a_copy_that_is_gone_refuses_the_resume(runs, monkeypatch):
    named = runs.launch_dir / "named.json"
    named.write_text(json.dumps(_servers("first")))
    seen = _record_runs(monkeypatch)

    await flow._run_flow("claude_code/sonnet", "work", mcp_config="named.json")
    first = runs.allocated[0]
    (first.state_root / "mcp-config.json").unlink()
    _resume_from(monkeypatch, first)

    with pytest.raises(FlowResumeError, match=r"mcp-config\.json.*--mcp-config.*--no-mcp-config"):
        await flow._resume_flow("run-0")
    assert seen == [{"first"}]
    assert len(runs.allocated) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"mcp_config_named": "relative/named.json"},
        {"mcp_config_named": ""},
        {"mcp_config_named": None},
        {"mcp_config_named": ["/abs/named.json"]},
        {"mcp_config_origin": "submit_snapshot", "mcp_submit_snapshot": "/abs/snapshot.json"},
    ],
)
async def test_an_invalid_named_record_refuses_before_any_config_is_read(monkeypatch, override):
    config = {
        "mcp_config": "/abs/run/mcp-config.json",
        "mcp_config_origin": "explicit",
        "mcp_config_named": "/abs/named.json",
        "no_mcp_config": False,
        "mcp_launch_dir": "/abs",
        **override,
    }
    if override.get("mcp_config_origin") == "submit_snapshot":
        config["mcp_config"] = None
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, {"config": config}))
    )
    monkeypatch.setattr(mcp_config, "_read_config", lambda _: pytest.fail("config was read"))
    run = AsyncMock()
    monkeypatch.setattr(flow, "_run_flow", run)

    with pytest.raises(FlowResumeError, match="fresh run"):
        await flow._resume_flow("recorded-run")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_record_from_before_the_copy_reads_the_named_file_again(runs, monkeypatch):
    """An explicit record with no `mcp_config_named` keeps the old behaviour."""
    named = runs.launch_dir / "named.json"
    named.write_text(json.dumps(_servers("first")))
    seen = _record_runs(monkeypatch)

    await flow._run_flow("claude_code/sonnet", "work", mcp_config="named.json")
    first = runs.allocated[0]
    checkpoint = load_checkpoint(first.checkpoint_path)
    checkpoint["config"]["mcp_config"] = str(named.resolve())
    checkpoint["config"].pop("mcp_config_named", None)
    first.checkpoint_path.write_text(json.dumps(checkpoint))
    named.write_text(json.dumps(_servers("second")))
    _resume_from(monkeypatch, first)

    await flow._resume_flow("run-0")

    assert seen == [{"first"}, {"second"}]
    resumed = load_checkpoint(runs.allocated[1].checkpoint_path)["config"]
    assert resumed["mcp_config"] == str(named.resolve())
    assert "mcp_config_named" not in resumed
    assert not (runs.allocated[1].state_root / "mcp-config.json").exists()
