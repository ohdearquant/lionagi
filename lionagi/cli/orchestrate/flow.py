# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Auto-DAG flow: orchestrator plans DAG → engine executes with deps."""

from __future__ import annotations

import re
import time
from typing import ClassVar

from pydantic import Field, field_validator

from lionagi import Branch, FieldModel
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import move_on_after
from lionagi.models import HashableModel
from lionagi.operations.fields import Instruct

from .._agents import AgentProfile, list_agents, load_agent_profile
from .._logging import progress
from .._providers import parse_model_spec
from ._common import _create_fanout_team, _format_result_json, _post_results_to_team
from ._orchestration import (
    EFFORT_GUIDANCE,
    EFFORT_MAP,
    OrchestrationEnv,
    build_worker_branch,
    finalize_orchestration,
    resolve_worker_spec,
    setup_orchestration,
    start_live_persist,
    stop_live_persist,
    team_guidance,
)

# ── Security: restrict model-produced identifiers used as filesystem paths ──
#
# FlowAgent.id and FlowOp.id end up as directory names under `artifact_root/`
# (see RunDir.agent_artifact_dir) and as worker repo/branch labels. An
# unconstrained, model-controlled id (e.g. ``/etc/passwd`` or ``../../tmp``)
# would escape the artifact root and let a compromised planner write outside
# the intended sandbox. Enforce a narrow alphanumeric + underscore + dash
# identifier policy at model-validation time; RunDir also re-validates on
# path construction as defense-in-depth.
_FLOW_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_flow_id(v: str, *, kind: str) -> str:
    """Reject path-escape and non-identifier values. Raises ValueError on bad input."""
    if not isinstance(v, str):
        raise ValueError(f"{kind} must be a string, got {type(v).__name__}")
    if not _FLOW_ID_RE.fullmatch(v):
        raise ValueError(
            f"{kind} must match {_FLOW_ID_RE.pattern!r} "
            f"(alphanumeric + '_' '-' only, 1-64 chars); got {v!r}"
        )
    return v


# ── Flow models ───────────────────────────────────────────────────────────


