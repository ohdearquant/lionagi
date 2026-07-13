# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for checkpoint persistence and cross-process flow resume."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from lionagi import Branch, Session
from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate._checkpoint import (
    CheckpointWriter,
    FlowResumeError,
    load_checkpoint,
    resolve_checkpoint_target,
)
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    start_live_persist,
    stop_live_persist,
)
from lionagi.cli.orchestrate.flow import (
    _apply_checkpoint_precompletion,
    _build_dag,
    _DagState,
    _execute_dag,
    _PlanResult,
    _reconstruct_spawned_nodes,
    _run_flow_inner,
)
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.types import EventStatus
from lionagi.session.signal import NodeCompleted, NodeFailed
from lionagi.state.db import StateDB

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _minimal_real_env() -> OrchestrationEnv:
    """A real Session/Branch env (no provider setup) for start/stop_live_persist tests."""
    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    return OrchestrationEnv(
        run=MagicMock(),
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
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


class _FakeOrcBranch:
    def __init__(self):
        self.id = uuid4()
        self.name = "orchestrator"
        self.system = None
        self.chat_model = SimpleNamespace(
            endpoint=SimpleNamespace(config=SimpleNamespace(provider="codex", kwargs={}))
        )

    async def operate(self, **kw):
        return SimpleNamespace(assignments=[])


class _FakeSession:
    def __init__(self):
        self.id = uuid4()
        self.branches: list = []

    def observe(self, signal_type, handler):
        pass

    def include_branches(self, branch):
        self.branches.append(branch)


class _FakeBranch:
    def __init__(self, name="worker"):
        self.id = uuid4()
        self.name = name
        self.system = None
        self.chat_model = SimpleNamespace(
            endpoint=SimpleNamespace(config=SimpleNamespace(provider="codex", kwargs={}))
        )


class _FakeNode:
    def __init__(self, node_id: str):
        self.id = node_id
        self.metadata: dict = {}
        self.execution = SimpleNamespace(status=None, response=None)


class _FakeBuilder:
    """Builder whose get_graph() exposes internal_nodes, as _apply_checkpoint_precompletion needs."""

    def __init__(self):
        self._nodes: dict[str, _FakeNode] = {}
        self._counter = 0

    def add_operation(self, op_type, *, branch=None, depends_on=None, instruction="", context=None):
        node_id = f"node-{self._counter}"
        self._counter += 1
        self._nodes[node_id] = _FakeNode(node_id)
        return node_id

    def get_graph(self):
        return SimpleNamespace(internal_nodes=self._nodes)


def _make_resume_env(tmp_path: Path) -> SimpleNamespace:
    """Lightweight OrchestrationEnv stand-in for _run_flow_inner resume-path tests."""
    name_counts: dict[str, int] = {}

    def assign_name(role: str) -> str:
        name_counts[role] = name_counts.get(role, 0) + 1
        n = name_counts[role]
        return f"{role}-{n}" if n > 1 else role

    def register_name(name: str) -> None:
        pass

    return SimpleNamespace(
        run=SimpleNamespace(
            run_id="run-resume-test",
            artifact_root=tmp_path,
            dag_image_path=tmp_path / "dag.png",
            synthesis_path=tmp_path / "synthesis.md",
            agent_artifact_dir=lambda a: tmp_path / a,
        ),
        orc_branch=_FakeOrcBranch(),
        session=_FakeSession(),
        builder=_FakeBuilder(),
        default_model_spec="codex/gpt-5.5",
        bare=True,
        effort=None,
        total_budget=None,
        team_data=None,
        team_attach=None,
        pack=None,
        verbose=False,
        yolo=False,
        bypass=False,
        theme=None,
        fast=False,
        cwd=None,
        assign_name=assign_name,
        register_name=register_name,
        _name_counts=name_counts,
        _live_persist=None,
        _finalize_extras=None,
    )


def _asyncio_coro(value):
    async def _inner():
        return value

    return _inner()


# ── CheckpointWriter: atomic write + schema ──────────────────────────────────


async def test_checkpoint_writer_record_writes_valid_schema_no_leftover_tmp(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(
        path=path,
        session_id="sess-1",
        prompt="do the thing",
        plan=[
            {
                "task": "do it",
                "assignee": "worker",
                "agent_id": "worker",
                "dep_indices": [],
            }
        ],
        config={"model_spec": "claude"},
    )

    await writer.record("worker", status="completed", response="result-1")

    assert path.exists()
    assert not list(tmp_path.glob("checkpoint.*.tmp"))

    data = load_checkpoint(path)
    assert data["version"] == 2
    assert data["session_id"] == "sess-1"
    assert data["prompt"] == "do the thing"
    assert data["ops"]["worker"] == {
        "agent_id": "worker",
        "status": "completed",
        "response": "result-1",
    }
    assert data["plan"][0]["agent_id"] == "worker"


async def test_checkpoint_writer_second_record_preserves_prior_ops(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="sess-1", prompt="p", plan=[], config={})

    await writer.record("worker-1", status="completed", response="r1")
    await writer.record("worker-2", status="failed", response=None)

    data = load_checkpoint(path)
    assert set(data["ops"]) == {"worker-1", "worker-2"}
    assert data["ops"]["worker-1"]["status"] == "completed"
    assert data["ops"]["worker-2"]["status"] == "failed"
    assert data["ops"]["worker-2"]["response"] is None


async def test_checkpoint_writer_concurrent_records_stay_valid_and_lose_nothing(tmp_path: Path):
    """asyncio.Lock around serialize-write-rename must serialize concurrent
    record() calls — no torn file, no leftover temp files, every op lands."""
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    await asyncio.gather(
        *(writer.record(f"agent-{i}", status="completed", response=i) for i in range(20))
    )

    assert not list(tmp_path.glob("checkpoint.*.tmp"))
    data = load_checkpoint(path)
    assert len(data["ops"]) == 20
    assert writer._seq == 20


async def test_checkpoint_writer_flush_persists_without_touching_ops(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(
        path=path,
        session_id="s",
        prompt="p",
        plan=[],
        config={},
        ops={"seeded": {"agent_id": "seeded", "status": "completed", "response": "x"}},
    )

    await writer.flush()

    data = load_checkpoint(path)
    assert data["ops"] == {"seeded": {"agent_id": "seeded", "status": "completed", "response": "x"}}


async def test_checkpoint_writer_record_flow_context_updates_running_snapshot(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    await writer.record("a", status="completed", response="r1", flow_context={"k": 1})
    await writer.record("b", status="completed", response="r2")  # no flow_context this time
    await writer.record("c", status="completed", response="r3", flow_context={"k": 1, "j": 2})

    data = load_checkpoint(path)
    # The middle record (no flow_context passed) must not reset it to {} --
    # only an explicit non-None flow_context updates the running snapshot.
    assert data["flow_context"] == {"k": 1, "j": 2}


async def test_checkpoint_writer_record_spawned_keeps_separate_from_ops_and_dedups_by_node_id(
    tmp_path: Path,
):
    """Spawned nodes use a separate `spawned` keyspace from planned `ops` (a spawned branch can share a planned agent_id's name); re-recording the same node id updates in place, and CHECKPOINT_VERSION 2 reconstruction fields round-trip, defaulting to None when omitted."""
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    await writer.record("critic", status="completed", response="planned-result")
    await writer.record_spawned("spawned-1", status="completed", response="child-result-v1")
    await writer.record_spawned(
        "spawned-1",
        status="completed",
        response="child-result-v2",
        operation="operate",
        assignee="critic",
        instruction="critique the draft",
        parent_id="node-0",
        spawn_id="spawn-3",
    )

    data = load_checkpoint(path)
    assert data["ops"] == {
        "critic": {"agent_id": "critic", "status": "completed", "response": "planned-result"}
    }
    assert data["spawned"] == [
        {
            "node_id": "spawned-1",
            "status": "completed",
            "response": "child-result-v2",
            "operation": "operate",
            "assignee": "critic",
            "instruction": "critique the draft",
            "parent_id": "node-0",
            "spawn_id": "spawn-3",
        }
    ]


# ── _apply_checkpoint_precompletion ──────────────────────────────────────────


def test_apply_checkpoint_precompletion_marks_completed_ops_and_flags_degraded():
    nodes = {"n-researcher": _FakeNode("n-researcher"), "n-critic": _FakeNode("n-critic")}
    nodes["n-critic"].metadata["inherit_context"] = True
    env = SimpleNamespace(
        builder=SimpleNamespace(get_graph=lambda: SimpleNamespace(internal_nodes=nodes))
    )

    assignments = [
        TaskAssignment(task="research", assignee="researcher"),
        TaskAssignment(task="critique", assignee="critic", depends_on=["1"]),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher", "critic"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["n-researcher", "n-critic"],
        known_nodes={"n-researcher", "n-critic"},
        deps_by_node={},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    # "critic" has no checkpoint entry — still pending — and declares
    # inherit_context, so it must refuse loudly by default.
    checkpoint_ops = {
        "researcher": {"agent_id": "researcher", "status": "completed", "response": "findings"}
    }

    with pytest.raises(FlowResumeError, match="critic"):
        _apply_checkpoint_precompletion(
            env, plan_result, dag_state, checkpoint_ops, allow_degraded_context=False
        )

    # Marking is not transactional: researcher (iterated before critic) is
    # already marked even though the same call went on to raise.
    assert nodes["n-researcher"].execution.status == EventStatus.COMPLETED
    assert nodes["n-researcher"].execution.response == "findings"
    assert nodes["n-critic"].execution.status is None

    # allow_degraded_context permits the run to proceed; it does not
    # fake-complete the degraded op — that one still runs, against an
    # empty branch, exactly as documented.
    _apply_checkpoint_precompletion(
        env, plan_result, dag_state, checkpoint_ops, allow_degraded_context=True
    )
    assert nodes["n-critic"].execution.status is None


def test_apply_checkpoint_precompletion_no_degraded_ops_is_a_silent_noop():
    nodes = {"n-worker": _FakeNode("n-worker")}
    env = SimpleNamespace(
        builder=SimpleNamespace(get_graph=lambda: SimpleNamespace(internal_nodes=nodes))
    )
    assignments = [TaskAssignment(task="do it", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["n-worker"],
        known_nodes={"n-worker"},
        deps_by_node={},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    checkpoint_ops = {"worker": {"agent_id": "worker", "status": "completed", "response": "done"}}

    _apply_checkpoint_precompletion(
        env, plan_result, dag_state, checkpoint_ops, allow_degraded_context=False
    )

    assert nodes["n-worker"].execution.status == EventStatus.COMPLETED
    assert nodes["n-worker"].execution.response == "done"


def test_apply_checkpoint_precompletion_preserves_failed_ops_as_terminal_not_rerun():
    """A checkpointed 'failed' op is restored as terminal FAILED, not re-run -- it may have already produced side effects and resume must not guess at retry semantics."""
    nodes = {"n-worker": _FakeNode("n-worker")}
    env = SimpleNamespace(
        builder=SimpleNamespace(get_graph=lambda: SimpleNamespace(internal_nodes=nodes))
    )
    assignments = [TaskAssignment(task="do it", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["n-worker"],
        known_nodes={"n-worker"},
        deps_by_node={},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    checkpoint_ops = {
        "worker": {"agent_id": "worker", "status": "failed", "response": {"error": "boom"}}
    }

    _apply_checkpoint_precompletion(
        env, plan_result, dag_state, checkpoint_ops, allow_degraded_context=False
    )

    assert nodes["n-worker"].execution.status == EventStatus.FAILED
    assert nodes["n-worker"].execution.response == {"error": "boom"}


def test_apply_checkpoint_precompletion_refuses_when_spawned_entries_present():
    """Pre-CHECKPOINT_VERSION-2 `spawned` entries (no `operation` recorded) carry nothing to rebuild from, so resume refuses unconditionally before mutating any node -- contrast with the version-2-shaped entry below, which resumes cleanly."""
    nodes = {"n-worker": _FakeNode("n-worker")}
    env = SimpleNamespace(
        builder=SimpleNamespace(get_graph=lambda: SimpleNamespace(internal_nodes=nodes))
    )
    assignments = [TaskAssignment(task="do it", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["n-worker"],
        known_nodes={"n-worker"},
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    checkpoint_ops = {"worker": {"agent_id": "worker", "status": "completed", "response": "done"}}
    checkpoint_spawned = [{"node_id": "spawn-1", "status": "completed", "response": "child"}]

    with pytest.raises(FlowResumeError, match="spawn-1"):
        _apply_checkpoint_precompletion(
            env,
            plan_result,
            dag_state,
            checkpoint_ops,
            allow_degraded_context=False,
            checkpoint_spawned=checkpoint_spawned,
        )
    with pytest.raises(FlowResumeError, match="spawn-1"):
        _apply_checkpoint_precompletion(
            env,
            plan_result,
            dag_state,
            checkpoint_ops,
            allow_degraded_context=True,
            checkpoint_spawned=checkpoint_spawned,
        )
    assert nodes["n-worker"].execution.status is None


# ── _reconstruct_spawned_nodes: sound resume-after-partial-reactive-run ─────
#
# These use a REAL OperationGraphBuilder/Graph (not the dict-backed _FakeNode
# fixtures above) because reconstruction adds real Operation nodes and Edge
# objects into the graph -- something a plain dict can't stand in for.


def _real_planned_node(assignee: str = "worker") -> tuple[OperationGraphBuilder, UUID, Branch]:
    builder = OperationGraphBuilder()
    branch = Branch(name=assignee)
    node_id = builder.add_operation("operate", branch=branch, instruction="do it")
    return builder, node_id, branch


def _terminal_plan_and_ops(agent_id: str, node_id, status: str = "completed") -> tuple:
    """A minimal _PlanResult + checkpoint_ops pair recording *agent_id*'s
    planned node as checkpointed terminal -- what the soundness gate
    requires of a spawn's planned parent before accepting it."""
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee=agent_id)],
        agent_ids=[agent_id],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    checkpoint_ops = {agent_id: {"agent_id": agent_id, "status": status, "response": "done"}}
    return plan_result, checkpoint_ops


def test_reconstruct_spawned_nodes_precompletes_completed_child_with_edge_and_branch():
    """Core sound-replay case: a completed reactively-spawned node with its CHECKPOINT_VERSION 2 fields is rebuilt, wired to its parent by a 'spawn' edge, routed to the same role branch, and pre-completed so the executor's terminal short-circuit skips re-running it."""
    builder, parent_id, worker_branch = _real_planned_node("worker")
    env = SimpleNamespace(builder=builder)
    dag_state = _DagState(
        node_ids=[parent_id],
        known_nodes={parent_id},
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": worker_branch},
        worker_models=[],
    )
    plan_result, checkpoint_ops = _terminal_plan_and_ops("worker", parent_id)
    child_id = str(uuid4())
    checkpoint_spawned = [
        {
            "node_id": child_id,
            "status": "completed",
            "response": "child result",
            "operation": "operate",
            "assignee": "worker",
            "instruction": "follow-up task",
            "parent_id": str(parent_id),
            "spawn_id": "spawn-1",
        }
    ]

    _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

    graph = builder.get_graph()
    child = graph.internal_nodes[UUID(child_id)]
    assert child.execution.status == EventStatus.COMPLETED
    assert child.execution.response == "child result"
    assert child.branch_id == worker_branch.id
    assert child.metadata["spawn_id"] == "spawn-1"
    assert child.metadata["reference_id"] == "spawn-1"
    incoming_heads = list(graph.node_edge_mapping[UUID(child_id)]["in"].values())
    assert parent_id in incoming_heads


def test_reconstruct_spawned_nodes_marks_failed_child_terminal_not_rerun():
    builder, parent_id, worker_branch = _real_planned_node("worker")
    env = SimpleNamespace(builder=builder)
    dag_state = _DagState(
        node_ids=[parent_id],
        known_nodes={parent_id},
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": worker_branch},
        worker_models=[],
    )
    plan_result, checkpoint_ops = _terminal_plan_and_ops("worker", parent_id)
    child_id = str(uuid4())
    checkpoint_spawned = [
        {
            "node_id": child_id,
            "status": "failed",
            "response": {"error": "boom"},
            "operation": "operate",
            "assignee": "worker",
            "instruction": "follow-up task",
            "parent_id": str(parent_id),
            "spawn_id": "spawn-2",
        }
    ]

    _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

    child = builder.get_graph().internal_nodes[UUID(child_id)]
    assert child.execution.status == EventStatus.FAILED
    assert child.execution.response == {"error": "boom"}


def test_reconstruct_spawned_nodes_chains_through_another_reconstructed_spawn():
    """A grandchild spawn resolves its parent against the other entries in the same checkpoint_spawned batch, not just the statically planned nodes."""
    builder = OperationGraphBuilder()
    env = SimpleNamespace(builder=builder)
    dag_state = _DagState(
        node_ids=[],
        known_nodes=set(),
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    plan_result = _PlanResult(
        assignments=[], agent_ids=[], dep_indices=[], pool=[], budget_preambles={}
    )
    checkpoint_ops: dict[str, dict] = {}
    grandparent_id = str(uuid4())
    child_id = str(uuid4())
    checkpoint_spawned = [
        {
            "node_id": grandparent_id,
            "status": "completed",
            "response": "a",
            "operation": "operate",
            "assignee": None,
            "instruction": "a",
            "parent_id": None,
        },
        {
            "node_id": child_id,
            "status": "completed",
            "response": "b",
            "operation": "operate",
            "assignee": None,
            "instruction": "b",
            "parent_id": grandparent_id,
        },
    ]

    _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

    graph = builder.get_graph()
    assert len(graph.internal_nodes) == 2
    incoming_heads = list(graph.node_edge_mapping[UUID(child_id)]["in"].values())
    assert UUID(grandparent_id) in incoming_heads


def test_reconstruct_spawned_nodes_refuses_when_parent_not_checkpointed_terminal():
    """A spawned node whose parent op had not itself reached a checkpointed terminal state before the crash can't be soundly replayed (would duplicate or lose the spawn); refusal names only the affected node and mutates nothing first."""
    builder = OperationGraphBuilder()
    env = SimpleNamespace(builder=builder)
    dag_state = _DagState(
        node_ids=[],
        known_nodes=set(),
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    plan_result = _PlanResult(
        assignments=[], agent_ids=[], dep_indices=[], pool=[], budget_preambles={}
    )
    checkpoint_ops: dict[str, dict] = {}
    checkpoint_spawned = [
        {
            "node_id": "spawn-a",
            "status": "completed",
            "response": "x",
            "operation": "operate",
            "assignee": None,
            "instruction": "do it",
            "parent_id": "some-op-that-never-reached-a-checkpointed-terminal-state",
        }
    ]

    with pytest.raises(FlowResumeError, match="spawn-a"):
        _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

    assert len(builder.get_graph().internal_nodes) == 0


def test_reconstruct_spawned_nodes_refuses_when_planned_parent_exists_but_is_still_pending():
    """Tighter parent-soundness case: parent_id names a real planned node, but it has no terminal entry in checkpoint_ops (still pending at crash time) -- accepting this would pre-complete the child while the parent reruns live and re-emits the same spawn."""
    builder, parent_id, worker_branch = _real_planned_node("worker")
    env = SimpleNamespace(builder=builder)
    dag_state = _DagState(
        node_ids=[parent_id],
        known_nodes={parent_id},
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": worker_branch},
        worker_models=[],
    )
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    # "worker" has no entry at all in checkpoint_ops -- still pending.
    checkpoint_ops: dict[str, dict] = {}
    checkpoint_spawned = [
        {
            "node_id": "spawn-a",
            "status": "completed",
            "response": "x",
            "operation": "operate",
            "assignee": None,
            "instruction": "do it",
            "parent_id": str(parent_id),
        }
    ]

    with pytest.raises(FlowResumeError, match="spawn-a"):
        _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

    assert len(builder.get_graph().internal_nodes) == 1  # only the pre-existing planned node


def test_reconstruct_spawned_nodes_refuses_unrecognized_status():
    builder = OperationGraphBuilder()
    env = SimpleNamespace(builder=builder)
    dag_state = _DagState(
        node_ids=[],
        known_nodes=set(),
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=[],
    )
    plan_result = _PlanResult(
        assignments=[], agent_ids=[], dep_indices=[], pool=[], budget_preambles={}
    )
    checkpoint_ops: dict[str, dict] = {}
    checkpoint_spawned = [
        {
            "node_id": "spawn-x",
            "status": "running",
            "response": None,
            "operation": "operate",
            "assignee": None,
            "instruction": "x",
            "parent_id": None,
        }
    ]

    with pytest.raises(FlowResumeError, match="spawn-x"):
        _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

    assert len(builder.get_graph().internal_nodes) == 0


def test_reconstruct_spawned_nodes_refuses_assignee_without_spawn_id():
    """role_node_builder always stamps spawn_id and assignee together, so an entry with assignee but no spawn_id can't have come from a sound live spawn; reconstructing it would hand finalize an assignee-bearing node with no spawn_id, violating its invariant."""
    builder, parent_id, worker_branch = _real_planned_node("worker")
    env = SimpleNamespace(builder=builder)
    dag_state = _DagState(
        node_ids=[parent_id],
        known_nodes={parent_id},
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": worker_branch},
        worker_models=[],
    )
    plan_result, checkpoint_ops = _terminal_plan_and_ops("worker", parent_id)
    checkpoint_spawned = [
        {
            "node_id": str(uuid4()),
            "status": "completed",
            "response": "x",
            "operation": "operate",
            "assignee": "worker",
            "instruction": "follow-up",
            "parent_id": str(parent_id),
            "spawn_id": None,
        }
    ]

    with pytest.raises(FlowResumeError, match="spawn_id"):
        _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

    assert len(builder.get_graph().internal_nodes) == 1  # only the pre-existing planned node


def test_apply_checkpoint_precompletion_reconstructs_valid_spawned_entry_without_refusing():
    """End-to-end through _run_flow_inner's entry point: a CHECKPOINT_VERSION-2-shaped spawned entry resumes cleanly alongside normal planned-op precompletion -- no refusal just because a spawn occurred."""
    builder, parent_id, worker_branch = _real_planned_node("worker")
    env = SimpleNamespace(builder=builder)

    assignments = [TaskAssignment(task="do it", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[parent_id],
        known_nodes={parent_id},
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": worker_branch},
        worker_models=[],
    )
    checkpoint_ops = {"worker": {"agent_id": "worker", "status": "completed", "response": "done"}}
    child_id = str(uuid4())
    checkpoint_spawned = [
        {
            "node_id": child_id,
            "status": "completed",
            "response": "child done",
            "operation": "operate",
            "assignee": "worker",
            "instruction": "follow-up",
            "parent_id": str(parent_id),
            "spawn_id": "spawn-9",
        }
    ]

    _apply_checkpoint_precompletion(
        env,
        plan_result,
        dag_state,
        checkpoint_ops,
        allow_degraded_context=False,
        checkpoint_spawned=checkpoint_spawned,
    )

    graph = builder.get_graph()
    parent_node = graph.internal_nodes[parent_id]
    child_node = graph.internal_nodes[UUID(child_id)]
    assert parent_node.execution.status == EventStatus.COMPLETED
    assert parent_node.execution.response == "done"
    assert child_node.execution.status == EventStatus.COMPLETED
    assert child_node.execution.response == "child done"
    assert child_node.branch_id == worker_branch.id
    assert child_node.metadata["spawn_id"] == "spawn-9"
    assert child_node.metadata["reference_id"] == "spawn-9"


# ── _execute_dag: checkpoint write correctness ───────────────────────────────


async def test_checkpoint_captures_nonempty_executor_flow_context_on_completion(tmp_path: Path):
    """Write side of the flow_context guarantee: _checkpoint_record must snapshot the live executor's shared context, not just the completing op's response, or --resume has nothing correct to restore."""
    env = _make_resume_env(tmp_path)
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
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
        worker_models=["claude"],
    )

    fake_executor = SimpleNamespace(
        context=SimpleNamespace(content={"shared_note": "value-from-completed-op"}),
        results={},
    )

    async def _run_dag_result(*args, executor_ref=None, **_kw):
        executor_ref["executor"] = fake_executor
        await env.session.emit(
            NodeCompleted(op_id="node-0", name="worker", elapsed=0.1, parent_id=None, depends_on=[])
        )
        return {
            "operation_results": {"node-0": "result-A"},
            "spawned_operations": 0,
            "escalated_operations": [],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _run_dag_result

    from lionagi.engines import PlanningEngine

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
            checkpoint_prompt="write the brief",
            checkpoint_plan=[{"agent_id": "worker"}],
            checkpoint_config={"model_spec": "claude"},
        )

    data = load_checkpoint(env.run.checkpoint_path)
    assert data["flow_context"] == {"shared_note": "value-from-completed-op"}
    assert data["ops"]["worker"]["status"] == "completed"


async def test_checkpoint_spawned_node_name_collision_does_not_overwrite_planned_ops_entry(
    tmp_path: Path,
):
    """A spawned child's cloned branch can share a planned agent_id's name; using that name as the `ops` key would silently overwrite the planned entry, so spawned completions route to `spawned` keyed by node id instead."""
    env = _make_resume_env(tmp_path)
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="critique", assignee="critic")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["critic"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-critic"],
        known_nodes={"node-critic"},
        deps_by_node={"node-critic": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["claude"],
    )

    async def _run_dag_result(*args, executor_ref=None, **_kw):
        executor_ref["executor"] = SimpleNamespace(context=SimpleNamespace(content={}), results={})
        # The planned "critic" op completes successfully.
        await env.session.emit(
            NodeCompleted(
                op_id="node-critic", name="critic", elapsed=0.1, parent_id=None, depends_on=[]
            )
        )
        # A reactively spawned child whose cloned branch also happens to be
        # named "critic" -- the exact collision the review flagged -- fails.
        await env.session.emit(
            NodeFailed(
                op_id="spawned-node-xyz",
                name="critic",
                elapsed=0.2,
                parent_id="node-critic",
                depends_on=["node-critic"],
            )
        )
        return {
            "operation_results": {
                "node-critic": "critic-result",
                "spawned-node-xyz": "spawned-result",
            },
            "spawned_operations": 1,
            "escalated_operations": [],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _run_dag_result

    from lionagi.engines import PlanningEngine

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
            checkpoint_prompt="critique",
            checkpoint_plan=[{"agent_id": "critic"}],
            checkpoint_config={"model_spec": "claude"},
        )

    data = load_checkpoint(env.run.checkpoint_path)
    # The planned entry must still read "completed" -- not clobbered to
    # "failed" by the same-named spawned child that completed after it.
    assert data["ops"] == {
        "critic": {"agent_id": "critic", "status": "completed", "response": None}
    }
    # op_id "spawned-node-xyz" isn't a real UUID (test shorthand), so the
    # live-graph-node lookup in _checkpoint_record can't resolve it and
    # operation/assignee/instruction stay unset -- only parent_id, which
    # comes straight off the NodeFailed signal, is captured.
    assert data["spawned"] == [
        {
            "node_id": "spawned-node-xyz",
            "status": "failed",
            "response": None,
            "operation": None,
            "assignee": None,
            "instruction": None,
            "parent_id": "node-critic",
            "spawn_id": None,
        }
    ]


async def test_checkpoint_record_captures_spawn_id_from_live_role_routed_graph_node(
    tmp_path: Path,
):
    """Write-side counterpart: role_node_builder stamps spawn_id unconditionally on a spawned node's metadata, so _checkpoint_record must capture it off the live graph node the same way it captures assignee, for reconstruction's assignee-without-spawn_id check."""
    builder, parent_id, worker_branch = _real_planned_node("worker")
    env = _make_resume_env(tmp_path)
    env.builder = builder
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[parent_id],
        known_nodes={parent_id},
        deps_by_node={parent_id: []},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": worker_branch},
        worker_models=["claude"],
    )

    from lionagi.operations.node import create_operation

    spawned_node = create_operation("operate", parameters={"instruction": "follow-up"})
    spawned_node.metadata["assignee"] = "worker"
    spawned_node.metadata["spawn_id"] = "spawn-7"
    spawned_node.metadata["reference_id"] = "spawn-7"
    builder.get_graph().add_node(spawned_node)
    spawned_id_str = str(spawned_node.id)

    async def _run_dag_result(*args, executor_ref=None, **_kw):
        executor_ref["executor"] = SimpleNamespace(
            context=SimpleNamespace(content={}),
            results={spawned_node.id: "spawned-result"},
        )
        await env.session.emit(
            NodeCompleted(
                op_id=spawned_id_str,
                name="worker-clone",
                elapsed=0.1,
                parent_id=str(parent_id),
                depends_on=[str(parent_id)],
            )
        )
        return {
            "operation_results": {spawned_id_str: "spawned-result"},
            "spawned_operations": 1,
            "escalated_operations": [],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _run_dag_result

    from lionagi.engines import PlanningEngine

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
            checkpoint_prompt="write the brief",
            checkpoint_plan=[{"agent_id": "worker"}],
            checkpoint_config={"model_spec": "claude"},
        )

    data = load_checkpoint(env.run.checkpoint_path)
    assert data["spawned"] == [
        {
            "node_id": spawned_id_str,
            "status": "completed",
            "response": "spawned-result",
            "operation": "operate",
            "assignee": "worker",
            "instruction": "follow-up",
            "parent_id": str(parent_id),
            "spawn_id": "spawn-7",
        }
    ]


async def test_execute_dag_seeds_fresh_checkpoint_with_already_reconstructed_spawned_entries(
    tmp_path: Path,
):
    """Double-crash/double-resume: a resume's brand-new CheckpointWriter must be seeded with already-reconstructed spawned entries, or its first flush overwrites `spawned` back to `[]` and a second crash loses that completed work."""
    env = _make_resume_env(tmp_path)
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
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
        worker_models=["claude"],
    )

    seeded_spawned_entry = {
        "node_id": str(uuid4()),
        "status": "completed",
        "response": "child done",
        "operation": "operate",
        "assignee": None,
        "instruction": "follow-up",
        "parent_id": "node-0",
        "spawn_id": None,
    }

    async def _run_dag_result(*args, executor_ref=None, **_kw):
        executor_ref["executor"] = SimpleNamespace(context=SimpleNamespace(content={}), results={})
        # This generation crashes again before ANYTHING new completes -- the
        # already-reconstructed spawned node never re-emits a signal either.
        return {"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _run_dag_result

    from lionagi.engines import PlanningEngine

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
            checkpoint_prompt="write the brief",
            checkpoint_plan=[{"agent_id": "worker"}],
            checkpoint_config={"model_spec": "claude"},
            checkpoint_ops_seed={
                "worker": {"agent_id": "worker", "status": "completed", "response": "already done"}
            },
            checkpoint_spawned_seed=[seeded_spawned_entry],
        )

    data = load_checkpoint(env.run.checkpoint_path)
    assert data["spawned"] == [seeded_spawned_entry]


async def test_execute_dag_max_spawn_budget_accounts_for_restored_spawns(tmp_path: Path):
    """--max-ops is a total budget across resumes; recomputing the live spawn budget as max_ops - len(assignments) with no adjustment for spawns already restored would silently re-grant the same budget every resume."""
    env = _make_resume_env(tmp_path)
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
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
        worker_models=["claude"],
    )

    # max_ops=10 total; 1 planned assignment; 8 spawns already restored from
    # the checkpoint -- only 1 more spawn slot should remain this generation
    # (10 - 1 - 8 == 1), not the naive 9 (10 - 1) a resume without this fix
    # would recompute every time.
    seeded_spawned = [
        {
            "node_id": str(uuid4()),
            "status": "completed",
            "response": f"child-{i}",
            "operation": "operate",
            "assignee": None,
            "instruction": "follow-up",
            "parent_id": "node-0",
            "spawn_id": None,
        }
        for i in range(8)
    ]

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = AsyncMock(
        return_value={"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}
    )

    from lionagi.engines import PlanningEngine

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=10,
            checkpoint_prompt="write the brief",
            checkpoint_plan=[{"agent_id": "worker"}],
            checkpoint_config={"model_spec": "claude"},
            checkpoint_spawned_seed=seeded_spawned,
        )

    assert fake_engine_run.run_dag.call_args.kwargs["max_spawn"] == 1


async def test_execute_dag_n_spawned_counts_restored_spawns_alongside_new_ones(tmp_path: Path):
    """The synthesis gate is `with_synthesis or exec_result.n_spawned`; a resume where every spawn was already completed before the crash (zero new spawns this generation) must still report the restored count, or synthesis is silently skipped."""
    env = _make_resume_env(tmp_path)
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
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
        worker_models=["claude"],
    )
    seeded_spawned = [
        {
            "node_id": str(uuid4()),
            "status": "completed",
            "response": "child done",
            "operation": "operate",
            "assignee": None,
            "instruction": "follow-up",
            "parent_id": "node-0",
            "spawn_id": None,
        }
    ]

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = AsyncMock(
        return_value={"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}
    )

    from lionagi.engines import PlanningEngine

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
            checkpoint_prompt="write the brief",
            checkpoint_plan=[{"agent_id": "worker"}],
            checkpoint_config={"model_spec": "claude"},
            checkpoint_spawned_seed=seeded_spawned,
        )

    assert exec_result.n_spawned == 1


async def test_resumed_run_with_only_restored_spawns_triggers_synthesis(tmp_path: Path):
    """End-to-end through _run_flow_inner: the synthesis gate must fire even when every reactively spawned node was already completed and this generation produces zero new spawns -- proves the n_spawned accounting fix reaches the gate check, not just _execute_dag's return value."""
    env = _make_resume_env(tmp_path)
    checkpoint = {
        "version": 2,
        "session_id": "prior-session",
        "prompt": "write the brief",
        "plan": [
            {
                "task": "write the brief",
                "assignee": "worker",
                "inputs": [],
                "exit_criteria": None,
                "depends_on": [],
                "modes": [],
                "agent_id": "worker",
                "dep_indices": [],
            }
        ],
        "config": {},
        "flow_context": {},
        "ops": {
            "worker": {"agent_id": "worker", "status": "completed", "response": "already done"}
        },
        "spawned": [
            {
                "node_id": str(uuid4()),
                "status": "completed",
                "response": "child done",
                "operation": "operate",
                "assignee": None,
                "instruction": "follow-up",
                "parent_id": "node-0",
                "spawn_id": None,
            }
        ],
    }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {
                "operation_results": {"node-0": "already done"},
                "spawned_operations": 0,
                "escalated_operations": [],
            }
        )
    )

    synthesize_mock = AsyncMock(return_value="synthesized")

    from lionagi.engines import PlanningEngine

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch("worker"), "codex/gpt-5.5", None, False),
        ),
        patch.object(PlanningEngine, "new_run", return_value=fake_engine_run),
        patch("lionagi.cli.orchestrate.flow.plan") as plan_mock,
        patch("lionagi.cli.orchestrate.flow._apply_checkpoint_precompletion"),
        patch("lionagi.cli.orchestrate.flow._synthesize", synthesize_mock),
        patch("lionagi.cli.orchestrate.flow._finalize_flow", return_value="ok"),
    ):
        await _run_flow_inner(
            "codex/gpt-5.5",
            "write the brief",
            env=env,
            resume_checkpoint=checkpoint,
            allow_degraded_context=False,
            checkpoint_config=None,
            reactive_spec="on",
        )

    plan_mock.assert_not_called()
    synthesize_mock.assert_called_once()


# ── Spawn-id sequence: resume must not reissue a restored spawn's id ────────


def test_role_node_builder_start_seeds_first_spawn_id_past_restored_ordinal():
    """role_node_builder(..., start=2) must issue spawn-2 for the first live spawn, not spawn-1 -- the exact id/artifact-directory collision a resumed run's fresh closure would otherwise reissue against an already-restored spawn-1."""
    from lionagi.casts.emission import SpawnRequest
    from lionagi.orchestration.patterns import role_node_builder

    build = role_node_builder({}, start=2)
    node = build(SpawnRequest(instruction="follow-up"), None)

    assert node.metadata["spawn_id"] == "spawn-2"
    assert node.metadata["reference_id"] == "spawn-2"


async def test_execute_dag_seeds_spawn_sequence_past_restored_ordinal(tmp_path: Path):
    """Resuming a checkpoint that already restored spawn-1, the fresh role_node_builder(...) this _execute_dag call constructs must start its sequence at 2, or a new spawn after resume reuses spawn-1's id and artifact directory."""
    env = _make_resume_env(tmp_path)
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
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
        worker_models=["claude"],
    )
    seeded_spawned = [
        {
            "node_id": str(uuid4()),
            "status": "completed",
            "response": "child done",
            "operation": "operate",
            "assignee": None,
            "instruction": "follow-up",
            "parent_id": "node-0",
            "spawn_id": "spawn-1",
        }
    ]

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = AsyncMock(
        return_value={"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}
    )

    from lionagi.engines import PlanningEngine

    with (
        patch.object(PlanningEngine, "new_run", return_value=fake_engine_run),
        patch("lionagi.cli.orchestrate.flow.role_node_builder") as role_node_builder_mock,
    ):
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
            checkpoint_prompt="write the brief",
            checkpoint_plan=[{"agent_id": "worker"}],
            checkpoint_config={"model_spec": "claude"},
            checkpoint_spawned_seed=seeded_spawned,
        )

    assert role_node_builder_mock.call_args.kwargs["start"] == 2


async def test_execute_dag_seeds_spawn_sequence_past_gap_in_restored_ordinals(tmp_path: Path):
    """A crashed run can leave gaps (an allocated spawn ordinal that never reached a checkpointed terminal state, so it's absent from `spawned`); the next-ordinal seed must take MAX(restored)+1, not len(restored)+1, or it double-allocates into the gap."""
    env = _make_resume_env(tmp_path)
    env.session = Session(default_branch=Branch(name="orchestrator"))
    env.run.checkpoint_path = tmp_path / "checkpoint.json"

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
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
        worker_models=["claude"],
    )
    seeded_spawned = [
        {
            "node_id": str(uuid4()),
            "status": "completed",
            "response": "child done",
            "operation": "operate",
            "assignee": None,
            "instruction": "follow-up",
            "parent_id": "node-0",
            "spawn_id": "spawn-3",
        }
    ]

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = AsyncMock(
        return_value={"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}
    )

    from lionagi.engines import PlanningEngine

    with (
        patch.object(PlanningEngine, "new_run", return_value=fake_engine_run),
        patch("lionagi.cli.orchestrate.flow.role_node_builder") as role_node_builder_mock,
    ):
        await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
            checkpoint_prompt="write the brief",
            checkpoint_plan=[{"agent_id": "worker"}],
            checkpoint_config={"model_spec": "claude"},
            checkpoint_spawned_seed=seeded_spawned,
        )

    assert role_node_builder_mock.call_args.kwargs["start"] == 4


