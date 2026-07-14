# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterization tests for the complete ``_run_flow_inner`` sequence."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate import flow as flow_module
from lionagi.cli.orchestrate.flow import FlowPlanError, _ExecResult, _run_flow_inner
from lionagi.engines import EngineRun, PlanningEngine
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.graph.graph import Graph
from lionagi.session.branch import Branch
from tests.cli.orchestrate.test_flow_phases import _make_env

_PLANNER_ROSTER = ["implementer", "researcher"]
_RETRY_GUIDANCE_SUFFIX = " Return ONLY the assignments list — do not perform the task."
_RUN_DAG_KEYWORDS = {
    "reactive",
    "spawn_type",
    "node_builder",
    "max_spawn",
    "max_concurrent",
    "verbose",
    "executor_ref",
    "context",
    "on_branch_created",
    "spawn_branch_setup",
    "on_op_complete",
}


class _Planner:
    def __init__(self, responses: list[object], *, expected_max_tasks: int = 0):
        self.responses = list(responses)
        self.expected_max_tasks = expected_max_tasks
        self.calls: list[dict] = []

    async def __call__(
        self,
        orchestrator,
        prompt: str,
        *,
        roles: list[str] | set[str],
        dag: bool = True,
        guidance: str = "",
        max_tasks: int = 0,
        context: dict | None = None,
    ) -> list[TaskAssignment]:
        call = {
            "orchestrator": orchestrator,
            "prompt": prompt,
            "roles": roles,
            "dag": dag,
            "guidance": guidance,
            "max_tasks": max_tasks,
            "context": context,
        }
        self.calls.append(call)
        assert roles == _PLANNER_ROSTER
        assert dag is True
        assert max_tasks == self.expected_max_tasks
        assert context is None
        if not self.responses:
            raise AssertionError("unexpected planner call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _PopulatedSession:
    def __init__(self, execution_responses: list[str], *, spawned_operations: int = 0):
        self.execution_responses = execution_responses
        self.spawned_operations = spawned_operations
        self.synthesis_response = "synthesized deliverable"
        self.branches: list[Branch] = []
        self.observers: list[tuple] = []
        self.flow_calls: list[tuple[Graph, dict]] = []
        self.run_dag_calls: list[tuple[Graph, dict]] = []

    def observe(self, signal_type, handler):
        self.observers.append((signal_type, handler))

    def include_branches(self, branch):
        self.branches.append(branch)

    async def flow(self, graph, verbose=False):
        assert isinstance(graph, Graph)
        self.flow_calls.append((graph, {"verbose": verbose}))
        node_ids = tuple(node.id for node in graph.internal_nodes)
        return {
            "operation_results": {node_ids[-1]: self.synthesis_response},
            "spawned_operations": 0,
        }


class _SessionBackedEngineRun:
    def __init__(self, session: _PopulatedSession):
        self.session = session

    async def run_dag(self, graph, **kwargs):
        inspect.signature(EngineRun.run_dag).bind(self, graph, **kwargs)
        assert isinstance(graph, Graph)
        assert set(kwargs) == _RUN_DAG_KEYWORDS
        self.session.run_dag_calls.append((graph, dict(kwargs)))
        node_ids = tuple(node.id for node in graph.internal_nodes)
        assert len(node_ids) == len(self.session.execution_responses)
        return {
            "operation_results": dict(zip(node_ids, self.session.execution_responses, strict=True)),
            "spawned_operations": self.session.spawned_operations,
        }


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch):
    async def fake_build_worker(env, *, agent_id, **kwargs):
        branch = Branch(name=agent_id)
        env.session.include_branches(branch)
        return branch, "fake/model", None, False

    def fake_new_run(self, *, session):
        return _SessionBackedEngineRun(session)

    finalize = MagicMock()
    monkeypatch.setattr(flow_module, "available_roles", lambda: list(_PLANNER_ROSTER))
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


