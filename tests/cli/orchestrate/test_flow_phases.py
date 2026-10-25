# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the extracted _run_flow_inner phase functions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate._common import _build_worker_operate_node, bare_worker_system
from lionagi.cli.orchestrate.flow import (
    _build_dag,
    _DagState,
    _deps_from_built_graph,
    _ExecResult,
    _execute_dag,
    _finalize_flow,
    _PlanResult,
    _synthesize,
)

# Shared stubs


def _make_env(
    tmp_path,
    *,
    bare=True,
    total_budget=None,
    budget_deadline_epoch=None,
    team_data=None,
    live_persist=None,
):
    """Minimal OrchestrationEnv stub for phase tests."""
    name_counts: dict = {}

    def assign_name(role: str) -> str:
        name_counts[role] = name_counts.get(role, 0) + 1
        n = name_counts[role]
        return f"{role}-{n}" if n > 1 else role

    def register_name(name: str) -> None:
        pass

    builder = _FakeBuilder()
    session = _FakeSession()
    expected_worker_ids: list[str] = []

    def expect_worker(agent_id: str) -> None:
        if agent_id not in expected_worker_ids:
            expected_worker_ids.append(agent_id)

    return SimpleNamespace(
        run=SimpleNamespace(
            artifact_root=tmp_path,
            dag_image_path=tmp_path / "dag.png",
            synthesis_path=tmp_path / "synthesis.md",
            agent_artifact_dir=lambda a: tmp_path / a,
        ),
        orc_branch=_FakeOrcBranch(),
        session=session,
        builder=builder,
        default_model_spec="codex/gpt-5.5",
        bare=bare,
        effort=None,
        total_budget=total_budget,
        budget_deadline_epoch=budget_deadline_epoch,
        team_data=team_data,
        pack=None,
        verbose=False,
        yolo=False,
        bypass=False,
        theme=None,
        fast=False,
        cwd=None,
        assign_name=assign_name,
        register_name=register_name,
        _live_persist=live_persist,
        _finalize_extras=None,
        worker_artifact_dirs={},
        expected_worker_ids=expected_worker_ids,
        expect_worker=expect_worker,
    )


class _FakeMsgs:
    """Enough of the `Branch.msgs` surface to observe a system-prompt rewrite."""

    def __init__(self, system_text: str):
        self.system_text = system_text

    @property
    def system(self):
        return SimpleNamespace(content=SimpleNamespace(system_message=self.system_text))

    def create_system(self, *, system):
        return system

    def set_system(self, system) -> None:
        self.system_text = system


class _FakeOrcBranch:
    def __init__(self, scripted=None):
        self.id = uuid4()
        self.name = "orchestrator"
        self.system = None
        self._scripted = list(scripted or [])
        self.calls: list = []
        self.chat_model = SimpleNamespace(
            endpoint=SimpleNamespace(config=SimpleNamespace(provider="codex", kwargs={}))
        )

    async def operate(self, **kw):
        self.calls.append(kw)
        if self._scripted:
            return self._scripted.pop(0)
        return SimpleNamespace(assignments=[])


class _FakeSession:
    def __init__(self):
        self.id = uuid4()
        self.branches: list = []
        self._observers: list = []

    def observe(self, signal_type, handler):
        self._observers.append((signal_type, handler))

    def include_branches(self, branch):
        self.branches.append(branch)

    async def flow(self, graph, verbose=False):
        return {"operation_results": {}}

    def to_dict(self, mode="python"):
        return {"id": str(self.id), "created_at": 0, "node_metadata": {}}


class _FakeBuilder:
    """Stub that keeps the real builder's edge semantics: an explicit list of
    dependencies makes exactly those edges, `None` chains onto the current
    heads. Callers that want a graph disagreeing with the plan can drive that
    through `add_operation` and read it back off `get_graph()`.
    """

    def __init__(self):
        self._nodes: list[str] = []
        self._ops: list[dict] = []
        self._mapping: dict[str, dict] = {}
        self._graph_nodes: dict[str, SimpleNamespace] = {}
        self._heads: list[str] = []
        self._edges = 0

    def add_operation(
        self, op_type, *, branch, depends_on=None, instruction="", context=None, **kwargs
    ):
        node_id = f"node-{len(self._nodes)}"
        self._nodes.append(node_id)
        self._mapping[node_id] = {"in": {}, "out": {}}
        self._graph_nodes[node_id] = SimpleNamespace(id=node_id, metadata={})
        heads = list(depends_on) if depends_on is not None else list(self._heads)
        for head_id in heads:
            if head_id in self._mapping:
                self._edges += 1
                edge_id = f"edge-{self._edges}"
                self._mapping[head_id]["out"][edge_id] = node_id
                self._mapping[node_id]["in"][edge_id] = head_id
        self._heads = [node_id]
        self._ops.append(
            {
                "id": node_id,
                "type": op_type,
                "depends_on": depends_on or [],
                "instruction": instruction,
            }
        )
        return node_id

    def get_graph(self):
        return SimpleNamespace(
            nodes=list(self._nodes),
            internal_nodes=dict(self._graph_nodes),
            node_edge_mapping={
                k: {"in": dict(v["in"]), "out": dict(v["out"])} for k, v in self._mapping.items()
            },
        )


class _FakeDB:
    """Minimal live-persist db stub — records update_session calls and
    round-trips node_metadata through get_session the way the real StateDB
    does (JSON-serialized on write, dict on read)."""

    def __init__(self, seed_node_metadata: dict | None = None):
        self.calls: list[tuple] = []
        self._node_metadata: dict = dict(seed_node_metadata or {})

    async def update_session(self, session_id, **kw):
        self.calls.append((session_id, kw))
        if "node_metadata" in kw:
            raw = kw["node_metadata"]
            self._node_metadata = json.loads(raw) if isinstance(raw, str) else dict(raw or {})

    async def get_session(self, session_id):
        return {"node_metadata": dict(self._node_metadata)}

    async def merge_session_node_metadata(self, session_id, patch):
        """Mirrors StateDB.merge_session_node_metadata's merge-patch result
        (RFC 7396: null deletes the key) without the real atomic SQL — this
        stub is single-threaded, so it isn't exercising the race itself, only
        the shape callers get back from it."""
        self.calls.append((session_id, {"merge_patch": patch}))
        merged = {**self._node_metadata, **patch}
        for k, v in patch.items():
            if v is None:
                merged.pop(k, None)
        self._node_metadata = merged


class _FakeBranch:
    def __init__(self, name="worker"):
        self.id = uuid4()
        self.name = name
        self.system = None
        self.chat_model = SimpleNamespace(
            endpoint=SimpleNamespace(config=SimpleNamespace(provider="codex", kwargs={}))
        )

    async def operate(self, **kw):
        return "ok"

    def to_dict(self, mode="python"):
        return {"id": str(self.id), "created_at": 0, "name": self.name}


# Tests for _build_dag


@pytest.mark.asyncio
async def test_build_dag_populates_node_ids(tmp_path):
    """_build_dag must produce one node_id per assignment in order."""
    env = _make_env(tmp_path)
    assignments = [
        TaskAssignment(task="research it", assignee="researcher"),
        TaskAssignment(task="write it", assignee="implementer", depends_on=["1"]),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher", "implementer"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(_FakeBranch("researcher"), "codex/gpt-5.5", None, False),
    ):
        dag_state = await _build_dag(
            env, "do stuff", plan_result, reactive_spec="off", max_spawn=20
        )

    assert len(dag_state.node_ids) == 2
    assert len(dag_state.worker_models) == 2
    assert dag_state.reactive is False
    assert dag_state.known_nodes == set(dag_state.node_ids)
    # The plan is also the run's statement of which workers it will have, kept
    # apart from the directories actually handed out so the two can disagree.
    # `build_worker_branch` is patched out here, so nothing registered a
    # directory — and the roster still names both workers.
    assert env.expected_worker_ids == ["researcher", "implementer"]
    assert env.worker_artifact_dirs == {}


@pytest.mark.asyncio
async def test_build_dag_forwards_assignment_inputs_to_worker_context(tmp_path):
    """Planner-declared inputs are part of the worker's execution context."""
    env = _make_env(tmp_path)
    assignment = TaskAssignment(
        task="review the implementation",
        assignee="reviewer",
        inputs=["requirements.md", "the implementation diff"],
    )
    plan_result = _PlanResult(
        assignments=[assignment],
        agent_ids=["reviewer"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch("reviewer"), "codex/gpt-5.5", None, False),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._build_worker_operate_node",
            wraps=_build_worker_operate_node,
        ) as build_node,
    ):
        await _build_dag(env, "ship safely", plan_result, reactive_spec="off", max_spawn=20)

    assert {"assignment_inputs": ["requirements.md", "the implementation diff"]} in (
        build_node.call_args.kwargs["context"]
    )


@pytest.mark.asyncio
async def test_build_dag_forwards_assignment_exit_criteria_to_worker_instruction(tmp_path):
    """The worker sees the planner's definition of done in its instruction."""
    env = _make_env(tmp_path)
    assignment = TaskAssignment(
        task="implement the fix",
        assignee="implementer",
        exit_criteria="The regression test and focused suite pass.",
    )
    plan_result = _PlanResult(
        assignments=[assignment],
        agent_ids=["implementer"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={0: "[BUDGET]\n"},
    )

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch("implementer"), "codex/gpt-5.5", None, False),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._build_worker_operate_node",
            wraps=_build_worker_operate_node,
        ) as build_node,
    ):
        await _build_dag(env, "ship safely", plan_result, reactive_spec="off", max_spawn=20)

    assert build_node.call_args.kwargs["instruction"] == (
        "[BUDGET]\nimplement the fix\n\n"
        "Exit criteria (must be satisfied before completion):\n"
        "The regression test and focused suite pass."
    )


