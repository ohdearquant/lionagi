# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A fanout leg that fails is visible as a failure, everywhere it is read.

Fan-out is the partial-failure-across-N-parallel-legs pattern, so a failed leg
must be tellable apart from a quiet success at every surface that reads it: the
run's terminal status, the per-worker render, and the synthesis context. Before
this guard existed, the only signal feeding terminal status was an artifact
write error, and a failed leg rendered with the same placeholder as an empty
success — which a synthesis pass would then read as content.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from lionagi import Branch, Session
from lionagi.casts.emission import TaskAssignment
from lionagi.cli._runs import RunDir
from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.cli.orchestrate._orchestration import OrchestrationEnv
from lionagi.cli.orchestrate.fanout import (
    FAILED_SYNTHESIS_MARKER,
    FAILED_WORKER_MARKER,
)
from lionagi.engines import PlanningEngine
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.signal import NodeCompleted, NodeFailed


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
        orc_profile_name=None,
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


def _wire_fanout(monkeypatch, env, assignments, run_dag, warnings=None, progress=None):
    """Stub the orchestration seams around `_run_fanout` the way the artifact
    durability tests do, leaving the code under test — signal observation,
    result assembly, terminal-status resolution — real."""

    async def build_worker(env, *, explicit_name, **kwargs):
        branch = Branch(name=explicit_name)
        env.session.include_branches(branch)
        return branch, "codex/model", None, False

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
    if warnings is not None:
        monkeypatch.setattr(fanout_module, "warn", warnings.append, raising=False)
    if progress is not None:
        monkeypatch.setattr(fanout_module, "progress", progress.append)
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, **kwargs: engine_run)
    return stop_persist


def _one_fails_one_completes(session):
    """A run_dag stub: the first leg raises, the second completes normally."""

    async def run_dag(graph, **kwargs):
        nodes = list(graph.internal_nodes.values())
        operation_results = {}
        emits = [
            asyncio.create_task(
                session.emit(NodeFailed(op_id=str(nodes[0].id), name="worker", elapsed=0.01))
            )
        ]
        nodes[1].execution.response = "worker 2 result"
        operation_results[nodes[1].id] = "worker 2 result"
        emits.append(
            asyncio.create_task(
                session.emit(NodeCompleted(op_id=str(nodes[1].id), name="worker", elapsed=0.01))
            )
        )
        await asyncio.gather(*emits, return_exceptions=True)
        return {"operation_results": operation_results}

    return run_dag


async def test_a_failed_worker_flips_terminal_status_and_renders_its_marker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]
    warnings: list[str] = []
    stop_persist = _wire_fanout(
        monkeypatch, env, assignments, _one_fails_one_completes(session), warnings=warnings
    )

    output, terminal_status = await fanout_module._run_fanout("codex/model", "work", num_workers=2)

    # The failed leg is a failure at every surface: status, render, warning.
    assert terminal_status == "failed"
    stop_persist.assert_awaited_once_with(env, status="failed")
    assert FAILED_WORKER_MARKER in output
    assert "worker 2 result" in output
    assert "(no response)" not in output
    assert any("Worker 1 failed" in message for message in warnings)


async def test_a_failed_worker_is_excluded_from_the_synthesis_context(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]
    _wire_fanout(monkeypatch, env, assignments, _one_fails_one_completes(session))

    added_contexts: list = []
    original_add = env.builder.add_operation

    def capture_add(operation, **kwargs):
        added_contexts.append(kwargs.get("context"))
        return original_add(operation, **kwargs)

    monkeypatch.setattr(env.builder, "add_operation", capture_add)

    async def fake_flow(self, graph, **kwargs):
        return {"operation_results": {}}

    monkeypatch.setattr(Session, "flow", fake_flow)

    await fanout_module._run_fanout("codex/model", "work", num_workers=2, with_synthesis=True)

    # The synthesis operation is added last; its context must carry the real
    # result and not the failed leg's marker.
    synthesis_context = added_contexts[-1]
    assert synthesis_context == ["worker 2 result"]


async def test_all_workers_failed_skips_synthesis_and_says_why(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]

    async def run_dag(graph, **kwargs):
        emits = [
            asyncio.create_task(
                session.emit(NodeFailed(op_id=str(node.id), name="worker", elapsed=0.01))
            )
            for node in graph.internal_nodes.values()
        ]
        await asyncio.gather(*emits, return_exceptions=True)
        return {"operation_results": {}}

    warnings: list[str] = []
    _wire_fanout(monkeypatch, env, assignments, run_dag, warnings=warnings)

    synth_flow = AsyncMock()
    monkeypatch.setattr(Session, "flow", synth_flow)

    output, terminal_status = await fanout_module._run_fanout(
        "codex/model", "work", num_workers=2, with_synthesis=True
    )

    assert terminal_status == "failed"
    synth_flow.assert_not_awaited()
    assert any("no output to synthesize" in message for message in warnings)
    assert output.count(FAILED_WORKER_MARKER) == 2


async def test_a_failed_synthesis_is_marked_as_failed_not_as_empty(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]

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

    _wire_fanout(monkeypatch, env, assignments, run_dag)

    async def failing_synthesis_flow(self, graph, **kwargs):
        synthesis_node = list(graph.internal_nodes.values())[-1]
        await session.emit(NodeFailed(op_id=str(synthesis_node.id), name="synthesis", elapsed=0.01))
        return {"operation_results": {}}

    monkeypatch.setattr(Session, "flow", failing_synthesis_flow)

    output, terminal_status = await fanout_module._run_fanout(
        "codex/model", "work", num_workers=2, with_synthesis=True
    )

    # The workers all succeeded; only the synthesis leg failed. The run must
    # not read as completed, and the synthesis must not read as merely empty.
    assert terminal_status == "failed"
    assert FAILED_SYNTHESIS_MARKER in output
    assert "(no response)" not in output