# ── Resume sequencing: planner skipped, finalization tail still runs ────────


async def test_resume_skips_planner_and_still_calls_finalize_with_zero_pending_ops(tmp_path: Path):
    """A checkpoint where every op is already completed must still reach _finalize_flow -- no shortcut skips finalization just because execute_dag had nothing new to run. Also pins: the planner is never called on resume."""
    env = _make_resume_env(tmp_path)
    checkpoint = {
        "version": 1,
        "session_id": "prior-session",
        "prompt": "write the brief",
        "plan": [
            {
                "task": "write the brief",
                "assignee": "worker",
                "inputs": [],
                "exit_criteria": None,
                "depends_on": [],
                "modes": [],
                "agent_id": "worker",
                "dep_indices": [],
            }
        ],
        "config": {},
        "flow_context": {},
        "ops": {
            "worker": {"agent_id": "worker", "status": "completed", "response": "already done"}
        },
        "spawned": [],
    }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}
        )
    )

    from lionagi.engines import PlanningEngine

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch("worker"), "codex/gpt-5.5", None, False),
        ),
        patch.object(PlanningEngine, "new_run", return_value=fake_engine_run),
        patch("lionagi.cli.orchestrate.flow.plan") as plan_mock,
        patch("lionagi.cli.orchestrate.flow._finalize_flow", return_value="ok") as finalize_mock,
    ):
        output = await _run_flow_inner(
            "codex/gpt-5.5",
            "write the brief",
            env=env,
            resume_checkpoint=checkpoint,
            allow_degraded_context=False,
            checkpoint_config=None,
            reactive_spec="off",
        )

    plan_mock.assert_not_called()
    finalize_mock.assert_called_once()
    assert output == "ok"
    # The one op was pre-marked completed by the resume path along the way.
    node = env.builder._nodes["node-0"]
    assert node.execution.status == EventStatus.COMPLETED
    assert node.execution.response == "already done"