@pytest.mark.asyncio
async def test_build_dag_early_graph_write_preserves_unrelated_metadata(tmp_path):
    """_build_dag's early-DAG-snapshot write must merge onto node_metadata,
    not replace it — a kill-sweep may have stamped unverifiable-pid evidence
    (unverifiable_since/unverifiable_count) on this session row before the
    flow got this far, and this write must not erase it."""
    env = _make_env(tmp_path)
    db = _FakeDB(seed_node_metadata={"unverifiable_since": 111.0, "unverifiable_count": 2})
    env._live_persist = {
        "db": db,
        "session_id": "sess-1",
        "identity_markers": {"pid": 4242, "pid_create_time": 1.5},
    }
    assignments = [TaskAssignment(task="research it", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(_FakeBranch("researcher"), "codex/gpt-5.5", None, False),
    ):
        await _build_dag(env, "do stuff", plan_result, reactive_spec="off", max_spawn=20)

    assert db._node_metadata["unverifiable_since"] == 111.0
    assert db._node_metadata["unverifiable_count"] == 2
    assert db._node_metadata["pid"] == 4242
    assert db._node_metadata.get("agents") is not None, (
        "the early-graph snapshot itself must still land"
    )


@pytest.mark.asyncio
async def test_build_dag_deps_by_node_format(tmp_path):
    """deps_by_node must map node ids to 1-based string dep indices."""
    env = _make_env(tmp_path)
    assignments = [
        TaskAssignment(task="a", assignee="researcher"),
        TaskAssignment(task="b", assignee="architect", depends_on=["1"]),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher", "architect"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(_FakeBranch(), "codex/gpt-5.5", None, False),
    ):
        dag_state = await _build_dag(env, "task", plan_result, reactive_spec="off", max_spawn=20)

    nid0, nid1 = dag_state.node_ids
    assert dag_state.deps_by_node[nid0] == []
    assert dag_state.deps_by_node[nid1] == ["1"]


@pytest.mark.asyncio
async def test_build_dag_deps_follow_the_built_graph_not_the_declared_plan(tmp_path):
    """The reported dependencies are the edges that exist, not the ones asked for.

    Four assignments wired so the declared plan and the built graph disagree in
    both directions: step 2 is built exactly as declared and must still come out
    right, step 3 loses a declared dependency, step 4 gains one no assignment
    ever declared. Re-deriving from the plan would give [], ["1"], ["1", "2"], []
    — three of the four wrong.
    """
    from lionagi.cli.orchestrate._common import _build_worker_operate_node as _real_build
    from lionagi.operations.builder import OperationGraphBuilder

    env = _make_env(tmp_path)
    env.builder = OperationGraphBuilder()

    assignments = [
        TaskAssignment(task="a", assignee="researcher"),
        TaskAssignment(task="b", assignee="architect", depends_on=["1"]),
        TaskAssignment(task="c", assignee="implementer", depends_on=["1", "2"]),
        TaskAssignment(task="d", assignee="critic"),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher", "architect", "implementer", "critic"],
        dep_indices=[[], [0], [0, 1], []],
        pool=[],
        budget_preambles={},
    )

    built: list[str] = []

    def _diverging_build(builder, **kwargs):
        step = len(built)
        deps = kwargs.pop("depends_on")
        if step == 2:
            deps = [built[1]]  # declared on steps 1 and 2; only 2 gets built
        elif step == 3:
            deps = [built[0]]  # declared nothing; built onto step 1 anyway
        node_id = _real_build(builder, depends_on=deps, **kwargs)
        built.append(node_id)
        return node_id

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch(), "codex/gpt-5.5", None, False),
        ),
        patch("lionagi.cli.orchestrate.flow._build_worker_operate_node", _diverging_build),
    ):
        dag_state = await _build_dag(env, "task", plan_result, reactive_spec="off", max_spawn=20)

    nid0, nid1, nid2, nid3 = dag_state.node_ids
    assert dag_state.deps_by_node[nid0] == []
    assert dag_state.deps_by_node[nid1] == ["1"]  # declared and built agree
    assert dag_state.deps_by_node[nid2] == ["2"]  # the plan says ["1", "2"]
    assert dag_state.deps_by_node[nid3] == ["1"]  # the plan says []

    # The Studio snapshot is rendered from the same reading, not a second one.
    assert [op["depends_on"] for op in env._finalize_extras["operations"]] == [
        [],
        ["1"],
        ["2"],
        ["1"],
    ]


def test_deps_from_built_graph_names_a_head_that_has_no_plan_ordinal():
    """A node injected after planning is named by its stamped spawn id."""
    builder = _FakeBuilder()
    planned = builder.add_operation("operate", branch=None, depends_on=[])
    injected = builder.add_operation("operate", branch=None, depends_on=[planned])
    child = builder.add_operation("operate", branch=None, depends_on=[injected])
    builder._graph_nodes[injected].metadata["spawn_id"] = "spawn-1"

    deps = _deps_from_built_graph(builder, {planned: "1"})

    assert deps[planned] == []
    assert deps[injected] == ["1"]
    assert deps[child] == ["spawn-1"]


def test_deps_from_built_graph_keeps_an_unnamed_head_rather_than_dropping_it():
    """No ordinal and no spawn id: report the node id, never an empty list."""
    builder = _FakeBuilder()
    unnamed = builder.add_operation("operate", branch=None, depends_on=[])
    child = builder.add_operation("operate", branch=None, depends_on=[unnamed])

    deps = _deps_from_built_graph(builder, {})

    assert deps[child] == [unnamed]


@pytest.mark.asyncio
async def test_build_dag_reactive_all_grants_spawn(tmp_path):
    """reactive_spec='all' sets reactive=True and spawn_roles=None."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(_FakeBranch(), "codex/gpt-5.5", None, False),
    ):
        dag_state = await _build_dag(env, "task", plan_result, reactive_spec="all", max_spawn=20)

    assert dag_state.reactive is True
    assert dag_state.spawn_roles is None


@pytest.mark.asyncio
async def test_build_dag_pool_override_passes_to_worker(tmp_path):
    """pool entries are forwarded as model_override for each worker in round-robin order."""
    env = _make_env(tmp_path)
    assignments = [
        TaskAssignment(task="a", assignee="researcher"),
        TaskAssignment(task="b", assignee="implementer"),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher", "implementer"],
        dep_indices=[[], []],
        pool=["codex/cheap", "codex/expensive"],
        budget_preambles={},
    )

    calls: list[dict] = []

    async def fake_build(env, *, agent_id, role, model_override=None, **kw):
        calls.append({"role": role, "model_override": model_override})
        return _FakeBranch(role), model_override or "default", None, False

    with patch("lionagi.cli.orchestrate.flow.build_worker_branch", side_effect=fake_build):
        await _build_dag(env, "task", plan_result, reactive_spec="off", max_spawn=20)

    assert calls[0]["model_override"] == "codex/cheap"
    assert calls[1]["model_override"] == "codex/expensive"


# Tests for _execute_dag


@pytest.mark.asyncio
async def test_execute_dag_collects_planned_results(tmp_path):
    """_execute_dag maps op_results back to agent_results in plan order."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    agent_ids = ["researcher"]
    worker_branch = _FakeBranch("researcher")
    env.session.include_branches(worker_branch)

    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=agent_ids,
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.session.signal import NodeCompleted, NodeFailed, NodeStarted
    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {"operation_results": {"node-0": "research output"}, "spawned_operations": 0}
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert len(exec_result.agent_results) == 1
    assert exec_result.agent_results[0]["response"] == "research output"
    assert exec_result.agent_results[0]["id"] == "researcher"
    assert exec_result.n_spawned == 0


@pytest.mark.asyncio
async def test_execute_dag_tags_spawned_nodes(tmp_path):
    """Reactively spawned nodes (not in known_nodes) get spawned=True in results."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {
                    "node-0": "planned result",
                    "node-spawn-1": "spawned result",
                },
                "spawned_operations": 1,
            }
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    planned = [r for r in exec_result.agent_results if not r.get("spawned")]
    spawned = [r for r in exec_result.agent_results if r.get("spawned")]
    assert len(planned) == 1
    assert len(spawned) == 1
    assert spawned[0]["id"] == "spawn-1"
    assert exec_result.n_spawned == 1


@pytest.mark.asyncio
async def test_execute_dag_reactive_wires_spawn_branch_setup_for_cli_workspace(tmp_path):
    """Reactive execution must pass a spawn_branch_setup callback into
    run_dag that retargets a CLI-backed spawned branch's writable workspace
    (chat_model.endpoint.config.kwargs['repo']) to that spawn's own artifact
    dir. Branch.clone() otherwise carries the emitting leg's repo forward
    unchanged — a sibling directory outside the spawned artifact contract —
    so without this seam a spawned CLI child can only write where its
    emitter can, not where its own artifact contract expects.

    Only the workspace move is CLI-specific. A non-CLI spawned branch has no
    `repo` kwarg to retarget, but it inherited the same prompt naming the
    emitter's directory and it belongs on the same end-of-run roster, so it
    must still be registered and have its prompt retargeted."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {"operation_results": {"node-0": "planned result"}, "spawned_operations": 0}
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    call_kwargs = fake_engine_run.run_dag.call_args.kwargs
    spawn_branch_setup = call_kwargs["spawn_branch_setup"]
    assert spawn_branch_setup is not None

    operation = SimpleNamespace(metadata={"spawn_id": "spawn-1"})
    # A spawned branch is a clone, and this one is not cloned from the orchestrator's.
    cli_branch = SimpleNamespace(
        metadata={"clone_from": object()},
        chat_model=SimpleNamespace(
            is_cli=True,
            endpoint=SimpleNamespace(
                config=SimpleNamespace(
                    provider="codex",
                    kwargs={
                        "contain_account_integrations": True,
                        "account_connectors": ["github"],
                    },
                )
            ),
        ),
    )
    spawn_branch_setup(operation, cli_branch)

    expected_dir = env.run.agent_artifact_dir("spawn-1")
    assert cli_branch.chat_model.endpoint.config.kwargs["repo"] == expected_dir
    assert "account_connectors" not in cli_branch.chat_model.endpoint.config.kwargs
    assert cli_branch.chat_model.endpoint.config.kwargs["contain_account_integrations"] is True
    assert expected_dir.exists()

    assert env.worker_artifact_dirs["spawn-1"] == expected_dir

    non_cli_op = SimpleNamespace(metadata={"spawn_id": "spawn-2"})
    non_cli_branch = SimpleNamespace(
        chat_model=SimpleNamespace(
            is_cli=False,
            endpoint=SimpleNamespace(config=SimpleNamespace(kwargs={})),
        ),
        msgs=_FakeMsgs(bare_worker_system(artifact_dir="/somewhere/else/emitter")),
    )
    spawn_branch_setup(non_cli_op, non_cli_branch)

    non_cli_dir = env.run.agent_artifact_dir("spawn-2")
    # No writable-root concept, so no workspace move …
    assert "repo" not in non_cli_branch.chat_model.endpoint.config.kwargs
    # … but it is on the roster and its prompt names its own directory.
    assert env.worker_artifact_dirs["spawn-2"] == non_cli_dir
    assert f"ARTIFACT DIRECTORY: {non_cli_dir}" in non_cli_branch.msgs.system_text
    assert "/somewhere/else/emitter" not in non_cli_branch.msgs.system_text
    assert "not an assigned working directory" in non_cli_branch.msgs.system_text
    assert "It is your working directory" not in non_cli_branch.msgs.system_text


