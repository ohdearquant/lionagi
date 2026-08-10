# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Does the budget's scheduling model promise more parallelism than the executor delivers?

`op_budget_share` divides a flow's total budget by `schedule_wave_count`, so a
count that lands BELOW the number of stages the executor actually runs hands
every op more time than the flow can afford, and the flow overruns its
deadline. `schedule_wave_count` documents the opposite direction: the real
schedule "can only beat, never lose to" its count when ops take comparable
time. Every other test of that function checks its arithmetic against a
hand-computed number, which cannot see whether the executor agrees. These run
the executor.

The scheduling here is real — the same `flow()` entry point production uses.
Only the work inside an op belongs to the test: a fixed sleep, so the
equal-duration precondition the claim names actually holds. The stage count is
then read off the recorded spans as a longest chain of non-overlapping ops,
which is what the divisor means and which does not vary with how heavy the
executor's per-op overhead happens to be.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from lionagi.cli.orchestrate.flow import schedule_wave_count
from lionagi.operations.flow import flow
from lionagi.operations.node import Operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.session.session import Session

# Long enough that scheduling overhead is a rounding error against it, short
# enough that a dozen stages stay quick.
WORK = 0.05


def _sequential_depth(spans: list[tuple[float, float]]) -> int:
    """Longest run of ops that executed strictly one after another.

    This is the quantity the budget divisor is supposed to be — the function
    under test opens by calling it "how many ops actually end up running one
    after another". Read off the spans as a longest-path, it is independent of
    how long an op takes and of the executor's per-op overhead. Dividing the
    elapsed time by the sleep instead would count that overhead as extra
    stages: a four-op chain measures seven that way, because ~37ms of setup
    per op is most of a 50ms work unit.
    """
    ordered = sorted(spans)
    depths: list[int] = []
    best = 0
    for i, (start_i, _end_i) in enumerate(ordered):
        depth = 1
        for j in range(i):
            _start_j, end_j = ordered[j]
            if end_j <= start_i:
                depth = max(depth, depths[j] + 1)
        depths.append(depth)
        best = max(best, depth)
    return best


def _peak_concurrency(spans: list[tuple[float, float]]) -> int:
    """Most ops in flight at once, by sweeping the start/end edges."""
    edges: list[tuple[float, int]] = []
    for start, end in spans:
        edges.append((start, 1))
        edges.append((end, -1))
    edges.sort()
    peak = current = 0
    for _t, delta in edges:
        current += delta
        peak = max(peak, current)
    return peak


async def _run_real_flow(
    dep_indices: list[list[int]], num_ops: int, max_concurrent: int
) -> tuple[int, int]:
    """Execute this shape on the real executor; return (stages, peak concurrency)."""
    graph = Graph()
    ops = []
    for i in range(num_ops):
        op = Operation(operation="chat", parameters={"idx": i})
        graph.add_node(op)
        ops.append(op)
    for i, deps in enumerate(dep_indices):
        for d in deps:
            # head runs before tail, so the dependency is the head.
            graph.add_edge(Edge(head=ops[d].id, tail=ops[i].id))

    spans: list[tuple[float, float]] = []
    t0 = time.monotonic()

    async def work(**_kwargs):
        start = time.monotonic() - t0
        await asyncio.sleep(WORK)
        spans.append((start, time.monotonic() - t0))
        return "ok"

    # An op that waits on a dependency is run against a CLONE of the branch,
    # so the clone has to route to the same work function. Without this only
    # the dependency-free ops execute, and a stage count taken from that
    # partial run flatters the model instead of testing it.
    def _wire(b):
        b.id = str(uuid4())
        b.chat = AsyncMock(side_effect=work)
        b.get_operation = MagicMock(
            side_effect=lambda operation: b.chat if operation == "chat" else None
        )
        b.clone = MagicMock(side_effect=lambda sender=None: _wire(MagicMock()))
        b._message_manager = MagicMock()
        b._message_manager.pile = MagicMock()
        b._message_manager.pile.clear = MagicMock()
        return b

    branch = _wire(MagicMock())

    session = Session()
    session.branches.include(branch)
    session.default_branch = branch

    result = await flow(session, graph, max_concurrent=max_concurrent, verbose=False)

    # Two independent channels agreeing that the whole graph ran: the
    # executor's own tally, and the work actually performed. A stage count
    # derived from a partial run is meaningless, and it fails toward the
    # model looking correct.
    completed = len(result["completed_operations"])
    assert completed == num_ops, f"executor completed {completed} of {num_ops} ops"
    assert len(spans) == num_ops, f"work ran for {len(spans)} of {num_ops} ops"

    return _sequential_depth(spans), _peak_concurrency(spans)


# --- Instrument controls -------------------------------------------------
#
# Both of these have a stage count that is true by construction rather than by
# measurement. If either misreads, the timing instrument is not fit to judge
# anything below it and a disagreement further down would be an artefact.


@pytest.mark.asyncio
async def test_a_straight_chain_measures_one_stage_per_op():
    stages, peak = await _run_real_flow([[], [0], [1], [2]], 4, 2)
    assert stages == 4
    assert peak == 1  # a chain can never overlap, whatever the cap allows


@pytest.mark.asyncio
async def test_independent_ops_fill_the_cap_and_no_more():
    stages, peak = await _run_real_flow([[], [], [], []], 4, 2)
    assert stages == 2
    assert peak == 2


# --- The claim -----------------------------------------------------------

SHAPES = [
    # (name, dep_indices, num_ops, max_concurrent)
    ("one root feeding three dependents", [[], [0], [0], [0]], 4, 2),
    ("a chain whose ops sort last", [[], [], [], [2], [3]], 5, 2),
    ("a chain competing with independent work", [[], [], [0], [], [], [2]], 6, 2),
    ("two chains sharing a cap of two", [[], [0], [], [2], [1], [3]], 6, 2),
    ("a wide fan-in behind a narrow cap", [[], [], [], [], [0, 1, 2, 3]], 5, 2),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,deps,num_ops,cap", SHAPES)
async def test_the_model_never_promises_more_parallelism_than_the_executor_delivers(
    name, deps, num_ops, cap
):
    """The budget divisor must not come in under the executor's real stage count.

    Measured on the best of three runs. A run that happens to be slowed by
    other load inflates its stage count, so taking the fastest keeps a busy
    machine from manufacturing a failure — and a model that undercounts even
    the executor's best schedule has undercounted.
    """
    model = schedule_wave_count(deps, num_ops, cap)

    observed = []
    for _attempt in range(3):
        stages, peak = await _run_real_flow(deps, num_ops, cap)
        assert peak <= cap, (
            f"{name}: {peak} ops in flight under a cap of {cap} — the cap was not "
            f"enforced, so elapsed time says nothing about how many stages ran"
        )
        observed.append(stages)

    best = min(observed)
    assert best <= model, (
        f"{name}: the executor needed {best} stages (runs: {observed}) where the "
        f"model budgeted for {model}. Each op is handed total/{model} seconds, so "
        f"a flow of this shape overruns its deadline by {best - model} stages."
    )