async def test_resume_seeds_executor_with_checkpointed_flow_context(tmp_path: Path):
    """A completed op's flow_context written before a crash must hand off to the fresh post-resume executor, not the empty workspace a brand-new DependencyAwareExecutor starts with -- proven across a simulated process boundary."""
    env = _make_resume_env(tmp_path)
    checkpoint = {
        "version": 1,
        "session_id": "prior-session",
        "prompt": "write the brief",
        "plan": [
            {
                "task": "write the brief",
                "assignee": "worker",
                "inputs": [],
                "exit_criteria": None,
                "depends_on": [],
                "modes": [],
                "agent_id": "worker",
                "dep_indices": [],
            }
        ],
        "config": {},
        "flow_context": {"shared_note": "value-from-completed-op"},
        "ops": {
            "worker": {"agent_id": "worker", "status": "completed", "response": "already done"}
        },
        "spawned": [],
    }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(
        return_value=_asyncio_coro(
            {"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}
        )
    )

    from lionagi.engines import PlanningEngine

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch("worker"), "codex/gpt-5.5", None, False),
        ),
        patch.object(PlanningEngine, "new_run", return_value=fake_engine_run),
        patch("lionagi.cli.orchestrate.flow.plan"),
        patch("lionagi.cli.orchestrate.flow._finalize_flow", return_value="ok"),
    ):
        await _run_flow_inner(
            "codex/gpt-5.5",
            "write the brief",
            env=env,
            resume_checkpoint=checkpoint,
            allow_degraded_context=False,
            checkpoint_config=None,
            reactive_spec="off",
        )

    _args, kwargs = fake_engine_run.run_dag.call_args
    assert kwargs["context"] == {"shared_note": "value-from-completed-op"}


