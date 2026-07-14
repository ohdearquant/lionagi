# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterization tests for the complete ``_run_flow_inner`` sequence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate import flow as flow_module
from lionagi.cli.orchestrate.flow import FlowPlanError, _run_flow_inner
from lionagi.engines import PlanningEngine
from tests.cli.orchestrate.test_flow_phases import _FakeBranch, _make_env


class _GraphNode:
    def __init__(self, node_id: str, branch: _FakeBranch):
        self.id = node_id
        self.branch_id = branch.id
        self.metadata: dict = {}
        self.execution = SimpleNamespace(status=None, response=None)


class _GraphBuilder:
    def __init__(self):
        self.nodes: list[str] = []
        self.internal_nodes: dict[str, _GraphNode] = {}
        self.operations: list[dict] = []

    def add_operation(
        self,
        op_type,
        *,
        branch,
        depends_on=None,
        instruction="",
        context=None,
        **kwargs,
    ):
        node_id = f"node-{len(self.nodes)}"
        self.nodes.append(node_id)
        self.internal_nodes[node_id] = _GraphNode(node_id, branch)
        self.operations.append(
            {
                "id": node_id,
                "type": op_type,
                "branch": branch,
                "depends_on": list(depends_on or []),
                "instruction": instruction,
                "context": context,
                **kwargs,
            }
        )
        return node_id

    def get_graph(self):
        return SimpleNamespace(nodes=list(self.nodes), internal_nodes=self.internal_nodes)


class _PopulatedSession:
    def __init__(self, execution_responses: list[str]):
        self.execution_responses = execution_responses
        self.synthesis_response = "synthesized deliverable"
        self.branches: list[_FakeBranch] = []
        self.observers: list[tuple] = []
        self.flow_calls: list[tuple[str, ...]] = []

    def observe(self, signal_type, handler):
        self.observers.append((signal_type, handler))

    def include_branches(self, branch):
        self.branches.append(branch)

    async def flow(self, graph, verbose=False):
        node_ids = tuple(graph.nodes)
        self.flow_calls.append(node_ids)
        if len(self.flow_calls) == 1:
            assert len(node_ids) == len(self.execution_responses)
            return {
                "operation_results": dict(zip(node_ids, self.execution_responses, strict=True)),
                "spawned_operations": 0,
            }
        return {
            "operation_results": {node_ids[-1]: self.synthesis_response},
            "spawned_operations": 0,
        }


class _SessionBackedEngineRun:
    def __init__(self, session: _PopulatedSession):
        self.session = session

    async def run_dag(self, graph, **kwargs):
        return await self.session.flow(graph, verbose=kwargs["verbose"])


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch):
    async def fake_build_worker(env, *, agent_id, **kwargs):
        branch = _FakeBranch(agent_id)
        env.session.include_branches(branch)
        return branch, "fake/model", None, False

    def fake_new_run(self, *, session):
        return _SessionBackedEngineRun(session)

    finalize = MagicMock()
    monkeypatch.setattr(flow_module, "build_worker_branch", fake_build_worker)
    monkeypatch.setattr(PlanningEngine, "new_run", fake_new_run)
    monkeypatch.setattr(flow_module, "finalize_orchestration", finalize)
    return finalize


def _assignments() -> list[TaskAssignment]:
    return [
        TaskAssignment(task="research the behavior", assignee="researcher"),
        TaskAssignment(
            task="implement from the findings",
            assignee="implementer",
            depends_on=["1"],
        ),
    ]


def _env(tmp_path, execution_responses: list[str]):
    env = _make_env(tmp_path)
    env.builder = _GraphBuilder()
    env.session = _PopulatedSession(execution_responses)
    env._name_counts = {}
    return env


