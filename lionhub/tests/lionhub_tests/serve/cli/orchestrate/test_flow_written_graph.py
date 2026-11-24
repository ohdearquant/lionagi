# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A written graph runs through the flow command in place of the planner.

The graph's nodes become the run's assignments as written: no planner call, no
synthesis pass, and each node sees the outputs of the nodes it names under ``needs``.
The run keeps a copy of the graph beside its checkpoint, and a resume reads that copy,
never the file the run was started from.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.branch import Branch
from lionagi.session.session import Session
from lionhub.orchestration.engines import EngineRun, PlanningEngine
from lionhub.orchestration.flow.checkpoint import FlowResumeError
from lionhub.orchestration.flow.graph import GRAPH_COPY_FILENAME, load_graph, write_graph_copy
from lionhub.serve.cli.orchestrate import flow as flow_module
from lionhub.serve.cli.orchestrate.flow import _run_flow_inner
from tests._checkpoint_test_support import temp_db_path as temp_db_path

from .test_flow_phases import _make_env
from .test_run_flow_inner import _env, _SessionBackedEngineRun


def _node(node_id: str, needs: list[str] | None = None, **extra) -> dict:
    return {
        "id": node_id,
        "agent": "worker",
        "prompt": f"do {node_id}",
        "needs": needs or [],
        **extra,
    }


def _source(tmp_path: Path, nodes: list[dict], name: str = "graph.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"nodes": nodes}))
    return load_graph(str(path))


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch):
    """Workers are plain branches named by node id; the planner must never be reached."""

    async def build_worker(env, *, agent_id, **kwargs):
        branch = Branch(name=agent_id)
        env.session.include_branches(branch)
        return branch, "fake/model", None, False

    async def no_planner(*args, **kwargs):
        raise AssertionError("a written graph called the planner")

    monkeypatch.setattr(flow_module, "build_worker_branch", build_worker)
    monkeypatch.setattr(flow_module, "plan", no_planner)
    monkeypatch.setattr(flow_module, "finalize_orchestration", MagicMock())


@pytest.mark.asyncio
async def test_a_written_graph_runs_its_nodes_as_written_without_planner_or_synthesis(
    tmp_path, monkeypatch: pytest.MonkeyPatch, runtime
):
    monkeypatch.setattr(
        PlanningEngine, "new_run", lambda self, *, session: _SessionBackedEngineRun(session)
    )
    synthesize = AsyncMock(return_value=None)
    monkeypatch.setattr(flow_module, "_synthesize", synthesize)
    # A spawned op and --with-synthesis would each start a synthesis pass on a planned run.
    env = _env(tmp_path, ["a out", "b out", "c out"], spawned_operations=1)
    source = _source(
        tmp_path,
        [_node("a"), _node("b", ["a"]), _node("c", ["a", "b"], exit_criteria="c is checked")],
    )

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "",
        env=env,
        with_synthesis=True,
        reactive_spec="off",
        graph_source=source,
    )

    synthesize.assert_not_called()
    assert len(env.session.run_dag_calls) == 1
    assert env.session.flow_calls == []
    graph = env.builder.get_graph()
    nodes = list(graph.internal_nodes)
    assert len(nodes) == 3
    branches = {branch.id: branch.name for branch in env.session.branches}
    assert [branches[node.branch_id] for node in nodes] == ["a", "b", "c"]
    assert graph.predecessor_ids(nodes[0]) == ()
    assert graph.predecessor_ids(nodes[1]) == (nodes[0].id,)
    assert set(graph.predecessor_ids(nodes[2])) == {nodes[0].id, nodes[1].id}
    assert nodes[0].parameters["instruction"].endswith("do a")
    assert "c is checked" in nodes[2].parameters["instruction"]
    for node in nodes:
        # The run has no prompt, so no node is handed an empty task beside its own.
        assert "original_task" not in json.dumps(node.parameters["context"], default=str)
    assert "a out" in output and "c out" in output


