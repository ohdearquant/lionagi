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
from lionagi.protocols.types import EventStatus
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


def _completed(session, node, response, name="worker"):
    """Leave a node the way a real run_dag leaves a finished one — terminal status
    included, so a later pass over the same graph does not re-execute it."""
    node.execution.status = EventStatus.COMPLETED
    node.execution.response = response
    return asyncio.create_task(
        session.emit(NodeCompleted(op_id=str(node.id), name=name, elapsed=0.01))
    )


def _failed(session, node, name="worker"):
    node.execution.status = EventStatus.FAILED
    return asyncio.create_task(
        session.emit(NodeFailed(op_id=str(node.id), name=name, elapsed=0.01))
    )


def _one_fails_one_completes(session):
    """A run_dag stub: on the worker pass the first leg fails and the second
    completes. Synthesis goes through run_dag as well, so a second call gets the
    graph with the synthesis node appended and completes that. ``passes`` records
    one entry per call, which is how a test tells the two phases apart."""
    passes: list = []

    async def run_dag(graph, **kwargs):
        passes.append(graph)
        nodes = list(graph.internal_nodes.values())
        if len(passes) == 1:
            emits = [
                _failed(session, nodes[0]),
                _completed(session, nodes[1], "worker 2 result"),
            ]
            await asyncio.gather(*emits, return_exceptions=True)
            return {"operation_results": {nodes[1].id: "worker 2 result"}}
        synth = nodes[-1]
        await asyncio.gather(
            _completed(session, synth, "synthesis result", name="synthesis"),
            return_exceptions=True,
        )
        return {"operation_results": {synth.id: "synthesis result"}}

    run_dag.passes = passes
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
    run_dag = _one_fails_one_completes(session)
    _wire_fanout(monkeypatch, env, assignments, run_dag)

    added_contexts: list = []
    original_add = env.builder.add_operation

    def capture_add(operation, **kwargs):
        added_contexts.append(kwargs.get("context"))
        return original_add(operation, **kwargs)

    monkeypatch.setattr(env.builder, "add_operation", capture_add)

    await fanout_module._run_fanout("codex/model", "work", num_workers=2, with_synthesis=True)

    # The synthesis operation is added last; its context must carry the real
    # result and not the failed leg's marker.
    synthesis_context = added_contexts[-1]
    assert synthesis_context == ["worker 2 result"]
    # Both phases execute through the engine, so synthesis is a second pass.
    assert len(run_dag.passes) == 2


async def test_all_workers_failed_skips_synthesis_and_says_why(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]

    passes: list = []

    async def run_dag(graph, **kwargs):
        passes.append(graph)
        emits = [_failed(session, node) for node in graph.internal_nodes.values()]
        await asyncio.gather(*emits, return_exceptions=True)
        return {"operation_results": {}}

    warnings: list[str] = []
    _wire_fanout(monkeypatch, env, assignments, run_dag, warnings=warnings)

    output, terminal_status = await fanout_module._run_fanout(
        "codex/model", "work", num_workers=2, with_synthesis=True
    )

    assert terminal_status == "failed"
    # Synthesis is skipped, so the worker pass is the only pass. Counting passes
    # is what makes this a real assertion: synthesis executes through the same
    # run_dag seam as the workers, so a second pass would mean it ran anyway.
    assert len(passes) == 1
    assert any("Every worker failed" in message for message in warnings)
    assert output.count(FAILED_WORKER_MARKER) == 2


async def test_a_failed_synthesis_is_marked_as_failed_not_as_empty(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env, run, session = _fanout_env(tmp_path)
    assignments = [
        TaskAssignment(task="first", assignee="worker"),
        TaskAssignment(task="second", assignee="worker"),
    ]

    # The synthesis pass runs the real engine, so the failure signal this test
    # reads is emitted by production code rather than injected here. Take the
    # run before `new_run` is stubbed out for the worker pass.
    real_run = PlanningEngine().new_run(session=session)
    passes: list = []

    async def run_dag(graph, **kwargs):
        passes.append(graph)
        if len(passes) > 1:
            return await real_run.run_dag(graph, **kwargs)
        operation_results = {}
        emits = []
        for number, node in enumerate(graph.internal_nodes.values(), start=1):
            response = f"worker {number} result"
            operation_results[node.id] = response
            emits.append(_completed(session, node, response))
        await asyncio.gather(*emits, return_exceptions=True)
        return {"operation_results": operation_results}

    _wire_fanout(monkeypatch, env, assignments, run_dag)

    # Fail the synthesis operation itself. Nothing here emits a signal: getting
    # that outcome to the observer is the job of the engine's signal bridge,
    # which is the thing under test.
    async def failing_operate(self, *args, **kwargs):
        raise RuntimeError("synthesis model unavailable")

    monkeypatch.setattr(Branch, "operate", failing_operate)

    output, terminal_status = await fanout_module._run_fanout(
        "codex/model", "work", num_workers=2, with_synthesis=True
    )

    # The workers all succeeded; only the synthesis leg failed. The run must
    # not read as completed, and the synthesis must not read as merely empty.
    assert terminal_status == "failed"
    assert FAILED_SYNTHESIS_MARKER in output
    assert "(no response)" not in output
    # And it got there through the engine, whose signal bridge is the only thing
    # that can carry a synthesis failure to the observer above.
    assert len(passes) == 2