@pytest.mark.asyncio
async def test_a_reactively_spawned_codex_node_runs_without_the_user_config(tmp_path, codex_home):
    """A spawned node is cloned from its emitter's branch, so its model starts
    with the emitter's CLI settings. The spawn hook isolates the clone the way a
    planned worker is isolated, and the emitter's own model keeps its settings."""
    spec, user_key, user_value = "codex/gpt-5", "profile", "work"
    flag_key, flag_value = "ignore_user_config", True
    from lionagi.cli._providers import build_imodel_from_spec
    from lionagi.session.branch import Branch
    from lionhub.orchestration.engines import PlanningEngine

    env = _make_env(tmp_path)
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=[spec],
    )
    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {"operation_results": {"node-0": "planned result"}, "spawned_operations": 0}
        )
    )
    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
    spawn_branch_setup = fake_engine_run.run_dag.call_args.kwargs["spawn_branch_setup"]

    imodel = build_imodel_from_spec(spec)
    imodel.endpoint.config.kwargs[user_key] = user_value
    emitter = Branch(chat_model=imodel)
    spawned = emitter.clone()

    spawn_branch_setup(SimpleNamespace(metadata={"spawn_id": "spawn-1"}), spawned)

    spawned_kwargs = spawned.chat_model.endpoint.config.kwargs
    assert spawned_kwargs[flag_key] == flag_value
    assert spawned_kwargs[user_key] is None
    assert spawned.parse_model is spawned.chat_model
    emitter_kwargs = emitter.chat_model.endpoint.config.kwargs
    assert emitter_kwargs[user_key] == user_value
    assert emitter_kwargs.get(flag_key) != flag_value


@pytest.mark.asyncio
async def test_a_spawn_cloned_from_the_orchestrator_drops_its_derived_effort(tmp_path, codex_home):
    """A codex orchestrator asked for no effort derives one from the user config.
    A node cloned from it no longer loads that config, so it runs at what a planned
    worker with no role effort gets; a clone of a worker keeps that worker's effort,
    and a run-wide effort the caller asked for is kept."""
    from lionagi.cli._providers import build_imodel_from_spec
    from lionagi.session.branch import Branch
    from lionhub.orchestration.engines import PlanningEngine

    (codex_home.path / "config.toml").write_text('model_reasoning_effort = "high"\n')
    spec = "codex/gpt-5.3-codex-spark"
    env = _make_env(tmp_path)
    env.orc_branch = Branch(chat_model=build_imodel_from_spec(spec))
    assert env.orc_branch.chat_model.endpoint.config.kwargs["reasoning_effort"] == "high"
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=[spec],
    )
    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {"operation_results": {"node-0": "planned result"}, "spawned_operations": 0}
        )
    )
    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
    spawn_branch_setup = fake_engine_run.run_dag.call_args.kwargs["spawn_branch_setup"]

    def _spawn(base, spawn_id):
        clone = base.clone()
        spawn_branch_setup(SimpleNamespace(metadata={"spawn_id": spawn_id}), clone)
        return clone.chat_model.endpoint.config.kwargs

    assert "reasoning_effort" not in _spawn(env.orc_branch, "spawn-1")
    assert env.orc_branch.chat_model.endpoint.config.kwargs["reasoning_effort"] == "high"

    worker = Branch(
        chat_model=build_imodel_from_spec(spec, effort_override="low", ignore_user_config=True)
    )
    assert _spawn(worker, "spawn-2")["reasoning_effort"] == "low"

    env.effort = "high"
    assert _spawn(env.orc_branch, "spawn-3")["reasoning_effort"] == "high"

    # A codex profile the orchestrator's spec names pins its effort as an override.
    # A planned worker built from that spec keeps it, and so does the clone.
    (codex_home.path / "work.config.toml").write_text(
        'model = "gpt-5.3-codex-spark"\nmodel_reasoning_effort = "medium"\n'
    )
    env.effort = None
    env.orc_branch = Branch(chat_model=build_imodel_from_spec("codex/work"))
    orc_kwargs = env.orc_branch.chat_model.endpoint.config.kwargs
    assert orc_kwargs["config_overrides"]["model_reasoning_effort"] == "medium"
    planned = build_imodel_from_spec("codex/work", ignore_user_config=True)
    assert planned.endpoint.config.kwargs["config_overrides"]["model_reasoning_effort"] == "medium"
    assert (
        _spawn(env.orc_branch, "spawn-4")["config_overrides"]["model_reasoning_effort"] == "medium"
    )
    assert orc_kwargs["config_overrides"]["model_reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_a_reactive_run_refuses_a_set_a_codex_orchestrator_clone_cannot_take(
    tmp_path, codex_home
):
    """A spawn routed to no role can be cloned from the orchestrator and isolated
    like a worker, and a codex worker refuses a server whose `env` or
    `http_headers` would need the profile isolation drops. The run is refused
    before any worker is built, as a planned codex worker's build refuses it,
    rather than failing when such a node spawns."""
    from lionagi._errors import ConfigurationError
    from lionagi.cli._providers import build_imodel_from_spec

    codex_home.write_config({})
    env = _make_env(tmp_path)
    env.orc_branch.chat_model = build_imodel_from_spec("codex/gpt-5")
    env.mcp_servers = {"search": {"command": "search-mcp", "env": {"SEARCH_TOKEN": "s3cret"}}}
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(_FakeBranch("researcher"), "claude_code/sonnet", None, False),
    ) as build:
        with pytest.raises(ConfigurationError, match="reactive spawn") as refused:
            await _build_dag(env, "do stuff", plan_result, reactive_spec="all", max_spawn=5)
        build.assert_not_called()
        assert "s3cret" not in str(refused.value)
        assert codex_home.profile_files() == []

        # No spawn, no clone: the same set builds with reactive spawning off.
        await _build_dag(env, "do stuff", plan_result, reactive_spec="off", max_spawn=5)
        # A set the clone can take builds with spawning on.
        env.mcp_servers = {"search": {"command": "search-mcp"}}
        await _build_dag(env, "do stuff", plan_result, reactive_spec="all", max_spawn=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "openai-codex"])
async def test_a_reactive_run_refuses_a_codex_orchestrator_profile_its_clone_cannot_take(
    tmp_path, codex_home, provider
):
    """A clone of the orchestrator ignores the user config like a worker and keeps
    the config overrides its model was built with. A profile the orchestrator's spec
    names that sets the sandbox or the approval policy is refused before any worker
    is built, as a planned worker's build refuses it."""
    from lionagi._errors import ConfigurationError
    from lionagi.cli._providers import build_imodel_from_spec

    codex_home.write_config({})
    (codex_home.path / "wide.config.toml").write_text(
        'model = "vendor/m"\nmodel_provider = "openrouter"\napproval_policy = "never"\n'
    )
    env = _make_env(tmp_path)
    env.default_model_spec = f"{provider}/wide"
    env.orc_branch.chat_model = build_imodel_from_spec(f"{provider}/wide")
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(_FakeBranch("researcher"), "claude_code/sonnet", None, False),
    ) as build:
        with pytest.raises(ConfigurationError, match="sets approval_policy besides") as refused:
            await _build_dag(env, "do stuff", plan_result, reactive_spec="all", max_spawn=5)
        assert "reactive spawn" in str(refused.value)
        build.assert_not_called()

        # No spawn, no clone: the same orchestrator builds with reactive spawning off.
        await _build_dag(env, "do stuff", plan_result, reactive_spec="off", max_spawn=5)
        # A profile that sets only its model and provider builds with spawning on.
        (codex_home.path / "wide.config.toml").write_text(
            'model = "vendor/m"\nmodel_provider = "openrouter"\n'
        )
        await _build_dag(env, "do stuff", plan_result, reactive_spec="all", max_spawn=5)
    assert build.call_count == 2


@pytest.mark.asyncio
async def test_execute_dag_escalated_spawned_node_evidence_uses_spawn_id(tmp_path):
    """An escalated node that was reactively spawned (not in the plan) must
    surface its role_node_builder-stamped spawn_id in the escalation
    evidence, not the internal Operation UUID — so a reviewer reading the
    teardown evidence sees the same 'spawn-N' label the artifact dirs and
    contract entries already use."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    escalated_node = SimpleNamespace(
        metadata={"assignee": "critic", "spawn_id": "spawn-7"}, branch_id=None
    )
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-escalated": escalated_node}
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {"node-0": "planned result"},
                "spawned_operations": 1,
                "escalated_operations": ["node-escalated"],
            }
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert env._escalated_evidence == [
        {"kind": "escalated_operation", "id": "spawn-7", "label": "spawn-7"}
    ]


@pytest.mark.asyncio
async def test_execute_dag_spawned_node_registers_artifact_contract(tmp_path):
    """A spawned node running under a role with artifact_defaults must be
    attributed back to that role and get its own contract entry folded into
    the live-persist context for post-run visibility, keeping the role's own
    required flag (decorate_instruction tells the spawned node its artifact
    dir + REQUIRED files before it runs, the same as a planned leg)."""
    env = _make_env(tmp_path)
    db = _FakeDB()
    env._live_persist = {
        "db": db,
        "session_id": "sess-1",
        "artifact_contract": None,
        "identity_markers": {},
    }
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
        role_artifact_defaults={
            "implementer": {"expected": [{"id": "report", "path": "report.md", "required": True}]}
        },
    )

    # The spawned node's graph entry carries the spawn_id + assignee
    # role_node_builder stamps on it at construction time (patterns.py) —
    # that's how a post-run surface recovers which role a reactively-injected
    # node ran under, and its stable correlation id.
    spawned_node = SimpleNamespace(
        metadata={"assignee": "implementer", "spawn_id": "spawn-1"}, branch_id=None
    )
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-spawn-1": spawned_node}
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {
                    "node-0": "planned result",
                    "node-spawn-1": "spawned result",
                },
                "spawned_operations": 1,
            }
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    spawned = next(r for r in exec_result.agent_results if r.get("spawned"))
    assert spawned["assignee"] == "implementer"
    assert spawned["id"] == "spawn-1"

    contract = env._live_persist["artifact_contract"]
    assert contract is not None
    ids = {e["id"] for e in contract["expected"]}
    assert "spawn-1__report" in ids
    paths = {e["path"] for e in contract["expected"]}
    assert "spawn-1/report.md" in paths
    # Stays required — the role default declares required=True and the
    # spawned node was told its artifact dir before it ran (decorate_instruction).
    spawned_entry = next(e for e in contract["expected"] if e["id"] == "spawn-1__report")
    assert spawned_entry["required"] is True


@pytest.mark.asyncio
async def test_execute_dag_spawned_node_without_role_defaults_no_contract(tmp_path):
    """A spawned node whose role declares no artifact_defaults must not
    fabricate a contract entry — only fires for a real per-role declaration."""
    env = _make_env(tmp_path)
    env._live_persist = {
        "db": _FakeDB(),
        "session_id": "sess-1",
        "artifact_contract": None,
        "identity_markers": {},
    }
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
        role_artifact_defaults={"implementer": None},
    )
    spawned_node = SimpleNamespace(
        metadata={"assignee": "implementer", "spawn_id": "spawn-1"}, branch_id=None
    )
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-spawn-1": spawned_node}
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {
                    "node-0": "planned result",
                    "node-spawn-1": "spawned result",
                },
                "spawned_operations": 1,
            }
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert env._live_persist["artifact_contract"] is None


@pytest.mark.asyncio
async def test_execute_dag_spawned_ids_independent_of_completion_order(tmp_path):
    """Two role-built spawned nodes must keep their builder-stamped spawn_id
    regardless of which one appears FIRST in op_results — completion order
    (dict insertion order here) must never override the stamped id. Reversing
    the dict's insertion order relative to construction order is exactly the
    scenario that let an unrelated node "steal" spawn-1 under the old
    completion-order-derived minting."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    # Node built SECOND (spawn-2) completes and is recorded FIRST; node built
    # FIRST (spawn-1) completes and is recorded SECOND.
    node_a = SimpleNamespace(
        metadata={"assignee": "implementer", "spawn_id": "spawn-1"}, branch_id=None
    )
    node_b = SimpleNamespace(
        metadata={"assignee": "researcher", "spawn_id": "spawn-2"}, branch_id=None
    )
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-b": node_b, "node-a": node_a}
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {
                    "node-0": "planned result",
                    # insertion order: node-b (spawn-2) BEFORE node-a (spawn-1)
                    "node-b": "b result",
                    "node-a": "a result",
                },
                "spawned_operations": 2,
            }
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    spawned_by_id = {r["id"]: r for r in exec_result.agent_results if r.get("spawned")}
    assert spawned_by_id["spawn-2"]["response"] == "b result"
    assert spawned_by_id["spawn-2"]["assignee"] == "researcher"
    assert spawned_by_id["spawn-1"]["response"] == "a result"
    assert spawned_by_id["spawn-1"]["assignee"] == "implementer"