def _env(tmp_path, execution_responses: list[str], *, spawned_operations: int = 0):
    env = _make_env(tmp_path)
    env.builder = OperationGraphBuilder()
    env.orc_branch = Branch(name="orchestrator")
    env.session = _PopulatedSession(execution_responses, spawned_operations=spawned_operations)
    env._name_counts = {}
    return env


def _assert_run_dag_contract(env, *, expected_max_concurrent: int = 2) -> None:
    assert len(env.session.run_dag_calls) == 1
    graph, kwargs = env.session.run_dag_calls[0]
    assert graph is env.builder.get_graph()
    on_branch_created = kwargs.pop("on_branch_created")
    assert callable(on_branch_created)
    assert kwargs == {
        "reactive": False,
        "spawn_type": None,
        "node_builder": None,
        "max_spawn": 20,
        "max_concurrent": expected_max_concurrent,
        "verbose": False,
        "executor_ref": {},
        "context": None,
        "spawn_branch_setup": None,
        "on_op_complete": None,
    }


@pytest.mark.asyncio
async def test_run_flow_inner_sequences_phases_and_propagates_results_to_synthesis(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    phase_order: list[str] = []
    planner = _Planner([_assignments()])
    original_build = flow_module._build_dag
    original_execute = flow_module._execute_dag
    original_synthesize = flow_module._synthesize
    original_finalize = flow_module._finalize_flow

    async def tracked_plan(
        orchestrator,
        prompt: str,
        *,
        roles: list[str] | set[str],
        dag: bool = True,
        guidance: str = "",
        max_tasks: int = 0,
        context: dict | None = None,
    ) -> list[TaskAssignment]:
        phase_order.append("plan")
        return await planner(
            orchestrator,
            prompt,
            roles=roles,
            dag=dag,
            guidance=guidance,
            max_tasks=max_tasks,
            context=context,
        )

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

    monkeypatch.setattr(flow_module, "plan", tracked_plan)
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
    graph = env.builder.get_graph()
    nodes = list(graph.internal_nodes)
    branches_by_id = {branch.id: branch for branch in env.session.branches}
    assert [branches_by_id[node.branch_id].name for node in nodes[:2]] == [
        "researcher",
        "implementer",
    ]
    assert graph.predecessor_ids(nodes[0]) == ()
    assert graph.predecessor_ids(nodes[1]) == (nodes[0].id,)
    assert nodes[-1].parameters["context"] == [
        "[researcher via researcher]: research output",
        "[implementer via implementer]: implementation output",
    ]
    _assert_run_dag_contract(env)
    assert len(env.session.flow_calls) == 1
    assert "research output" in output
    assert "implementation output" in output
    assert output.endswith("synthesized deliverable\n")
    fake_runtime.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_synthesis", "spawned_operations", "expected_synthesis"),
    [(True, 0, True), (False, 0, False), (False, 1, True)],
)
async def test_run_flow_inner_with_synthesis_controls_synthesis_gate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fake_runtime,
    with_synthesis: bool,
    spawned_operations: int,
    expected_synthesis: bool,
):
    planner = _Planner([_assignments()])
    monkeypatch.setattr(flow_module, "plan", planner)
    env = _env(
        tmp_path,
        ["research output", "implementation output"],
        spawned_operations=spawned_operations,
    )

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "characterize the flow",
        env=env,
        with_synthesis=with_synthesis,
        reactive_spec="off",
    )

    _assert_run_dag_contract(env)
    assert len(env.session.flow_calls) == int(expected_synthesis)
    assert ("synthesized deliverable" in output) is expected_synthesis
    assert len(env.builder.get_graph().internal_nodes) == (3 if expected_synthesis else 2)


