# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Orchestration patterns over lionagi primitives.

This is thin glue, not a parallel stack. Roles are casts ``AgentSpec``s wired
into a ``Session`` as branches; a plan is a list of ``TaskAssignment`` emissions;
execution is ``session.flow`` (optionally ``reactive=True`` so workers may grow
the DAG by emitting ``SpawnRequest``). The orchestration layer only supplies:

1. ``role_node_builder`` — maps a SpawnRequest's ``assignee`` to an operate node
   on that role's branch (the flow-layer executor stays role-agnostic).
2. ``spawn_roles`` — build role branches from specs and grant spawn rights.
3. ``fanout`` — wire N assignments + an optional synthesis into a graph and run.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from lionagi import FieldModel
from lionagi.agent import AgentSpec, create_agent
from lionagi.casts.emission import (
    _SPAWN_ALLOWED_OPERATIONS,
    SpawnRequest,
    TaskAssignment,
    build_emission_operable,
)
from lionagi.operations.node import Operation, create_operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.graph.graph import Graph

from .prompts import (
    DECOMPOSE_DAG_INSTRUCTION,
    DECOMPOSE_DISCIPLINE,
    DECOMPOSE_INSTRUCTION,
    SYNTHESIS_INSTRUCTION,
)

if TYPE_CHECKING:
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

logger = logging.getLogger(__name__)

__all__ = (
    "grant_spawn",
    "role_node_builder",
    "spawn_roles",
    "plan",
    "build_fanout_graph",
    "build_dag_graph",
    "fanout",
)

# The orchestrator's plan IS a list[TaskAssignment] — no bespoke plan model.
# Named "assignments" so a parsed operate() result exposes ``res.assignments``.
_ASSIGNMENTS_FIELD = FieldModel(list[TaskAssignment], name="assignments")


def grant_spawn(branch: Branch, *, prompt: bool = True) -> None:
    """Let an agent grow the live DAG by emitting a ``SpawnRequest``.

    Grants the SpawnRequest capability (and, when *prompt*, injects the
    capability instruction block so the model knows it may emit one).
    """
    branch.grant_capabilities(build_emission_operable((SpawnRequest,), name="spawn"), prompt=prompt)


def role_node_builder(roles: dict[str, Branch]):
    """Build a ``node_builder`` that routes a SpawnRequest to a role's branch.

    The flow-layer ``ReactiveExecutor`` is role-agnostic; this closure gives it
    the role→branch map. The executor clones the referenced branch per injected
    node, so concurrent spawns of the same role do not collide.
    """

    def build(req: SpawnRequest, emitter: Operation) -> Operation:
        # Defense-in-depth: validate the operation at the routing boundary even
        # though SpawnRequest.operation is already typed as a Literal. Custom
        # operation names registered on a session branch must NOT be reachable
        # via model-emitted spawn requests. Fail closed on anything outside the
        # documented allowlist.
        op = req.operation or "operate"
        if op not in _SPAWN_ALLOWED_OPERATIONS:
            logger.warning(
                "SpawnRequest.operation %r is outside the allowed set %r — "
                "falling back to 'operate'. (Possible prompt injection.)",
                op,
                sorted(_SPAWN_ALLOWED_OPERATIONS),
            )
            op = "operate"
        node = create_operation(
            op,
            parameters={"instruction": req.instruction},
        )
        target = roles.get(req.assignee) if req.assignee else None
        if target is not None:
            node.branch_id = target.id
        return node

    return build


