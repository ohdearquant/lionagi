# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Does the budget's divisor cover every schedule the executor can produce?

`op_budget_share` divides a flow's total budget by `max_sequential_depth`; a
divisor landing BELOW what the executor actually consumes hands every op more
time than the flow can afford, overrunning the deadline with later ops
cancelled part-written. Every other test of that function checks its
arithmetic against a hand-computed number, which cannot see whether the
executor agrees -- these run the executor, through the real `flow()` entry
point production uses. Only the work inside an op belongs to the test.

An earlier version slept a FIXED amount in every op, which held the
equal-duration assumption the divisor was built on and so agreed with a
divisor that undercounted, reading as coverage while doing it. Durations here
are deliberately unequal, and each shape is swept with the long op in every
admission position, since a long-running op holding a slot is what makes the
ops behind it serialize.

Every op sleeps exactly one budget unit in the primary pattern, so elapsed
wall clock divided by that unit is literally the number of op-budgets
consumed -- the quantity the divisor bounds: the flow overruns precisely when
consumption exceeds the divisor.

This replaces an earlier reading that took the longest chain of
non-overlapping spans, which is wrong in both directions. It OVER-counts:
two ops separated by an idle gap are non-overlapping while consuming no
budget in between, so a chain across a gap some third op owns reads as extra
free stages (measured: deps [[], [0], [0], [1], [2]] under a cap of 3 with
one slow op reads a chain of 4, while the same shape at full budget consumes
3). It UNDER-counts: two ops overlapping by half consume one and a half
budgets at a chain length of 1. Because the span reading over-counts, it
would have failed this suite on shapes that do not overrun -- a false alarm
in a test whose whole job is to be believed.

