# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coarse perf smoke gate on the flow executor's per-node scheduling tax.

Regression class guarded: per-node scheduling overhead (dependency
tracking, predecessor lookup, edge-condition checks — independent of the
model call) is invisible in normal test runs since every unit test uses
small graphs; a regression only surfaces once an orchestration run with
hundreds/thousands of nodes gets noticeably slower in production. A prior
optimization pass (adjacency edge lookup, a predecessor cache, an alcall
fast path) roughly halved this cost; nothing previously guarded the floor,
so e.g. an accidental O(V*E) reintroduction in predecessor/edge-condition
lookups could silently undo that win.

This is a ceiling assert, not a benchmark or percentage-based regression
check: it drives a 1000-node linear chain and a 1000-node wide fan-out
through ``Session.flow``/``DependencyAwareExecutor`` with a stubbed,
near-instant ``Branch.chat`` (isolates scheduling overhead from provider
variance) and asserts each shape completes under a generous wall-clock
ceiling. Hosted/shared-host CPU variance has been observed to exceed 20% on
runs like this and previously false-redded a CI perf gate on an unrelated
diff — hence a wide ceiling tuned to catch an order-of-magnitude regression,
not percentage drift. Construction reuses the same path production flows use
(``OperationGraphBuilder`` -> ``Graph`` -> ``Session.flow``).

Ceiling provenance: local medians on this heavily loaded shared dev host
(12 repeats/shape, stubbed chat, max_concurrent=50): linear ~5.8-7.0s median
(worst 17.3s), fan-out ~3.1-3.2s median (worst 14.3s). A quiet,
process-isolated run of the same code recorded linear=718ms / fanout=502ms.
The ceilings below are ~10x the noisy local median, clearing both the quiet
reference and every noisy sample observed here.

The wall-clock ceiling asserts run in the repository's dedicated
performance lane (advisory, outside the required correctness suite) since
they're timing-sensitive to shared-host variance. The scheduling
correctness those ceilings depend on — every node of a linear chain and a
wide fan-out reaches COMPLETED — is asserted separately at a small,
scale-independent size in the required suite, so a scheduling regression
that fails nodes is still caught when the timing lane is advisory.
"""

from __future__ import annotations

import time

import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.flow import flow
from lionagi.operations.node import Operation
from lionagi.protocols.types import EventStatus
from lionagi.session.branch import Branch
from lionagi.session.session import Session

N_NODES = 1000

# A small, scale-independent size for the required-suite correctness checks —
# large enough to exercise real linear + fan-out scheduling, small enough that
# there is no meaningful wall-clock component to be flaky about.
SMALL_N = 25

# ~10x the measured local median on this host (see module docstring).
LINEAR_CEILING_S = 75.0
FANOUT_CEILING_S = 45.0

# Well above either ceiling so a hang/deadlock in the executor fails loud
# instead of wedging the CI job.
TEST_TIMEOUT_S = 180


def _build_linear(n: int) -> OperationGraphBuilder:
    builder = OperationGraphBuilder("linear")
    prev = None
    for i in range(n):
        prev = builder.add_operation(
            "chat", depends_on=[prev] if prev else None, instruction=f"n{i}"
        )
    return builder


def _build_fanout(n: int) -> OperationGraphBuilder:
    """One root, n-1 children all depending directly on the root."""
    builder = OperationGraphBuilder("fanout")
    root = builder.add_operation("chat", instruction="root")
    for i in range(n - 1):
        builder.add_operation("chat", depends_on=[root], instruction=f"leaf{i}")
    return builder


async def _stub_chat(self, **kwargs):
    """Instant coroutine standing in for a real LLM call — zero real work,
    so the measured wall time isolates executor scheduling overhead."""
    return "stub-response"


@pytest.fixture
def stub_branch_chat(monkeypatch):
    monkeypatch.setattr(Branch, "chat", _stub_chat)


async def _run_flow(builder: OperationGraphBuilder, n: int) -> float:
    session = Session()
    graph = builder.get_graph()
    nodes = [node for node in graph.internal_nodes if isinstance(node, Operation)]
    t0 = time.perf_counter()
    result = await flow(session, graph, max_concurrent=50)
    elapsed = time.perf_counter() - t0

    # A count-only check on completed_operations is not enough: the executor
    # records a FAILED operation's id alongside its {"error": ...} result, so
    # a fast-failing stub (e.g. a broken Branch.chat signature) would still
    # produce `len(completed_operations) == n` and a low elapsed time, greening
    # the ceiling assert below without ever exercising successful scheduling.
    # Assert every node actually completed.
    not_completed = [
        (str(node.id)[:8], node.execution.status)
        for node in nodes
        if node.execution.status != EventStatus.COMPLETED
    ]
    assert not not_completed, (
        f"{len(not_completed)}/{n} operations did not reach COMPLETED "
        f"status (first few: {not_completed[:5]}) — the smoke gate must "
        "exercise real successful flow scheduling, not just a node count"
    )
    assert len(result["completed_operations"]) == n
    return elapsed


@pytest.mark.xdist_group(name="flow_perf_smoke")
async def test_linear_flow_completes_all_nodes(stub_branch_chat):
    # Correctness gate (required suite, NOT performance-marked): the executor
    # drives every node of a small linear chain to COMPLETED. No wall-clock
    # assertion — _run_flow already fails if any node is not COMPLETED, so a
    # scheduling regression that fails nodes trips required CI even though the
    # 1000-node timing ceilings below are advisory-only.
    await _run_flow(_build_linear(SMALL_N), SMALL_N)


@pytest.mark.xdist_group(name="flow_perf_smoke")
async def test_fanout_flow_completes_all_nodes(stub_branch_chat):
    # Correctness gate (required suite, NOT performance-marked) — wide fan-out.
    await _run_flow(_build_fanout(SMALL_N), SMALL_N)


@pytest.mark.performance
@pytest.mark.timeout(TEST_TIMEOUT_S)
@pytest.mark.xdist_group(name="flow_perf_smoke")
async def test_linear_flow_1000_nodes_under_ceiling(stub_branch_chat):
    elapsed = await _run_flow(_build_linear(N_NODES), N_NODES)
    assert elapsed < LINEAR_CEILING_S, (
        f"{N_NODES}-node linear flow took {elapsed:.2f}s, exceeding the "
        f"{LINEAR_CEILING_S}s smoke ceiling. This is a coarse gate for an "
        "order-of-magnitude scheduling regression, not a percentage-based "
        "perf check — treat a trip here as a real red flag, not noise."
    )


@pytest.mark.performance
@pytest.mark.timeout(TEST_TIMEOUT_S)
@pytest.mark.xdist_group(name="flow_perf_smoke")
async def test_fanout_flow_1000_nodes_under_ceiling(stub_branch_chat):
    elapsed = await _run_flow(_build_fanout(N_NODES), N_NODES)
    assert elapsed < FANOUT_CEILING_S, (
        f"{N_NODES}-node fan-out flow took {elapsed:.2f}s, exceeding the "
        f"{FANOUT_CEILING_S}s smoke ceiling. This is a coarse gate for an "
        "order-of-magnitude scheduling regression, not a percentage-based "
        "perf check — treat a trip here as a real red flag, not noise."
    )