async def spawn_roles(
    session: Session,
    specs: dict[str, AgentSpec | str],
    *,
    spawners: tuple[str, ...] | set[str] = (),
) -> dict[str, Branch]:
    """Create one Branch per role spec and wire them into *session*.

    Args:
        session: Session the role branches join.
        specs: ``{role_name: AgentSpec | role_name_str}``. A bare string is
            composed via ``AgentSpec.compose(name)``.
        spawners: role names granted the SpawnRequest capability (allowed to
            grow the DAG mid-run).

    Returns:
        ``{role_name: Branch}`` — the role templates. Workers run on clones.
    """
    roles: dict[str, Branch] = {}
    spawn_set = set(spawners)
    for name, spec in specs.items():
        if isinstance(spec, str):
            spec = AgentSpec.compose(spec)
        branch = await create_agent(spec, load_settings=False)
        branch.name = name
        session.include_branches(branch)
        if name in spawn_set:
            grant_spawn(branch)
        roles[name] = branch
    return roles


async def plan(
    orchestrator: Branch,
    prompt: str,
    *,
    roles: list[str] | set[str],
    dag: bool = True,
    guidance: str = "",
    max_tasks: int = 0,
    context: dict | None = None,
) -> list[TaskAssignment]:
    """Have *orchestrator* decompose *prompt* into a list of TaskAssignments.

    The plan is the casts coordination emission — a ``list[TaskAssignment]`` —
    not a bespoke model. ``assignee`` values outside *roles* are dropped (the
    orchestrator was told to use only the roster). When ``dag`` the orchestrator
    is asked to wire ``depends_on`` (1-based step indices); otherwise all
    assignments are independent (fanout).

    Returns the validated assignments (capped at ``max_tasks`` when > 0).
    """
    instruction = DECOMPOSE_DAG_INSTRUCTION if dag else DECOMPOSE_INSTRUCTION
    res = await orchestrator.operate(
        instruction=instruction,
        context={"task": prompt, **(context or {})},
        guidance=f"{guidance} {DECOMPOSE_DISCIPLINE}".strip(),
        field_models=[_ASSIGNMENTS_FIELD],
        reason=True,
    )
    raw = list(getattr(res, "assignments", None) or [])
    known = set(roles)
    valid: list[TaskAssignment] = []
    for ta in raw:
        if ta.assignee not in known:
            logger.warning("plan: dropping assignment with unknown assignee %r", ta.assignee)
            continue
        valid.append(ta)
    if max_tasks and len(valid) > max_tasks:
        logger.warning("plan: truncating %d assignments to max_tasks=%d", len(valid), max_tasks)
        valid = valid[:max_tasks]
    return valid


def _resolve_dep_indices(assignments: list[TaskAssignment]) -> dict[int, list[int]]:
    """Map each assignment index → list of 0-based predecessor indices.

    ``depends_on`` entries are 1-based step numbers (the orchestrator numbers
    assignments by list position). Out-of-range, self, and non-integer refs are
    dropped — the executor re-validates acyclicity as defense in depth.
    """
    deps: dict[int, list[int]] = {}
    n = len(assignments)
    for i, ta in enumerate(assignments):
        preds: list[int] = []
        for ref in ta.depends_on or []:
            try:
                j = int(str(ref).strip()) - 1
            except (TypeError, ValueError):
                logger.warning("build_dag_graph: non-integer depends_on %r on step %d", ref, i + 1)
                continue
            if j == i or not (0 <= j < n):
                logger.warning(
                    "build_dag_graph: dropping out-of-range dep %r on step %d", ref, i + 1
                )
                continue
            preds.append(j)
        deps[i] = preds
    return deps


