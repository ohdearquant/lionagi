# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""PlanningEngine — plan retry/loud-fail, synthesis wiring, reactive params. No LLM."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lionagi.casts.emission import SpawnRequest, TaskAssignment
from lionagi.engines.planning import PlanError, PlanningEngine


async def _fake_make(role, **kw):
    return SimpleNamespace(name=role)


@pytest.mark.asyncio
async def test_plan_retries_once_then_succeeds(monkeypatch):
    """An empty first plan triggers exactly one reinforced retry."""
    eng = PlanningEngine()
    run = eng.new_run()
    run.make_agent = _fake_make
    calls = {"n": 0}

    async def fake_plan(orchestrator, prompt, *, roles, dag, max_tasks, guidance=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        return [TaskAssignment(task="dig", assignee="researcher")]

    monkeypatch.setattr("lionagi.engines.planning.plan", fake_plan)
    assignments = await eng._plan(run, "task", 0)
    assert calls["n"] == 2
    assert assignments[0].assignee == "researcher"


@pytest.mark.asyncio
async def test_plan_empty_after_retry_raises(monkeypatch):
    """Both attempts empty → PlanError (never a silent no-op)."""
    eng = PlanningEngine()
    run = eng.new_run()
    run.make_agent = _fake_make

    async def fake_plan(*a, **kw):
        return []

    monkeypatch.setattr("lionagi.engines.planning.plan", fake_plan)
    with pytest.raises(PlanError, match="no usable plan"):
        await eng._plan(run, "task", 0)


@pytest.mark.asyncio
async def test_synthesize_reads_worker_outputs():
    """Each worker's DAG output is folded into the synthesizer's instruction."""
    eng = PlanningEngine()
    run = eng.new_run()
    captured: dict = {}

    class FakeSynth:
        name = "synthesizer"

        async def operate(self, *, instruction):
            captured["instruction"] = instruction
            return "DELIVERABLE"

    async def fake_make(role, **kw):
        return FakeSynth()

    run.make_agent = fake_make
    assignments = [
        TaskAssignment(task="a", assignee="researcher"),
        TaskAssignment(task="b", assignee="architect"),
    ]
    node_ids = ["n1", "n2"]
    result = {"operation_results": {"n1": "found X", "n2": "designed Y"}}

    out = await eng._synthesize(run, "the goal", assignments, node_ids, result)
    assert out == "DELIVERABLE"
    assert "found X" in captured["instruction"]
    assert "designed Y" in captured["instruction"]
    assert "the goal" in captured["instruction"]


@pytest.mark.asyncio
async def test_synthesize_skips_dropped_nodes():
    """A None node id (dropped unknown-assignee assignment) is skipped, not crashed."""
    eng = PlanningEngine()
    run = eng.new_run()

    class FakeSynth:
        name = "synthesizer"

        async def operate(self, *, instruction):
            return "OUT"

    async def fake_make(role, **kw):
        return FakeSynth()

    run.make_agent = fake_make
    assignments = [TaskAssignment(task="a", assignee="researcher")]
    out = await eng._synthesize(run, "g", assignments, [None], {"operation_results": {}})
    assert out == "OUT"


@pytest.mark.asyncio
async def test_run_wires_plan_build_execute_synth(monkeypatch):
    """_run threads plan → spawn_roles → build_dag_graph → run_dag → synthesize,
    and reactive=True propagates SpawnRequest + a node_builder into run_dag."""
    eng = PlanningEngine(reactive=True)
    run = eng.new_run()

    assignments = [TaskAssignment(task="x", assignee="researcher")]
    captured: dict = {}

    async def fake_plan(_run, prompt, max_ops):
        captured["planned"] = prompt
        return assignments

    async def fake_spawn_roles(session, specs, *, spawners=()):
        captured["spawners"] = spawners
        return {"researcher": SimpleNamespace(name="researcher")}

    def fake_build_dag_graph(session, asg, roles):
        return ("GRAPH", ["n1"])

    def fake_role_node_builder(roles):
        return "NODE_BUILDER"

    async def fake_run_dag(graph, **kw):
        captured["run_dag"] = {"graph": graph, **kw}
        return {"operation_results": {"n1": "did x"}}

    async def fake_synth(_run, prompt, asg, node_ids, result):
        captured["synth_result"] = result
        return "FINAL"

    eng._plan = fake_plan
    eng._synthesize = fake_synth
    run.run_dag = fake_run_dag
    monkeypatch.setattr("lionagi.engines.planning.spawn_roles", fake_spawn_roles)
    monkeypatch.setattr("lionagi.engines.planning.build_dag_graph", fake_build_dag_graph)
    monkeypatch.setattr("lionagi.engines.planning.role_node_builder", fake_role_node_builder)

    out = await eng._run(run, "do the work")
    assert out == "FINAL"
    assert captured["planned"] == "do the work"
    assert captured["spawners"] == ("researcher",)  # reactive → assignees may spawn
    assert captured["run_dag"]["graph"] == "GRAPH"
    assert captured["run_dag"]["reactive"] is True
    assert captured["run_dag"]["spawn_type"] is SpawnRequest
    assert captured["run_dag"]["node_builder"] == "NODE_BUILDER"
    assert captured["synth_result"]["operation_results"] == {"n1": "did x"}


@pytest.mark.asyncio
async def test_run_non_reactive_disables_spawn(monkeypatch):
    """reactive=False → no spawners, no spawn_type, no node_builder."""
    eng = PlanningEngine(reactive=False)
    run = eng.new_run()
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    captured: dict = {}

    async def fake_plan(_run, prompt, max_ops):
        return assignments

    async def fake_spawn_roles(session, specs, *, spawners=()):
        captured["spawners"] = spawners
        return {"researcher": SimpleNamespace(name="researcher")}

    async def fake_run_dag(graph, **kw):
        captured["run_dag"] = kw
        return {"operation_results": {}}

    async def fake_synth(*a, **kw):
        return "FINAL"

    eng._plan = fake_plan
    eng._synthesize = fake_synth
    run.run_dag = fake_run_dag
    monkeypatch.setattr("lionagi.engines.planning.spawn_roles", fake_spawn_roles)
    monkeypatch.setattr("lionagi.engines.planning.build_dag_graph", lambda *a: ("G", ["n1"]))

    await eng._run(run, "task")
    assert captured["spawners"] == ()
    assert captured["run_dag"]["reactive"] is False
    assert captured["run_dag"]["spawn_type"] is None
    assert captured["run_dag"]["node_builder"] is None