@pytest.mark.asyncio
async def test_execute_dag_unstamped_node_fallback_skips_stamped_ids(tmp_path):
    """A node injected WITHOUT going through role_node_builder (no spawn_id
    in its metadata — e.g. an escalation child or a raw inject()) must fall
    back to a synthesized id, but that fallback must never collide with an
    id a role-built sibling already carries."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    # role-built sibling already owns "spawn-1"; the unstamped node has no
    # spawn_id and no assignee (a raw injected node carries neither).
    stamped_node = SimpleNamespace(
        metadata={"assignee": "researcher", "spawn_id": "spawn-1"}, branch_id=None
    )
    unstamped_node = SimpleNamespace(metadata={}, branch_id=None)
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[],
        internal_nodes={"node-stamped": stamped_node, "node-unstamped": unstamped_node},
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {
                    "node-0": "planned result",
                    "node-unstamped": "unstamped result",
                    "node-stamped": "stamped result",
                },
                "spawned_operations": 2,
            }
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    spawned_ids = {r["id"] for r in exec_result.agent_results if r.get("spawned")}
    # spawn-1 is already taken by the stamped node — the fallback must skip
    # it and mint spawn-2, never a second "spawn-1".
    assert spawned_ids == {"spawn-1", "spawn-2"}


@pytest.mark.asyncio
async def test_execute_dag_role_attributed_node_missing_spawn_id_fails_loud(tmp_path):
    """A node carrying a role assignee (the role_node_builder trail) but no
    spawn_id indicates the stamping invariant broke upstream — this must
    raise, not silently mint a fresh id that hides the defect."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    broken_node = SimpleNamespace(metadata={"assignee": "researcher"}, branch_id=None)
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-broken": broken_node}
    )

    from lionhub.orchestration.engines import PlanningEngine

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {
                    "node-0": "planned result",
                    "node-broken": "broken result",
                },
                "spawned_operations": 1,
            }
        )
    )

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        with pytest.raises(RuntimeError, match="no spawn_id"):
            await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)


async def test_execute_dag_drains_segment_metadata_write_before_returning(tmp_path):
    """Node-completion segment metadata is written by a fire-and-forget task.
    If _execute_dag returned before that write landed, a caller running
    finalization right after (as teardown does) could overwrite the
    session's whole node_metadata object without ever seeing the segment.
    The write must be drained -- awaited -- before _execute_dag returns."""
    import asyncio as _real_asyncio

    class _SlowFakeDB(_FakeDB):
        async def update_session(self, session_id, **kw):
            await _real_asyncio.sleep(0.05)
            await super().update_session(session_id, **kw)

        # The segment write goes through the atomic merge, so that is the call
        # the drain has to wait for. Without a slow path here the test would
        # pass whether or not _execute_dag drains anything.
        async def merge_session_node_metadata(self, session_id, patch):
            await _real_asyncio.sleep(0.05)
            await super().merge_session_node_metadata(session_id, patch)

    fake_db = _SlowFakeDB()
    env = _make_env(
        tmp_path,
        live_persist={"db": fake_db, "session_id": "sess-1", "identity_markers": {}},
    )
    env.session.include_branches(_FakeBranch("researcher"))

    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.session.signal import NodeCompleted, NodeStarted
    from lionhub.orchestration.engines import PlanningEngine

    async def _run_dag_and_emit_node_signals(*_args, **_kwargs):
        handlers = dict(env.session._observers)
        handlers[NodeStarted](NodeStarted(op_id="node-0", name="researcher"), None)
        handlers[NodeCompleted](NodeCompleted(op_id="node-0", name="researcher"), None)
        return {"operation_results": {"node-0": "output"}, "spawned_operations": 0}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _run_dag_and_emit_node_signals

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    # Accept either write shape: a merge patch carries the dict directly, a
    # whole-column write carries it JSON-serialized. What is being pinned is
    # that the segment write landed, not which call carried it.
    segment_writes = []
    for _sid, kw in fake_db.calls:
        if "merge_patch" in kw:
            payload = kw["merge_patch"]
        elif "node_metadata" in kw:
            raw = kw["node_metadata"]
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        else:
            continue
        if "segments" in payload:
            segment_writes.append(payload)
    assert segment_writes, "expected the node-completion segment write to have landed"
    assert segment_writes[-1]["segments"], "segment entry for the completed node was not recorded"


async def test_synthesis_drains_the_reused_observers_second_write_wave(tmp_path):
    """The execution drain is reusable after synthesis fires the same observers."""
    import asyncio as _real_asyncio

    events: list[str] = []

    class _SlowFakeDB(_FakeDB):
        async def update_branch(self, branch_id, **kw):
            await _real_asyncio.sleep(0.05)
            events.append(f"branch:{kw.get('status')}")

        async def merge_session_node_metadata(self, session_id, patch):
            await _real_asyncio.sleep(0.05)
            await super().merge_session_node_metadata(session_id, patch)
            if patch.get("segments"):
                events.append(f"segments:{patch['segments'][-1]['op_id']}")

    fake_db = _SlowFakeDB()
    env = _make_env(
        tmp_path,
        live_persist={"db": fake_db, "session_id": "sess-1", "identity_markers": {}},
    )
    worker = _FakeBranch("researcher")
    env.session.include_branches(worker)
    env.session.include_branches(env.orc_branch)
    worker_node = env.builder.add_operation("operate", branch=worker, depends_on=[])
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[worker_node],
        known_nodes={worker_node},
        deps_by_node={worker_node: []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.session.signal import NodeCompleted, NodeStarted
    from lionhub.orchestration.engines import PlanningEngine

    pass_number = 0

    async def _run_dag_and_emit_node_signals(graph, **_kwargs):
        nonlocal pass_number
        pass_number += 1
        handlers = dict(env.session._observers)
        if pass_number == 1:
            op_id, name, response = worker_node, "researcher", "worker output"
        else:
            op_id = next(node_id for node_id in graph.internal_nodes if node_id != worker_node)
            name, response = "orchestrator", "synthesis output"
        handlers[NodeStarted](NodeStarted(op_id=op_id, name=name), None)
        handlers[NodeCompleted](NodeCompleted(op_id=op_id, name=name), None)
        return {"operation_results": {op_id: response}, "spawned_operations": 0}

    fake_engine_run = MagicMock(run_dag=_run_dag_and_emit_node_signals)
    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
        )

    events.clear()
    result = await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    assert result is not None and result["response"] == "synthesis output"
    assert "branch:completed" in events
    assert "segments:node-1" in events


class _SlowStatusDB(_FakeDB):
    """A store whose writes take a set time: ``status_delay`` for branch status,
    ``metadata_delay`` for session metadata; 0 is immediate and ``None`` never returns.

    Each branch status that lands is recorded in ``landed``, and ``write_started`` is set when
    a delayed write begins. With ``ignores_cancel``, a write that never returns also absorbs
    every cancel and goes on waiting, until ``release`` is set. ``cancels`` counts the cancels
    the writes that never return receive, and ``cancels_by`` splits that count by write: branch
    ``status``, the control poller's ``marker`` and every other session ``metadata`` write.
    """

    def __init__(self):
        super().__init__()
        self.status_delay: float | None = 0.0
        self.metadata_delay: float | None = 0.0
        self.landed: list[str] = []
        self.write_started = asyncio.Event()
        self.ignores_cancel = False
        self.release = asyncio.Event()
        self.cancels = 0
        self.cancels_by = {"status": 0, "marker": 0, "metadata": 0}

    def _cancelled(self, kind):
        self.cancels += 1
        self.cancels_by[kind] += 1

    async def _wait(self, delay, kind):
        if delay == 0.0:
            return
        self.write_started.set()
        if delay is None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._cancelled(kind)
                if not self.ignores_cancel:
                    raise
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        self._cancelled(kind)
                raise
        await asyncio.sleep(delay)

    async def update_branch(self, branch_id, **kw):
        await self._wait(self.status_delay, "status")
        self.landed.append(kw["status"])

    async def merge_session_node_metadata(self, session_id, patch):
        kind = "marker" if set(patch) == {"flow_control_poller"} else "metadata"
        await self._wait(self.metadata_delay, kind)
        await super().merge_session_node_metadata(session_id, patch)


class _FakeExchange:
    """An exchange whose final collect raises *collect_raises* if given. With *hold*, its run
    loop goes on after stop() until ``release`` is set; ``lingering`` is set once stop() has
    been seen, and ``finished`` when the run loop ends, however it ends. With *hold_collect*,
    the final collect sets ``collecting`` and waits for ``collect_release``."""

    def __init__(self, *, hold=False, collect_raises=None, hold_collect=False):
        self.hold = hold
        self.collect_raises = collect_raises
        self.hold_collect = hold_collect
        self.stopped = asyncio.Event()
        self.lingering = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.collecting = asyncio.Event()
        self.collect_release = asyncio.Event()

    async def run(self, _interval):
        try:
            await self.stopped.wait()
            self.lingering.set()
            if self.hold:
                await self.release.wait()
        finally:
            self.finished.set()

    def stop(self):
        self.stopped.set()

    async def collect_all(self):
        if self.hold_collect:
            self.collecting.set()
            await self.collect_release.wait()
        if self.collect_raises is not None:
            raise self.collect_raises


