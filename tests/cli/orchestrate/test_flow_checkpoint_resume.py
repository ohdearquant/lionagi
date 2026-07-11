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
from uuid import uuid4

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
    _run_flow_inner,
)
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
    assert data["version"] == 1
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
    """Spawned nodes must never share the `ops` keyspace with planned agent_ids
    -- a spawned child's branch can be named identically to a planned agent_id
    (clones inherit the source branch's name), so `ops` and `spawned` are kept
    as two distinct groves, and re-recording the same spawned node id updates
    its one entry rather than appending a duplicate.
    """
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path=path, session_id="s", prompt="p", plan=[], config={})

    await writer.record("critic", status="completed", response="planned-result")
    await writer.record_spawned("spawned-1", status="completed", response="child-result-v1")
    await writer.record_spawned("spawned-1", status="completed", response="child-result-v2")

    data = load_checkpoint(path)
    assert data["ops"] == {
        "critic": {"agent_id": "critic", "status": "completed", "response": "planned-result"}
    }
    assert data["spawned"] == [
        {"node_id": "spawned-1", "status": "completed", "response": "child-result-v2"}
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
    """A checkpointed 'failed' op must be restored as terminal FAILED, not
    silently treated as pending and re-run -- it may already have produced
    side effects or partial artifacts before the process died, and resume
    must never guess at retry semantics on its own.
    """
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
    """Replaying reactively spawned work on resume isn't implemented, so a
    checkpoint that recorded any must refuse resume outright rather than
    silently drop that completed work. Refusal is unconditional -- it is not
    something --allow-degraded-context (a different concern) can bypass -- and
    happens before any node is mutated.
    """
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


# ── _execute_dag: checkpoint write correctness ───────────────────────────────


async def test_checkpoint_captures_nonempty_executor_flow_context_on_completion(tmp_path: Path):
    """The write side of the flow_context guarantee: _checkpoint_record must
    snapshot the live executor's shared context workspace, not just the
    completing op's own response -- otherwise --resume has nothing correct to
    restore even though the checkpoint's `ops` entries look fine on their own.
    """
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
    """A reactively spawned child's branch can carry a name identical to a
    planned agent_id's (clones inherit the source branch's name). Using that
    name as the checkpoint's `ops` key would silently overwrite the planned
    op's entry -- spawned completions must route to `spawned`, keyed by their
    own node id, and never touch `ops`.
    """
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
    assert data["spawned"] == [
        {"node_id": "spawned-node-xyz", "status": "failed", "response": None}
    ]


# ── Resume sequencing: planner skipped, finalization tail still runs ────────


async def test_resume_skips_planner_and_still_calls_finalize_with_zero_pending_ops(tmp_path: Path):
    """A checkpoint where every op is already completed must still reach
    _finalize_flow — no shortcut may skip the finalization tail just because
    execute_dag had nothing new to run. Also pins the companion guarantee:
    the planner is never called on resume.
    """
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
    """A completed op that wrote into the executor's shared flow_context
    before a crash must hand that same context to the fresh (post-resume)
    executor, so pending ops downstream see it exactly as they would have
    live -- not the empty workspace a brand new DependencyAwareExecutor
    otherwise starts with. Proven across a simulated process boundary: this
    _run_flow_inner call knows nothing except what the checkpoint dict carries.
    """
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
    """A second-generation checkpoint -- one written by a RESUMED run -- must
    still carry forward the flow_context restored from the prior checkpoint,
    even if the resumed run crashes before any op completes and the writer
    only ever flushes its initial construction-time seed. Otherwise a
    resume-of-a-resume silently loses shared context that nothing actually
    went wrong with restoring, across this second simulated process boundary.
    """
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
    """Replaying reactively spawned work on resume isn't implemented, so a
    checkpoint that recorded any must refuse resume outright -- silently
    proceeding would drop that completed spawned work along with its
    artifact-contract/synthesis participation.
    """
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
    """Concrete DB-level proof of the same gate: resuming a checkpoint whose
    only op is already 'completed' (nothing left for run_dag to execute) must
    still drive the artifact-verification teardown check. A required artifact
    genuinely missing from disk must still flip the session to failed, not
    silently read as a clean completion because resume had nothing new to run.
    """
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
    """Mimic the real CLI pipeline: pre-scan for playbook → inject flags → parse.

    Mirrors tests/cli/orchestrate/test_flow_spec_file.py's helper of the same name.
    """
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