async def test_resumed_checkpoint_carries_restored_flow_context_across_zero_completions(
    tmp_path: Path,
):
    """A second-generation checkpoint (written by a resumed run) must still carry forward the flow_context restored from the prior checkpoint, even if it crashes before any op completes and the writer only flushes its initial seed."""
    env = _make_resume_env(tmp_path)
    env.run.checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = {
        "version": 1,
        "session_id": "prior-session",
        "prompt": "write the brief",
        "plan": [
            {
                "task": "write the brief",
                "assignee": "worker",
                "inputs": [],
                "exit_criteria": None,
                "depends_on": [],
                "modes": [],
                "agent_id": "worker",
                "dep_indices": [],
            }
        ],
        "config": {},
        "flow_context": {"shared_note": "value-from-completed-op"},
        "ops": {
            "worker": {"agent_id": "worker", "status": "completed", "response": "already done"}
        },
        "spawned": [],
    }

    async def _run_dag_result(*args, executor_ref=None, **_kw):
        # No NodeCompleted/NodeFailed signal ever fires -- simulating the
        # resumed run crashing before any op completes, so the only
        # checkpoint write this generation makes is the initial flush.
        return {"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = _run_dag_result

    from lionagi.engines import PlanningEngine

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch("worker"), "codex/gpt-5.5", None, False),
        ),
        patch.object(PlanningEngine, "new_run", return_value=fake_engine_run),
        patch("lionagi.cli.orchestrate.flow.plan"),
        patch("lionagi.cli.orchestrate.flow._finalize_flow", return_value="ok"),
    ):
        await _run_flow_inner(
            "codex/gpt-5.5",
            "write the brief",
            env=env,
            resume_checkpoint=checkpoint,
            allow_degraded_context=False,
            checkpoint_config={"model_spec": "codex/gpt-5.5"},
            reactive_spec="off",
        )

    data = load_checkpoint(env.run.checkpoint_path)
    assert data["flow_context"] == {"shared_note": "value-from-completed-op"}


