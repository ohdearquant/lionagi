# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fanout worker artifacts are durable before the whole phase completes."""

from __future__ import annotations

import asyncio
from pathlib import Path
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


def _fanout_env(tmp_path) -> tuple[OrchestrationEnv, RunDir, Session]:
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
    return env, run, session


async def test_timeout_keeps_each_worker_artifact_completed_before_cancellation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
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


async def test_worker_write_failure_is_recorded_without_losing_other_artifacts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]

    async def build_worker(env, *, explicit_name, **kwargs):
        branch = Branch(name=explicit_name)
        env.session.include_branches(branch)
        return branch, "codex/model", None, False

    async def run_dag(graph, **kwargs):
        operation_results = {}
        emits = []
        for number, node in enumerate(graph.internal_nodes.values(), start=1):
            response = f"worker {number} result"
            node.execution.response = response
            operation_results[node.id] = response
            emits.append(
                asyncio.create_task(
                    session.emit(NodeCompleted(op_id=str(node.id), name="worker", elapsed=0.01))
                )
            )
        await asyncio.gather(*emits, return_exceptions=True)
        return {"operation_results": operation_results}

    real_write_text = Path.write_text

    def fail_first_worker(path, data, *args, **kwargs):
        if path.name == "worker_1.md":
            raise OSError("disk full")
        return real_write_text(path, data, *args, **kwargs)

    warnings: list[str] = []
    progress_messages: list[str] = []
    engine_run = type("EngineRunStub", (), {"run_dag": staticmethod(run_dag)})()
    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    stop_persist = AsyncMock(side_effect=lambda env, status: status)
    monkeypatch.setattr(fanout_module, "stop_live_persist", stop_persist)
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=assignments))
    monkeypatch.setattr(fanout_module, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(fanout_module, "role_roster", lambda model: "worker")
    monkeypatch.setattr(fanout_module, "build_worker_branch", build_worker)
    monkeypatch.setattr(fanout_module, "finalize_orchestration", lambda *args, **kwargs: None)
    monkeypatch.setattr(fanout_module, "progress", progress_messages.append)
    monkeypatch.setattr(fanout_module, "warn", warnings.append, raising=False)
    monkeypatch.setattr(Path, "write_text", fail_first_worker)
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, **kwargs: engine_run)

    _, terminal_status = await fanout_module._run_fanout("codex/model", "work", num_workers=2)

    assert not (run.artifact_root / "worker_1.md").exists()
    assert (run.artifact_root / "worker_2.md").read_text() == "worker 2 result"
    assert any(message.startswith("Saved 1 worker results") for message in progress_messages)
    assert any("worker 1" in message and "worker_1.md" in message for message in warnings)
    assert terminal_status == "failed"
    stop_persist.assert_awaited_once_with(env, status="failed")