@pytest.mark.asyncio
async def test_run_flow_inner_skips_synthesis_when_execution_has_no_agent_results(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = _Planner([_assignments()])

    async def empty_execute(
        env,
        plan_result,
        dag_state,
        *,
        max_concurrent: int,
        max_ops: int,
        checkpoint_prompt: str = "",
        checkpoint_plan: list[dict] | None = None,
        checkpoint_config: dict | None = None,
        checkpoint_ops_seed: dict[str, dict] | None = None,
        checkpoint_flow_context: dict | None = None,
        checkpoint_spawned_seed: list[dict] | None = None,
        team_max_rounds: int = 2,
    ) -> _ExecResult:
        return _ExecResult(agent_results=[], n_spawned=1, t_exec_elapsed=0.0)

    async def synthesis_must_be_skipped(
        env,
        prompt: str,
        plan_result,
        dag_state,
        exec_result,
        *,
        synthesis_model: str | None,
        model_spec: str,
    ):
        raise AssertionError("synthesis must be skipped without agent results")

    monkeypatch.setattr(flow_module, "plan", planner)
    monkeypatch.setattr(flow_module, "_execute_dag", empty_execute)
    monkeypatch.setattr(flow_module, "_synthesize", synthesis_must_be_skipped)
    env = _env(tmp_path, ["unused", "unused"])

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "characterize the flow",
        env=env,
        with_synthesis=True,
        reactive_spec="off",
    )

    assert env.session.run_dag_calls == []
    assert env.session.flow_calls == []
    assert "synthesized deliverable" not in output


@pytest.mark.asyncio
async def test_run_flow_inner_empty_plan_after_retry_raises_flow_plan_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = _Planner([[], []])
    monkeypatch.setattr(flow_module, "plan", planner)
    env = _env(tmp_path, [])

    with pytest.raises(FlowPlanError, match="no usable plan"):
        await _run_flow_inner("codex/gpt-5.5", "characterize the flow", env=env)

    assert len(planner.calls) == 2
    assert all(call["roles"] == _PLANNER_ROSTER for call in planner.calls)
    assert all(call["dag"] is True for call in planner.calls)
    assert all(call["max_tasks"] == 0 for call in planner.calls)
    assert planner.calls[1]["guidance"] == (planner.calls[0]["guidance"] + _RETRY_GUIDANCE_SUFFIX)
    assert env.session.run_dag_calls == []


@pytest.mark.asyncio
async def test_run_flow_inner_dry_run_renders_plan_without_execution(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = _Planner([_assignments()])
    monkeypatch.setattr(flow_module, "plan", planner)
    env = _env(tmp_path, ["unused", "unused"])

    output = await _run_flow_inner("codex/gpt-5.5", "characterize the flow", env=env, dry_run=True)

    assert output.startswith("Plan (2 assignments):")
    assert "2. [implementer] implement from the findings" in output
    assert "depends_on: 1" in output
    assert env.session.run_dag_calls == []
    assert len(env.builder.get_graph().internal_nodes) == 0
    fake_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_run_flow_inner_max_ops_caps_planning_request(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = _Planner([_assignments()], expected_max_tasks=2)
    monkeypatch.setattr(flow_module, "plan", planner)
    env = _env(tmp_path, ["unused", "unused"])

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "characterize the flow",
        env=env,
        max_ops=2,
        dry_run=True,
    )

    assert output.startswith("Plan (2 assignments):")
    assert len(planner.calls) == 1
    assert planner.calls[0]["roles"] == _PLANNER_ROSTER
    assert planner.calls[0]["dag"] is True
    assert planner.calls[0]["max_tasks"] == 2
    assert "BUDGET: at most 2 ops total" in planner.calls[0]["guidance"]
    assert env.session.run_dag_calls == []
    fake_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_run_flow_inner_resume_uses_persisted_plan_without_planning(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fake_runtime
):
    planner = _Planner([AssertionError("planner must be skipped")])
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

    assert planner.calls == []
    _assert_run_dag_contract(env)
    assert env.session.flow_calls == []
    graph = env.builder.get_graph()
    nodes = list(graph.internal_nodes)
    branches_by_id = {branch.id: branch for branch in env.session.branches}
    assert [branches_by_id[node.branch_id].name for node in nodes] == [
        "saved-researcher",
        "saved-implementer",
    ]
    assert graph.predecessor_ids(nodes[1]) == (nodes[0].id,)
    assert "resumed research" in output
    assert "resumed implementation" in output
