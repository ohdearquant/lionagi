"""Orchestration flow — multi-agent DAG with reactive spawning.

The full orchestration pipeline: compose agent roles from casts at runtime,
have an orchestrator plan a dependency DAG of TaskAssignments, build the
graph, and execute it with reactive self-expansion (workers can spawn new
nodes into the running DAG).

Requires a codex CLI installation and API access.

    uv run python examples/orchestration_flow.py
"""

from __future__ import annotations

import asyncio
import logging

from lionagi.agent import AgentSpec
from lionagi.casts.emission import SpawnRequest
from lionagi.hooks import HookBus, HookPoint, HookSignal
from lionagi.orchestration import (
    build_dag_graph,
    plan,
    role_node_builder,
    spawn_roles,
)
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.session.session import Session

logging.basicConfig(level=logging.WARNING)

MODEL = "codex"

# Swap this task for your own — the orchestration machinery is the same.
TASK = """\
Review lionagi/hooks/bus.py for two concerns:
(1) Is the StopHook short-circuit in blocking_emit correct? Does break vs
    return change observable behavior?
(2) Can exceptions from _record() leak to callers, or are they properly
    isolated?

The auditor inspects each concern with code evidence.
The critic challenges the auditor's findings for gaps or false positives.
The synthesizer produces a final verdict with severity ratings.
"""


async def main():
    # ── Build model + session ────────────────────────────────────────────
    chat_model = iModel(
        provider="codex",
        model="gpt-5.3-codex-spark",
        api_key="dummy",
        full_auto=True,
        skip_git_repo_check=True,
        reasoning_effort="low",
    )
    session = Session()
    orc = Branch(chat_model=chat_model)
    session.include_branches(orc)
    session.default_branch = orc
    print(f"Session {str(session.id)[:8]}, orchestrator on codex-spark")

    # ── Instrument with hooks ────────────────────────────────────────────
    bus = HookBus(observer=session.observer)
    session.observe(HookSignal, handler=lambda s, _c: None)  # record silently
    await bus.emit(HookPoint.SESSION_START, session_id=str(session.id))

    # ── Compose roles from casts ─────────────────────────────────────────
    role_names = ["auditor", "critic", "synthesizer"]
    roles = await spawn_roles(
        session,
        {name: AgentSpec.compose(name, model=MODEL, yolo=True) for name in role_names},
        spawners=set(role_names),
    )
    print(f"Roles: {list(roles.keys())}")

    # ── Plan (orchestrator decomposes task into assignments) ──────────────
    assignments = await plan(orc, TASK, roles=list(roles.keys()))
    print(f"\nPlan: {len(assignments)} assignments")
    for i, a in enumerate(assignments, 1):
        print(f"  [{i}] {a.assignee}: {a.task[:80]}…  deps={a.depends_on}")

    # ── Build DAG and execute ────────────────────────────────────────────
    graph, node_ids = build_dag_graph(session, assignments, roles)
    print(f"\nExecuting DAG ({len(graph.internal_nodes)} nodes, reactive)…")
    results = await session.flow(
        graph,
        reactive=True,
        spawn_type=SpawnRequest,
        node_builder=role_node_builder(roles),
        max_spawn=5,
        max_concurrent=3,
    )

    # ── Results ──────────────────────────────────────────────────────────
    op_results = results.get("operation_results", {})
    spawned = results.get("spawned_operations", 0)
    await bus.emit(HookPoint.SESSION_END, session_id=str(session.id))

    print(f"\nCompleted: {len(op_results)} results, {spawned} reactive spawns")
    print("=" * 70)
    for nid, res in op_results.items():
        text = str(res)[:200].replace("\n", " ")
        print(f"\n  [{str(nid)[:8]}] {text}")
    print("\n" + "=" * 70)

    signals = session.observer.by_type(HookSignal)
    total = len(list(session.observer.flow.items))
    print(f"\nObserver: {total} items total, {len(signals)} HookSignals")


if __name__ == "__main__":
    asyncio.run(main())
