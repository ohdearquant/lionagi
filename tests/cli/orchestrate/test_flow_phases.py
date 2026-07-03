# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the extracted _run_flow_inner phase functions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate.flow import (
    _build_dag,
    _DagState,
    _ExecResult,
    _execute_dag,
    _finalize_flow,
    _PlanResult,
    _synthesize,
)

# ── Shared stubs ──────────────────────────────────────────────────────────────


def _make_env(tmp_path, *, bare=True, total_budget=None, team_data=None, live_persist=None):
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
    )


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
    def __init__(self):
        self._nodes: list[str] = []
        self._ops: list[dict] = []

    def add_operation(self, op_type, *, branch, depends_on=None, instruction="", context=None):
        node_id = f"node-{len(self._nodes)}"
        self._nodes.append(node_id)
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
        return SimpleNamespace(nodes=list(self._nodes))


class _FakeDB:
    """Minimal live-persist db stub — records update_session calls."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def update_session(self, session_id, **kw):
        self.calls.append((session_id, kw))


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


# ── Tests for _build_dag ──────────────────────────────────────────────────────


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
        return_value=(_FakeBranch("researcher"), "codex/gpt-5.5", None),
    ):
        dag_state = await _build_dag(env, "do stuff", plan_result, reactive_spec="off")

    assert len(dag_state.node_ids) == 2
    assert len(dag_state.worker_models) == 2
    assert dag_state.reactive is False
    assert dag_state.known_nodes == set(dag_state.node_ids)


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
        return_value=(_FakeBranch(), "codex/gpt-5.5", None),
    ):
        dag_state = await _build_dag(env, "task", plan_result, reactive_spec="off")

    nid0, nid1 = dag_state.node_ids
    assert dag_state.deps_by_node[nid0] == []
    assert dag_state.deps_by_node[nid1] == ["1"]


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
        return_value=(_FakeBranch(), "codex/gpt-5.5", None),
    ):
        dag_state = await _build_dag(env, "task", plan_result, reactive_spec="all")

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
        return _FakeBranch(role), model_override or "default", None

    with patch("lionagi.cli.orchestrate.flow.build_worker_branch", side_effect=fake_build):
        await _build_dag(env, "task", plan_result, reactive_spec="off")

    assert calls[0]["model_override"] == "codex/cheap"
    assert calls[1]["model_override"] == "codex/expensive"


# ── Tests for _execute_dag ────────────────────────────────────────────────────


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

    from lionagi.engines import PlanningEngine
    from lionagi.session.signal import NodeCompleted, NodeFailed, NodeStarted

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

    from lionagi.engines import PlanningEngine

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
async def test_execute_dag_spawned_node_registers_artifact_contract(tmp_path):
    """A spawned node running under a role with artifact_defaults must be
    attributed back to that role and get its own contract entry folded into
    the live-persist context for post-run visibility — folded non-required,
    since a spawned node is never told its artifact dir and so has no path to
    satisfy a required entry (enforcing one would fail an otherwise-complete
    run)."""
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

    # The spawned node's graph entry carries the assignee role_node_builder
    # stamped on it (patterns.py) — that's how a post-run surface recovers
    # which role a reactively-injected node ran under.
    spawned_node = SimpleNamespace(metadata={"assignee": "implementer"}, branch_id=None)
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-spawn-1": spawned_node}
    )

    from lionagi.engines import PlanningEngine

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

    contract = env._live_persist["artifact_contract"]
    assert contract is not None
    ids = {e["id"] for e in contract["expected"]}
    assert "spawn-1__report" in ids
    paths = {e["path"] for e in contract["expected"]}
    assert "spawn-1/report.md" in paths
    # Folded non-required even though the role default declares required=True —
    # a spawned node can't be held to an artifact it was never told to write.
    spawned_entry = next(e for e in contract["expected"] if e["id"] == "spawn-1__report")
    assert spawned_entry["required"] is False


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
    spawned_node = SimpleNamespace(metadata={"assignee": "implementer"}, branch_id=None)
    env.builder.get_graph = lambda: SimpleNamespace(
        nodes=[], internal_nodes={"node-spawn-1": spawned_node}
    )

    from lionagi.engines import PlanningEngine

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


# ── Tests for _synthesize ─────────────────────────────────────────────────────


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

    # session.flow returns a synthesis response.
    env.session.flow = _make_flow_returning("node-1", "synthesized content")

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
    )

    env.session.flow = _make_flow_returning("node-1", "synthesized content")

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


# ── Tests for _finalize_flow ──────────────────────────────────────────────────


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


# ── Helpers ───────────────────────────────────────────────────────────────────


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
