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
            return_value=(_FakeBranch("worker"), "codex/gpt-5.5", None),
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
        return_value=(Branch(name="worker"), "claude", None),
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