def build_fanout_graph(
    session: Session,
    assignments: list[TaskAssignment],
    roles: dict[str, Branch],
    *,
    synthesis_role: str | None = None,
) -> tuple[Graph, list[str]]:
    """Wire assignments into a parallel fanout graph (+ optional synthesis).

    Each assignment becomes an independent ``operate`` node on a *clone* of its
    assignee's role branch (clone = isolated history, shared model backend). A
    synthesis node, if requested, aggregates all workers.

    Returns ``(graph, worker_node_ids)``. Pure — does not execute.
    """
    graph = Graph()
    worker_ids: list[str] = []
    workers: list[Operation] = []

    for ta in assignments:
        template = roles.get(ta.assignee)
        if template is None:
            logger.warning(
                "fanout: no role branch for assignee %r; skipping task %r",
                ta.assignee,
                ta.task[:60],
            )
            continue
        worker_branch = template.clone(sender=session.id)
        session.include_branches(worker_branch)

        node = create_operation("operate", parameters={"instruction": ta.task})
        node.branch_id = worker_branch.id
        graph.add_node(node)
        workers.append(node)
        worker_ids.append(node.id)

    if not workers:
        raise ValueError("fanout: no assignments mapped to a known role")

    if synthesis_role and synthesis_role in roles:
        synth_branch = roles[synthesis_role].clone(sender=session.id)
        session.include_branches(synth_branch)
        synth = create_operation(
            "operate",
            parameters={"instruction": SYNTHESIS_INSTRUCTION},
        )
        synth.branch_id = synth_branch.id
        synth.metadata["aggregation"] = True
        synth.metadata["aggregation_sources"] = [str(w.id) for w in workers]
        synth.metadata["aggregation_count"] = len(workers)
        graph.add_node(synth)
        for w in workers:
            graph.add_edge(Edge(head=w.id, tail=synth.id, label=["aggregate"]))

    return graph, worker_ids


def build_dag_graph(
    session: Session,
    assignments: list[TaskAssignment],
    roles: dict[str, Branch],
) -> tuple[Graph, list[str | None]]:
    """Wire assignments into a dependency DAG (honours ``depends_on``).

    Like :func:`build_fanout_graph`, each assignment runs on a *clone* of its
    assignee's role branch. Unlike fanout, ``depends_on`` (1-based step indices)
    becomes graph edges, so independent assignments parallelize while dependent
    ones inherit their predecessors' output as upstream context.

    Returns ``(graph, node_ids)`` where ``node_ids[i]`` is the node for
    ``assignments[i]`` (``None`` for a dropped unknown-assignee step). Pure —
    does not execute.
    """
    graph = Graph()
    deps = _resolve_dep_indices(assignments)
    nodes: list[Operation | None] = []

    for ta in assignments:
        template = roles.get(ta.assignee)
        if template is None:
            logger.warning(
                "build_dag_graph: no role branch for assignee %r; skipping task %r",
                ta.assignee,
                ta.task[:60],
            )
            nodes.append(None)
            continue
        worker_branch = template.clone(sender=session.id)
        session.include_branches(worker_branch)
        node = create_operation("operate", parameters={"instruction": ta.task})
        node.branch_id = worker_branch.id
        graph.add_node(node)
        nodes.append(node)

    if not any(nodes):
        raise ValueError("build_dag_graph: no assignments mapped to a known role")

    for i, node in enumerate(nodes):
        if node is None:
            continue
        for j in deps[i]:
            pred = nodes[j]
            if pred is not None:
                graph.add_edge(Edge(head=pred.id, tail=node.id, label=["depends_on"]))

    return graph, [n.id if n is not None else None for n in nodes]


async def fanout(
    session: Session,
    assignments: list[TaskAssignment],
    roles: dict[str, Branch],
    *,
    synthesis_role: str | None = None,
    reactive: bool = False,
    max_concurrent: int | None = None,
    max_spawn: int = 50,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run assignments in parallel on their role branches; optionally synthesize.

    When *reactive*, workers granted spawn rights (see :func:`spawn_roles`) may
    emit a ``SpawnRequest`` to add work to the running DAG without halting.

    Returns the ``session.flow`` result dict (``operation_results``,
    ``completed_operations``, ``final_context``, and — when reactive —
    ``spawned_operations``).
    """
    graph, worker_ids = build_fanout_graph(
        session, assignments, roles, synthesis_role=synthesis_role
    )
    return await session.flow(
        graph,
        reactive=reactive,
        node_builder=role_node_builder(roles) if reactive else None,
        max_spawn=max_spawn,
        max_concurrent=max_concurrent or len(worker_ids),
        verbose=verbose,
    )