A straight chain consumes exactly one budget per op by construction, so it
also measures this machine's per-op executor overhead in the same units
everything else is reported in. The work unit has to stay well above that
overhead: at 0.15s the ~5ms per-op cost perturbs which ops get admitted
together, so the instrument changes the schedule it is measuring and
readings move by a whole budget between runs. At the unit below, readings
sit within 0.06 of an integer.
"""

import asyncio
import sys
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

# One op-budget. Large enough that per-op executor overhead is a rounding error
# against it (see the module docstring: at half this, it is not).
BUDGET = 0.30

# The short work length in the unequal-duration patterns. An op is free to
# finish early; the budget only promises it will not run longer.
SHORT = BUDGET / 8

# Readings land within ~0.06 of an integer on an idle machine. This absorbs
# executor overhead and ordinary load without absorbing a whole extra op.
TOLERANCE = 0.35


def _duration_patterns(num_ops: int) -> list[tuple[str, list[float]]]:
    """Patterns obeying the only thing a per-op budget promises: no op exceeds it.

    The all-at-budget row is the case where every op actually spends what it was
    given, and it is the worst of these for every shape measured. The others put
    a single full-length op in each admission position with the rest short, which
    is what makes a slot-holder stagger the ops behind it — the effect a fixed
    sleep in every op cannot produce.
    """
    patterns = [("all at budget", [BUDGET] * num_ops)]
    for long_at in range(num_ops):
        patterns.append(
            (
                f"only admission {long_at} at budget",
                [BUDGET if i == long_at else SHORT for i in range(num_ops)],
            )
        )
    return patterns


async def _measure(
    dep_indices: list[list[int]],
    num_ops: int,
    max_concurrent: int,
    durations: list[float] | None = None,
    attempts: int = 3,
) -> tuple[float, int]:
    """Run the shape several times and keep the LOWEST consumption.

    Contention — other tests on the same machine, or a parallel test runner —
    can only push two ops that would have overlapped apart, which inflates a
    reading. Nothing makes a flow consume less than it really does, so the
    smallest reading is the truest one.

    That makes the error one-sided but not bounded: enough load and every one of
    a handful of attempts is inflated, which is a reading above the truth and no
    way to tell it from a real change. Taking the minimum is what makes the
    remedy safe rather than what removes the need for one — see
    `_measure_patiently`.

    Peak concurrency is taken as the MAXIMUM across attempts, since a cap breach
    is a breach whenever it happens.
    """
    best = float("inf")
    peak = 0
    for _ in range(attempts):
        consumed, attempt_peak = await _run_real_flow(
            dep_indices, num_ops, max_concurrent, durations
        )
        best = min(best, consumed)
        peak = max(peak, attempt_peak)
    return best, peak


# Rounds spent before believing a reading that came in HIGH. Only high readings
# buy them, and only once.
PATIENT_ATTEMPTS = 8


async def _measure_patiently(
    dep_indices: list[list[int]],
    num_ops: int,
    max_concurrent: int,
    durations: list[float] | None = None,
    *,
    at_most: float,
    attempts: int = 3,
) -> tuple[float, int]:
    """`_measure`, given more rounds when the first reading lands above `at_most`.

    This is not sampling until the answer is convenient. The error is one-sided:
    a flow cannot consume less than it really does, so more rounds move the
    minimum down toward the truth and never below it. A shape whose consumption
    genuinely changed reads high in every round, however many are spent, which
    is what `test_extra_rounds_do_not_rescue_a_genuinely_slower_shape` holds to.

    Spending the rounds only on high readings keeps the suite's cost where the
    ordinary case is, and a reading that comes in low is already the assertion's
    own answer — re-measuring it could only raise it, which is the direction the
    minimum exists to discard.

    The cost is worth stating plainly, because it is paid by whoever is running
    a loaded machine: a pattern that reads high costs `attempts + PATIENT_ATTEMPTS`
    executions instead of `attempts`, so the sweep's worst case is several times
    its ordinary one. That worst case only arrives when readings are already
    unreliable, which is when the extra rounds are worth their cost.
    """
    consumed, peak = await _measure(dep_indices, num_ops, max_concurrent, durations, attempts)
    if consumed <= at_most:
        return consumed, peak
    patient, patient_peak = await _measure(
        dep_indices, num_ops, max_concurrent, durations, PATIENT_ATTEMPTS
    )
    return min(consumed, patient), max(peak, patient_peak)


async def _run_real_flow(
    dep_indices: list[list[int]],
    num_ops: int,
    max_concurrent: int,
    durations: list[float] | None = None,
) -> tuple[float, int]:
    """Execute this shape on the real executor; return (budgets consumed, peak).

    `durations` assigns work lengths in the order ops are admitted rather than by
    op index, because the point of a long op is that it occupies a slot, and
    which op holds a slot is a scheduling outcome rather than a property of the
    graph.

    Peak concurrency comes from a counter incremented and decremented inside the
    work itself, so it reports ops genuinely in flight. Deriving it from span
    arithmetic instead reports a transient breach at a phase boundary, where one
    op's recorded end and the next op's recorded start differ by microseconds.
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

    finished_at: list[float] = []
    t0 = time.monotonic()
    admitted = 0
    in_flight = 0
    peak = 0

    async def work(**_kwargs):
        nonlocal admitted, in_flight, peak
        length = BUDGET if durations is None else durations[admitted % len(durations)]
        admitted += 1
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(length)
        in_flight -= 1
        finished_at.append(time.monotonic() - t0)
        return "ok"

    # An op that waits on a dependency is run against a CLONE of the branch, so
    # the clone has to route to the same work function. Without this only the
    # dependency-free ops execute, and a reading taken from that partial run
    # flatters the model instead of testing it.
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

    # Two independent channels agreeing that the whole graph ran: the executor's
    # own tally, and the work actually performed. A reading derived from a
    # partial run is meaningless, and it fails toward the model looking correct.
    completed = len(result["completed_operations"])
    assert completed == num_ops, f"executor completed {completed} of {num_ops} ops"
    assert len(finished_at) == num_ops, f"work ran for {len(finished_at)} of {num_ops} ops"

    return max(finished_at) / BUDGET, peak


# Instrument controls.
#
# Both have an answer that is true by construction rather than by measurement.
# If either misreads, the timing instrument is not fit to judge anything below
# it and a disagreement further down would be an artefact.