async def test_resume_refuses_checkpoint_with_spawned_entries(tmp_path: Path):
    """Replaying reactively spawned work on resume isn't implemented, so a checkpoint recording any must refuse outright rather than silently drop that completed spawned work."""
    env = _make_resume_env(tmp_path)
    checkpoint = {
        "version": 1,
        "session_id": "prior-session",
        "prompt": "write the brief",
        "plan": [
            {
                "task": "write the brief",
                "assignee": "worker",
                "inputs": [],
                "exit_criteria": None,
                "depends_on": [],
                "modes": [],
                "agent_id": "worker",
                "dep_indices": [],
            }
        ],
        "config": {},
        "flow_context": {},
        "ops": {"worker": {"agent_id": "worker", "status": "completed", "response": "done"}},
        "spawned": [
            {"node_id": "spawned-node-xyz", "status": "completed", "response": "child result"}
        ],
    }

    with (
        patch(
            "lionagi.cli.orchestrate.flow.build_worker_branch",
            return_value=(_FakeBranch("worker"), "codex/gpt-5.5", None, False),
        ),
        patch("lionagi.cli.orchestrate.flow.plan") as plan_mock,
        pytest.raises(FlowResumeError, match="spawned-node-xyz"),
    ):
        await _run_flow_inner(
            "codex/gpt-5.5",
            "write the brief",
            env=env,
            resume_checkpoint=checkpoint,
            allow_degraded_context=False,
            checkpoint_config=None,
            reactive_spec="off",
        )

    plan_mock.assert_not_called()


