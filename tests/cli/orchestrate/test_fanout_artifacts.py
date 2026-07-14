# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fanout worker artifacts are durable before the whole phase completes."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from lionagi import Branch, Session
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.casts.emission import TaskAssignment
from lionagi.cli._runs import RunDir
from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.cli.orchestrate._orchestration import OrchestrationEnv
from lionagi.engines import PlanningEngine
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.signal import NodeCompleted


async def test_timeout_keeps_each_worker_artifact_completed_before_cancellation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    orchestrator = Branch(name="orchestrator")
    session = Session(default_branch=orchestrator)
    run = RunDir(
        run_id="fanout-run",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    run.ensure_artifact_root()
    env = OrchestrationEnv(
        run=run,
        session=session,
        orc_branch=orchestrator,
        builder=OperationGraphBuilder(),
        orc_profile=None,
        default_model_spec="codex/model",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]

    async def build_worker(env, *, agent_id, explicit_name, **kwargs):
        branch = Branch(name=explicit_name)
        env.session.include_branches(branch)
        return branch, "codex/model", None, False

    async def run_dag(graph, **kwargs):
        first_node = next(iter(graph.internal_nodes.values()))
        first_id = first_node.id
        first_node.execution.response = "first worker result"
        await session.emit(NodeCompleted(op_id=str(first_id), name="worker", elapsed=0.01))
        await asyncio.Event().wait()

    engine_run = type("EngineRunStub", (), {"run_dag": staticmethod(run_dag)})()
    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    monkeypatch.setattr(
        fanout_module,
        "stop_live_persist",
        AsyncMock(side_effect=lambda env, status: status),
    )
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=assignments))
    monkeypatch.setattr(fanout_module, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(fanout_module, "role_roster", lambda model: "worker")
    monkeypatch.setattr(fanout_module, "build_worker_branch", build_worker)
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, **kwargs: engine_run)

    with pytest.raises(LionTimeoutError):
        await fanout_module._run_fanout(
            "codex/model",
            "work",
            num_workers=2,
            timeout=0.5,
        )

    assert (run.artifact_root / "worker_1.md").read_text() == "first worker result"
    assert not (run.artifact_root / "worker_2.md").exists()