class _SlowPollUnwindDB(_SlowStatusDB):
    """A store whose read of pending session controls never returns, and takes *unwind*
    seconds to unwind once cancelled. ``polling`` is set when the read begins and
    ``unwinding`` when its cancel arrives."""

    def __init__(self, unwind):
        super().__init__()
        self.unwind = unwind
        self.polling = asyncio.Event()
        self.unwinding = asyncio.Event()

    async def list_pending_session_controls(self, session_id):
        self.polling.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.unwinding.set()
            await asyncio.sleep(self.unwind)
            raise


def _watch_drain_bounds(monkeypatch):
    """Record each deadline the flow module sets with ``move_on_after``, in order, and return
    them with an event set at the first. The flow's own timeout sets one too, but these tests
    run the DAG without it, so the first is the drain's, set as it starts on its first writes."""
    import lionagi.cli.orchestrate.flow as flow_mod

    real_move_on_after = flow_mod.move_on_after
    bounds: list[float] = []
    started = asyncio.Event()

    def _watching_move_on_after(delay):
        bounds.append(delay)
        started.set()
        return real_move_on_after(delay)

    monkeypatch.setattr(flow_mod, "move_on_after", _watching_move_on_after)
    return bounds, started


async def _executed_with_live_observers(
    tmp_path, db, *, synthesis_raises=None, escalated=False, exchange=None, in_first_pass=None
):
    """Run the execution phase on *db* and return what `_synthesize` takes after it.

    The engine's second pass, the synthesis, emits its node's started and completed signals
    through the observers the execution phase installed, then raises *synthesis_raises* if
    given. With *escalated*, the first pass also completes an escalated node, which queues an
    escalation link. *exchange*, if given, is the run's message exchange, and *in_first_pass*,
    if given, is awaited with the engine's keyword arguments at the end of the first pass."""
    from lionagi.operations.node import create_operation
    from lionagi.session.signal import NodeCompleted, NodeStarted
    from lionhub.orchestration.engines import PlanningEngine

    env = _make_env(
        tmp_path,
        live_persist={"db": db, "session_id": "sess-1", "identity_markers": {}},
    )
    env.run.run_id = "run-1"
    if exchange is not None:
        env.exchange = exchange
    worker = _FakeBranch("researcher")
    env.session.include_branches(worker)
    env.session.include_branches(env.orc_branch)
    worker_node = env.builder.add_operation("operate", branch=worker, depends_on=[])
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[worker_node],
        known_nodes={worker_node},
        deps_by_node={worker_node: []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    passes: list = []

    async def run_dag(graph, **_kwargs):
        passes.append(graph)
        handlers = dict(env.session._observers)
        if len(passes) == 1:
            op_id, name, response = worker_node, "researcher", "worker output"
        else:
            op_id = next(node_id for node_id in graph.internal_nodes if node_id != worker_node)
            name, response = "orchestrator", "synthesis output"
        handlers[NodeStarted](NodeStarted(op_id=op_id, name=name), None)
        handlers[NodeCompleted](NodeCompleted(op_id=op_id, name=name), None)
        if len(passes) == 1 and escalated:
            node = create_operation("operate", parameters={})
            node.metadata["escalated_from"] = "parent-op-1"
            node._branch = SimpleNamespace(
                chat_model=SimpleNamespace(provider_session_id="cli-session-xyz")
            )
            _kwargs["on_op_complete"](node)
        if len(passes) == 1 and in_first_pass is not None:
            await in_first_pass(_kwargs)
        if len(passes) > 1 and synthesis_raises is not None:
            raise synthesis_raises
        return {"operation_results": {op_id: response}, "spawned_operations": 0}

    with patch.object(PlanningEngine, "new_run", return_value=MagicMock(run_dag=run_dag)):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
    return env, plan_result, dag_state, exec_result


@pytest.mark.asyncio
async def test_a_branch_status_write_that_never_returns_cannot_hold_the_synthesis_drain(
    tmp_path, monkeypatch, caplog
):
    """A status write gets as long as a store write may wait for the writer, then is cancelled.

    Waiting on it without a bound would keep `_synthesize` from returning and, because the
    drain is shielded, keep the flow's own timeout from firing."""
    import lionagi.cli.orchestrate.flow as flow_mod
    import lionagi.state.engine as engine_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 50)
    db = _SlowStatusDB()
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(tmp_path, db)
    db.status_delay = None

    task = asyncio.ensure_future(
        _synthesize(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=None,
            model_spec="codex/gpt-5.5",
        )
    )
    done, _ = await asyncio.wait({task}, timeout=3)

    assert task in done, "the synthesis drain is still waiting on a write that never returns"
    assert task.result()["response"] == "synthesis output"
    assert "cancelled 2 branch-status task(s) after timeout" in caplog.text


@pytest.mark.asyncio
async def test_a_cancel_during_the_synthesis_drain_waits_for_its_writes(tmp_path):
    """A cancel() that lands while the drain runs is raised once the writes have landed, so
    teardown never closes the store under a write still in flight."""
    db = _SlowStatusDB()
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(tmp_path, db)
    db.landed.clear()
    db.status_delay = 0.2

    task = asyncio.ensure_future(
        _synthesize(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=None,
            model_spec="codex/gpt-5.5",
        )
    )
    await db.write_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.landed == ["running", "completed"]


@pytest.mark.asyncio
async def test_a_synthesis_failure_outranks_a_cancel_that_lands_during_its_drain(tmp_path):
    """The exception the synthesis was already unwinding with is the one the caller gets, with
    the later cancel as its cause, and the writes still land first."""
    db = _SlowStatusDB()
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(
        tmp_path, db, synthesis_raises=RuntimeError("synthesis failed")
    )
    db.landed.clear()
    db.status_delay = 0.2

    task = asyncio.ensure_future(
        _synthesize(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=None,
            model_spec="codex/gpt-5.5",
        )
    )
    await db.write_started.wait()
    task.cancel()

    with pytest.raises(RuntimeError, match="synthesis failed") as raised:
        await task
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert db.landed == ["running", "completed"]


@pytest.mark.asyncio
async def test_a_timeout_during_the_synthesis_drain_lands_the_writes_before_it_fires(tmp_path):
    """A cancel scope that expires during the drain takes effect after the writes land."""
    from lionagi.ln.concurrency import move_on_after

    db = _SlowStatusDB()
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(tmp_path, db)
    db.landed.clear()
    db.status_delay = 0.2

    with move_on_after(0.05) as scope:
        await _synthesize(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=None,
            model_spec="codex/gpt-5.5",
        )

    assert scope.cancelled_caught
    assert db.landed == ["running", "completed"]


@pytest.mark.asyncio
async def test_the_synthesis_drain_does_not_report_writes_the_execution_drain_cancelled(
    tmp_path, monkeypatch, caplog
):
    """Each drain takes and clears its own snapshot, so a write the first drain gave up on is
    reported once, by that drain."""
    import lionagi.cli.orchestrate.flow as flow_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 0.05)
    db = _SlowStatusDB()
    db.metadata_delay = None
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(tmp_path, db)
    assert "segment-metadata task(s) after timeout" in caplog.text
    caplog.clear()
    db.metadata_delay = 0.0

    await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    assert "segment-metadata" not in caplog.text


@pytest.mark.asyncio
async def test_a_cancel_before_the_escalation_links_are_drained_gives_them_the_grace_period(
    tmp_path, monkeypatch
):
    """A cancel that lands while the drain still waits on status writes puts the escalation
    links, drained after them, under the grace period instead of their retry budget."""
    import time

    import lionagi.cli.orchestrate.flow as flow_mod
    import lionagi.state.engine as engine_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 500)
    db = _SlowStatusDB()
    db.status_delay = 0.5

    async def _link_that_never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    with patch("lionagi.state.claude_mirror.link_escalation_session", _link_that_never_returns):
        task = asyncio.ensure_future(_executed_with_live_observers(tmp_path, db, escalated=True))
        await db.write_started.wait()
        await asyncio.sleep(0.05)
        task.cancel()
        start = time.monotonic()
        done, _ = await asyncio.wait({task}, timeout=3)

    # The links' retry budget here is 4 x 1.0 s + 5 x 0.5 s + 0.05 s, about 6.5 s.
    assert task in done, "the escalation links were given their retry budget under a cancel"
    assert task.cancelled()
    assert time.monotonic() - start < 2


@pytest.mark.asyncio
async def test_a_cancel_while_the_escalation_links_are_drained_cuts_their_wait_to_the_grace(
    tmp_path, monkeypatch
):
    """Links already waiting out their retry budget when a cancel lands get the grace period
    from then on, not the rest of the budget."""
    import time

    import lionagi.cli.orchestrate.flow as flow_mod
    import lionagi.state.engine as engine_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 500)
    db = _SlowStatusDB()
    link_started = asyncio.Event()

    async def _link_that_never_returns(*_args, **_kwargs):
        link_started.set()
        await asyncio.Event().wait()

    with patch("lionagi.state.claude_mirror.link_escalation_session", _link_that_never_returns):
        task = asyncio.ensure_future(_executed_with_live_observers(tmp_path, db, escalated=True))
        await link_started.wait()
        # every other write is immediate, so by now the drain is waiting on the links
        await asyncio.sleep(0.2)
        task.cancel()
        start = time.monotonic()
        done, _ = await asyncio.wait({task}, timeout=3)

    # The links' retry budget here is 4 x 1.0 s + 5 x 0.5 s + 0.05 s, about 6.5 s.
    assert task in done, "the running escalation links kept their retry budget under a cancel"
    assert task.cancelled()
    assert time.monotonic() - start < 2


