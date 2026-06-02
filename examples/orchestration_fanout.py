"""Orchestration fanout — parallel workers with synthesis.

The fanout pattern runs N workers in parallel on independent tasks,
then optionally synthesizes their outputs. Unlike the DAG flow, there
is no dependency graph — all workers start immediately.

This example uses manual TaskAssignment objects instead of the
orchestrator's plan() function, showing the explicit API.

Requires a codex CLI installation and API access.

    uv run python examples/orchestration_fanout.py
"""

from __future__ import annotations

import asyncio
import logging

from lionagi.agent import AgentSpec
from lionagi.casts.emission import TaskAssignment
from lionagi.orchestration import fanout, spawn_roles
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.session.session import Session

logging.basicConfig(level=logging.WARNING)

MODEL = "codex"


async def main():
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
    print(f"Session {str(session.id)[:8]}")

    roles = await spawn_roles(
        session,
        {
            "researcher": AgentSpec.compose("researcher", model=MODEL, yolo=True),
            "reviewer": AgentSpec.compose("reviewer", model=MODEL, yolo=True),
            "synthesizer": AgentSpec.compose("synthesizer", model=MODEL, yolo=True),
        },
    )
    print(f"Roles: {list(roles.keys())}")

    # ── Manual assignments (no orchestrator planning) ────────────────────
    assignments = [
        TaskAssignment(
            task="List all public methods on lionagi.session.branch.Branch. "
            "Categorize each as: LLM-calling, state-management, or configuration.",
            assignee="researcher",
        ),
        TaskAssignment(
            task="Review lionagi.session.session.Session. How does flow() work? "
            "What is the reactive execution path? Trace the code.",
            assignee="researcher",
        ),
        TaskAssignment(
            task="Review the lionagi.casts module (pattern.py, profile.py, pack.py, emission.py). "
            "How do Role, Mode, Profile, and Pack compose? Is the algebra sound?",
            assignee="reviewer",
        ),
    ]
    print(f"\n{len(assignments)} assignments (manual, no planning step)")

    results = await fanout(
        session,
        assignments,
        roles,
        synthesis_role="synthesizer",
        max_concurrent=3,
    )

    op_results = results.get("operation_results", {})
    print(f"\nCompleted: {len(op_results)} results (3 workers + 1 synthesis)")
    print("=" * 70)
    for nid, res in op_results.items():
        text = str(res)[:200].replace("\n", " ")
        print(f"\n  [{str(nid)[:8]}] {text}")
    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