class FlowAgent(HashableModel):
    """An agent in a flow — a Branch identity with persistent memory.

    Agents are defined once and can be invoked multiple times in the DAG.
    Every invocation on the same agent reuses the same Branch, which
    means the agent remembers everything it did before. Use this for
    iterative refinement (r1 → impl1 → r1 again sees r1's first turn).
    """

    id: str = Field(
        description=(
            "Short unique identifier for this agent, e.g. 'r1', 'impl1'. "
            "Reused across multiple FlowOp.agent_id references. "
            "Must match ^[A-Za-z0-9_-]{1,64}$ (alphanumeric + _ -, 1-64 chars). "
            "Path separators, dots, and escape sequences are rejected."
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return _validate_flow_id(v, kind="FlowAgent.id")

    role: str = Field(
        description=(
            "Role name from the available-agents roster (e.g. 'researcher', "
            "'implementer', 'critic'). Determines the agent's profile "
            "(system prompt, default model, effort). Do not invent roles."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Explicit model spec override (e.g. 'codex/gpt-5.4-high'). "
            "Leave null to use the role's profile default."
        ),
    )
    guidance: str | None = Field(
        default=None,
        description=(
            "Default behavioral guidance applied to every op on this agent. "
            "Op-level guidance overrides this when set."
        ),
    )


class FlowOp(HashableModel):
    """One DAG node — a single invocation on some agent.

    ``agent_id`` must reference a FlowAgent in the same FlowPlan. Multiple
    ops can share an agent_id; the framework reuses the Branch so the
    agent's conversation history carries across.
    """

    id: str = Field(
        description=(
            "Short unique op id, e.g. 'o1', 'review1'. Referenced by "
            "other ops via depends_on. "
            "Must match ^[A-Za-z0-9_-]{1,64}$."
        ),
    )

    @field_validator("id", "agent_id")
    @classmethod
    def _validate_op_ids(cls, v: str) -> str:
        return _validate_flow_id(v, kind="FlowOp.id / agent_id")

    agent_id: str = Field(
        description=(
            "The id of the FlowAgent that executes this op. Multiple ops "
            "can share an agent_id — the second invocation has memory of "
            "the first. Reusing an existing agent is CHEAPER than spawning "
            "a new one (no re-context, less tokens)."
        ),
    )
    instruction: str = Field(
        description="Concrete task instruction for this invocation.",
    )
    guidance: str | None = Field(
        default=None,
        description=(
            "Per-op behavioral framing. Overrides the agent's default "
            "guidance when set (e.g. 'skim quickly' vs 'deep analysis')."
        ),
    )
    depends_on: list[str] | None = Field(
        default=None,
        description=(
            "Other FlowOp ids this op waits on. Results from those ops "
            "are available as upstream context. Same-agent deps are free "
            "(branch already has memory); cross-agent deps require the "
            "downstream agent to read the upstream's artifact dir."
        ),
    )
    control: bool = Field(
        default=False,
        description=(
            "Set True to make this a control/critic checkpoint. Control "
            "ops produce a FlowControlVerdict and may trigger re-planning. "
            "At most one control op per round."
        ),
    )


class FlowPlan(HashableModel):
    """Two-level DAG plan: agent identities + operation DAG.

    Agents are the branches (who); operations are the DAG nodes (what
    happens, in what order, to which branch). An operation with
    ``control=True`` acts as a critic checkpoint — if its verdict says
    should_continue, the orchestrator is re-invoked to plan more ops
    (and may introduce new agents if needed).
    """

    agents: list[FlowAgent] = Field(
        description=(
            "Persistent agent identities (Branches). Keep count minimal — "
            "reusing an agent across ops is cheaper than spawning a new "
            "one. Spawn new only for fresh perspective or different role."
        ),
    )
    operations: list[FlowOp] = Field(
        description=(
            "DAG of invocations. Each op picks its executing agent via "
            "agent_id and declares upstream deps via depends_on. Ops form "
            "an acyclic graph; independent ops run in parallel."
        ),
    )
    synthesis: bool = Field(
        default=False,
        description=(
            "Set True if the task benefits from a final consolidated "
            "synthesis op after all others complete."
        ),
    )

    # ── Prompt templates consumed by the orchestrator planner ──────────
    PLANNING_INSTRUCTION: ClassVar[str] = (
        "Produce a FlowPlan with TWO levels:\n"
        "  1. agents: list of FlowAgent — each is a persistent Branch "
        "     (identity + memory). Same agent can run multiple ops.\n"
        "  2. operations: list of FlowOp — DAG of invocations. Each op "
        "     picks which agent runs it via agent_id.\n\n"
        "Example (research → implement → research re-reviews):\n"
        "  agents: [\n"
        "    FlowAgent(id='r1', role='researcher'),\n"
        "    FlowAgent(id='i1', role='implementer'),\n"
        "  ]\n"
        "  operations: [\n"
        "    FlowOp(id='o1', agent_id='r1', instruction='research X'),\n"
        "    FlowOp(id='o2', agent_id='i1', instruction='implement based on o1', depends_on=['o1']),\n"
        "    FlowOp(id='o3', agent_id='r1', instruction='review i1 work', depends_on=['o2']),\n"
        "  ]\n"
        "Because o3 reuses r1, r1 remembers its own research from o1 — "
        "no need to re-inject context. Reusing agents is cheaper than "
        "spawning new ones."
    )

    PLANNING_DISCIPLINE: ClassVar[str] = (
        "CRITICAL: You MUST produce your output ONLY via the structured "
        "output fields (the FlowPlan). Do NOT use any provider-native "
        "subagent or tool-spawning features (no Agent tool, no subprocess "
        "spawning, no delegation tools). The ONLY correct way to define "
        "the pipeline is by filling in the FlowPlan structured output. "
        "Use ONLY roles from the available list above — do not invent "
        "custom role names. "
        "AGENT VS OP COUNT: keep the agent count minimal — an agent is a "
        "Branch with memory, so reusing an agent across ops is cheaper "
        "than spawning a new one. Spawn a new agent only when you need a "
        "fresh perspective or different role. Prefer 2-4 agents driving "
        "several ops over 8 agents with one op each. "
        "CONTROL OPS: Set op.control=true on an op to make it a flow "
        "control checkpoint. Place at most one control op per round; it "
        "should depend on the ops whose work it reviews. "
        "Set synthesis=true if the task benefits from a final consolidated output. "
        "AGENT & OP IDS: Use short alphanumeric identifiers matching "
        "^[A-Za-z0-9_-]{1,64}$ (e.g. 'r1', 'impl_v2', 'ctx-fetch'). Ids are used "
        "as filesystem path segments — do not include slashes, spaces, or dots."
    )

    REPLAN_INSTRUCTION: ClassVar[str] = (
        "The control op requested continuation. Return a FlowPlan with:\n"
        "  - agents: ONLY new agents you need (reuse existing when possible — "
        "    list only the new ones here).\n"
        "  - operations: the NEW ops to run. You may reference existing "
        "    agents (by their original id) or your new agents.\n"
        "Reusing an existing agent is cheaper — its branch already has "
        "memory from its prior turns."
    )

    REPLAN_GUIDANCE: ClassVar[str] = (
        "Add only ops needed to address the control feedback. "
        "Do NOT re-run ops that already succeeded. "
        "Use ONLY the structured output. Do NOT spawn subagents."
    )


FLOW_PLAN_FIELDS = FieldModel(FlowPlan, name="plan")


class FlowControlVerdict(HashableModel):
    """Structured output from a flow control op.

    Control ops review prior work and decide whether the flow should
    continue with additional rounds of planning/execution.
    """

    should_continue: bool = Field(
        description=(
            "If False, the flow ends here. If True, the orchestrator is "
            "invoked to plan additional ops targeting the gaps named in "
            "next_steps."
        ),
    )
    reason: str = Field(
        description=(
            "Concise justification for the verdict — what criteria were "
            "or were not met. Grounded in the specific op outputs, not vague."
        ),
    )
    next_steps: str | None = Field(
        default=None,
        description=(
            "If should_continue, specific gaps that need addressing "
            "(name EXACT issues, not 'improve quality'). Read by the "
            "orchestrator during re-planning."
        ),
    )

    # Appended to each control op's instruction at execution time.
    VERDICT_CONTRACT: ClassVar[str] = (
        "Review all prior op outputs and produce a verdict: "
        "should_continue (bool), reason (str), and optional next_steps "
        "(str) guidance if continuing.\n\n"
        "VERDICT CONSEQUENCES: If should_continue=False, the flow ends. "
        "If should_continue=True, the orchestrator plans ADDITIONAL ops "
        "(possibly reusing existing agents) to address your next_steps. "
        "Be specific: name EXACT gaps, not 'improve quality'."
    )


FLOW_VERDICT_FIELDS = FieldModel(FlowControlVerdict, name="verdict")


def _topo_sort_ops(
    ops: list[FlowOp],
    *,
    existing_op_ids: set[str] | None = None,
) -> list[FlowOp]:
    """Topological sort of FlowOps so parents appear before children.

    Uses iterative Kahn's BFS to avoid Python recursion limits on deep chains.
    The 200-op hard cap is enforced upstream by the plan validation layer
    in ``_run_flow_inner`` before this function is called.

    Raises
    ------
    ValueError
        If any ``depends_on`` references an id not in ``ops`` or
        ``existing_op_ids`` (fail-closed on typos and hallucinated deps),
        if an op id is duplicated in ``ops``, or if the
        dependency graph has a cycle.
    """
    by_id: dict[str, FlowOp] = {}
    for op in ops:
        if op.id in by_id:
            raise ValueError(f"Duplicate op id {op.id!r}")
        by_id[op.id] = op

    known_ids = set(by_id) | (existing_op_ids or set())

    for op in ops:
        for dep in op.depends_on or []:
            if dep not in known_ids:
                raise ValueError(f"Op {op.id!r} declares unknown dependency {dep!r}")

    # Kahn's BFS — only local (within-batch) deps affect sort order.
    # Deps on existing_op_ids are satisfied by definition (already executed).
    from collections import deque

    in_degree: dict[str, int] = {op_id: 0 for op_id in by_id}
    children: dict[str, list[str]] = {op_id: [] for op_id in by_id}
    for op in ops:
        for dep in op.depends_on or []:
            if dep in by_id:
                in_degree[op.id] += 1
                children[dep].append(op.id)

    queue: deque[str] = deque(op_id for op_id in by_id if in_degree[op_id] == 0)
    order: list[FlowOp] = []

    while queue:
        op_id = queue.popleft()
        order.append(by_id[op_id])
        for child_id in children[op_id]:
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                queue.append(child_id)

    if len(order) != len(ops):
        remaining = {op_id for op_id in by_id if by_id[op_id] not in order}
        cycle_node = next(iter(remaining))
        raise ValueError(f"Dependency cycle detected involving {cycle_node!r}")

    return order


def _format_flow_result_text(
    agent_results: list[dict],
    synthesis_result: dict | None = None,
) -> str:
    lines = []
    for w in agent_results:
        deps = w.get("depends_on") or []
        dep_str = f"  deps: {', '.join(deps)}" if deps else ""
        lines.append(f"{'═' * 60}")
        lines.append(f"  {w['id']} ({w['name']})  [{w['model']}]{dep_str}")
        lines.append(f"  {w['time_ms']:.0f}ms")
        lines.append(f"{'═' * 60}")
        lines.append(w.get("response", "(no response)"))
        lines.append("")

    if synthesis_result is not None:
        lines.append(f"{'═' * 60}")
        lines.append(f"  Synthesis  [{synthesis_result['model']}]")
        lines.append(f"  {synthesis_result['time_ms']:.0f}ms")
        lines.append(f"{'═' * 60}")
        lines.append(synthesis_result.get("response", "(no response)"))
        lines.append("")

    return "\n".join(lines)


async def _run_flow(
    model_spec: str,
    prompt: str,
    *,
    with_synthesis: bool = False,
    synthesis_model: str | None = None,
    max_concurrent: int = 0,
    yolo: bool = False,
    bypass: bool = False,
    verbose: bool = False,
    effort: str | None = None,
    theme: str | None = None,
    output_format: str = "text",
    save_dir: str | None = None,
    team_name: str | None = None,
    team_attach: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    agent_name: str | None = None,
    bare: bool = False,
    max_ops: int = 0,
    dry_run: bool = False,
    show_graph: bool = False,
    fast: bool = False,
    playbook_name: str | None = None,
    **legacy_kwargs,
) -> str:
    """Auto-DAG flow: orchestrator plans DAG → engine executes with deps.

    ``max_ops`` caps the number of operations (DAG nodes) the planner may
    emit. ``max_agents`` is accepted as a deprecated alias.
    """
    # Accept legacy max_agents kwarg for backward compat with direct callers.
    if "max_agents" in legacy_kwargs and max_ops == 0:
        max_ops = legacy_kwargs.pop("max_agents")
    elif "max_agents" in legacy_kwargs:
        legacy_kwargs.pop("max_agents")
    if legacy_kwargs:
        # Surface unrecognized kwargs instead of swallowing.
        raise TypeError(
            f"_run_flow() got unexpected keyword arguments: {list(legacy_kwargs)}"
        )
    env = setup_orchestration(
        pattern_name="Flow",
        model_spec=model_spec,
        agent_name=agent_name,
        save_dir=save_dir,
        cwd=cwd,
        yolo=yolo,
        bypass=bypass,
        verbose=verbose,
        effort=effort,
        theme=theme,
        bare=bare,
        fast=fast,
    )

    # ADR-0012: `flow` and `play` are distinct invocation kinds. A playbook
    # run (li play NAME, or li o flow -p NAME) is `play`; an ad-hoc DAG flow
    # without a playbook is `flow`.
    await start_live_persist(
        env,
        invocation_kind="play" if playbook_name else "flow",
        playbook_name=playbook_name,
        agent_name=agent_name,
        artifacts_path=str(env.run.artifact_root),
    )

    inner_kw = dict(
        env=env,
        with_synthesis=with_synthesis,
        synthesis_model=synthesis_model,
        max_concurrent=max_concurrent,
        output_format=output_format,
        team_name=team_name,
        team_attach=team_attach,
        max_ops=max_ops,
        dry_run=dry_run,
        show_graph=show_graph,
    )
    _terminal_status = "completed"
    try:
        if timeout:
            with move_on_after(timeout) as cancel_scope:
                result = await _run_flow_inner(model_spec, prompt, **inner_kw)
            if cancel_scope.cancelled_caught:
                _terminal_status = "aborted"
                raise LionTimeoutError(f"Flow timed out after {timeout}s")
            return result
        return await _run_flow_inner(model_spec, prompt, **inner_kw)
    except KeyboardInterrupt:
        _terminal_status = "aborted"
        raise
    except BaseException as exc:
        from lionagi.ln.concurrency import get_cancelled_exc_class

        if isinstance(exc, get_cancelled_exc_class()):
            _terminal_status = "aborted"
        else:
            _terminal_status = "failed"
        raise
    finally:
        # Shield teardown from outer cancellation so iModel executors are
        # always closed; see lionagi/cli/agent.py for the full rationale.
        import anyio

        with anyio.CancelScope(shield=True):
            await stop_live_persist(env, status=_terminal_status)
            for _br in env.session.branches:
                await _br.mdls.shutdown()


async def _run_flow_inner(
    model_spec: str,
    prompt: str,
    *,
    env: OrchestrationEnv,
    with_synthesis: bool = False,
    synthesis_model: str | None = None,
    max_concurrent: int = 0,
    output_format: str = "text",
    team_name: str | None = None,
    team_attach: str | None = None,
    max_ops: int = 0,
    dry_run: bool = False,
    show_graph: bool = False,
) -> str:
    """Inner flow logic (no timeout wrapper)."""
    t0 = time.monotonic()

    # Working objects: four subjects this function mutates across phases.
    # All config (bare/verbose/effort/yolo/theme/cwd) is read from `env`
    # directly — no aliases, one source of truth.
    run = env.run
    session = env.session
    orc_branch = env.orc_branch
    builder = env.builder

    # ── Phase 0: Orchestrator plans the DAG ──────────────────────────
    # Build role roster for orchestrator guidance
    available_roles = list_agents()
    if env.bare:
        roles_guidance = (
            f"Available roles: {', '.join(available_roles)}. "
            f"All workers use model {model_spec}. "
            f"Roles define behavioral focus only — model is fixed."
        )
    else:
        role_details = []
        for role in available_roles:
            try:
                rp = load_agent_profile(role)
                rm = rp.model or model_spec
                detail = f"{role} (model: {rm}"
                if rp.effort:
                    detail += f", effort: {rp.effort}"
                detail += ")"
                role_details.append(detail)
            except FileNotFoundError:
                role_details.append(f"{role} (model: {model_spec})")
        roles_guidance = f"Available agents: {'; '.join(role_details)}."

    budget_note = ""
    if max_ops > 0:
        budget_note = (
            f"BUDGET: Your plan may contain at most {max_ops} ops (DAG nodes) "
            "total, INCLUDING any synthesis/critic ops you intend to run at "
            "the end. Plans exceeding this cap will be truncated — design "
            "accordingly. "
        )

    # Build guidance blocks for plan root
    artifact_guidance = (
        "ARTIFACT PROTOCOL: Each agent gets ONE directory at "
        f"{run.artifact_root}/{{agent_id}}/ — all invocations of the same "
        "agent share that directory. In EVERY op.instruction you MUST specify: "
        "(1) whether the agent should write files or return text inline. "
        "(2) if files are needed, the names required by the task. "
        "(3) WHERE to read upstream: 'Read ../{dep_agent_id}/{filename}.md' for "
        "each upstream dep that belongs to a different agent. If an upstream "
        "dep is the SAME agent, the agent already remembers it — no re-read needed. "
    )

    plan_root = builder.add_operation(
        "operate",
        branch=orc_branch,
        instruct=Instruct(
            instruction=FlowPlan.PLANNING_INSTRUCTION,
            context={"task": prompt},
            guidance=(
                f"{roles_guidance} "
                f"{budget_note}"
                f"{artifact_guidance}"
                f"{EFFORT_GUIDANCE}"
                f"{team_guidance(team_attach or team_name)}"
                f"{FlowPlan.PLANNING_DISCIPLINE}"
            ),
        ),
        field_models=[FLOW_PLAN_FIELDS],
        reason=True,
    )

    progress("Planning DAG...")

    result0 = await session.flow(builder.get_graph())
    t_plan = time.monotonic() - t0

    plan_result = result0.get("operation_results", {}).get(plan_root)
    plan: FlowPlan | None = getattr(plan_result, "plan", None)

    if not plan or not plan.agents or not plan.operations:
        return "Orchestrator produced no flow plan."

    # Reject duplicate FlowAgent.id — agents_by_id would silently
    # overwrite, losing whichever agent came first.
    seen_agent_ids: set[str] = set()
    for a in plan.agents:
        if a.id in seen_agent_ids:
            return f"Invalid plan: duplicate FlowAgent.id {a.id!r}. Each agent must have a unique id."
        seen_agent_ids.add(a.id)

    # Reject duplicate FlowOp.id as well — depends_on references would
    # become ambiguous.
    seen_op_ids: set[str] = set()
    for op in plan.operations:
        if op.id in seen_op_ids:
            return f"Invalid plan: duplicate FlowOp.id {op.id!r}. Each op must have a unique id."
        seen_op_ids.add(op.id)

    # Validate op.agent_id references resolve to a defined agent
    agent_ids = seen_agent_ids
    for op in plan.operations:
        if op.agent_id not in agent_ids:
            return (
                f"Invalid plan: op {op.id!r} references unknown agent "
                f"{op.agent_id!r} (known: {sorted(agent_ids)})"
            )

    # Reject plans exceeding the hard op count cap before sorting.
    if len(plan.operations) > 200:
        return f"Invalid plan: Plan has {len(plan.operations)} operations; maximum allowed is 200"

    # Validate topology and depends_on targets up front. Rejects plans
    # with typo'd deps or dependency cycles before we spend any agent
    # time executing them.
    try:
        plan.operations = _topo_sort_ops(plan.operations)
    except ValueError as e:
        return f"Invalid plan: {e}"

    # max_ops caps the total operation count — that is the real work
    # budget. Agent count is bounded implicitly by the op count since
    # every agent runs at least one op.
    if max_ops > 0 and len(plan.operations) > max_ops:
        dropped = len(plan.operations) - max_ops
        plan.operations = plan.operations[:max_ops]
        from .._logging import warn as _warn

        _warn(
            f"Plan truncated: {dropped} op(s) dropped to fit --max-ops={max_ops}. "
            "The truncated ops (often terminal synthesis/critic) will not run. "
            "Raise --max-ops or design a tighter plan."
        )

    dag_lines = []
    for op in plan.operations:
        deps = f" ← {','.join(op.depends_on)}" if op.depends_on else ""
        ctrl = "!" if op.control else ""
        dag_lines.append(f"{op.id}{ctrl}:{op.agent_id}{deps}")
    progress(
        f"Plan done ({t_plan:.1f}s): {len(plan.agents)} agents, "
        f"{len(plan.operations)} ops — {' | '.join(dag_lines)}"
    )

    if plan.synthesis and not with_synthesis:
        with_synthesis = True

    # ── Dry run: dump plan and exit ─────────────────────────────────
    if dry_run:
        lines = [
            f"FlowPlan ({len(plan.agents)} agents, {len(plan.operations)} ops, "
            f"synthesis={plan.synthesis})",
            "",
            "Agents:",
        ]
        for a in plan.agents:
            lines.append(f"  {a.id}: {a.role}")
            if a.model:
                lines.append(f"    model: {a.model}")
            if a.guidance:
                lines.append(f"    guidance: {a.guidance[:80]}...")
        lines.append("")
        lines.append("Operations:")
        for op in plan.operations:
            ctrl = " [CONTROL]" if op.control else ""
            deps = f"  depends_on: {', '.join(op.depends_on)}" if op.depends_on else ""
            lines.append(f"  {op.id} → {op.agent_id}{ctrl}")
            lines.append(f"    instruction: {op.instruction[:120]}...")
            if deps:
                lines.append(deps)
            if op.guidance:
                lines.append(f"    guidance: {op.guidance[:80]}...")
            lines.append("")

        # Show resolved models per agent
        lines.append("Model resolution:")
        for a in plan.agents:
            if env.bare:
                rm = a.model or model_spec
                lines.append(f"  {a.id}: {rm} (bare)")
            else:
                rm, rp = resolve_worker_spec(a.role)
                if a.model:
                    rm = a.model
                src = "plan" if a.model else ("profile" if rp else "default")
                lines.append(f"  {a.id}: {rm} ({src})")

        if show_graph:
            # Pre-execution preview — draw from plan directly, NOT from the
            # builder (which has only the orchestrator's seed op at this point).
            from lionagi.operations._visualize_graph import visualize_plan

            visualize_plan(
                plan,
                title=(
                    f"Flow DAG plan — {len(plan.agents)} agents / {len(plan.operations)} ops"
                ),
                save_path=str(run.dag_image_path),
            )

        return "\n".join(lines)

    # ── Name allocation: one name per agent, deduped by role ────────
    name_counts: dict[str, int] = {}
    agent_id_to_name: dict[str, str] = {}
    all_agent_names: list[str] = []
    for a in plan.agents:
        base = a.role
        name_counts[base] = name_counts.get(base, 0) + 1
        wname = f"{base}-{name_counts[base]}" if name_counts[base] > 1 else base
        agent_id_to_name[a.id] = wname
        all_agent_names.append(wname)

    if team_attach:
        # Upsert: load existing team by name, else create a fresh one.
        from ..team import _load_team

        try:
            env.team_data = _load_team(team_attach)
            progress(
                f"Team '{team_attach}' attached ({env.team_data['id']}, "
                f"{len(env.team_data.get('messages', []))} prior msgs)"
            )
        except FileNotFoundError:
            env.team_data = _create_fanout_team(team_attach, all_agent_names)
            progress(f"Team '{team_attach}' created ({env.team_data['id']})")
    elif team_name:
        env.team_data = _create_fanout_team(team_name, all_agent_names)
        progress(f"Team '{team_name}' created ({env.team_data['id']})")
    team_data = env.team_data

    # ── Helper: build branch for a single agent spec ───────────────
    def _build_agent_branch(
        a: FlowAgent,
    ) -> tuple[Branch, str, AgentProfile | None]:
        return build_worker_branch(
            env,
            agent_id=a.id,
            role=a.role,
            model_override=a.model,
            explicit_name=agent_id_to_name[a.id],
        )

    # ── Helper: build context + add a node for a single op ──────────
    # Agent-scoped artifact dirs: every op of the same agent shares
    # one dir. Upstream reads resolve by mapping dep op → dep agent.
    def _add_op_node(
        op: FlowOp,
        agent: FlowAgent,
        profile: AgentProfile | None,
        branch: Branch,
        dep_nodes: list[str],
        op_id_to_agent: dict[str, str],
        field_models=None,
    ) -> str:
        ctx: list = [{"original_task": prompt}]
        artifact_note = (
            f"Your artifact directory (shared across all ops on {agent.id}): "
            f"{run.agent_artifact_dir(agent.id)}/ — write output files here."
        )
        if op.depends_on:
            dep_notes = []
            for d in op.depends_on:
                dep_agent = op_id_to_agent.get(d)
                if dep_agent is None:
                    continue
                if dep_agent == agent.id:
                    # Same-agent dep: the branch already remembers the upstream
                    # turn — no need to re-gather context or re-read files.
                    dep_notes.append(f"{d}: already in your memory (same agent)")
                else:
                    dep_notes.append(
                        f"{d} ({dep_agent}): {run.agent_artifact_dir(dep_agent)}/"
                    )
            if dep_notes:
                artifact_note += f" Upstream: {'; '.join(dep_notes)}."
        ctx.append({"artifact_instructions": artifact_note})

        if team_data:
            ctx.append(
                {
                    "team": {
                        "id": team_data["id"],
                        "name": team_data["name"],
                        "your_name": agent_id_to_name[agent.id],
                    }
                }
            )

        w_effort = env.effort
        if not env.bare and profile and profile.effort:
            w_effort = profile.effort
        if w_effort:
            ctx.append({"effort_guidance": EFFORT_MAP.get(w_effort, "")})

        add_kw = dict(
            branch=branch,
            depends_on=dep_nodes,
            instruction=op.instruction,
            guidance=op.guidance or agent.guidance,
            context=ctx,
        )
        if field_models is not None:
            add_kw["field_models"] = field_models
            add_kw["reason"] = True
        return builder.add_operation("operate", **add_kw)

    # ── Pass 1: build all agent branches ───────────────────────────
    agents_by_id: dict[str, Branch] = {}
    agent_model_by_id: dict[str, str] = {}
    agent_profile_by_id: dict[str, AgentProfile | None] = {}
    for a in plan.agents:
        b, m, p = _build_agent_branch(a)
        agents_by_id[a.id] = b
        agent_model_by_id[a.id] = m
        agent_profile_by_id[a.id] = p

    # agent_spec lookup used by op construction + re-plan
    agent_spec_by_id: dict[str, FlowAgent] = {a.id: a for a in plan.agents}

    # ── Pass 2: build regular op nodes ─────────────────────────────
    op_to_node: dict[str, str] = {}
    op_meta: dict[str, dict] = {}
    op_id_to_agent: dict[str, str] = {op.id: op.agent_id for op in plan.operations}
    agent_results: list[dict] = []

    def _record_result(result: dict) -> None:
        """Append result and persist immediately to artifact dir."""
        agent_results.append(result)
        try:
            agent_dir = run.agent_artifact_dir(result["agent_id"])
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / f"{result['id']}.md").write_text(result["response"])
        except OSError as exc:
            from .._logging import warn as _warn

            _warn(f"Failed to save artifact for {result['id']}: {exc}")

    regular_ops = [op for op in plan.operations if not op.control]
    control_ops = [op for op in plan.operations if op.control]

    ctrl_note = f" ({len(control_ops)} control)" if control_ops else ""
    progress(
        f"Executing DAG: {len(plan.agents)} agents / {len(regular_ops)} ops{ctrl_note}..."
    )

    for op in regular_ops:
        a = agent_spec_by_id[op.agent_id]
        b = agents_by_id[op.agent_id]
        p = agent_profile_by_id[op.agent_id]
        dep_nodes = [op_to_node[d] for d in (op.depends_on or []) if d in op_to_node]
        if not dep_nodes:
            dep_nodes = [plan_root]
        node_id = _add_op_node(op, a, p, b, dep_nodes, op_id_to_agent)
        op_to_node[op.id] = node_id
        op_meta[op.id] = {
            "agent_id": op.agent_id,
            "agent_name": agent_id_to_name[op.agent_id],
            "model": agent_model_by_id[op.agent_id],
            "depends_on": op.depends_on or [],
        }

    # Progress callback for real-time status
    def _progress(op_id, name, status, elapsed):
        if status == "started":
            progress(f"  ▶ {name} started")
        elif status == "completed":
            progress(f"  ✓ {name} done ({elapsed:.1f}s)")
        elif status == "failed":
            progress(f"  ✗ {name} FAILED ({elapsed:.1f}s)")

    # Execute regular ops
    t_exec = time.monotonic()
    conc = max_concurrent if max_concurrent > 0 else max(len(regular_ops), 1)
    dag_result = await session.flow(
        builder.get_graph(),
        max_concurrent=conc,
        verbose=env.verbose,
        on_progress=_progress,
    )
    t_exec_elapsed = time.monotonic() - t_exec

    op_results = dag_result.get("operation_results", {})
    for op in regular_ops:
        nid = op_to_node[op.id]
        meta = op_meta[op.id]
        res = op_results.get(nid)
        _record_result(
            {
                "id": op.id,
                "agent_id": op.agent_id,
                "name": meta["agent_name"],
                "model": meta["model"],
                "depends_on": meta["depends_on"],
                "control": False,
                "response": str(res) if res is not None else "(no response)",
                "time_ms": t_exec_elapsed * 1000,
            }
        )

    progress(f"DAG done ({t_exec_elapsed:.1f}s).")

    # ── Execute control ops sequentially: each may trigger a re-plan ─
    max_rounds = 3
    round_num = 0
    for cop in control_ops:
        if cop.agent_id not in agents_by_id:
            progress(f"Control op {cop.id!r} references unknown agent, skipping.")
            continue
        c_branch = agents_by_id[cop.agent_id]
        c_model = agent_model_by_id[cop.agent_id]

        artifacts = [
            f"[op {r['id']} via {r['name']}]: {r['response']}" for r in agent_results
        ]
        dep_nodes = [op_to_node[d] for d in (cop.depends_on or []) if d in op_to_node]
        if not dep_nodes:
            dep_nodes = list(op_to_node.values())[-1:] or [plan_root]

        progress(f"Control [{cop.id} via {cop.agent_id}]: evaluating...")

        # The orchestrator's `cop.instruction` provides domain context;
        # we append the verdict contract from the schema's ClassVar.
        instr = f"{cop.instruction}\n\n{FlowControlVerdict.VERDICT_CONTRACT}"
        ctrl_op = FlowOp(
            id=cop.id,
            agent_id=cop.agent_id,
            instruction=instr,
            guidance=cop.guidance,
            control=True,
        )
        ctrl_node = _add_op_node(
            ctrl_op,
            agent_spec_by_id[cop.agent_id],
            agent_profile_by_id[cop.agent_id],
            c_branch,
            dep_nodes,
            op_id_to_agent,
            field_models=[FLOW_VERDICT_FIELDS],
        )
        op_to_node[cop.id] = ctrl_node

        t_ctrl = time.monotonic()
        ctrl_result = await session.flow(builder.get_graph(), verbose=env.verbose)
        t_ctrl_elapsed = time.monotonic() - t_ctrl

        ctrl_res = ctrl_result.get("operation_results", {}).get(ctrl_node)
        verdict: FlowControlVerdict | None = getattr(ctrl_res, "verdict", None)
        verdict_text = str(ctrl_res) if ctrl_res is not None else "(no response)"

        _record_result(
            {
                "id": cop.id,
                "agent_id": cop.agent_id,
                "name": agent_id_to_name[cop.agent_id],
                "model": c_model,
                "depends_on": cop.depends_on or [],
                "control": True,
                "response": verdict_text,
                "time_ms": t_ctrl_elapsed * 1000,
            }
        )

        cont = verdict.should_continue if verdict else False
        progress(f"Control [{cop.id}] done ({t_ctrl_elapsed:.1f}s): continue={cont}")

        # ── If verdict says continue: orchestrator re-plans ────────
        if verdict and verdict.should_continue:
            round_num += 1
            if round_num >= max_rounds:
                progress(f"Max rounds ({max_rounds}) reached, stopping.")
                break

            progress(f"Round {round_num + 1}: orchestrator re-planning...")

            existing_roster = ", ".join(f"{a.id} ({a.role})" for a in plan.agents)
            replan_node = builder.add_operation(
                "operate",
                branch=orc_branch,
                depends_on=[ctrl_node],
                instruct=Instruct(
                    instruction=(
                        f"{FlowPlan.REPLAN_INSTRUCTION}\n\n"
                        f"Existing agents you can reuse: {existing_roster}."
                    ),
                    context={
                        "original_task": prompt,
                        "prior_results": artifacts,
                        "control_verdict": verdict_text,
                        "next_steps_guidance": verdict.next_steps or "",
                    },
                    guidance=(
                        f"{roles_guidance} "
                        f"This is round {round_num + 1}. "
                        f"{FlowPlan.REPLAN_GUIDANCE}"
                    ),
                ),
                field_models=[FLOW_PLAN_FIELDS],
                reason=True,
            )

            replan_result = await session.flow(builder.get_graph(), verbose=env.verbose)
            replan_res = replan_result.get("operation_results", {}).get(replan_node)
            next_plan: FlowPlan | None = getattr(replan_res, "plan", None)

            if not next_plan or not next_plan.operations:
                progress("Re-plan produced no new operations; ending.")
                continue

            # Validate new agents have unique ids, new ops reference some agent
            combined_agent_ids = set(agents_by_id)
            for na in next_plan.agents:
                if na.id in combined_agent_ids:
                    progress(f"Re-plan: skipping duplicate agent id {na.id!r}")
                    continue
                combined_agent_ids.add(na.id)

                base = na.role
                name_counts[base] = name_counts.get(base, 0) + 1
                wname = f"{base}-{name_counts[base]}" if name_counts[base] > 1 else base
                agent_id_to_name[na.id] = wname
                all_agent_names.append(wname)
                nb, nm, np = _build_agent_branch(na)
                agents_by_id[na.id] = nb
                agent_model_by_id[na.id] = nm
                agent_profile_by_id[na.id] = np
                agent_spec_by_id[na.id] = na

            # Register new ops (and update op_id_to_agent for downstream
            # artifact-path resolution).
            new_ops: list[FlowOp] = []
            for nop in next_plan.operations:
                if nop.agent_id not in agents_by_id:
                    progress(
                        f"Re-plan: skipping op {nop.id!r} — unknown agent {nop.agent_id!r}"
                    )
                    continue
                if nop.id in op_to_node:
                    progress(f"Re-plan: skipping duplicate op id {nop.id!r}")
                    continue
                new_ops.append(nop)

            if not new_ops:
                progress("Re-plan: no executable new ops; ending.")
                continue

            # Enforce --max-ops cumulatively. Initial plan and every
            # re-plan round share one op budget; without this check a
            # control op that repeatedly returns should_continue could
            # bypass the cap by stretching over multiple rounds.
            if max_ops > 0:
                current_total = len(op_to_node)
                budget_left = max_ops - current_total
                if budget_left <= 0:
                    from .._logging import warn as _warn

                    _warn(
                        f"Re-plan rejected: --max-ops={max_ops} already "
                        f"reached (current total {current_total}). "
                        f"Dropping {len(new_ops)} proposed op(s)."
                    )
                    continue
                if len(new_ops) > budget_left:
                    from .._logging import warn as _warn

                    dropped = len(new_ops) - budget_left
                    _warn(
                        f"Re-plan truncated: {dropped} of {len(new_ops)} "
                        f"proposed op(s) dropped to fit --max-ops={max_ops} "
                        f"(current total {current_total}, budget left "
                        f"{budget_left})."
                    )
                    new_ops = new_ops[:budget_left]

            try:
                new_ops = _topo_sort_ops(new_ops, existing_op_ids=set(op_to_node))
            except ValueError as e:
                progress(f"Re-plan rejected: {e}")
                continue

            for nop in new_ops:
                op_id_to_agent[nop.id] = nop.agent_id

            ids = ", ".join(o.id for o in new_ops)
            progress(
                f"Re-plan: +{len(next_plan.agents)} agents, +{len(new_ops)} ops: {ids}"
            )

            for nop in new_ops:
                na = agent_spec_by_id[nop.agent_id]
                nb = agents_by_id[nop.agent_id]
                np = agent_profile_by_id[nop.agent_id]
                nd = [op_to_node[d] for d in (nop.depends_on or []) if d in op_to_node]
                if not nd:
                    nd = [ctrl_node]
                nid = _add_op_node(nop, na, np, nb, nd, op_id_to_agent)
                op_to_node[nop.id] = nid
                op_meta[nop.id] = {
                    "agent_id": nop.agent_id,
                    "agent_name": agent_id_to_name[nop.agent_id],
                    "model": agent_model_by_id[nop.agent_id],
                    "depends_on": nop.depends_on or [],
                }

            t_new = time.monotonic()
            new_result = await session.flow(
                builder.get_graph(),
                max_concurrent=conc,
                verbose=env.verbose,
            )
            t_new_elapsed = time.monotonic() - t_new
            new_res_map = new_result.get("operation_results", {})
            for nop in new_ops:
                nid = op_to_node[nop.id]
                meta = op_meta[nop.id]
                res = new_res_map.get(nid)
                _record_result(
                    {
                        "id": nop.id,
                        "agent_id": nop.agent_id,
                        "name": meta["agent_name"],
                        "model": meta["model"],
                        "depends_on": meta["depends_on"],
                        "control": False,
                        "response": str(res) if res is not None else "(no response)",
                        "time_ms": t_new_elapsed * 1000,
                    }
                )
            progress(f"Round {round_num + 1} done ({t_new_elapsed:.1f}s).")

    # ── Synthesis ────────────────────────────────────────────────────
    synthesis_result = None
    if with_synthesis and agent_results:
        synth_spec = synthesis_model or model_spec
        synth_label = str(parse_model_spec(synth_spec))
        progress(f"Synthesis [{synth_label}]...")

        # Leaf ops = those not depended on by any other op. Walk op_meta
        # since it covers both initial plan and any re-planned ops.
        depended_on_nodes: set[str] = set()
        for _oid, meta in op_meta.items():
            for d in meta.get("depends_on", []):
                if d in op_to_node:
                    depended_on_nodes.add(op_to_node[d])
        all_op_node_ids = set(op_to_node.values())
        leaf_nodes = list(all_op_node_ids - depended_on_nodes) or list(all_op_node_ids)

        artifacts = [
            f"[op {r['id']} via {r['name']}]: {r['response']}" for r in agent_results
        ]
        adirs = [str(run.agent_artifact_dir(aid)) for aid in agents_by_id]
        artifact_chain_note = (
            f"\n\nARTIFACT CHAIN: Read ALL files in: {', '.join(adirs)}. "
            "Trace how work flowed through the DAG."
        )
        team_synth_note = ""
        if team_data:
            team_synth_note = (
                f"\n\nTEAM MESSAGES: Review inter-agent messages (team {team_data['id']}) "
                "for coordination context not captured in artifacts."
            )

        synth_node = builder.add_operation(
            "operate",
            branch=orc_branch,
            depends_on=leaf_nodes,
            instruction=(
                f"Synthesize all op outputs into a final cohesive deliverable.\n\n"
                f"Original task: {prompt}\n\n"
                "Your synthesis must:\n"
                "1. RECONCILE: When ops disagree, present both views with evidence.\n"
                "2. FILL GAPS: Name what no op covered.\n"
                "3. TRACE: Show how work flowed through the DAG "
                "(who did what, when, and what changed across op iterations).\n"
                "4. HONOR CRITIC: If a control op was in the pipeline, its verdict is authoritative.\n"
                "5. RESUME: End with branch IDs so the user can follow up with any agent."
                f"{artifact_chain_note}"
                f"{team_synth_note}"
            ),
            context=artifacts,
        )
        t_synth = time.monotonic()
        synth_result = await session.flow(builder.get_graph(), verbose=env.verbose)
        t_synth_elapsed = time.monotonic() - t_synth
        synth_res = synth_result.get("operation_results", {}).get(synth_node)
        synthesis_result = {
            "model": synth_label,
            "response": str(synth_res) if synth_res is not None else "(no response)",
            "time_ms": t_synth_elapsed * 1000,
        }
        progress(f"Synthesis done ({t_synth_elapsed:.1f}s).")

    # ── Output ───────────────────────────────────────────────────────
    if output_format == "json":
        output = _format_result_json(agent_results, synthesis_result)
    else:
        output = _format_flow_result_text(agent_results, synthesis_result)

    if synthesis_result:
        run.synthesis_path.write_text(synthesis_result["response"])

    if team_data:
        _post_results_to_team(
            team_data, agent_results, all_agent_names, synthesis_result
        )

    # ── Persist branches + run manifest + hints ──────────────────────
    finalize_orchestration(
        env,
        kind="flow",
        prompt=prompt,
        extras={
            "agents": [
                {
                    "id": agent_id,
                    "name": agent_id_to_name[agent_id],
                    "model": agent_model_by_id[agent_id],
                    "artifact_dir": str(run.agent_artifact_dir(agent_id)),
                }
                for agent_id in agents_by_id
            ],
            "operations": [
                {
                    "id": r["id"],
                    "agent_id": r["agent_id"],
                    "control": r.get("control", False),
                    "depends_on": r.get("depends_on") or [],
                }
                for r in agent_results
            ],
        },
    )

    if show_graph:
        from lionagi.operations._visualize_graph import visualize_graph

        visualize_graph(
            builder,
            title=f"Flow DAG — {len(plan.agents)} agents (completed)",
            save_path=str(run.dag_image_path),
        )

    t_total = time.monotonic() - t0
    progress(f"\nTotal: {t_total:.1f}s")

    return output