@pytest.mark.asyncio
async def test_a_status_write_that_ignores_every_cancel_is_abandoned_after_the_grace_period(
    tmp_path, monkeypatch, caplog
):
    """A write that absorbs each cancel the drain sends and goes on waiting is left behind, so
    the synthesis still returns."""
    import lionagi.cli.orchestrate.flow as flow_mod
    import lionagi.state.engine as engine_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 50)
    db = _SlowStatusDB()
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(tmp_path, db)
    db.status_delay = None
    db.ignores_cancel = True

    task = asyncio.ensure_future(
        _synthesize(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=None,
            model_spec="codex/gpt-5.5",
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done, "the synthesis drain is waiting on a write that ignores its cancel"
        assert task.result()["response"] == "synthesis output"
        assert "abandoned 2 branch-status task(s)" in caplog.text
    finally:
        db.release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_status_write_is_cancelled_once_and_then_given_the_grace_period(
    tmp_path, monkeypatch, caplog
):
    """Run data still being written at its budget gets one cancel, not another while it unwinds:
    a store write needs only one, unwinding once its statement returns, and a further one would
    only land in its cleanup. The grace period here is three re-cancel intervals, so a repeated
    cancel would show."""
    import lionagi.cli.orchestrate.flow as flow_mod
    import lionagi.state.engine as engine_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 3 * flow_mod._DRAIN_RECANCEL_SECONDS)
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 50)
    db = _SlowStatusDB()
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(tmp_path, db)
    db.status_delay = None
    db.ignores_cancel = True

    task = asyncio.ensure_future(
        _synthesize(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=None,
            model_spec="codex/gpt-5.5",
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done, "the synthesis drain is waiting on a write that ignores its cancel"
        assert db.cancels == 2, "each of the two status writes is cancelled exactly once"
        assert "abandoned 2 branch-status task(s)" in caplog.text
    finally:
        db.release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_metadata_write_that_ignores_every_cancel_is_abandoned_after_the_grace_period(
    tmp_path, monkeypatch, caplog
):
    """Segment metadata gets the grace period as its whole budget, then one cancel and the grace
    period again, and a write that absorbs the cancel is left behind. The grace period here is
    three re-cancel intervals, so a repeated cancel would show."""
    import lionagi.cli.orchestrate.flow as flow_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 3 * flow_mod._DRAIN_RECANCEL_SECONDS)
    db = _SlowStatusDB()
    db.metadata_delay = None
    db.ignores_cancel = True
    try:
        task = asyncio.ensure_future(_executed_with_live_observers(tmp_path, db))
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done, "the execution drain is waiting on a write that ignores its cancel"
        task.result()
        # the node's started and completed segments, each cancelled once
        assert "abandoned 2 segment-metadata task(s)" in caplog.text
        assert db.cancels_by["metadata"] == 2
        # the control poller's two marker writes stall on the same store and are each cancelled
        # once after their own budget
        assert db.cancels_by["marker"] == 2
        assert db.cancels == 4
    finally:
        db.release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_an_escalation_link_that_ignores_every_cancel_is_abandoned_after_the_grace_period(
    tmp_path, monkeypatch, caplog
):
    """A link past its retry budget is cancelled again every re-cancel interval of the grace
    period, and one that absorbs every cancel is left behind once the grace period ends."""
    import lionagi.cli.orchestrate.flow as flow_mod
    import lionagi.state.engine as engine_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 3.5 * flow_mod._DRAIN_RECANCEL_SECONDS)
    monkeypatch.setattr(flow_mod, "_ESCALATION_LINK_RETRY_INTERVAL", 0.01)
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 10)
    release = asyncio.Event()
    cancels = 0

    async def _link_that_ignores_every_cancel(*_args, **_kwargs):
        nonlocal cancels
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancels += 1
        return True

    try:
        with patch(
            "lionagi.state.claude_mirror.link_escalation_session", _link_that_ignores_every_cancel
        ):
            task = asyncio.ensure_future(
                _executed_with_live_observers(tmp_path, _SlowStatusDB(), escalated=True)
            )
            done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done, "the execution drain is waiting on a link that ignores its cancel"
        task.result()
        assert "abandoned 1 escalation-link task(s)" in caplog.text
        # one cancel at the budget, then one at each re-cancel interval inside the grace period
        assert cancels >= 2, "the link was cancelled only once"
    finally:
        release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_status_writes_queued_behind_a_link_on_the_write_lock_still_land(
    tmp_path, monkeypatch, caplog
):
    """Each store write still running when the drain starts may hold the store's write lock
    ahead of a status write, so the status writes get the store's writer wait once for each.

    Here an escalation link takes the lock first and holds it until the drain has started, so
    the link and both status writes are running then: the status writes get three writer waits
    and the grace period, and land behind the link."""
    import lionagi.cli.orchestrate.flow as flow_mod
    import lionagi.state.engine as engine_mod

    monkeypatch.setattr(flow_mod, "_DRAIN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 1000)
    bounds, drain_started = _watch_drain_bounds(monkeypatch)
    db = _SlowStatusDB()
    write_lock = asyncio.Lock()
    held: list[str] = []

    async def _update_branch(branch_id, **kw):
        await asyncio.sleep(0.01)  # the link, created after this write, takes the lock first
        async with write_lock:
            held.append(kw["status"])
        db.landed.append(kw["status"])

    async def _link(*_args, **_kwargs):
        async with write_lock:
            held.append("link")
            await asyncio.wait_for(drain_started.wait(), 5)
        return True

    db.update_branch = _update_branch
    with patch("lionagi.state.claude_mirror.link_escalation_session", _link):
        await _executed_with_live_observers(tmp_path, db, escalated=True)

    assert held == ["link", "running", "completed"]
    assert db.landed == ["running", "completed"]
    assert bounds[0] == pytest.approx(3 * 1.0 + 0.05)
    assert "branch-status" not in caplog.text


async def _fail_the_run(_engine_kwargs):
    raise RuntimeError("dag failed")


@pytest.mark.asyncio
async def test_a_dag_failure_outranks_a_cancel_while_the_exchange_winds_down(tmp_path):
    """The run's own failure is what the caller gets, with the later cancel as its cause, when
    the cancel lands while teardown waits for the exchange to stop."""
    db = _SlowStatusDB()
    exchange = _FakeExchange(hold=True)

    task = asyncio.ensure_future(
        _executed_with_live_observers(tmp_path, db, exchange=exchange, in_first_pass=_fail_the_run)
    )
    await exchange.lingering.wait()
    # As in the cancel-only case: the heartbeat and the poller stop at once, so the teardown is
    # waiting on the exchange alone by the time this timer fires.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done
        with pytest.raises(RuntimeError, match="dag failed") as raised:
            task.result()
        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        assert db.landed == ["running", "completed"]
    finally:
        exchange.release.set()
        await asyncio.wait_for(exchange.finished.wait(), 3)


@pytest.mark.asyncio
async def test_a_dag_failure_outranks_a_cancel_during_the_final_exchange_collect(tmp_path):
    """The same holds when the cancel lands in the exchange's final collect."""
    db = _SlowStatusDB()
    exchange = _FakeExchange(hold_collect=True)

    task = asyncio.ensure_future(
        _executed_with_live_observers(tmp_path, db, exchange=exchange, in_first_pass=_fail_the_run)
    )
    await exchange.collecting.wait()
    task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done
        with pytest.raises(RuntimeError, match="dag failed") as raised:
            task.result()
        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        assert db.landed == ["running", "completed"]
    finally:
        exchange.collect_release.set()


@pytest.mark.asyncio
async def test_a_failing_final_exchange_collect_does_not_skip_the_drain(tmp_path):
    """The run's writes are drained even when the exchange teardown before the drain raises,
    and the caller still gets that error."""
    db = _SlowStatusDB()
    db.status_delay = 0.2
    exchange = _FakeExchange(collect_raises=RuntimeError("collect failed"))

    with pytest.raises(RuntimeError, match="collect failed"):
        await _executed_with_live_observers(tmp_path, db, exchange=exchange)
    assert db.landed == ["running", "completed"]


@pytest.mark.asyncio
async def test_a_cancel_while_the_exchange_winds_down_is_not_swallowed(tmp_path):
    """A cancel of the run that lands while teardown waits for the exchange to stop reaches the
    caller, after the writes are drained, rather than letting the phase return normally."""
    db = _SlowStatusDB()
    exchange = _FakeExchange(hold=True)

    task = asyncio.ensure_future(_executed_with_live_observers(tmp_path, db, exchange=exchange))
    await exchange.lingering.wait()
    # The heartbeat and the control poller stop at once, so by the time this timer fires the
    # teardown is waiting on the exchange alone.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done
        assert task.cancelled()
        assert db.landed == ["running", "completed"]
    finally:
        exchange.release.set()
        await asyncio.wait_for(exchange.finished.wait(), 3)


@pytest.mark.asyncio
async def test_a_cancel_while_the_control_poller_winds_down_is_not_swallowed(tmp_path, monkeypatch):
    """A cancel of the run that lands while teardown waits for the control poller to stop
    reaches the caller, rather than being taken for the poller's own cancellation."""
    import lionagi.cli.orchestrate.flow as flow_mod

    monkeypatch.setattr(flow_mod, "_CONTROL_POLL_INTERVAL", 0.01)
    db = _SlowPollUnwindDB(unwind=0.5)

    async def _poller_reading(engine_kwargs):
        engine_kwargs["executor_ref"]["executor"] = SimpleNamespace(results={})
        await db.polling.wait()

    task = asyncio.ensure_future(
        _executed_with_live_observers(tmp_path, db, in_first_pass=_poller_reading)
    )
    await db.unwinding.wait()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=3)

    assert task in done
    assert task.cancelled()
    assert db.landed == ["running", "completed"]


@pytest.mark.asyncio
async def test_a_dag_failure_outranks_a_cancel_while_the_control_poller_winds_down(
    tmp_path, monkeypatch
):
    """The run's own failure is what the caller gets, with the later cancel as its cause, when
    the cancel lands while teardown waits for the heartbeat and the control poller to stop."""
    import lionagi.cli.orchestrate.flow as flow_mod

    monkeypatch.setattr(flow_mod, "_CONTROL_POLL_INTERVAL", 0.01)
    db = _SlowPollUnwindDB(unwind=0.5)

    async def _poller_reading_then_fail(engine_kwargs):
        engine_kwargs["executor_ref"]["executor"] = SimpleNamespace(results={})
        await db.polling.wait()
        raise RuntimeError("dag failed")

    task = asyncio.ensure_future(
        _executed_with_live_observers(tmp_path, db, in_first_pass=_poller_reading_then_fail)
    )
    await db.unwinding.wait()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=3)

    assert task in done
    with pytest.raises(RuntimeError, match="dag failed") as raised:
        task.result()
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert db.landed == ["running", "completed"]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Task.cancelling() is 3.11+")
@pytest.mark.asyncio
async def test_a_cancel_the_drain_holds_stays_pending_on_the_task(tmp_path):
    """When the synthesis's own failure is raised in place of a cancel that landed during its
    drain, the cancel request is left counted on the task, so asyncio still sees it."""
    db = _SlowStatusDB()
    env, plan_result, dag_state, exec_result = await _executed_with_live_observers(
        tmp_path, db, synthesis_raises=RuntimeError("synthesis failed")
    )
    db.status_delay = 0.2
    cancelling: list[int] = []

    async def _synthesis_then_count():
        try:
            await _synthesize(
                env,
                "task",
                plan_result,
                dag_state,
                exec_result,
                synthesis_model=None,
                model_spec="codex/gpt-5.5",
            )
        except RuntimeError:
            cancelling.append(asyncio.current_task().cancelling())

    task = asyncio.ensure_future(_synthesis_then_count())
    await db.write_started.wait()
    task.cancel()
    await asyncio.wait({task}, timeout=3)

    assert cancelling == [1]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Task.cancelling() is 3.11+")
