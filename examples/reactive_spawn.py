"""Reactive spawn — one role injects a new node into a running flow.

Minimal demonstration of self-expanding orchestration: a single "auditor"
node executes, and mid-review it emits a SpawnRequest asking a "researcher"
role to independently investigate an out-of-scope concern it noticed. The
flow engine injects that request as a new node into the still-running DAG —
`spawned_operations` in the result comes back >= 1.

The task instruction below deliberately narrows the auditor's normal review
scope and then explicitly directs it to use its granted spawn capability for
the adjacent concern. This nudge is what makes the spawn fire reliably with
a live model — a generic "look around and spawn if you feel like it" task
does not reliably trigger one (see orchestration_flow.py, which shows the
general multi-role DAG pattern but is not written to force a spawn).

Requires a codex CLI installation and API access.

    uv run python examples/reactive_spawn.py

Production users: don't drive the reactive substrate directly like this —
use lionagi/engines/ (Engine class), which wraps it with an agent budget
cap, a wall-clock deadline, and a JudgeVerdict quality gate before allowing
expansion.
"""

from __future__ import annotations

import asyncio
import logging

from lionagi.agent import AgentSpec
from lionagi.casts.emission import SpawnRequest
from lionagi.operations.node import create_operation
from lionagi.orchestration import role_node_builder, spawn_roles
from lionagi.protocols.graph.graph import Graph
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.session.session import Session

logging.basicConfig(level=logging.WARNING)

MODEL = "codex"

# Narrow the auditor's normal scope, then explicitly direct it to spawn for
# the adjacent concern via its granted capability — this is what makes the
# spawn fire reliably.
INSTRUCTION = """\
Review lionagi/hooks/bus.py ONLY for the StopHook short-circuit correctness in
blocking_emit (does break vs return change observable behavior?). That is the
ENTIRE scope of this review -- do not investigate anything else yourself.

However, while reading the file you will also notice lionagi/session/branch.py
is imported/related and may have its own issues worth a second look -- that is
explicitly OUT OF SCOPE for you. Per your granted capability, emit a
spawn_request JSON block asking the 'researcher' role to independently
investigate lionagi/session/branch.py's emit()/_schedule_emit() path for
correctness, with independent=true. Do this in addition to your normal review
of bus.py -- do not skip the spawn just because you narrated the concern in
prose.
"""


async def main():
    # ── Build model + session ────────────────────────────────────────────
    chat_model = iModel(
        provider="codex",
        model="gpt-5.3-codex-spark",
        api_key="dummy",
        full_auto=True,
        skip_git_repo_check=True,
        reasoning_effort="medium",
    )
    session = Session()
    orc = Branch(chat_model=chat_model)
    session.include_branches(orc)
    session.default_branch = orc
    print(f"Session {str(session.id)[:8]}")

    # ── Compose roles; only the auditor is granted spawn capability ───────
    roles = await spawn_roles(
        session,
        {
            "auditor": AgentSpec.compose("auditor", model=MODEL, yolo=True),
            "researcher": AgentSpec.compose("researcher", model=MODEL, yolo=True),
        },
        spawners={"auditor"},
    )
    print(f"Roles: {list(roles.keys())}")

    # ── Single-node graph — the second node is injected reactively ────────
    graph = Graph()
    node = create_operation("operate", parameters={"instruction": INSTRUCTION})
    node.branch_id = roles["auditor"].id
    graph.add_node(node)

    print("Executing single-node reactive flow...")
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
    print(f"\nCompleted: {len(op_results)} results, {spawned} reactive spawns")
    print("=" * 70)
    for nid, res in op_results.items():
        text = str(res)[:400].replace("\n", " ")
        print(f"\n  [{str(nid)[:8]}] {text}")
    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
