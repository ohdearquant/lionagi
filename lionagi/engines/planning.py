# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Planning engine — reactive DAG flow as an Engine (ADR-0075 §4); the library-level backend for ``li o flow``."""

from __future__ import annotations

from typing import Any

from lionagi.casts.emission import SpawnRequest
from lionagi.orchestration import (
    build_dag_graph,
    plan,
    role_node_builder,
    spawn_roles,
)

from .engine import Engine, EngineRun

__all__ = ("PlanningEngine", "PlanError")


class PlanError(RuntimeError):
    """Raised when the orchestrator produces an empty TaskAssignment list, so a planning run never silently no-ops."""


# A small default roster the orchestrator may assign to. Callers pass their own
# via ``roles=`` when they want a different set (the CLI uses the full casts ∪
# user-profile roster).
_DEFAULT_ROLES: tuple[str, ...] = ("researcher", "analyst", "critic", "architect", "synthesizer")


def _synthesis_instruction(prompt: str, outputs: list[str]) -> str:
    body = "\n\n".join(outputs) if outputs else "(no worker output)"
    return (
        "Synthesize the worker outputs below into a single cohesive deliverable.\n\n"
        f"Original task: {prompt}\n\n"
        f"# Worker outputs\n{body}\n\n"
        "Reconcile disagreements with evidence, name gaps no worker covered, and "
        "organize by theme — not by which worker produced what."
    )


class PlanningEngine(Engine):
    """Plan-then-execute engine over the reactive DAG executor (stateless config). See docs/reference/engines.md for parameter details."""

    def __init__(
        self,
        *,
        orchestrator_role: str = "orchestrator",
        roles: tuple[str, ...] = _DEFAULT_ROLES,
        synthesis_role: str = "synthesizer",
        reactive: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.orchestrator_role = orchestrator_role
        self.roles = roles
        self.synthesis_role = synthesis_role
        self.reactive = reactive

    # -- lifecycle ------------------------------------------------------------

    async def _run(self, run: EngineRun, prompt: str, *, max_ops: int = 0) -> str:
        """Plan *prompt* into a DAG, execute it (reactively when self.reactive), then synthesize worker outputs."""
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is empty")

        assignments = await self._plan(run, prompt, max_ops)

        # One role-template branch per distinct assignee; build_dag_graph runs
        # each assignment on a clone. Every assignee role may grow the live DAG
        # when reactive.
        assignees = {ta.assignee for ta in assignments}
        spawners = tuple(assignees) if self.reactive else ()
        roles = await spawn_roles(run.session, {a: a for a in assignees}, spawners=spawners)

        graph, node_ids = build_dag_graph(run.session, assignments, roles)
        run.notify("executing", assignments=len(assignments))
        result = await run.run_dag(
            graph,
            reactive=self.reactive,
            spawn_type=SpawnRequest if self.reactive else None,
            node_builder=role_node_builder(roles) if self.reactive else None,
            max_concurrent=max(len(assignments), 1),
            verbose=False,
        )
        return await self._synthesize(run, prompt, assignments, node_ids, result)

    # -- stages ---------------------------------------------------------------

    async def _plan(self, run: EngineRun, prompt: str, max_ops: int) -> list:
        """Decompose *prompt* into TaskAssignment list; retries once with explicit guidance, then raises PlanError."""
        orchestrator = await run.make_agent(self.orchestrator_role, name="orchestrator")
        roster = list(self.roles)
        assignments = await plan(orchestrator, prompt, roles=roster, dag=True, max_tasks=max_ops)
        if not assignments:
            assignments = await plan(
                orchestrator,
                prompt,
                roles=roster,
                dag=True,
                max_tasks=max_ops,
                guidance="Return ONLY the assignments list — do not perform the task.",
            )
        if not assignments:
            raise PlanError(
                "orchestrator produced no usable plan (empty assignment list) after a retry"
            )
        return assignments

    async def _synthesize(
        self, run: EngineRun, prompt: str, assignments: list, node_ids: list, result: dict
    ) -> str:
        """Collect worker outputs from the DAG result and synthesize a single cohesive deliverable."""
        op_results = result.get("operation_results", {})
        outputs: list[str] = []
        for ta, nid in zip(assignments, node_ids, strict=True):
            if nid is None:
                continue
            res = op_results.get(nid)
            outputs.append(f"## {ta.assignee}\n{res if res is not None else '(no output)'}")
        run.notify("synthesizing", outputs=len(outputs))
        synth = await run.make_agent(
            self.synthesis_role,
            name="synthesizer",
            model=self.model_for("synthesize"),
            exempt=True,
        )
        res = await synth.operate(instruction=_synthesis_instruction(prompt, outputs))
        return str(res) if res is not None else ""