@pytest.mark.asyncio
async def test_a_cancel_the_execution_drain_holds_stays_pending_on_the_task(tmp_path, monkeypatch):
    """The same holds for the execution phase's drain: the run's own failure is raised in place
    of the cancel, the writes land first, and the cancel request stays counted on the task."""
    _, drain_started = _watch_drain_bounds(monkeypatch)
    db = _SlowStatusDB()
    db.status_delay = 0.2
    cancelling: list[int] = []

    async def _run_then_count():
        try:
            await _executed_with_live_observers(tmp_path, db, in_first_pass=_fail_the_run)
        except RuntimeError as exc:
            assert isinstance(exc.__cause__, asyncio.CancelledError)
            cancelling.append(asyncio.current_task().cancelling())

    task = asyncio.ensure_future(_run_then_count())
    await asyncio.wait_for(drain_started.wait(), 5)
    task.cancel()
    await asyncio.wait({task}, timeout=3)

    assert cancelling == [1]
    assert db.landed == ["running", "completed"]


# Reactive spawn artifact enforcement through REAL teardown
# Replaces the old interim regression test that pinned spawned artifacts as
# permanently non-required (a spawned node used to have no way to learn its
# own artifact dir before running). decorate_instruction now tells it that
# dir before execution, so a role's required:True declaration is a real,
# enforceable gate for a reactively spawned node too — exercised here through
# the actual teardown path (start_live_persist / stop_live_persist / StateDB),
# not just the in-memory contract dict.


@pytest.fixture
def _flow_phase_state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["reviewer", "critic"])
@pytest.mark.parametrize(
    "outcome, file_content",
    [
        ("pass", "## Verdict\nAPPROVE\n"),
        ("missing", None),
        ("zero_byte", ""),
    ],
)
async def test_reactive_spawn_required_artifact_persistence_matrix(
    _flow_phase_state_db, tmp_path, monkeypatch, role, outcome, file_content
):
    """A spawned node running under reviewer/critic (both declare a required
    review.md) must flip the run to failed at teardown when that artifact is
    missing OR present-but-empty, and stay completed only when a real
    non-empty file is written — the actual enforcement the role declaration
    exists for, now that the spawned node is told its artifact dir up front."""
    from lionagi import Branch, Session
    from lionagi.casts.pattern import Role
    from lionagi.cli._runs import allocate_run
    from lionagi.cli.orchestrate._orchestration import (
        OrchestrationEnv,
        start_live_persist,
        stop_live_persist,
    )
    from lionagi.state.db import StateDB
    from lionhub.orchestration.engines import PlanningEngine

    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    # `save_dir` only moves the artifact root; the run's state dirs still land
    # under the module-level runs root, which is the user's real one. Redirect
    # it so the test allocates entirely inside tmp_path.
    monkeypatch.setattr("lionagi.cli._runs.RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(save_dir=str(tmp_path / "artifacts"))

    env = OrchestrationEnv(
        run=run,
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        orc_profile_name=None,
        default_model_spec="claude",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(run.artifact_root))

    assignments = [TaskAssignment(task="review it", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    role_defaults = Role.load(role).artifact_defaults
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
        role_artifact_defaults={role: role_defaults},
    )

    spawned_node = SimpleNamespace(
        metadata={"assignee": role, "spawn_id": "spawn-1"}, branch_id=None
    )
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-spawn-1": spawned_node}
    )

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {
                    "node-0": "planned result",
                    "node-spawn-1": "spawned result",
                },
                "spawned_operations": 1,
            }
        )
    )
    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    # Simulate the spawned worker itself writing (or not writing) the file it
    # was told about via decorate_instruction, at the exact path the contract
    # entry expects (namespaced under its own stamped spawn_id).
    if file_content is not None:
        artifact_path = run.agent_artifact_dir("spawn-1") / "review.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(file_content)

    session_id = env._live_persist["session_id"]
    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(session_id)
    assert s is not None
    if outcome == "pass":
        assert s["status"] == "completed"
    else:
        assert s["status"] == "failed"
        assert s["status_reason_code"] == "run.failed.missing_artifact"


@pytest.mark.asyncio
async def test_execute_dag_segment_writer_merges_into_real_statedb(
    _flow_phase_state_db, tmp_path, monkeypatch
):
    """The live segment writer (_on_node_started/_on_node_completed ->
    _record_segment -> _persist_segments), reached only through the real
    session signal bus that _execute_dag wires with
    `env.session.observe(NodeStarted/NodeCompleted, ...)`, must merge its
    write onto node_metadata a concurrent writer (here: a pre-existing
    kill-sweep unverifiable-pid marker) already put there -- not replace the
    column outright. A test that calls `_persist_node_metadata_patch`
    directly, as an isolated helper test does, proves the helper merges but
    not that these two callbacks are still wired to it; this one drives the
    actual NodeStarted/NodeCompleted signals through a real Session and reads
    the result back off a real StateDB."""
    import asyncio

    from lionagi import Branch, Session
    from lionagi.cli._runs import allocate_run
    from lionagi.cli.orchestrate._orchestration import (
        OrchestrationEnv,
        start_live_persist,
    )
    from lionagi.session.signal import NodeCompleted, NodeStarted
    from lionagi.state.db import StateDB
    from lionhub.orchestration.engines import PlanningEngine

    orc_branch = Branch(name="orchestrator")
    worker_branch = Branch(name="researcher")
    session = Session(default_branch=orc_branch)
    session.include_branches(worker_branch)
    monkeypatch.setattr("lionagi.cli._runs.RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(save_dir=str(tmp_path / "artifacts"))

    env = OrchestrationEnv(
        run=run,
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        orc_profile_name=None,
        default_model_spec="claude",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(run.artifact_root))
    session_id = env._live_persist["session_id"]

    # A prior concurrent writer (the stale-pid sweep) already stamped evidence
    # on this row before the flow got here.
    async with StateDB() as db:
        await db.merge_session_node_metadata(
            session_id, {"unverifiable_since": 111.0, "unverifiable_count": 2}
        )

    assignments = [TaskAssignment(task="research it", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    async def fake_run_dag(graph, **kwargs):
        # Real signals through the real session bus -- not a call into the
        # write helper, the actual lifecycle events _execute_dag observes.
        await env.session.emit(NodeStarted(op_id="node-0", name="researcher", elapsed=0.0))
        await env.session.emit(NodeCompleted(op_id="node-0", name="researcher", elapsed=1.2))
        return {"operation_results": {"node-0": "research output"}, "spawned_operations": 0}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = fake_run_dag
    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    # _persist_segments fires the DB write as a background task (fire-and-
    # forget from _record_segment); poll rather than assume it's landed the
    # instant _execute_dag returns.
    async def _read_meta():
        async with StateDB() as db:
            s = await db.get_session(session_id)
        return s["node_metadata"] if s else {}

    meta = {}
    for _ in range(100):
        meta = await _read_meta()
        if "segments" in meta:
            break
        await asyncio.sleep(0.02)

    assert meta.get("segments"), "the live segment writer never reached node_metadata"
    statuses = {seg["op_id"]: seg["status"] for seg in meta["segments"]}
    assert statuses.get("node-0") == "completed"
    assert meta.get("unverifiable_since") == 111.0, (
        "the segment write must merge onto the sweep's marker, not replace it"
    )
    assert meta.get("unverifiable_count") == 2


# Tests for _synthesize


@pytest.mark.asyncio
async def test_synthesize_returns_none_for_empty_results(tmp_path):
    """_synthesize must return None immediately when agent_results is empty."""
    env = _make_env(tmp_path)
    plan_result = _PlanResult(
        assignments=[],
        agent_ids=[],
        dep_indices=[],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[],
        known_nodes=set(),
        deps_by_node={},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    exec_result = _ExecResult(agent_results=[], n_spawned=0, t_exec_elapsed=0.1)

    result = await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_returns_dict_with_model_key(tmp_path):
    """_synthesize result dict must include 'model' and 'response' keys."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    drain_observer_writes = AsyncMock()
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "response": "findings",
            }
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
        engine_run=SimpleNamespace(
            run_dag=AsyncMock(return_value={"operation_results": {"node-0": "synthesized content"}})
        ),
        drain_observer_writes=drain_observer_writes,
    )

    result = await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    assert result is not None
    assert "model" in result
    assert "response" in result
    assert "time_ms" in result
    drain_observer_writes.assert_awaited_once_with(cancellation_in_flight=False, in_flight_exc=None)


@pytest.mark.asyncio
async def test_synthesize_drains_observer_writes_when_engine_fails(tmp_path):
    """A failed synthesis still has terminal observer writes to persist."""
    env = _make_env(tmp_path)
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    drain_observer_writes = AsyncMock()
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "response": "findings",
            }
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
        engine_run=SimpleNamespace(run_dag=AsyncMock(side_effect=RuntimeError("synth failed"))),
        drain_observer_writes=drain_observer_writes,
    )

    with pytest.raises(RuntimeError, match="synth failed"):
        await _synthesize(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=None,
            model_spec="codex/gpt-5.5",
        )

    drain_observer_writes.assert_awaited_once()
    kwargs = drain_observer_writes.await_args.kwargs
    assert kwargs["cancellation_in_flight"] is False
    assert isinstance(kwargs["in_flight_exc"], RuntimeError)


@pytest.mark.asyncio
async def test_synthesize_reuses_execution_engine_lifecycle_bridge(tmp_path):
    """Synthesis must use the same engine run that executed the worker DAG."""
    env = _make_env(tmp_path)
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-worker"],
        known_nodes={"node-worker"},
        deps_by_node={"node-worker": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "response": "findings",
            }
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
    )
    engine_run = SimpleNamespace(
        run_dag=AsyncMock(
            return_value={"operation_results": {"node-0": "synthesized through engine"}}
        )
    )
    exec_result.engine_run = engine_run
    env.session.flow = _make_flow_returning("node-0", "direct session result")

    result = await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    engine_run.run_dag.assert_awaited_once_with(
        env.builder.get_graph(),
        verbose=env.verbose,
        # The synthesis node is the only node in this graph, so there is no
        # earlier pass whose nodes have to be kept quiet.
        skip_signal_ops=set(),
    )
    assert result is not None
    assert result["response"] == "synthesized through engine"


@pytest.mark.asyncio
async def test_synthesize_includes_spawned_artifact_dir(tmp_path):
    """ARTIFACT CHAIN in the synthesis instruction must include a reactively
    spawned node's artifact dir, not just the plan-time agent_ids."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "response": "findings",
            },
            {
                "id": "spawn-1",
                "agent_id": "spawn-1",
                "name": "implementer",
                "response": "spawned output",
            },
        ],
        n_spawned=1,
        t_exec_elapsed=1.0,
        engine_run=SimpleNamespace(
            run_dag=AsyncMock(return_value={"operation_results": {"node-0": "synthesized content"}})
        ),
    )

    await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    instruction = env.builder._ops[-1]["instruction"]
    assert str(tmp_path / "spawn-1") in instruction
    assert str(tmp_path / "researcher") in instruction


# Tests for _finalize_flow


def test_finalize_flow_text_output(tmp_path):
    """_finalize_flow with output_format='text' must return a non-empty string."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "model": "codex/gpt-5.5",
                "depends_on": [],
                "spawned": False,
                "response": "great research",
                "time_ms": 100,
            }
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
    )

    with patch("lionagi.cli.orchestrate.flow.finalize_orchestration"):
        output = _finalize_flow(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            None,
            output_format="text",
            show_graph=False,
        )

    assert isinstance(output, str)
    assert len(output) > 0


def test_finalize_flow_json_output(tmp_path):
    """_finalize_flow with output_format='json' must return parseable JSON."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "model": "codex/gpt-5.5",
                "depends_on": [],
                "spawned": False,
                "response": "great research",
                "time_ms": 100,
            }
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
    )

    with patch("lionagi.cli.orchestrate.flow.finalize_orchestration"):
        output = _finalize_flow(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            None,
            output_format="json",
            show_graph=False,
        )

    parsed = json.loads(output)
    assert "results" in parsed or "agents" in parsed or isinstance(parsed, (list, dict))