@pytest.mark.asyncio
async def test_a_straight_chain_consumes_one_budget_per_op():
    """Also prices this machine's overhead, in the units everything else uses."""
    consumed, peak = await _measure_patiently([[], [0], [1], [2]], 4, 2, at_most=4 + TOLERANCE)
    assert peak == 1  # a chain can never overlap, whatever the cap allows
    assert consumed == pytest.approx(4, abs=TOLERANCE), (
        f"a four-op chain consumed {consumed:.2f} budgets where it must consume 4. "
        f"Per-op overhead is {(consumed - 4) / 4:.3f} budgets, which is too much of "
        f"a {BUDGET}s unit for any reading below to mean anything."
    )


@pytest.mark.asyncio
async def test_independent_ops_fill_the_cap_and_no_more():
    consumed, peak = await _measure_patiently([[], [], [], []], 4, 2, at_most=2 + TOLERANCE)
    assert peak == 2
    assert consumed == pytest.approx(2, abs=TOLERANCE)


@pytest.mark.asyncio
async def test_extra_rounds_do_not_rescue_a_genuinely_slower_shape(monkeypatch):
    """The patient re-measure is a better estimator, not a softer gate.

    A shape that really consumes more than its bound reads high in every round,
    so the extra rounds cannot turn it into a pass. Without this, the remedy for
    a load-sensitive timing assertion is indistinguishable from deleting it.
    """
    rounds = 0
    real = _run_real_flow

    async def counted(*args, **kwargs):
        nonlocal rounds
        rounds += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "_run_real_flow", counted)

    # One op that really takes half a budget, held to a fifth of one.
    consumed, _peak = await _measure_patiently([[]], 1, 1, [BUDGET / 2], at_most=0.2, attempts=3)

    assert rounds == 3 + PATIENT_ATTEMPTS, (
        f"the patient path ran {rounds} rounds where it must run {3 + PATIENT_ATTEMPTS}. "
        f"A guard that never reaches the retry says nothing about the retry."
    )
    assert consumed > 0.2, (
        f"{PATIENT_ATTEMPTS} extra rounds pulled a genuinely slow shape down to "
        f"{consumed:.2f} budgets, under a bound of 0.2 — the retry is hiding changes."
    )


@pytest.mark.asyncio
async def test_extra_rounds_do_not_rescue_a_shape_that_is_slow_from_its_cap(monkeypatch):
    """The same guard where the slowness is structural rather than a long op.

    A one-op shape has one schedule, so it cannot say whether extra rounds can
    find a faster ordering of a shape that has orderings to choose between.
    Here two independent ops compete for a single slot, so the executor does
    pick an order, and the two ops carry different work lengths so the orders
    are not interchangeable by inspection.

    They are interchangeable in the reading, and deliberately: work lengths are
    handed out in admission order rather than by op index, so whichever op goes
    first does the short work. The span is the sum either way, which is why the
    minimum across rounds is a noise floor here and not a choice between two
    real answers.
    """
    rounds = 0
    real = _run_real_flow

    async def counted(*args, **kwargs):
        nonlocal rounds
        rounds += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "_run_real_flow", counted)

    # Two independent ops, one slot: a quarter budget and a half, in whichever
    # order they are admitted, so the shape cannot finish inside 0.75 budgets.
    consumed, peak = await _measure_patiently(
        [[], []], 2, 1, [BUDGET / 4, BUDGET / 2], at_most=0.3, attempts=3
    )

    assert rounds == 3 + PATIENT_ATTEMPTS, (
        f"the patient path ran {rounds} rounds where it must run {3 + PATIENT_ATTEMPTS}. "
        f"A guard that never reaches the retry says nothing about the retry."
    )
    assert peak == 1, f"{peak} ops in flight under a cap of 1 — this shape was not serialized"
    assert consumed > 0.3, (
        f"{PATIENT_ATTEMPTS} extra rounds found a {consumed:.2f}-budget ordering of a shape "
        f"the cap forces to take 0.75 — the minimum is picking between schedules, not "
        f"discarding contention."
    )
    assert consumed == pytest.approx(0.75, abs=TOLERANCE), (
        f"the rounds settled on {consumed:.2f} budgets where the cap and the work lengths "
        f"require 0.75; the reading is not measuring what this shape costs."
    )


