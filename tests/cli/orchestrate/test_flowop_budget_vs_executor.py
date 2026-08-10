# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Does the budget's divisor cover every schedule the executor can produce?

`op_budget_share` divides a flow's total budget by `max_sequential_depth`, so a
divisor that lands BELOW the number of ops the executor actually runs in
sequence hands every op more time than the flow can afford, and the flow
overruns its deadline. Every other test of that function checks its arithmetic
against a hand-computed number, which cannot see whether the executor agrees.
These run the executor.

The scheduling here is real: the same `flow()` entry point production uses.
Only the work inside an op belongs to the test, and how long that work takes is
the whole point. An earlier version of this file slept a FIXED amount in every
op, which held the equal-duration assumption the divisor used to be built on,
so it agreed with a divisor that undercounted five of these eight shapes and
read as coverage while doing it. Durations here are deliberately unequal, and
each shape is swept with the slow op in every admission position, because a
long-running op holding a slot is what makes the ops behind it serialize.

The depth is then read off the recorded spans as a longest chain of
non-overlapping ops, which is what the divisor means and which does not vary
with how heavy the executor's per-op overhead happens to be.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from lionagi.cli.orchestrate.flow import max_sequential_depth
from lionagi.operations.flow import flow
from lionagi.operations.node import Operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph
from lionagi.session.session import Session

# Long enough that scheduling overhead is a rounding error against it, short
# enough that a dozen stages stay quick.
WORK = 0.05

# The two work lengths the sweep hands out. The gap between them has to be
# wide enough that a slow op is still holding its slot after several fast ops
# have come and gone, since that queueing is the effect being measured; an
# order of magnitude does it, and leaves the ordering robust on a loaded
# machine.
FAST, SLOW = 0.02, 0.22


def _duration_patterns(num_ops: int) -> list[list[float]]:
    """One slow op in each admission position, plus an all-fast control.

    Which op is slow decides how much serializing happens, and the answer is
    not the same for every position, so the shape is only swept once every
    position has been tried. The all-fast row is the schedule the old fixed
    sleep produced, kept so its result stays visible next to the others.
    """
    patterns = [[FAST] * num_ops]
    for slow_at in range(num_ops):
        patterns.append([SLOW if i == slow_at else FAST for i in range(num_ops)])
    return patterns


def _sequential_depth(spans: list[tuple[float, float]]) -> int:
    """Longest run of ops that executed strictly one after another.

    This is the quantity the budget divisor bounds — the function under test
    opens by calling itself "the most ops that can end up running one after
    another". Read off the spans as a longest-path, it is independent of
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
    dep_indices: list[list[int]],
    num_ops: int,
    max_concurrent: int,
    durations: list[float] | None = None,
) -> tuple[int, int]:
    """Execute this shape on the real executor; return (stages, peak concurrency).

    `durations` assigns work lengths in the order ops are admitted rather than
    by op index, because the point of a slow op is that it occupies a slot,
    and which op holds a slot is a scheduling outcome rather than a property
    of the graph. Omitted, every op takes the same fixed `WORK`.
    """
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
    admitted = 0

    async def work(**_kwargs):
        nonlocal admitted
        length = WORK if durations is None else durations[admitted % len(durations)]
        admitted += 1
        start = time.monotonic() - t0
        await asyncio.sleep(length)
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
    # Equal durations, so this shape runs as two clean pairs. The sweep below
    # gets the same four ops to three deep by making one of them slow, which
    # is the difference a fixed sleep cannot show.
    stages, peak = await _run_real_flow([[], [], [], []], 4, 2)
    assert stages == 2
    assert peak == 2


# --- The claim -----------------------------------------------------------
#
# `expected` is not derived from the function. It is the depth the executor was
# measured reaching on that shape, written down so a change to the divisor has
# to argue with a number rather than silently redefine what it is compared
# against. A test that asked only "does the divisor cover what we just
# measured" would follow the divisor wherever it went.

SWEEP_SHAPES = [
    # (name, dep_indices, num_ops, max_concurrent, expected depth)
    ("four independent ops", [[], [], [], []], 4, 2, 3),
    ("a straight chain of four", [[], [0], [1], [2]], 4, 2, 4),
    ("three independent ops and one dependent", [[], [], [], [0]], 4, 2, 3),
    ("one root feeding three dependents", [[], [0], [0], [0]], 4, 2, 3),
    ("a chain whose ops sort last", [[], [], [], [2], [3]], 5, 2, 4),
    ("a chain competing with independent work", [[], [], [0], [], [], [2]], 6, 2, 5),
    ("two chains sharing a cap of two", [[], [0], [], [2], [1], [3]], 6, 2, 5),
    ("a wide fan-in behind a narrow cap", [[], [], [], [], [0, 1, 2, 3]], 5, 2, 4),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,deps,num_ops,cap,expected", SWEEP_SHAPES)
async def test_the_divisor_covers_the_worst_schedule_without_being_looser(
    name, deps, num_ops, cap, expected
):
    """Two properties, and they fail in opposite directions.

    COVERAGE is the one that matters in production: a divisor below the depth
    the executor reaches hands every op more time than the flow can afford, and
    the flow overruns its own deadline with later ops cancelled part-written.

    TIGHTNESS keeps the first property from being satisfied trivially.
    Returning the op count always covers, and always shrinks every op's budget
    to the serial worst case, so this pins the divisor to the depth actually
    reachable rather than to a safe constant.

    Every duration pattern runs twice and the lower depth is kept. Load on the
    machine can only push apart two ops that would otherwise have overlapped,
    which inflates a depth; nothing makes a depth read low. So the smaller
    reading is the truer one, and a busy machine cannot manufacture either a
    coverage failure or a tightness failure out of noise.
    """
    divisor = max_sequential_depth(deps, num_ops, cap)
    assert divisor == expected, (
        f"{name}: the divisor is now {divisor} where this shape was measured at "
        f"{expected}. If the executor's behaviour changed, re-measure and update "
        f"`expected`; if it did not, the divisor has moved away from reality."
    )

    per_pattern: list[int] = []
    for durations in _duration_patterns(num_ops):
        attempts = []
        for _attempt in range(2):
            stages, peak = await _run_real_flow(deps, num_ops, cap, durations)
            assert peak <= cap, (
                f"{name}: {peak} ops in flight under a cap of {cap} — the cap was "
                f"not enforced, so this run says nothing about sequencing"
            )
            attempts.append(stages)
        per_pattern.append(min(attempts))

    worst = max(per_pattern)
    assert worst <= divisor, (
        f"{name}: the executor ran {worst} ops in sequence (per duration pattern: "
        f"{per_pattern}) where the divisor is {divisor}. Each op is handed "
        f"total/{divisor} seconds, so a flow of this shape overruns its deadline "
        f"by {worst - divisor} ops' worth of budget."
    )
    assert worst == divisor, (
        f"{name}: the divisor is {divisor} but no duration pattern got the "
        f"executor past {worst} (per pattern: {per_pattern}). The budget is being "
        f"divided by more stages than this shape can produce, so every op is told "
        f"it has less time than it really has."
    )