def test_finalize_flow_writes_synthesis_artifact(tmp_path):
    """When synthesis_result is present, its response must be written to synthesis_path."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "model": "codex/gpt-5.5",
                "depends_on": [],
                "spawned": False,
                "response": "data",
                "time_ms": 100,
            }
        ],
        n_spawned=1,
        t_exec_elapsed=1.0,
    )
    synthesis_result = {
        "model": "codex/gpt-5.5",
        "response": "the synthesized answer",
        "time_ms": 500,
    }

    with patch("lionagi.cli.orchestrate.flow.finalize_orchestration"):
        _finalize_flow(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_result,
            output_format="text",
            show_graph=False,
        )

    assert env.run.synthesis_path.exists()
    assert env.run.synthesis_path.read_text() == "the synthesized answer"


def test_finalize_flow_agents_includes_spawned_node(tmp_path):
    """extras['agents'] must include an entry for a reactively spawned node —
    it otherwise resolves to nothing in extras['operations'], which is built
    from agent_results and already carries the spawned entry."""
    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "model": "codex/gpt-5.5",
                "depends_on": [],
                "spawned": False,
                "response": "data",
                "time_ms": 100,
            },
            {
                "id": "spawn-1",
                "agent_id": "spawn-1",
                "name": "implementer",
                "model": "codex/gpt-5.5",
                "assignee": "implementer",
                "depends_on": [],
                "spawned": True,
                "response": "more data",
                "time_ms": 100,
            },
        ],
        n_spawned=1,
        t_exec_elapsed=1.0,
    )

    captured: dict = {}

    def _fake_finalize(env, *, kind, prompt, extras=None):
        captured["extras"] = extras

    with patch("lionagi.cli.orchestrate.flow.finalize_orchestration", side_effect=_fake_finalize):
        _finalize_flow(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            None,
            output_format="text",
            show_graph=False,
        )

    agent_ids_seen = {a["id"] for a in captured["extras"]["agents"]}
    op_ids_seen = {o["id"] for o in captured["extras"]["operations"]}
    assert "spawn-1" in agent_ids_seen
    # Every operation id must resolve to an agent entry (the bug this guards
    # against: a spawned op appearing in "operations" with nothing matching
    # in "agents").
    assert op_ids_seen <= agent_ids_seen
    spawned_agent = next(a for a in captured["extras"]["agents"] if a["id"] == "spawn-1")
    assert spawned_agent["name"] == "implementer"
    assert spawned_agent["spawned"] is True


# Post-DAG finalize failures must not become DAG failures.


def test_finalize_flow_team_post_failure_still_returns_output_and_records_error(tmp_path):
    """A DAG that already produced its result must not lose that result because
    a post-completion step (posting to the team inbox) raised. `_finalize_flow`
    must still return the formatted output and stash the failure on
    `env._finalize_error` instead of letting it propagate — a caller that let
    this propagate used to have `_run_flow`'s `except BaseException` handler
    reclassify a clean DAG completion as `failed` (classify_exception maps any
    non-timeout/cancel exception to "failed")."""
    env = _make_env(tmp_path, team_data={"id": "team-x", "name": "team-x"})
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "model": "codex/gpt-5.5",
                "depends_on": [],
                "spawned": False,
                "response": "great research",
                "time_ms": 100,
            }
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
    )

    with (
        patch(
            "lionagi.cli.orchestrate.flow._post_results_to_team",
            side_effect=RuntimeError("team inbox lock timed out"),
        ),
        patch("lionagi.cli.orchestrate.flow.finalize_orchestration"),
    ):
        output = _finalize_flow(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            None,
            output_format="text",
            show_graph=False,
        )

    # The DAG's own result must survive the finalize-step failure.
    assert isinstance(output, str)
    assert len(output) > 0
    # The failure is recorded, not silently dropped, but on its own field.
    assert env._finalize_error is not None
    assert env._finalize_error["error_class"] == "RuntimeError"
    assert "team inbox lock timed out" in env._finalize_error["error"]


def test_finalize_flow_artifact_write_failure_is_split_from_finalize_error(tmp_path):
    """The synthesis artifact IS the run's output, not a best-effort finalize
    side effect — a failure writing it must land on its own field
    (`env._artifact_write_error`), distinct from `env._finalize_error`, and
    must not prevent the rest of teardown (team post, `finalize_orchestration`)
    from still running. Pre-split, this write shared the same try/except as
    the team-inbox post and would have landed on `env._finalize_error`
    instead — which the run's teardown treats as a mere hiccup that leaves
    status="completed", exactly the "exit 0 with no artifact" outcome this
    fix must prevent."""
    finalize_calls: list = []

    def _fake_finalize(env, *, kind, prompt, extras=None):
        finalize_calls.append(extras)

    env = _make_env(tmp_path, team_data={"id": "team-x", "name": "team-x"})
    bad_synthesis_path = MagicMock()
    bad_synthesis_path.write_text.side_effect = OSError("disk full")
    env.run.synthesis_path = bad_synthesis_path

    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "model": "codex/gpt-5.5",
                "depends_on": [],
                "spawned": False,
                "response": "great research",
                "time_ms": 100,
            }
        ],
        n_spawned=1,
        t_exec_elapsed=1.0,
    )
    synthesis_result = {
        "model": "codex/gpt-5.5",
        "response": "the synthesized answer",
        "time_ms": 500,
    }

    with (
        patch("lionagi.cli.orchestrate.flow._post_results_to_team") as fake_post,
        patch("lionagi.cli.orchestrate.flow.finalize_orchestration", side_effect=_fake_finalize),
    ):
        output = _finalize_flow(
            env,
            "task",
            plan_result,
            dag_state,
            exec_result,
            synthesis_result,
            output_format="text",
            show_graph=False,
        )

    assert isinstance(output, str)
    assert len(output) > 0

    # The failure is on its own field, not folded into env._finalize_error.
    assert env._artifact_write_error is not None
    assert env._artifact_write_error["error_class"] == "OSError"
    assert "disk full" in env._artifact_write_error["error"]
    assert getattr(env, "_finalize_error", None) is None

    # The guarded, non-output side effects still ran — an output failure
    # must not block best-effort teardown.
    fake_post.assert_called_once()
    assert len(finalize_calls) == 1


# Helpers


def _asyncio_coro(value):
    """Wrap a value as an awaitable coroutine for use in MagicMock side effects."""
    import asyncio

    async def _inner():
        return value

    return _inner()


def _make_flow_returning(node_id: str, response: str):
    """Return a session.flow coroutine that yields response for node_id."""

    async def _flow(graph, verbose=False):
        return {"operation_results": {node_id: response}}

    return _flow


@pytest.mark.asyncio
async def test_synthesize_keeps_already_executed_workers_out_of_the_signal_pass(tmp_path):
    """Synthesis re-runs the whole graph, because the executor resolves the new
    node's dependencies from it. The worker nodes already ran, so they are named
    as skipped: signalling them here would record their work a second time and a
    checkpointed resume rebuilt from those events would treat the replay as real.
    """
    env = _make_env(tmp_path)
    # Two workers that the execution phase already ran.
    worker_a = env.builder.add_operation("operate", branch=env.orc_branch, depends_on=[])
    worker_b = env.builder.add_operation("operate", branch=env.orc_branch, depends_on=[worker_a])

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[worker_a, worker_b],
        known_nodes={worker_a, worker_b},
        deps_by_node={worker_a: [], worker_b: [worker_a]},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {"id": "researcher", "agent_id": "researcher", "name": "researcher", "response": "f"}
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
    )
    exec_result.engine_run = SimpleNamespace(
        run_dag=AsyncMock(return_value={"operation_results": {}})
    )

    await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    kwargs = exec_result.engine_run.run_dag.await_args.kwargs
    skipped = kwargs["skip_signal_ops"]
    assert skipped == {worker_a, worker_b}, (
        f"the executed workers must be kept out of the synthesis signal pass: {skipped}"
    )
    # The synthesis node is the work this pass actually does, so it must not be
    # skipped -- suppressing it would make the run's own synthesis invisible.
    graph_ids = {str(n.id) for n in env.builder.get_graph().internal_nodes.values()}
    synth_ids = graph_ids - skipped
    assert len(synth_ids) == 1, f"exactly one unskipped (synthesis) node expected: {synth_ids}"


async def test_synthesize_declares_its_node_as_uncheckpointable(tmp_path):
    """The synthesis node must be named before the pass that runs it.

    The checkpoint observer built during execution routes by a node set fixed
    at that time, so it treats this later node as a reactive spawn. Declaring
    it is what keeps it out of the checkpoint; ordering matters because the
    observer fires while run_dag is running, not after it returns.
    """
    env = _make_env(tmp_path)
    worker = env.builder.add_operation("operate", branch=env.orc_branch, depends_on=[])

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="x", assignee="researcher")],
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[worker],
        known_nodes={worker},
        deps_by_node={worker: []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {"id": "researcher", "agent_id": "researcher", "name": "researcher", "response": "f"}
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
    )

    declared_when_called: set[str] = set()

    async def _capture_run_dag(*_args, **_kw):
        # Read the set as run_dag sees it: a declaration made after this
        # returns would be too late for the observer.
        declared_when_called.update(exec_result.checkpoint_skip_ids)
        return {"operation_results": {}}

    exec_result.engine_run = SimpleNamespace(run_dag=_capture_run_dag)

    await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    graph_ids = {str(n.id) for n in env.builder.get_graph().internal_nodes.values()}
    synth_ids = graph_ids - {str(worker)}
    assert len(synth_ids) == 1, f"expected exactly one synthesis node: {synth_ids}"
    synth_id = synth_ids.pop()

    assert synth_id in declared_when_called, (
        "the synthesis node must be declared uncheckpointable BEFORE run_dag, "
        f"since the observer fires during it. declared={declared_when_called}"
    )
    assert str(worker) not in declared_when_called, (
        "only the synthesis node belongs in the skip set; skipping the planned "
        f"worker would drop its checkpoint entry. declared={declared_when_called}"
    )
