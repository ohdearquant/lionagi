# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coarse perf smoke gate on the flow executor's per-node scheduling tax.

REGRESSION CLASS this guards against: the executor's per-node scheduling
overhead (the fixed cost of driving one graph node through dependency
tracking, predecessor lookup, and edge-condition checks, independent of the
model call itself) is invisible in normal test runs because every unit test
uses small graphs. A regression here only shows up once an orchestration run
with hundreds or thousands of nodes gets noticeably slower in production. A
recent optimization pass (adjacency edge lookup, a predecessor cache, and an
alcall fast path) roughly halved this per-node cost; nothing previously
guarded the floor, so a future change (e.g. an accidental O(V*E)
reintroduction in predecessor/edge-condition lookups) could silently undo
that win.

This is deliberately a CEILING ASSERT, not a benchmark or a percentage-based
regression check: it drives a 1000-node linear chain and a 1000-node wide
fan-out through ``Session.flow`` / ``DependencyAwareExecutor`` with a stubbed,
near-instant ``Branch.chat`` (no network, no real model latency — isolates
scheduling overhead from provider variance) and asserts each shape completes
under a generous wall-clock ceiling. Hosted/shared-host CPU variance has been
observed to exceed 20% on runs like this and has previously false-redded a
CI perf gate on a diff that was provably unrelated to the hot path, which is
exactly why this gate uses a wide ceiling tuned to catch an
order-of-magnitude regression rather than tracking percentage drift.

Construction reuses the same path production flows use
(``OperationGraphBuilder`` -> ``Graph`` -> ``Session.flow``, with a stubbed
``Branch.chat``), the same approach used by this repo's flow-kernel
micro-benchmark scripts.

Ceiling provenance: local medians measured on this (heavily loaded, shared)
dev host across two independent runs (12 total repeats per shape, stubbed
chat, max_concurrent=50): linear ~5.8-7.0s median (worst single sample
17.3s), fan-out ~3.1-3.2s median (worst single sample 14.3s). A quiet,
process-isolated measurement of this same post-optimization code recorded
linear=718ms / fanout=502ms. The ceilings below are ~10x this host's noisy
local median, which comfortably clears both that quiet-host reference and
every noisy sample observed here.
"""

from __future__ import annotations

import time

import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.operations.flow import flow
from lionagi.session.branch import Branch
from lionagi.session.session import Session

N_NODES = 1000

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
    t0 = time.perf_counter()
    result = await flow(session, graph, max_concurrent=50)
    elapsed = time.perf_counter() - t0
    assert len(result["completed_operations"]) == n
    return elapsed


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