@pytest.mark.asyncio
async def test_a_resumed_written_graph_makes_no_synthesis_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch, runtime
):
    monkeypatch.setattr(
        PlanningEngine, "new_run", lambda self, *, session: _SessionBackedEngineRun(session)
    )
    synthesize = AsyncMock(return_value=None)
    monkeypatch.setattr(flow_module, "_synthesize", synthesize)
    env = _env(tmp_path, ["a out", "b out"], spawned_operations=1)
    source = _source(tmp_path, [_node("a"), _node("b", ["a"])])
    fields, agent_ids, dep_indices = source.plan()
    plan = [
        {**fields[i], "agent_id": agent_ids[i], "dep_indices": dep_indices[i]}
        for i in range(len(fields))
    ]

    await _run_flow_inner(
        "codex/gpt-5.5",
        "",
        env=env,
        with_synthesis=True,
        reactive_spec="off",
        resume_checkpoint={"plan": plan, "ops": {}, "spawned": []},
        graph_source=source,
    )

    synthesize.assert_not_called()
    assert len(env.session.run_dag_calls) == 1


@pytest.mark.asyncio
async def test_a_dry_run_shows_the_written_graph_as_its_plan(tmp_path, runtime):
    env = _make_env(tmp_path)
    env._name_counts = {}
    source = _source(tmp_path, [_node("a"), _node("b", ["a"], exit_criteria="b is done")])

    output = await _run_flow_inner(
        "codex/gpt-5.5", "", env=env, dry_run=True, reactive_spec="off", graph_source=source
    )

    assert output.startswith("Plan (2 assignments):")
    assert "Dependencies below are the ones the written graph declared." in output
    assert "  1. [worker] do a" in output
    assert "  2. [worker] do b\n  depends_on: 1\n    exit: b is done" in output
    assert "  a: codex/gpt-5.5 (bare)" in output
    assert "  b: codex/gpt-5.5 (bare)" in output


def _engine_env(tmp_path, monkeypatch, fail: str | None = None) -> tuple[object, dict, dict]:
    """An env whose DAG runs on the real engine and executor, each worker answering with
    its name."""
    seen: dict[str, object] = {}
    flow_result: dict = {}

    async def operate(self, *, instruction=None, context=None, **kwargs):
        seen[self.name] = context
        if self.name == fail:
            raise RuntimeError(f"{self.name} failed")
        return f"{self.name} output"

    real_run_dag = EngineRun.run_dag

    async def run_dag(self, graph, **kwargs):
        result = await real_run_dag(self, graph, **kwargs)
        flow_result.update(result)
        return result

    monkeypatch.setattr(Branch, "operate", operate)
    monkeypatch.setattr(EngineRun, "run_dag", run_dag)
    env = _make_env(tmp_path)
    env.builder = OperationGraphBuilder()
    env.orc_branch = Branch(name="orchestrator")
    env.session = Session(default_branch=env.orc_branch)
    env._name_counts = {}
    return env, seen, flow_result


@pytest.mark.asyncio
async def test_a_node_sees_the_output_of_the_nodes_it_needs_and_not_of_the_others(
    tmp_path, monkeypatch: pytest.MonkeyPatch, runtime
):
    env, seen, _ = _engine_env(tmp_path, monkeypatch)
    source = _source(tmp_path, [_node("a"), _node("b", ["a"]), _node("c", ["b"])])

    await _run_flow_inner("codex/gpt-5.5", "", env=env, reactive_spec="off", graph_source=source)

    assert set(seen) == {"a", "b", "c"}
    assert "a output" in json.dumps(seen["b"], default=str)
    c_context = json.dumps(seen["c"], default=str)
    assert "b output" in c_context
    assert "a output" not in c_context, "c named only b, yet it was handed a's output"


@pytest.mark.asyncio
async def test_a_failed_node_does_not_stop_the_nodes_that_need_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch, runtime
):
    env, seen, flow_result = _engine_env(tmp_path, monkeypatch, fail="a")
    env.run.checkpoint_path = tmp_path / "checkpoint.json"
    source = _source(tmp_path, [_node("a"), _node("b", ["a"])])

    # Returning, rather than raising, is what makes the job's terminal status completed.
    await _run_flow_inner(
        "codex/gpt-5.5",
        "",
        env=env,
        reactive_spec="off",
        graph_source=source,
        checkpoint_config={"graph": source.record(), "with_synthesis": False},
    )

    checkpoint = json.loads(env.run.checkpoint_path.read_text())
    assert checkpoint["version"] == 4
    assert checkpoint["ops"]["a"]["status"] == "failed"
    assert checkpoint["ops"]["b"]["status"] == "completed"
    assert "b" in seen, "the node needing a failed node did not run"
    assert "a output" not in json.dumps(seen["b"], default=str)
    nodes = list(env.builder.get_graph().internal_nodes)
    assert nodes[0].id in set(flow_result["failed_operations"])
    assert nodes[1].id in set(flow_result["completed_operations"])


