# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The two orchestration surfaces must build a fan, not a chain.

A serialized fan runs to completion; it is only slow. So these assert the
SHAPE of the operation graph — the count of `sequential` edges among worker
nodes — rather than that a run succeeds.
"""

from __future__ import annotations

import ast
import inspect
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate import fanout as fanout_mod
from lionagi.cli.orchestrate._common import _build_worker_operate_node
from lionagi.cli.orchestrate.flow import _build_dag, _PlanResult
from lionagi.operations.builder import OperationGraphBuilder

from .test_flow_phases import _FakeBranch, _make_env


def _label_counts(builder: OperationGraphBuilder) -> Counter:
    return Counter(tuple(e.label or []) for e in builder.graph.internal_edges.values())


# flow.py


@pytest.mark.asyncio
async def test_flow_dag_gives_independent_assignments_no_sequential_edges(tmp_path):
    """A plan whose assignments declare no dependencies must build five roots.

    The sixth assignment declares dependencies on all five and is the positive
    control: it keeps its `depends_on` edges, so an assertion here cannot pass
    because the graph came out empty.
    """
    env = _make_env(tmp_path)
    env.builder = OperationGraphBuilder("flow-fan")

    assignments = [TaskAssignment(task=f"independent {i}", assignee="researcher") for i in range(5)]
    assignments.append(
        TaskAssignment(
            task="synthesize",
            assignee="implementer",
            depends_on=["1", "2", "3", "4", "5"],
        )
    )
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=[f"researcher-{i}" for i in range(5)] + ["implementer"],
        dep_indices=[[], [], [], [], [], [0, 1, 2, 3, 4]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(_FakeBranch(), "codex/gpt-5.5", None, False),
    ):
        dag_state = await _build_dag(env, "do stuff", plan_result, reactive_spec="off")

    assert len(dag_state.node_ids) == 6

    counts = _label_counts(env.builder)
    assert counts[("sequential",)] == 0, "dependency-free assignments were chained"
    assert counts[("depends_on",)] == 5, "the declared-dependency control lost its edges"

    # The five independent workers have no incoming edge at all.
    workers = {str(n) for n in dag_state.node_ids[:5]}
    tails = {str(e.tail) for e in env.builder.graph.internal_edges.values()}
    assert tails == {str(dag_state.node_ids[5])}
    assert not (tails & workers)


@pytest.mark.asyncio
async def test_flow_dag_preserves_declared_dependency_chain(tmp_path):
    """A plan that IS a chain still gets its edges — from `depends_on`, not the fallback."""
    env = _make_env(tmp_path)
    env.builder = OperationGraphBuilder("flow-chain")

    assignments = [
        TaskAssignment(task="a", assignee="researcher"),
        TaskAssignment(task="b", assignee="implementer", depends_on=["1"]),
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
        return_value=(_FakeBranch(), "codex/gpt-5.5", None, False),
    ):
        dag_state = await _build_dag(env, "task", plan_result, reactive_spec="off")

    counts = _label_counts(env.builder)
    assert counts[("depends_on",)] == 1
    assert counts[("sequential",)] == 0

    edge = next(iter(env.builder.graph.internal_edges.values()))
    assert str(edge.head) == str(dag_state.node_ids[0])
    assert str(edge.tail) == str(dag_state.node_ids[1])


# fanout.py


def test_fanout_node_construction_builds_a_fan():
    """The shared node-construction helper, called the way fanout.py calls it."""
    builder = OperationGraphBuilder("fanout-fan")
    workers = [
        _build_worker_operate_node(
            builder,
            branch=None,
            depends_on=[],
            instruction=f"worker {i}",
            context=[{"overall_task": "t"}],
            messenger_bound=False,
        )
        for i in range(5)
    ]
    # Positive control: the synthesis node fanout.py adds after the workers.
    synth = builder.add_operation("operate", depends_on=workers, instruction="synthesize")

    counts = _label_counts(builder)
    assert counts[("sequential",)] == 0
    assert counts[("depends_on",)] == 5

    tails = {str(e.tail) for e in builder.graph.internal_edges.values()}
    assert tails == {str(synth)}


def _worker_node_call(module) -> ast.Call:
    """The `_build_worker_operate_node(...)` call in the module's worker loop."""
    tree = ast.parse(Path(inspect.getsourcefile(module)).read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_build_worker_operate_node"
    ]
    assert len(calls) == 1, f"expected one worker-node call in {module.__name__}"
    return calls[0]


def test_fanout_passes_an_explicit_empty_dependency_list():
    """fanout.py must say 'no dependencies', not leave it to the chaining default.

    This reads the call site rather than running the (network-bound) fanout
    loop; the graph shape it produces is covered by the helper test above.
    """
    call = _worker_node_call(fanout_mod)
    kw = {k.arg: k.value for k in call.keywords}
    assert "depends_on" in kw, "fanout.py omits depends_on, so workers chain"
    assert isinstance(kw["depends_on"], ast.List)
    assert kw["depends_on"].elts == []