async def test_resume_missing_artifact_still_flips_status_to_failed(
    temp_db_path: Path, tmp_path: Path
):
    """Concrete DB-level proof of the same gate: resuming a checkpoint with nothing left for run_dag to execute must still drive the artifact-verification teardown check, and flip status to failed if a required artifact is missing."""
    env = _minimal_real_env()
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    contract = {"expected": [{"id": "brief", "path": "brief.md"}]}
    await start_live_persist(
        env, invocation_kind="flow", artifacts_path=str(artifacts_dir), artifact_contract=contract
    )
    ctx = env._live_persist
    assert ctx is not None
    # brief.md is deliberately never written.

    assignments = [TaskAssignment(task="write the brief", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(Branch(name="worker"), "claude", None, False),
    ):
        dag_state = await _build_dag(env, "write the brief", plan_result, reactive_spec="off")

    checkpoint_ops = {
        "worker": {"agent_id": "worker", "status": "completed", "response": "done previously"}
    }
    _apply_checkpoint_precompletion(
        env, plan_result, dag_state, checkpoint_ops, allow_degraded_context=False
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {dag_state.node_ids[0]: "done previously"},
            "spawned_operations": 0,
            "escalated_operations": [],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert exec_result.agent_results
    assert exec_result.agent_results[0]["response"] == "done previously"

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.missing_artifact"


# ── resolve_checkpoint_target ────────────────────────────────────────────────


async def test_resolve_checkpoint_target_exact_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "20260703T000000-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "s1",
                "prompt": "p",
                "plan": [],
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    resolved_run_dir, checkpoint = await resolve_checkpoint_target("20260703T000000-abc123")
    assert resolved_run_dir.run_id == "20260703T000000-abc123"
    assert checkpoint["session_id"] == "s1"


async def test_resolve_checkpoint_target_prefix_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.cli.orchestrate._checkpoint as ckmod

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "20260703T000000-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "s1",
                "prompt": "p",
                "plan": [],
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    resolved_run_dir, _checkpoint = await resolve_checkpoint_target("20260703")
    assert resolved_run_dir.run_id == "20260703T000000-abc123"


async def test_resolve_checkpoint_target_run_dir_without_checkpoint_falls_back_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db_path: Path
):
    """A run dir that exists but never wrote a checkpoint (predates resume
    support, or never reached _build_dag) must not match — it falls through
    to the session-lookup path, and with nothing there either, refuses loudly."""
    import lionagi.cli.orchestrate._checkpoint as ckmod

    runs_root = tmp_path / "runs"
    (runs_root / "no-checkpoint-run").mkdir(parents=True)
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    with pytest.raises(FlowResumeError):
        await resolve_checkpoint_target("no-checkpoint-run")


async def test_resolve_checkpoint_target_falls_back_to_session_run_id(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A session/invocation/play id with no matching run directory of its own
    resolves via its node_metadata run_id — the path a real `--resume <session-id>`
    invocation takes, as opposed to `--resume <run-id>`."""
    import lionagi.cli.orchestrate._checkpoint as ckmod

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(ckmod, "RUNS_ROOT", runs_root)

    env = _minimal_real_env()
    await start_live_persist(
        env, invocation_kind="flow", artifacts_path=str(tmp_path / "artifacts")
    )
    ctx = env._live_persist
    assert ctx is not None
    session_id = ctx["session_id"]

    run_dir_path = runs_root / "run-for-session"
    run_dir_path.mkdir(parents=True)
    (run_dir_path / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": session_id,
                "prompt": "p",
                "plan": [],
                "flow_context": {},
                "ops": {},
                "spawned": [],
                "config": {},
            }
        )
    )

    async with StateDB() as db:
        await db.update_session(session_id, node_metadata=json.dumps({"run_id": "run-for-session"}))

    resolved_run_dir, checkpoint = await resolve_checkpoint_target(session_id)
    assert resolved_run_dir.run_id == "run-for-session"
    assert checkpoint["session_id"] == session_id

    await stop_live_persist(env, status="completed")


# ── CLI wiring: `li o flow --resume` argparse + dispatch ────────────────────


def _parse_flow_args(argv: list[str]) -> argparse.Namespace:
    """Mimics the real CLI pipeline (pre-scan for playbook -> inject flags -> parse); same helper name as test_flow_spec_file.py's, for parity."""
    from lionagi.cli.orchestrate import (
        add_orchestrate_subparser,
        inject_playbook_schema_into_parser,
    )

    parser = argparse.ArgumentParser(prog="li")
    subparsers = parser.add_subparsers(dest="command", required=True)
    orch_parsers = add_orchestrate_subparser(subparsers)
    full_argv = ["o", "flow", *argv]
    inject_playbook_schema_into_parser(orch_parsers["flow"], full_argv)
    return parser.parse_args(full_argv)


def test_flow_resume_flag_parses_and_defaults_allow_degraded_context_false():
    args = _parse_flow_args(["--resume", "abc123"])
    assert args.resume == "abc123"
    assert args.allow_degraded_context is False


def test_flow_resume_dispatch_bypasses_planner_args_and_prints_output(capsys):
    from lionagi.cli.orchestrate import run_orchestrate

    args = _parse_flow_args(["--resume", "abc123"])

    with patch(
        "lionagi.cli.orchestrate._resume_flow",
        AsyncMock(return_value=("resumed output", "completed")),
    ) as resume_mock:
        code = run_orchestrate(args)

    assert code == 0
    resume_mock.assert_called_once()
    assert resume_mock.call_args.args[0] == "abc123"
    assert resume_mock.call_args.kwargs["allow_degraded_context"] is False
    assert capsys.readouterr().out.strip() == "resumed output"


def test_flow_resume_dispatch_propagates_allow_degraded_context():
    from lionagi.cli.orchestrate import run_orchestrate

    args = _parse_flow_args(["--resume", "abc123", "--allow-degraded-context"])

    with patch(
        "lionagi.cli.orchestrate._resume_flow",
        AsyncMock(return_value=("ok", "completed")),
    ) as resume_mock:
        code = run_orchestrate(args)

    assert code == 0
    assert resume_mock.call_args.kwargs["allow_degraded_context"] is True


def test_flow_resume_dispatch_maps_resume_error_to_failed_exit_code(caplog):
    from lionagi.cli.orchestrate import run_orchestrate

    args = _parse_flow_args(["--resume", "no-such-run"])

    with patch(
        "lionagi.cli.orchestrate._resume_flow",
        AsyncMock(side_effect=FlowResumeError("no checkpoint found")),
    ):
        code = run_orchestrate(args)

    assert code == 1