# The claim.
#
# Neither pinned number is derived from the function under test. `divisor` is
# what the function returns today and `consumed` is what the executor was
# measured spending, so a change to either has to argue with a number rather
# than silently redefine what it is compared against.
#
# The two are NOT equal on every shape, and that is the design rather than a
# defect: the divisor is an upper bound, and overcounting only makes per-op
# budgets conservative while undercounting makes a flow overrun its own
# deadline. Where a shape carries slack, the capacity term bounds a
# serialization that this shape's dependencies never actually force. Pinning
# both columns is what keeps the bound honest — a divisor loosened toward the
# op count reddens the first column, and it cannot be excused by the second.

SWEEP_SHAPES = [
    # (name, dep_indices, num_ops, max_concurrent, divisor, budgets consumed)
    ("four independent ops", [[], [], [], []], 4, 2, 3, 2),
    ("a straight chain of four", [[], [0], [1], [2]], 4, 2, 4, 4),
    ("three independent ops and one dependent", [[], [], [], [0]], 4, 2, 3, 2),
    ("one root feeding three dependents", [[], [0], [0], [0]], 4, 2, 3, 3),
    ("a chain whose ops sort last", [[], [], [], [2], [3]], 5, 2, 4, 4),
    ("a chain competing with independent work", [[], [], [0], [], [], [2]], 6, 2, 5, 4),
    ("two chains sharing a cap of two", [[], [0], [], [2], [1], [3]], 6, 2, 5, 3),
    ("a wide fan-in behind a narrow cap", [[], [], [], [], [0, 1, 2, 3]], 5, 2, 4, 3),
    # Staggered unlocks: each of two mid-layer ops releases a different
    # dependent. With one op long and the rest short the ops finish in an order
    # that looks like a four-deep chain, which is what a span-based reading
    # counted; at a full budget each the flow consumes three.
    ("staggered unlocks under a cap of three", [[], [0], [0], [1], [2]], 5, 3, 3, 3),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,deps,num_ops,cap,divisor,consumed", SWEEP_SHAPES)
async def test_the_divisor_covers_every_schedule_this_shape_can_produce(
    name, deps, num_ops, cap, divisor, consumed
):
    """COVERAGE is the property that matters in production.

    A divisor below what the executor consumes hands every op more time than the
    flow can afford. The sweep runs every duration pattern in which no op exceeds
    its budget — the only thing a per-op budget promises — and takes the worst.
    """
    actual_divisor = max_sequential_depth(deps, num_ops, cap)
    assert actual_divisor == divisor, (
        f"{name}: the divisor is now {actual_divisor} where it was {divisor}. If "
        f"this is a deliberate change, the measured consumption below ({consumed}) "
        f"says whether it is still a bound: at least that, or flows of this shape "
        f"overrun their deadline."
    )

    worst = 0.0
    worst_pattern = ""
    # The bound assertion below fails upward only. The equality against the
    # recorded consumption fails in both directions, which is what keeps the
    # patient re-measure honest: readings pulled too far down redden it just as
    # readings that are too high redden the bound. A pattern buys extra rounds
    # when it lands above the lower of the two bounds it will face, since that
    # is the only direction extra rounds can move it.
    believable = min(divisor, consumed) + TOLERANCE
    for pattern_name, durations in _duration_patterns(num_ops):
        budgets, peak = await _measure_patiently(
            deps, num_ops, cap, durations, at_most=believable, attempts=2
        )
        assert peak <= cap, (
            f"{name}: {peak} ops in flight under a cap of {cap} — the cap was not "
            f"enforced, so this run says nothing about sequencing"
        )
        if budgets > worst:
            worst, worst_pattern = budgets, pattern_name

    assert worst <= divisor + TOLERANCE, (
        f"{name}: the executor consumed {worst:.2f} op-budgets under '{worst_pattern}' "
        f"where the divisor is {divisor}. Each op is handed total/{divisor} seconds, "
        f"so a flow of this shape overruns its deadline."
    )
    assert worst == pytest.approx(consumed, abs=TOLERANCE), (
        f"{name}: the executor consumed {worst:.2f} op-budgets under '{worst_pattern}' "
        f"where this shape was measured at {consumed}. The scheduler's behaviour on "
        f"this shape has changed; re-measure before touching the divisor."
    )