_RUN_ID = "written-graph-run"


def _checkpoint_dir(tmp_path: Path, source) -> Path:
    run_dir = tmp_path / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)
    write_graph_copy(run_dir, source)
    checkpoint = {
        "version": 4,
        "session_id": "prior-session",
        "prompt": "",
        "plan": [],
        "flow_context": {},
        "ops": {},
        "spawned": [],
        "config": {"graph": source.record(), "no_mcp_config": True, "mcp_config": None},
    }
    (run_dir / "checkpoint.json").write_text(json.dumps(checkpoint))
    return run_dir


@pytest.mark.asyncio
async def test_a_resume_reads_the_run_copy_and_ignores_a_changed_original(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    import lionhub.orchestration.flow.checkpoint as checkpoint_mod

    source = _source(tmp_path, [_node("a"), _node("b", ["a"])])
    _checkpoint_dir(tmp_path, source)
    Path(source.path).write_text(json.dumps({"nodes": [_node("z")]}))
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", tmp_path / "runs")
    resumed = AsyncMock(return_value=("ok", "completed"))
    monkeypatch.setattr(flow_module, "_run_flow", resumed)

    await flow_module._resume_flow(_RUN_ID)

    kwargs = resumed.call_args.kwargs
    replayed = kwargs["graph_source"]
    assert replayed.sha256 == source.sha256
    assert [node.id for node in replayed.nodes] == ["a", "b"]
    assert "graph" not in kwargs, "the record is consumed, not replayed as an option"
    assert kwargs["resume_checkpoint"]["config"]["graph"] == source.record()


@pytest.mark.asyncio
async def test_a_resume_refuses_a_copy_that_no_longer_matches_the_checkpoint(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    import lionhub.orchestration.flow.checkpoint as checkpoint_mod

    source = _source(tmp_path, [_node("a")])
    run_dir = _checkpoint_dir(tmp_path, source)
    (run_dir / GRAPH_COPY_FILENAME).write_text(json.dumps({"nodes": [_node("other")]}))
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", tmp_path / "runs")
    resumed = AsyncMock(return_value=("ok", "completed"))
    monkeypatch.setattr(flow_module, "_run_flow", resumed)

    with pytest.raises(FlowResumeError, match="Cannot resume this written-graph run"):
        await flow_module._resume_flow(_RUN_ID)
    resumed.assert_not_called()


@pytest.mark.asyncio
async def test_a_run_keeps_a_copy_of_its_graph_and_records_it_for_resume(
    temp_db_path: Path, tmp_path: Path
):
    from .test_flow_checkpoint_resume import _env_for_run_flow

    env = _env_for_run_flow(tmp_path)
    state_root = tmp_path / "state"
    state_root.mkdir()
    env.run.state_root = state_root
    source = _source(tmp_path, [_node("a"), _node("b", ["a"])])
    inner = AsyncMock(return_value="ok result")
    with (
        patch.object(flow_module, "setup_orchestration", AsyncMock(return_value=env)),
        patch.object(flow_module, "_run_flow_inner", inner),
        patch.object(flow_module, "start_live_persist", AsyncMock()),
        patch.object(flow_module, "stop_live_persist", AsyncMock()),
    ):
        await flow_module._run_flow("claude", "", reactive_spec="off", graph_source=source)

    copy = state_root / GRAPH_COPY_FILENAME
    assert copy.read_bytes() == Path(source.path).read_bytes()
    assert oct(copy.stat().st_mode & 0o777) == oct(0o600)
    assert inner.call_args.kwargs["checkpoint_config"]["graph"] == source.record()
    assert inner.call_args.kwargs["graph_source"] is source