@pytest.mark.asyncio
async def test_run_flow_inner_sequences_phases_and_propagates_results_to_synthesis(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    phase_order: list[str] = []
    original_build = flow_module._build_dag
    original_execute = flow_module._execute_dag
    original_synthesize = flow_module._synthesize
    original_finalize = flow_module._finalize_flow

    async def fake_plan(*args, **kwargs):
        phase_order.append("plan")
        return _assignments()

    async def tracked_build(*args, **kwargs):
        phase_order.append("build")
        return await original_build(*args, **kwargs)

    async def tracked_execute(*args, **kwargs):
        phase_order.append("execute")
        return await original_execute(*args, **kwargs)

    async def tracked_synthesize(*args, **kwargs):
        phase_order.append("synthesize")
        return await original_synthesize(*args, **kwargs)

    def tracked_finalize(*args, **kwargs):
        phase_order.append("finalize")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(flow_module, "plan", fake_plan)
    monkeypatch.setattr(flow_module, "_build_dag", tracked_build)
    monkeypatch.setattr(flow_module, "_execute_dag", tracked_execute)
    monkeypatch.setattr(flow_module, "_synthesize", tracked_synthesize)
    monkeypatch.setattr(flow_module, "_finalize_flow", tracked_finalize)
    env = _env(tmp_path, ["research output", "implementation output"])

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "characterize the flow",
        env=env,
        with_synthesis=True,
        reactive_spec="off",
    )

    assert phase_order == ["plan", "build", "execute", "synthesize", "finalize"]
    assert [op["branch"].name for op in env.builder.operations[:2]] == [
        "researcher",
        "implementer",
    ]
    assert env.builder.operations[0]["depends_on"] == []
    assert env.builder.operations[1]["depends_on"] == ["node-0"]
    assert env.builder.operations[-1]["context"] == [
        "[researcher via researcher]: research output",
        "[implementer via implementer]: implementation output",
    ]
    assert "research output" in output
    assert "implementation output" in output
    assert output.endswith("synthesized deliverable\n")
    fake_runtime.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_synthesis", "expected_flow_calls", "expected_synthesis"),
    [(True, 2, True), (False, 1, False)],
)
async def test_run_flow_inner_with_synthesis_controls_synthesis_gate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fake_runtime,
    with_synthesis: bool,
    expected_flow_calls: int,
    expected_synthesis: bool,
):
    planner = AsyncMock(return_value=_assignments())
    monkeypatch.setattr(flow_module, "plan", planner)
    env = _env(tmp_path, ["research output", "implementation output"])

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "characterize the flow",
        env=env,
        with_synthesis=with_synthesis,
        reactive_spec="off",
    )

    assert len(env.session.flow_calls) == expected_flow_calls
    assert ("synthesized deliverable" in output) is expected_synthesis
    assert len(env.builder.operations) == (3 if expected_synthesis else 2)


@pytest.mark.asyncio
async def test_run_flow_inner_empty_plan_after_retry_raises_flow_plan_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = AsyncMock(side_effect=[[], []])
    monkeypatch.setattr(flow_module, "plan", planner)
    env = _env(tmp_path, [])

    with pytest.raises(FlowPlanError, match="no usable plan"):
        await _run_flow_inner("codex/gpt-5.5", "characterize the flow", env=env)

    assert planner.await_count == 2
    assert env.session.flow_calls == []


@pytest.mark.asyncio
async def test_run_flow_inner_dry_run_renders_plan_without_execution(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = AsyncMock(return_value=_assignments())
    monkeypatch.setattr(flow_module, "plan", planner)
    env = _env(tmp_path, ["unused", "unused"])

    output = await _run_flow_inner("codex/gpt-5.5", "characterize the flow", env=env, dry_run=True)

    assert output.startswith("Plan (2 assignments):")
    assert "2. [implementer] implement from the findings" in output
    assert "depends_on: 1" in output
    assert env.session.flow_calls == []
    assert env.builder.operations == []
    fake_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_run_flow_inner_resume_uses_persisted_plan_without_planning(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = AsyncMock(side_effect=AssertionError("planner must be skipped"))
    monkeypatch.setattr(flow_module, "plan", planner)
    assignments = _assignments()
    checkpoint = {
        "plan": [
            {
                **assignments[0].model_dump(),
                "agent_id": "saved-researcher",
                "dep_indices": [],
            },
            {
                **assignments[1].model_dump(),
                "agent_id": "saved-implementer",
                "dep_indices": [0],
            },
        ],
        "ops": {},
    }
    env = _env(tmp_path, ["resumed research", "resumed implementation"])

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "characterize the flow",
        env=env,
        reactive_spec="off",
        resume_checkpoint=checkpoint,
    )

    planner.assert_not_awaited()
    assert len(env.session.flow_calls) == 1
    assert [op["branch"].name for op in env.builder.operations] == [
        "saved-researcher",
        "saved-implementer",
    ]
    assert env.builder.operations[1]["depends_on"] == ["node-0"]
    assert "resumed research" in output
    assert "resumed implementation" in output
