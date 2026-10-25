# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Planning engine — reactive DAG flow as an Engine (ADR-0034 §4); the library-level backend for ``li o flow``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lionagi._errors import ConfigurationError
from lionagi.casts.emission import SpawnRequest
from lionhub.orchestration import (
    build_dag_graph,
    grant_spawn,
    plan,
    role_node_builder,
)

from .engine import Engine, EngineRun

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

__all__ = ("PlanningEngine", "PlanError")


class PlanError(RuntimeError):
    """Raised when the orchestrator produces an empty TaskAssignment list, so a planning run never silently no-ops."""


# A small default roster the orchestrator may assign to; callers pass their own
# via ``roles=`` for a different set (the CLI uses the full casts ∪ user-profile roster).
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


def _inherit_worker_providers(branch: Branch) -> None:
    """Carry a role template's settings onto the managed clone that will run its task.

    ``Branch.clone`` keeps the template's system prompt but resets the capability
    grant and the branch name, and it does not copy context providers. A clone
    would therefore drop the template's spawn grant, so a fenced
    ``spawn_request`` an agent emits is never extracted, and its role name, so
    khive injection recalls and tags as the generic ``agent``. A clone that has
    no providers of its own takes the template's registry at the template's
    budget, under its own entry accounting.
    """
    source = branch.metadata.get("clone_from")
    if source is None:
        return
    if branch._capabilities is None and source._capabilities is not None:
        branch._capabilities = source._capabilities
    if branch.name is None and source.name:
        branch.name = source.name
    registry = source._context_providers
    if registry is None or not registry or branch._context_providers is not None:
        return
    branch.providers.budget = registry.budget
    for entry in registry._entries:
        branch.providers.register(
            entry.provider, priority=entry.priority, max_tokens=entry.max_tokens, name=entry.name
        )


def _emitter_role_template(roles: dict[str, Branch], session: Any, emitter: Any) -> Branch | None:
    """Resolve the role template a spawn emitter ran from, for an unassigned spawn.

    The planned clone carries its role name, and the clone walks back to its
    template through ``clone_from``; either name resolves to the template, which
    carries the engine's worker settings and no task conversation.
    """
    branch_id = getattr(emitter, "branch_id", None)
    if not branch_id:
        return None
    try:
        emitter_branch = session.branches[branch_id]
    except Exception:
        return None
    name = getattr(emitter_branch, "name", None)
    if name in roles:
        return roles[name]
    source = emitter_branch.metadata.get("clone_from")
    name = getattr(source, "name", None)
    return roles.get(name)


def _role_template_node_builder(roles: dict[str, Branch], session: Any) -> Any:
    """Wrap ``role_node_builder`` so an unassigned spawn keeps the emitter's settings.

    A request with no assignee reaches the executor with no branch of its own and
    no role to route to, so the executor falls back to the bare default branch
    and the spawn loses every engine setting. Naming the emitter's role template
    as the node's branch source makes the executor clone that template instead,
    exactly as a role-targeted spawn already does. That clone carries the
    engine's worker settings and none of the emitter's conversation, which is
    what an independent spawn must not inherit (``Branch.clone`` copies the
    emitter's messages).
    """
    builder = role_node_builder(roles)

    def build(req: Any, emitter: Any) -> Any:
        node = builder(req, emitter)
        # An independent request with no assignee has no role to route to, so the
        # executor would otherwise fall back to the bare default branch — the one
        # spawn kind that loses the engine's worker settings. Name the emitter's
        # role template as the node's own branch source instead. Only for
        # independent requests: a dependent unassigned spawn stays on the
        # emitter's branch, whose conversation is its context.
        # The assignee is read for falsiness, like the role router: an empty
        # assignee is unassigned, so it takes the template branch below rather
        # than the bare default branch.
        if node.branch_id is None and not getattr(req, "assignee", None):
            if getattr(req, "independent", False):
                template = _emitter_role_template(roles, session, emitter)
                if template is not None:
                    node.branch_id = template.id
        return node

    return build


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
        self._refuse_unproven_worker_settings()

    def _refuse_unproven_worker_settings(self) -> None:
        # DAG assignments run on clones of the managed role templates. Their
        # environment/containment provenance is not tracked by EngineRun.run_dag.
        unsupported = [
            name
            for name, value in (
                ("contain_account_integrations", self.contain_account_integrations),
                ("agent_env", self.agent_env),
            )
            if value
        ]
        if unsupported:
            raise ConfigurationError(
                f"PlanningEngine does not support {' or '.join(unsupported)}: managed-DAG "
                "worker clones do not have verified environment/containment provenance."
            )

    # -- lifecycle ------------------------------------------------------------

    async def _run(self, run: EngineRun, prompt: str, *, max_ops: int = 0) -> str:
        """Plan *prompt* into a DAG, execute it (reactively when self.reactive), then synthesize worker outputs."""
        # Checked again here: both are plain attributes a caller can set after construction.
        self._refuse_unproven_worker_settings()
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is empty")

        assignments = await self._plan(run, prompt, max_ops)

        # One role-template branch per distinct assignee; build_dag_graph runs each
        # assignment on a clone. Every assignee role may grow the live DAG when reactive.
        assignees = {ta.assignee for ta in assignments}
        roles = {}
        for assignee in assignees:
            # Exempt: a role template is scaffolding the plan already fixed, not a
            # discretionary agent, so a max_agents at or below the assignee count
            # must not abort the run before any task executes.
            branch = await run.make_agent(assignee, name=assignee, exempt=True)
            if self.reactive:
                grant_spawn(branch, assignees=assignees)
            roles[assignee] = branch

        graph, node_ids = build_dag_graph(run.session, assignments, roles)
        for node in graph.internal_nodes:
            _inherit_worker_providers(run.session.branches[node.branch_id])
        run.notify("executing", assignments=len(assignments))
        result = await run.run_dag(
            graph,
            reactive=self.reactive,
            spawn_type=SpawnRequest if self.reactive else None,
            node_builder=(
                _role_template_node_builder(roles, run.session) if self.reactive else None
            ),
            max_concurrent=max(len(assignments), 1),
            on_branch_created=_inherit_worker_providers,
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
