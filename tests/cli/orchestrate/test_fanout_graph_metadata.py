# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fanout's wrapper persists terminal graph metadata into Session detail."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from lionagi import Branch
from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate import _orchestration as orchestration_module
from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.operations.flow import DependencyAwareExecutor
from lionagi.state.db import StateDB
from lionagi.studio.operator.run_progress import _node_lanes_by_name
from lionagi.studio.services.sessions import get_session
from lionhub.orchestration.engines import PlanningEngine

from .test_fanout_artifacts import _fanout_env
from .test_fanout_failure_visibility import _completed, _failed


@pytest.mark.parametrize(
    ("worker_count", "with_synthesis", "failure", "role"),
    [
        pytest.param(1, False, None, "worker", id="single-worker"),
        pytest.param(3, False, None, "worker", id="independent-workers"),
        pytest.param(2, True, None, "worker", id="synthesis"),
        pytest.param(2, True, "one-worker", "worker", id="partial-worker-failure"),
        pytest.param(2, True, "all-workers", "worker", id="synthesis-requested-but-skipped"),
        pytest.param(2, True, "synthesis", "worker", id="failed-synthesis"),
        pytest.param(2, True, None, "synthesis", id="worker-role-named-synthesis"),
    ],
)
async def test_fanout_persists_the_executed_graph(
    tmp_path, monkeypatch, worker_count, with_synthesis, failure, role
):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(orchestration_module, "save_last_branch_pointer", Mock(return_value=None))
    env, _, session = _fanout_env(tmp_path)
    env.cwd = str(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Let the wrapper stamp depth, and restore the prior environment afterward.
    monkeypatch.setenv("LIONAGI_AGENT_DEPTH", "0")
    assignments = [
        TaskAssignment(task=f"task {index}", assignee=role) for index in range(worker_count)
    ]
    worker_names = [role, *(f"{role}-{index}" for index in range(2, worker_count + 1))]
    worker_models = [f"codex/model-{index}" for index in range(worker_count)]
    built_names = []

    async def build_worker(env, *, explicit_name, **kwargs):
        model = worker_models[len(built_names)]
        built_names.append(explicit_name)
        branch = Branch(name=explicit_name)
        env.session.include_branches(branch)
        return branch, model, None, False

    async def build_synthesis(env, name="synthesis"):
        branch = Branch(name=name)
        env.session.include_branches(branch)
        return branch

    passes = []
    graph_edges = []
    # The executor's own naming rule, applied without running an executor: a
    # queued signal is named before the node's branch is bound, a started or
    # terminal one after. Every name a node's signals would carry is kept.
    naming = object.__new__(DependencyAwareExecutor)
    signal_names = {}

    def signal_name(node):
        queued, _ = naming._display_name(node)
        terminal, _ = naming._branch_display_name(node, env.session.get_branch(node.branch_id))
        signal_names[str(node.id)] = {queued, terminal}
        return terminal

    async def run_dag(graph, **kwargs):
        assert env._live_persist is not None
        nodes = list(graph.internal_nodes.values())
        passes.append([str(node.id) for node in nodes])
        graph_edges[:] = [(str(edge.head), str(edge.tail)) for edge in graph.internal_edges]
        results = {}
        if len(passes) == 1:
            for index, node in enumerate(nodes):
                failed = failure == "all-workers" or (failure == "one-worker" and index == 0)
                if failed:
                    await _failed(session, node, name=signal_name(node))
                else:
                    response = f"worker {index} result"
                    await _completed(session, node, response, name=signal_name(node))
                    results[node.id] = response
        else:
            node = nodes[-1]
            if failure == "synthesis":
                await _failed(session, node, name=signal_name(node))
            else:
                await _completed(session, node, "integrated result", name=signal_name(node))
                results[node.id] = "integrated result"
        return {"operation_results": results}

    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=assignments))
    monkeypatch.setattr(fanout_module, "available_roles", Mock(return_value=[role]))
    monkeypatch.setattr(fanout_module, "role_roster", Mock(return_value=role))
    monkeypatch.setattr(fanout_module, "mode_roster", Mock(return_value=""))
    monkeypatch.setattr(fanout_module, "build_worker_branch", build_worker)
    fresh_synthesis = AsyncMock(side_effect=build_synthesis)
    monkeypatch.setattr(fanout_module, "_fresh_synthesis_branch", fresh_synthesis)
    engine_run = type("EngineRunStub", (), {"run_dag": staticmethod(run_dag)})()
    monkeypatch.setattr(PlanningEngine, "new_run", Mock(return_value=engine_run))

    # Drive the wrapper's own persistence lifecycle and inner execution. Keep
    # the actual finalizer, teardown, completion gate, and detail reader; stub
    # setup/planning/provider execution and the unrelated last-branch pointer.
    _, terminal_status = await fanout_module._run_fanout(
        "codex/model",
        "work",
        num_workers=worker_count,
        with_synthesis=with_synthesis,
        cwd=str(tmp_path),
        project="test/fanout",
    )
    expected_status = "failed" if failure else "completed"
    assert terminal_status == expected_status
    assert env._live_persist is None

    assert built_names == worker_names
    did_synthesize = with_synthesis and failure != "all-workers"
    assert len(passes) == 1 + int(did_synthesize)
    assert fresh_synthesis.await_count == int(did_synthesize)
    assert len(passes[0]) == worker_count
    expected_agents = [
        {"id": name, "name": name, "model": model}
        for name, model in zip(worker_names, worker_models, strict=True)
    ]
    expected_operations = [
        {"id": name, "agent_id": name, "control": False, "spawned": False, "depends_on": []}
        for name in worker_names
    ]
    expected_edges = []
    if did_synthesize:
        assert len(passes[-1]) == worker_count + 1
        # Workers whose role is "synthesis" hold that name and its numbered
        # forms, so the leg takes the next free one.
        synth_id = f"synthesis-{worker_count + 1}" if role == "synthesis" else "synthesis"
        expected_agents.append({"id": synth_id, "name": synth_id, "model": "codex/model"})
        expected_operations.append(
            {
                "id": synth_id,
                "agent_id": synth_id,
                "control": False,
                "spawned": False,
                "depends_on": worker_names,
            }
        )
        expected_edges = [
            {"id": f"e-{name}-{synth_id}", "source": name, "target": synth_id, "mode": "simple"}
            for name in worker_names
        ]
        assert graph_edges == [(node_id, passes[-1][-1]) for node_id in passes[0]]
    else:
        assert graph_edges == []

    async with StateDB(db_path) as db:
        row = await db.get_session(str(session.id))
    assert row is not None
    assert row["invocation_kind"] == "fanout"
    assert row["status"] == expected_status
    metadata = row["node_metadata"]
    assert metadata["agents"] == expected_agents
    assert metadata["operations"] == expected_operations
    assert metadata["workers"] == worker_models
    assert metadata["synthesis_model"] == ("codex/model" if did_synthesize else None)

    detail = await get_session(str(session.id))
    assert detail["graph"] == {
        "nodes": [
            {
                "id": op["id"],
                "label": op["id"],
                "role": agent["name"],
                "assignment": agent["model"],
                "prompt": "",
                "capacity": 1,
                "timeout": None,
                "inputs": op["depends_on"],
                "outputs": [],
            }
            for op, agent in zip(expected_operations, expected_agents, strict=True)
        ],
        "edges": expected_edges,
    }
    # Studio joins a node's status to the graph node by signal name, so each
    # node's signals carry the one id it is persisted under, and a failed leg,
    # the synthesis leg included, reads as failed.
    assert [signal_names[node_id] for node_id in passes[-1]] == [
        {op["id"]} for op in expected_operations
    ]
    expected_lanes = [
        "failed"
        if failure == "all-workers" or (failure == "one-worker" and index == 0)
        else "succeeded"
        for index in range(worker_count)
    ]
    if did_synthesize:
        expected_lanes.append("failed" if failure == "synthesis" else "succeeded")
    lanes = await _node_lanes_by_name(str(session.id))
    assert [lanes.get(node["id"]) for node in detail["graph"]["nodes"]] == expected_lanes
    # Finalization serializes the builder graph without editing its topology.
    assert [str(node.id) for node in env.builder.get_graph().internal_nodes.values()] == passes[-1]
    assert [
        (str(edge.head), str(edge.tail)) for edge in env.builder.get_graph().internal_edges
    ] == graph_edges
