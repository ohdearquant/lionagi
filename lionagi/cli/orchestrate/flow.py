# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Reactive DAG flow: orchestrator plans TaskAssignments, self-expanding execution."""

from __future__ import annotations

import asyncio as _asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field

from lionagi._errors import LionError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.casts.emission import SpawnRequest, TaskAssignment
from lionagi.ln.concurrency import CancelScope, move_on_after
from lionagi.orchestration import plan, role_node_builder

from .._logging import progress
from .._logging import warn as _warn
from .._providers import parse_model_spec
from .._util import classify_exception
from ._checkpoint import CheckpointWriter, FlowResumeError, resolve_checkpoint_target
from ._common import (
    _create_fanout_team,
    _format_result_json,
    _format_result_text,
    _post_results_to_team,
)
from ._notify import fire_terminal_notify
from ._orchestration import (
    EFFORT_MAP,
    OrchestrationEnv,
    available_roles,
    build_worker_branch,
    finalize_orchestration,
    mode_roster,
    parse_orchestrator_provider,
    resolve_modes,
    resolve_worker_spec,
    role_config,
    role_roster,
    setup_orchestration,
    start_live_persist,
    stop_live_persist,
    team_guidance,
)

logger = logging.getLogger(__name__)


class FlowPlanError(LionError):
    """Orchestrator failed to produce a usable plan."""


async def _persist_session_phase(env, phase: str) -> None:
    """Best-effort write of the live execution phase to the session row."""
    ctx = getattr(env, "_live_persist", None)
    if ctx and ctx.get("db"):
        with contextlib.suppress(Exception):
            await ctx["db"].update_session(ctx["session_id"], current_phase=phase)


# ── Control poller (ADR-0085 part 1: session_controls transport) ─────────────
# `li o ctl pause|resume|msg` enqueues a session_controls row from a separate
# process; this poller — running alongside the heartbeat loop in _execute_dag,
# same lifecycle — is the only consumer, and applies each row against the live
# executor. See docs/adrs/ADR-0085-flow-control-plane.md section 1 for the
# verb-classed apply/stamp ordering this implements.

_CONTROL_POLL_INTERVAL = 2.0

# Sentinel: the row's apply ran but no finalize write landed, so it is still
# pending in the DB. The poller must stop the tick rather than let later
# controls overtake it — the whole batch re-reads in order next tick.
_CONTROL_UNSTAMPED = "unstamped"


async def _apply_session_control(db, executor, row: dict) -> str | None:
    """Apply one session_controls row against *executor*; returns the finalize
    result string, or None if the row was left untouched (message already
    mid-apply from a prior poller crash).

    pause/resume are idempotent: apply, then stamp 'applied' — a poller crash
    between the two is harmless, since re-applying on the next poll is a no-op.
    message is not idempotent: stamp 'applying' first, then attempt the
    (checked, not assumed) injection, then finalize — a crash between stamp
    and apply leaves a visible 'applying' row (surfaced by `li o ctl status`)
    rather than risking a double injection on the next poll (at-most-once).

    Never raises: apply failures are recorded as a rejected result so a bad
    control row can never crash the run it rides alongside. Finalize failures
    after a successful apply never mislabel the row as rejected — the 'applied'
    stamp is retried, then the row is left for the next tick.
    """
    control_id = row["id"]
    verb = row["verb"]
    try:
        if verb == "pause":
            executor.pause()
            return await _finalize_applied(db, control_id)

        if verb == "resume":
            executor.resume()
            return await _finalize_applied(db, control_id)

        if verb == "message":
            if row.get("result") == "applying":
                # A prior poller crashed between stamp and apply. At-most-once:
                # leave it untouched — re-attempting could double-inject the
                # message if the earlier apply actually landed before the crash.
                return None
            await db.mark_session_control_applying(control_id)

            from lionagi.operations.node import Operation as _Operation  # noqa: PLC0415
            from lionagi.protocols.types import EventStatus as _EventStatus  # noqa: PLC0415

            has_pending_op = any(
                isinstance(node, _Operation) and node.execution.status == _EventStatus.PENDING
                for node in executor.graph.internal_nodes.values()
            )
            if not has_pending_op:
                result = "rejected:no-pending-ops"
                await db.finalize_session_control(control_id, result=result)
                return result

            from lionagi.libs.nested import deep_update  # noqa: PLC0415

            payload = row.get("payload") or {}
            existing = executor.context.content.get("operator_messages", [])
            entry = {"ts": time.time(), "text": payload.get("text", "")}
            deep_update(executor.context.content, {"operator_messages": [*existing, entry]})
            return await _finalize_applied(db, control_id)

        # 'stop' is schema-reserved for a later slice (the checkpoint writer);
        # any other verb is unrecognised. Reject loudly instead of leaving a
        # row that would be polled forever.
        result = f"rejected:unsupported-verb:{verb}"
        await db.finalize_session_control(control_id, result=result)
        return result
    except Exception as exc:  # noqa: BLE001 — the poller must never crash the run
        result = f"rejected:error:{exc}"[:500]
        logger.warning("control %s (%s) failed to apply: %s", control_id, verb, exc)
        try:
            await db.finalize_session_control(control_id, result=result)
        except Exception:  # noqa: BLE001
            # The row is still pending: a later control applied this tick
            # would be overtaken by this one re-applying next tick (e.g. a
            # stuck pause re-pausing after its resume). Signal the poller to
            # end the tick so ordering is preserved on the retry.
            return _CONTROL_UNSTAMPED
        return result


async def _finalize_applied(db, control_id: str) -> str:
    """Stamp 'applied' after a successful apply; never mislabel it rejected.

    A finalize failure here means the effect landed but the stamp did not:
    retry once, then hand the row back to the poller via the unstamped
    sentinel — pause/resume re-apply idempotently next tick, and a message
    row stays 'applying' so it is skipped rather than double-injected.
    """
    for _ in range(2):
        try:
            await db.finalize_session_control(control_id, result="applied")
            return "applied"
        except Exception as exc:  # noqa: BLE001 — the poller must never crash the run
            logger.warning("control %s applied but finalize failed: %s", control_id, exc)
    return _CONTROL_UNSTAMPED


_BUDGET_PREAMBLE_TEMPLATE = """\
[BUDGET]
You are op {op_index} of {num_ops} in this flow. Your share of the total \
budget is approximately {seconds} seconds (until {deadline_iso} UTC).
- Pace your reasoning accordingly.
- Prefer "good enough by the deadline" over "ideal but late".
- If you find yourself >70% through your budget and still in research, \
switch to writing the deliverable with what you have.
- You can check the current time: `date -Iseconds`.
[/BUDGET]

"""


def _format_budget_preamble(
    op_index: int,
    num_ops: int,
    op_budget_seconds: int,
    deadline_epoch: float,
) -> str:
    import datetime

    deadline_dt = datetime.datetime.fromtimestamp(deadline_epoch, tz=datetime.timezone.utc)
    deadline_iso = deadline_dt.strftime("%Y-%m-%dT%H:%M:%S")
    return _BUDGET_PREAMBLE_TEMPLATE.format(
        op_index=op_index,
        num_ops=num_ops,
        seconds=op_budget_seconds,
        deadline_iso=deadline_iso,
    )


async def _resolve_invocation_terminal_flow(
    invocation_id: str,
    *,
    fallback_status: str,
) -> tuple[str, str, str, list[dict], dict]:
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    async with StateDB() as db:
        sessions = await db.list_sessions_for_invocation(invocation_id)
        child_statuses = [str(s.get("status") or "") for s in sessions]
        evidence_refs = [{"kind": "session", "id": s["id"]} for s in sessions if s.get("id")]
        metadata: dict = {"child_statuses": child_statuses}

        # Precedence: timed_out > failed > aborted > cancelled > completed_empty
        # > completed. completed_empty outranks completed so one silently
        # empty leg still taints the whole flow's terminal status instead of
        # being averaged away by its siblings' real completions.
        if child_statuses:
            if any(s == "timed_out" for s in child_statuses):
                return (
                    "timed_out",
                    RunReasons.TIMED_OUT_DEADLINE,
                    "Flow timed out because at least one child session timed out.",
                    evidence_refs,
                    metadata,
                )
            if any(s == "failed" for s in child_statuses):
                return (
                    "failed",
                    RunReasons.FAILED_EXCEPTION,
                    "Flow failed because at least one child session failed.",
                    evidence_refs,
                    metadata,
                )
            if any(s == "aborted" for s in child_statuses):
                return (
                    "aborted",
                    RunReasons.CANCELLED_SIGINT,
                    "Flow was aborted because at least one child session was aborted (SIGINT).",
                    evidence_refs,
                    metadata,
                )
            if any(s == "cancelled" for s in child_statuses):
                return (
                    "cancelled",
                    RunReasons.CANCELLED_SYSTEM,
                    "Flow was cancelled because at least one child session was cancelled.",
                    evidence_refs,
                    metadata,
                )
            if any(s == "completed_empty" for s in child_statuses) and all(
                s in ("completed", "completed_empty") for s in child_statuses
            ):
                return (
                    "completed_empty",
                    RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
                    "Flow exited clean but at least one child session produced no "
                    "commits ahead of base and no artifacts.",
                    evidence_refs,
                    metadata,
                )
            if all(s == "completed" for s in child_statuses):
                return (
                    "completed",
                    RunReasons.COMPLETED_OK,
                    "All child sessions completed successfully.",
                    evidence_refs,
                    metadata,
                )

        if fallback_status == "completed":
            return (
                "completed",
                RunReasons.COMPLETED_OK,
                "Flow completed successfully.",
                evidence_refs,
                metadata,
            )
        if fallback_status == "timed_out":
            return (
                "timed_out",
                RunReasons.TIMED_OUT_DEADLINE,
                "Flow exceeded its configured timeout.",
                evidence_refs,
                metadata,
            )
        if fallback_status == "aborted":
            return (
                "aborted",
                RunReasons.CANCELLED_SIGINT,
                "Flow was aborted by the user (SIGINT).",
                evidence_refs,
                metadata,
            )
        if fallback_status == "cancelled":
            return (
                "cancelled",
                RunReasons.CANCELLED_SYSTEM,
                "Flow was cancelled by the runtime.",
                evidence_refs,
                metadata,
            )
        return "failed", RunReasons.FAILED_EXCEPTION, "Flow failed.", evidence_refs, metadata


def _earlier_dep_indices(depends_on: list[str] | None, position: int) -> list[int]:
    out: list[int] = []
    for ref in depends_on or []:
        try:
            j = int(str(ref).strip()) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= j < position:
            out.append(j)
        elif j >= position:
            logger.warning("Dropped forward dep ref %s (index %d >= position %d)", ref, j, position)
    return out


def _parse_reactive(spec: str | None) -> tuple[bool, set[str] | None]:
    """Parse --reactive into (reactive, spawn_roles)."""
    s = (spec or "all").strip().lower()
    if s in ("off", "none", "false", "no", "0"):
        return False, set()
    if s in ("all", "on", "true", "yes", "1", ""):
        return True, None
    roles = {r.strip() for r in spec.split(",") if r.strip()}
    return (True, roles) if roles else (True, None)


def _flow_header_fn(w: dict, i: int, n: int) -> list[str]:
    deps = w.get("depends_on") or []
    dep_str = f"  deps: {', '.join(deps)}" if deps else ""
    tag = "  [spawned]" if w.get("spawned") else ""
    return [f"  {w['id']} ({w['name']}){tag}  [{w['model']}]{dep_str}"]


# ── Phase data containers ─────────────────────────────────────────────────────


@dataclass
class _PlanResult:
    """Planning output: resolved assignments and per-agent metadata."""

    assignments: list
    agent_ids: list[str]
    dep_indices: list[list[int]]
    pool: list[str]
    budget_preambles: dict[int, str]


@dataclass
class _DagState:
    """Graph construction output: wired builder nodes and worker metadata."""

    node_ids: list[str]
    known_nodes: set[str]
    deps_by_node: dict[str, list[str]]
    reactive: bool
    spawn_roles: set[str] | None
    role_base: dict[str, object]
    worker_models: list[str]
    op_segments: list[dict] = field(default_factory=list)
    # role → its resolved artifact_defaults (profile first, else casts Role),
    # cached once per role in _build_dag so _execute_dag can register the same
    # contract for a reactively spawned node run under that role — spawned
    # nodes don't exist yet at DAG-build time so can't be folded in there.
    role_artifact_defaults: dict[str, dict | None] = field(default_factory=dict)


@dataclass
class _ExecResult:
    """Execution output: collected agent responses and spawn count."""

    agent_results: list[dict]
    n_spawned: int
    t_exec_elapsed: float
    escalated_agent_ids: list[str] = field(default_factory=list)


# ── Phase 1: build DAG ────────────────────────────────────────────────────────


async def _build_dag(
    env: OrchestrationEnv,
    prompt: str,
    plan_result: _PlanResult,
    *,
    reactive_spec: str,
) -> _DagState:
    """Wire worker branches into the operation graph builder and snapshot to Studio."""
    assignments = plan_result.assignments
    agent_ids = plan_result.agent_ids
    dep_indices = plan_result.dep_indices
    pool = plan_result.pool
    budget_preambles = plan_result.budget_preambles

    reactive, spawn_roles = _parse_reactive(reactive_spec)

    def _may_spawn(role: str) -> bool:
        return reactive and (spawn_roles is None or role in spawn_roles)

    worker_models: list[str] = []
    node_ids: list[str] = []
    role_base: dict[str, object] = {}
    role_artifact_entries: list[dict] = []
    role_artifact_defaults: dict[str, dict | None] = {}

    for i, ta in enumerate(assignments):
        w_branch, w_model, w_profile = await build_worker_branch(
            env,
            agent_id=agent_ids[i],
            role=ta.assignee,
            model_override=pool[i % len(pool)] if pool else None,
            explicit_name=agent_ids[i],
            grant_spawn=_may_spawn(ta.assignee),
            modes=ta.modes or None,
        )
        worker_models.append(w_model)
        role_base.setdefault(ta.assignee, w_branch)

        # ADR-0029: fold this leg's OWN declared artifact contract (profile
        # first, else the casts role's artifact_defaults — e.g. reviewer/
        # critic) into the flow-wide contract, namespaced under this leg's
        # own artifact subdirectory. A role that declares nothing leaves the
        # contract untouched — this only fires for a real declaration.
        if ta.assignee in role_artifact_defaults:
            role_defaults = role_artifact_defaults[ta.assignee]
        else:
            role_defaults = w_profile.artifact_defaults if w_profile else None
            if not role_defaults:
                from lionagi.casts.pattern import Role as _Role

                with contextlib.suppress(ValueError):
                    role_defaults = _Role.load(ta.assignee).artifact_defaults
            role_artifact_defaults[ta.assignee] = role_defaults
        leg_expected: list[dict] = []
        if role_defaults:
            for entry in role_defaults.get("expected", []):
                eid = entry.get("id", "")
                epath = entry.get("path", "")
                leg_expected.append(
                    {
                        **entry,
                        "id": f"{agent_ids[i]}__{eid}",
                        "path": f"{agent_ids[i]}/{epath}",
                        "source": "role_default",
                    }
                )
            role_artifact_entries.extend(leg_expected)

        ctx: list = [{"original_task": prompt}]
        artifact_note = (
            f"Your artifact directory: {env.run.agent_artifact_dir(agent_ids[i])}/ — "
            "write output files here."
        )
        if leg_expected:
            required_paths = ", ".join(e["path"].split("/", 1)[1] for e in leg_expected)
            artifact_note += (
                f" REQUIRED: write {required_paths} in that directory — the run "
                "is marked failed if it is missing at completion."
            )
        if dep_indices[i]:
            ups = "; ".join(
                f"step {j + 1} ({agent_ids[j]}): {env.run.agent_artifact_dir(agent_ids[j])}/"
                for j in dep_indices[i]
            )
            artifact_note += f" Upstream deps: {ups}."
        ctx.append({"artifact_instructions": artifact_note})
        if env.team_data:
            ctx.append(
                {
                    "team": {
                        "id": env.team_data["id"],
                        "name": env.team_data["name"],
                        "your_name": agent_ids[i],
                    }
                }
            )
        w_effort = env.effort
        if not env.bare and w_profile and w_profile.effort:
            w_effort = w_profile.effort
        if w_effort:
            ctx.append({"effort_guidance": EFFORT_MAP.get(w_effort, "")})

        instruction = budget_preambles.get(i, "") + ta.task
        dep_nodes = [node_ids[j] for j in dep_indices[i]]
        node = env.builder.add_operation(
            "operate",
            branch=w_branch,
            depends_on=dep_nodes or None,
            instruction=instruction,
            context=ctx,
        )
        node_ids.append(node)

    known_nodes = set(node_ids)
    deps_by_node = {
        node_ids[i]: [str(j + 1) for j in dep_indices[i]] for i in range(len(assignments))
    }

    # Early DAG snapshot for Studio.
    early_graph = {
        "agents": [
            {"id": agent_ids[i], "name": agent_ids[i], "model": worker_models[i]}
            for i in range(len(assignments))
        ],
        "operations": [
            {
                "id": agent_ids[i],
                "agent_id": agent_ids[i],
                "control": False,
                "depends_on": [str(j + 1) for j in dep_indices[i]],
            }
            for i in range(len(assignments))
        ],
    }
    env._finalize_extras = early_graph
    ctx_lp = getattr(env, "_live_persist", None)
    if ctx_lp and ctx_lp.get("db"):
        with contextlib.suppress(Exception):
            _markers = ctx_lp.get("identity_markers") or {}
            await ctx_lp["db"].update_session(
                ctx_lp["session_id"], node_metadata=json.dumps({**early_graph, **_markers})
            )

    # Persist the per-leg role/profile artifact declarations collected above.
    # start_live_persist already ran (playbook/whole-flow contract, if any) —
    # this extends that contract now that per-leg roles are resolved, so
    # teardown's verify_artifact_contract (reading ctx["artifact_contract"])
    # sees the full picture. Validated eagerly: a malformed role declaration
    # should fail loudly here, not be silently dropped.
    #
    # ADR-0029 extension (see db.py _SESSION_COLUMNS comment): this is the
    # one allowed post-creation write to artifact_contract_json, happening
    # once here at DAG-build time, before _execute_dag runs any leg. It must
    # reach the session row (not just env._live_persist) — a crash or
    # orphan exit before teardown should still leave the DB row showing what
    # was actually expected, matching what Studio/`li state show-session`
    # read directly from artifact_contract_json.
    if role_artifact_entries and ctx_lp is not None:
        from lionagi.state.artifact_verifier import validate_artifact_contract

        existing = ctx_lp.get("artifact_contract") or {"expected": []}
        merged_contract = {"expected": [*existing.get("expected", []), *role_artifact_entries]}
        validate_artifact_contract(merged_contract)
        ctx_lp["artifact_contract"] = merged_contract
        if ctx_lp.get("db"):
            with contextlib.suppress(Exception):
                await ctx_lp["db"].update_session(
                    ctx_lp["session_id"], artifact_contract_json=json.dumps(merged_contract)
                )

    return _DagState(
        node_ids=node_ids,
        known_nodes=known_nodes,
        deps_by_node=deps_by_node,
        reactive=reactive,
        spawn_roles=spawn_roles,
        role_base=role_base,
        worker_models=worker_models,
        role_artifact_defaults=role_artifact_defaults,
    )


# ── Resume: pre-mark checkpoint-completed nodes ───────────────────────────────


def _apply_checkpoint_precompletion(
    env: OrchestrationEnv,
    plan_result: _PlanResult,
    dag_state: _DagState,
    checkpoint_ops: dict[str, dict],
    *,
    allow_degraded_context: bool,
    checkpoint_spawned: list[dict] | None = None,
) -> None:
    """Mark nodes the checkpoint recorded as terminal so the executor's
    pre-completed seam (DependencyAwareExecutor._execute_operation's
    terminal-status skip) short-circuits them outright instead of re-running.

    completed ops restore their response and are treated as done; failed ops
    are restored as terminal FAILED rather than silently re-run — a failed op
    may already have produced side effects or partial artifacts before the
    process died, so resume must never guess at retry semantics on its own.

    A pending op that declared inherit_context expects its predecessor's
    actual conversation history, which resume does not restore (v1: results
    flow through parameters exactly as live, message history does not) — that
    combination is refused loudly unless the caller passes
    allow_degraded_context, naming every affected op so it is an informed
    choice rather than a silent correctness trap.

    checkpoint_spawned refuses resume outright when non-empty: reactively
    spawned nodes are not replayed by this version (their DAG position and
    branch aren't reconstructible from the persisted plan alone), so silently
    proceeding would drop completed spawned work without telling anyone.
    """
    from lionagi.protocols.types import EventStatus

    if checkpoint_spawned:
        spawned_ids = ", ".join(str(e.get("node_id", "?")) for e in checkpoint_spawned)
        raise FlowResumeError(
            "Resume refused: this checkpoint recorded reactively spawned "
            f"node(s) [{spawned_ids}] that this version cannot replay. "
            "Resuming would silently drop that completed work. Re-run the "
            "flow from scratch, or resume only checkpoints without spawned "
            "entries."
        )

    graph = env.builder.get_graph()
    degraded: list[str] = []

    for agent_id, node_id in zip(plan_result.agent_ids, dag_state.node_ids, strict=True):
        node = graph.internal_nodes.get(node_id)
        if node is None:
            continue
        entry = checkpoint_ops.get(agent_id)
        if entry and entry.get("status") == "completed":
            node.execution.status = EventStatus.COMPLETED
            node.execution.response = entry.get("response")
        elif entry and entry.get("status") == "failed":
            node.execution.status = EventStatus.FAILED
            node.execution.response = entry.get("response")
        elif node.metadata.get("inherit_context"):
            degraded.append(agent_id)

    if degraded and not allow_degraded_context:
        raise FlowResumeError(
            "Resume refused: pending op(s) "
            f"{', '.join(degraded)} expect inherited conversational context "
            "that resume cannot restore (v1 restores results-context only). "
            "Pass --allow-degraded-context to run them against an empty "
            "branch instead."
        )


# ── Phase 2: execution ────────────────────────────────────────────────────────


async def _execute_dag(
    env: OrchestrationEnv,
    plan_result: _PlanResult,
    dag_state: _DagState,
    *,
    max_concurrent: int,
    max_ops: int,
    checkpoint_prompt: str = "",
    checkpoint_plan: list[dict] | None = None,
    checkpoint_config: dict | None = None,
    checkpoint_ops_seed: dict[str, dict] | None = None,
    checkpoint_flow_context: dict | None = None,
) -> _ExecResult:
    """Drive the planning engine over the DAG and collect per-agent results.

    checkpoint_config is the opt-in gate for the whole checkpoint writer:
    only when it is not None is a CheckpointWriter constructed and wired
    into the completion/failure observers below. Tests that stub
    env.run/env.builder with MagicMock (no checkpoint_config passed) are
    unaffected — the writer, and every os.replace() it would do, never exists.

    checkpoint_flow_context seeds the executor's shared context workspace on
    resume with whatever was accumulated (via operation.response["context"])
    before the crash — without it, pending ops on a resumed run would see an
    empty workspace instead of what they would have seen live.
    """
    assignments = plan_result.assignments
    agent_ids = plan_result.agent_ids

    reactive = dag_state.reactive
    spawn_roles = dag_state.spawn_roles
    node_ids = dag_state.node_ids
    known_nodes = dag_state.known_nodes
    known_node_strs = {str(n) for n in known_nodes}
    deps_by_node = dag_state.deps_by_node
    worker_models = dag_state.worker_models
    role_base = dag_state.role_base
    _op_segments = dag_state.op_segments

    # Shared out-of-band handle for the live executor, populated synchronously
    # by DependencyAwareExecutor.__init__ the moment run_dag() constructs it —
    # both the control poller and the checkpoint writer's per-completion hook
    # (below) read from it.
    _executor_ref: dict[str, object] = {}
    _checkpoint_tasks: list = []

    _checkpoint_writer: CheckpointWriter | None = None
    if checkpoint_config is not None:
        _ctx_lp = getattr(env, "_live_persist", None)
        _checkpoint_writer = CheckpointWriter(
            path=env.run.checkpoint_path,
            session_id=(_ctx_lp or {}).get("session_id") or "",
            prompt=checkpoint_prompt,
            plan=checkpoint_plan or [],
            config=checkpoint_config,
            # Seed with whatever was restored from a prior checkpoint (empty
            # on a fresh, non-resumed run) so this generation's checkpoint
            # carries the context forward even if zero ops complete before
            # the next crash — otherwise a resume-of-a-resume would silently
            # lose it despite nothing having gone wrong with restoration.
            flow_context=dict(checkpoint_flow_context or {}),
            ops=dict(checkpoint_ops_seed or {}),
        )
        with contextlib.suppress(Exception):
            await _checkpoint_writer.flush()

    await _persist_session_phase(env, "executing")
    if reactive:
        scope = "all workers" if spawn_roles is None else f"roles {sorted(spawn_roles)}"
        progress(f"Executing reactive DAG: {len(assignments)} assignments (spawn: {scope})...")
    else:
        progress(f"Executing DAG (reactive off): {len(assignments)} assignments...")
    conc = max_concurrent if max_concurrent > 0 else max(len(assignments), 1)
    # Spawn budget: when --max-ops is set, the initial plan + spawns share it.
    # Otherwise fall back to a conservative default so an un-capped reactive run
    # cannot quietly fan out to dozens of (costly) child agents.
    max_spawn = max(0, max_ops - len(assignments)) if max_ops > 0 else 20

    heartbeat_interval = 60
    max_idle_seconds = 600

    def _persist_segments():
        ctx = getattr(env, "_live_persist", None)
        if not ctx or not ctx.get("db"):
            return
        extras = getattr(env, "_finalize_extras", {}) or {}
        extras["segments"] = _op_segments
        env._finalize_extras = extras

        async def _do():
            with contextlib.suppress(Exception):
                # Merge kill-identity markers last so segment writes keep the PID.
                _markers = ctx.get("identity_markers") or {}
                await ctx["db"].update_session(
                    ctx["session_id"], node_metadata=json.dumps({**extras, **_markers})
                )

        _asyncio.ensure_future(_do())

    def _update_branch_status(branch_name: str, new_status: str):
        ctx = getattr(env, "_live_persist", None)
        if not ctx or not ctx.get("db"):
            return
        branch = next((b for b in env.session.branches if b.name == branch_name), None)
        if not branch:
            return

        async def _do():
            with contextlib.suppress(Exception):
                kw = {"status": new_status}
                if new_status == "running":
                    kw["started_at"] = time.time()
                elif new_status in ("completed", "failed"):
                    kw["ended_at"] = time.time()
                await ctx["db"].update_branch(str(branch.id), **kw)

        _asyncio.ensure_future(_do())

    def _record_segment(op_id: str, branch_name: str, new_status: str):
        branch = next((b for b in env.session.branches if b.name == branch_name), None)
        branch_id = str(branch.id) if branch else ""
        now = time.time()
        if new_status == "running":
            _op_segments.append(
                {
                    "op_id": op_id,
                    "branch_id": branch_id,
                    "branch_name": branch_name,
                    "status": "running",
                    "started_at": now,
                    "ended_at": None,
                    "last_heartbeat_at": None,
                }
            )
        else:
            for seg in reversed(_op_segments):
                if seg["op_id"] == op_id:
                    seg["status"] = new_status
                    seg["ended_at"] = now
                    break
        _persist_segments()

    def _checkpoint_record(sig, status: str) -> None:
        """Fire-and-forget the checkpoint write for one op's outcome.

        sig.name is the agent_id (explicit_name at add_operation time) for a
        PLANNED node only — a reactively spawned node's branch can carry a
        name identical to a planned agent_id's (clones inherit the source
        branch's name), so sig.name is never trustworthy as a checkpoint key
        on its own. sig.op_id (the stringified node UUID) against
        known_node_strs is the only reliable way to tell a planned node from
        a spawned one; spawned nodes are recorded separately by node id via
        record_spawned so they can never collide with / overwrite a planned
        op's `ops` entry.
        """
        if _checkpoint_writer is None:
            return
        executor = _executor_ref.get("executor")
        response = None
        flow_ctx = None
        if executor is not None:
            with contextlib.suppress(Exception):
                from uuid import UUID as _UUID

                response = executor.results.get(_UUID(sig.op_id))
            with contextlib.suppress(Exception):
                flow_ctx = dict(executor.context.content)
        if sig.op_id in known_node_strs:
            _checkpoint_tasks.append(
                _asyncio.ensure_future(
                    _checkpoint_writer.record(
                        sig.name, status=status, response=response, flow_context=flow_ctx
                    )
                )
            )
        else:
            _checkpoint_tasks.append(
                _asyncio.ensure_future(
                    _checkpoint_writer.record_spawned(
                        sig.op_id, status=status, response=response, flow_context=flow_ctx
                    )
                )
            )

    def _on_node_started(sig, _ctx):
        progress(f"  ▶ {sig.name} started")
        _update_branch_status(sig.name, "running")
        _record_segment(sig.op_id, sig.name, "running")

    def _on_node_completed(sig, _ctx):
        progress(f"  ✓ {sig.name} done ({sig.elapsed:.1f}s)")
        _update_branch_status(sig.name, "completed")
        _record_segment(sig.op_id, sig.name, "completed")
        _checkpoint_record(sig, "completed")

    def _on_node_failed(sig, _ctx):
        progress(f"  ✗ {sig.name} FAILED ({sig.elapsed:.1f}s)")
        _update_branch_status(sig.name, "failed")
        _record_segment(sig.op_id, sig.name, "failed")
        _checkpoint_record(sig, "failed")

    # ADR-0075 §4: run_dag drives the session bus; observers above consume the signals.
    async def _heartbeat_loop() -> None:
        while True:
            await _asyncio.sleep(heartbeat_interval)
            _now = time.time()
            for _seg in _op_segments:
                if _seg["status"] != "running":
                    continue
                _elapsed = _now - _seg.get("started_at", _now)
                _seg["last_heartbeat_at"] = _now
                progress(f"  · {_seg['branch_name']} heartbeat {_elapsed / 60:.0f}m")
                if _elapsed > max_idle_seconds:
                    progress(
                        f"  ⚠ IDLE STALL: {_seg['branch_name']} running {_elapsed:.0f}s "
                        "with no completion — possible hung child process"
                    )

    # ADR-0085 part 1: control poller — the only consumer of session_controls
    # rows queued by `li o ctl pause|resume|msg`. _executor_ref (declared
    # above, shared with the checkpoint writer's completion hook) is
    # populated synchronously by DependencyAwareExecutor.__init__ the moment
    # run_dag() constructs it, so the "executor not yet available" window
    # below is at most one event-loop tick, well inside poll_interval.
    _control_log: list[dict] = []

    def _persist_control_log() -> None:
        ctx = getattr(env, "_live_persist", None)
        if not ctx or not ctx.get("db"):
            return
        extras = getattr(env, "_finalize_extras", {}) or {}
        extras["controls"] = _control_log
        env._finalize_extras = extras

        async def _do():
            with contextlib.suppress(Exception):
                _markers = ctx.get("identity_markers") or {}
                await ctx["db"].update_session(
                    ctx["session_id"], node_metadata=json.dumps({**extras, **_markers})
                )

        _asyncio.ensure_future(_do())

    async def _control_poll_loop() -> None:
        while True:
            await _asyncio.sleep(_CONTROL_POLL_INTERVAL)
            ctx = getattr(env, "_live_persist", None)
            if not ctx or not ctx.get("db"):
                continue
            executor = _executor_ref.get("executor")
            if executor is None:
                continue
            try:
                pending = await ctx["db"].list_pending_session_controls(ctx["session_id"])
            except Exception as exc:  # noqa: BLE001 — transient DB hiccup, retry next tick
                logger.debug("control poll: transient error listing pending controls: %s", exc)
                continue
            for row in pending:
                applied_result = await _apply_session_control(ctx["db"], executor, row)
                if applied_result == _CONTROL_UNSTAMPED:
                    break
                if applied_result is not None:
                    _control_log.append(
                        {
                            "id": row["id"],
                            "verb": row["verb"],
                            "result": applied_result,
                            "ts": time.time(),
                        }
                    )
                    _persist_control_log()

    from lionagi.engines import PlanningEngine
    from lionagi.session.signal import NodeCompleted, NodeFailed, NodeStarted

    env.session.observe(NodeStarted, handler=_on_node_started)
    env.session.observe(NodeCompleted, handler=_on_node_completed)
    env.session.observe(NodeFailed, handler=_on_node_failed)
    eng_run = PlanningEngine().new_run(session=env.session)

    t_exec = time.monotonic()
    _hb_task = _asyncio.ensure_future(_heartbeat_loop())
    _ctl_task = _asyncio.ensure_future(_control_poll_loop())
    try:
        dag_result = await eng_run.run_dag(
            env.builder.get_graph(),
            reactive=reactive,
            spawn_type=SpawnRequest if reactive else None,
            node_builder=role_node_builder(role_base) if reactive else None,
            max_spawn=max_spawn,
            max_concurrent=conc,
            verbose=env.verbose,
            executor_ref=_executor_ref,
            context=checkpoint_flow_context,
        )
    finally:
        _hb_task.cancel()
        _ctl_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError):
            await _hb_task
        with contextlib.suppress(_asyncio.CancelledError):
            await _ctl_task
    t_exec_elapsed = time.monotonic() - t_exec

    # Drain every scheduled checkpoint write before returning — the whole
    # point of the writer is surviving a crash right after this function
    # returns, so the last op's completion must be durably on disk by now,
    # not still queued behind a fire-and-forget task.
    if _checkpoint_tasks:
        with contextlib.suppress(Exception):
            await _asyncio.gather(*_checkpoint_tasks, return_exceptions=True)

    op_results = dag_result.get("operation_results", {})
    n_spawned = dag_result.get("spawned_operations", 0)

    # Escalation backstop: a leg the executor tracked as escalated (gave up
    # instead of producing a result — see NodeEscalated / EscalationRequest,
    # ADR-0072/0083) reads as a normal completed op_result to the loop below.
    # Without this, a reviewer/critic that emits EscalationRequest(route=
    # "give_up") instead of writing its artifact is indistinguishable from a
    # clean completion once execution finishes — this makes it loud at
    # teardown even when no artifact_defaults declaration exists to catch it.
    #
    # The escalation tracker itself is plan-agnostic: it records any emitting
    # node's id whether that node was planned up front or spawned mid-run via
    # SpawnRequest (reactive mode). Spawned nodes never appear in node_ids/
    # agent_ids (those are fixed-size arrays built once from the initial
    # assignments), so they must be checked separately against known_nodes
    # rather than only via the plan-time index walk below.
    escalated_op_ids = {str(x) for x in dag_result.get("escalated_operations", [])}
    escalated_evidence = [
        {"kind": "escalated_operation", "id": agent_ids[i], "label": assignments[i].assignee}
        for i in range(len(assignments))
        if node_ids[i] in escalated_op_ids
    ]
    for spawned_nid in sorted(escalated_op_ids - known_nodes):
        escalated_evidence.append(
            {"kind": "escalated_operation", "id": spawned_nid, "label": spawned_nid}
        )
    escalated_agent_ids = [entry["id"] for entry in escalated_evidence]
    if escalated_evidence:
        env._escalated_evidence = escalated_evidence

    agent_results: list[dict] = []

    def _record_result(result: dict) -> None:
        agent_results.append(result)
        with contextlib.suppress(OSError):
            agent_dir = env.run.agent_artifact_dir(result["agent_id"])
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / f"{result['id']}.md").write_text(result["response"])

    for i in range(len(assignments)):
        nid = node_ids[i]
        res = op_results.get(nid)
        _record_result(
            {
                "id": agent_ids[i],
                "agent_id": agent_ids[i],
                "name": agent_ids[i],
                "model": worker_models[i],
                "depends_on": deps_by_node[nid],
                "spawned": False,
                "response": str(res) if res is not None else "(no response)",
                "time_ms": t_exec_elapsed * 1000,
            }
        )

    # Reactively spawned nodes are in the result map but not in our plan. Their
    # graph node still carries the assignee role_node_builder stamped on it
    # (role_node_builder in lionagi/orchestration/patterns.py) and the branch
    # the executor ultimately ran it on, so both are recovered here — plan-
    # time arrays (agent_ids/worker_models) are fixed-size and can't cover
    # nodes injected mid-run via SpawnRequest.
    graph_nodes = getattr(env.builder.get_graph(), "internal_nodes", {}) or {}
    spawned_contract_entries: list[dict] = []
    spawn_idx = 0
    for nid, res in op_results.items():
        if nid in known_nodes:
            continue
        spawn_idx += 1
        sid = f"spawn-{spawn_idx}"
        graph_node = graph_nodes.get(nid)
        assignee = graph_node.metadata.get("assignee") if graph_node is not None else None
        spawn_model = ""
        if graph_node is not None and graph_node.branch_id is not None:
            with contextlib.suppress(Exception):
                branch = env.session.branches[graph_node.branch_id]
                from lionagi.state import provenance as _provenance

                ep_cfg = branch.chat_model.endpoint.config
                spawn_model = _provenance.resolve_model_spec(
                    getattr(ep_cfg, "provider", None), (ep_cfg.kwargs or {}).get("model")
                )
        _record_result(
            {
                "id": sid,
                "agent_id": sid,
                "name": assignee or "spawned",
                "model": spawn_model,
                "assignee": assignee,
                "depends_on": [],
                "spawned": True,
                "response": str(res) if res is not None else "(no response)",
                "time_ms": t_exec_elapsed * 1000,
            }
        )

        # Record the spawned node's role-declared artifacts in the session
        # contract for post-run visibility (synthesis, Studio), namespaced
        # under the node's own subdir. These are folded as non-required: a
        # reactively spawned node is built with only its instruction and is
        # never told its artifact dir, so it has no path to satisfy a required
        # entry — enforcing one would flip an otherwise-completed run to failed.
        if assignee:
            role_defaults = dag_state.role_artifact_defaults.get(assignee)
            if role_defaults:
                for entry in role_defaults.get("expected", []):
                    eid = entry.get("id", "")
                    epath = entry.get("path", "")
                    spawned_contract_entries.append(
                        {
                            **entry,
                            "id": f"{sid}__{eid}",
                            "path": f"{sid}/{epath}",
                            "required": False,
                            "source": "role_default",
                        }
                    )

    ctx_lp = getattr(env, "_live_persist", None)
    if spawned_contract_entries and ctx_lp is not None:
        from lionagi.state.artifact_verifier import validate_artifact_contract

        existing = ctx_lp.get("artifact_contract") or {"expected": []}
        merged_contract = {"expected": [*existing.get("expected", []), *spawned_contract_entries]}
        validate_artifact_contract(merged_contract)
        ctx_lp["artifact_contract"] = merged_contract
        if ctx_lp.get("db"):
            with contextlib.suppress(Exception):
                await ctx_lp["db"].update_session(
                    ctx_lp["session_id"], artifact_contract_json=json.dumps(merged_contract)
                )

    spawn_note = f" (+{n_spawned} spawned)" if n_spawned else ""
    progress(f"DAG done ({t_exec_elapsed:.1f}s){spawn_note}.")

    return _ExecResult(
        agent_results=agent_results,
        n_spawned=n_spawned,
        t_exec_elapsed=t_exec_elapsed,
        escalated_agent_ids=escalated_agent_ids,
    )


# ── Phase 3: synthesis ────────────────────────────────────────────────────────


async def _synthesize(
    env: OrchestrationEnv,
    prompt: str,
    plan_result: _PlanResult,
    dag_state: _DagState,
    exec_result: _ExecResult,
    *,
    synthesis_model: str | None,
    model_spec: str,
) -> dict | None:
    """Synthesize leaf-node outputs via the orchestrator branch; returns result dict or None."""
    agent_results = exec_result.agent_results
    if not agent_results:
        return None

    assignments = plan_result.assignments
    dep_indices = plan_result.dep_indices
    node_ids = dag_state.node_ids

    synth_spec = synthesis_model or model_spec
    synth_label = str(parse_model_spec(synth_spec))
    await _persist_session_phase(env, "synthesizing")
    progress(f"Synthesis [{synth_label}]...")

    # Leaf nodes = those nothing else depends on.
    depended: set[str] = set()
    for i in range(len(assignments)):
        for j in dep_indices[i]:
            depended.add(node_ids[j])
    leaf_nodes = [n for n in node_ids if n not in depended] or list(node_ids)

    artifacts = [f"[{r['id']} via {r['name']}]: {r['response']}" for r in agent_results]
    # Derived from agent_results (the executor's ground truth), not the
    # plan-time agent_ids array — reactively spawned nodes have their own
    # artifact dir (agent_results[i]["agent_id"]) and would otherwise be
    # silently omitted from what the synthesizer is told to read.
    adirs = [str(env.run.agent_artifact_dir(r["agent_id"])) for r in agent_results]
    team_synth_note = ""
    if env.team_data:
        team_synth_note = (
            f"\n\nTEAM MESSAGES: Review inter-agent messages (team {env.team_data['id']}) "
            "for coordination context not captured in artifacts."
        )

    synth_node = env.builder.add_operation(
        "operate",
        branch=env.orc_branch,
        depends_on=leaf_nodes,
        instruction=(
            f"Synthesize all op outputs into a final cohesive deliverable.\n\n"
            f"Original task: {prompt}\n\n"
            "Your synthesis must:\n"
            "1. RECONCILE: When ops disagree, present both views with evidence.\n"
            "2. FILL GAPS: Name what no op covered.\n"
            "3. TRACE: Show how work flowed through the DAG, including any "
            "reactively spawned follow-ups.\n"
            "4. RESUME: End with branch IDs so the user can follow up with any agent."
            f"\n\nARTIFACT CHAIN: Read ALL files in: {', '.join(adirs)}."
            f"{team_synth_note}"
        ),
        context=artifacts,
    )
    t_synth = time.monotonic()
    synth_result_raw = await env.session.flow(env.builder.get_graph(), verbose=env.verbose)
    t_synth_elapsed = time.monotonic() - t_synth
    synth_res = synth_result_raw.get("operation_results", {}).get(synth_node)
    synthesis_result = {
        "model": synth_label,
        "response": str(synth_res) if synth_res is not None else "(no response)",
        "time_ms": t_synth_elapsed * 1000,
    }
    progress(f"Synthesis done ({t_synth_elapsed:.1f}s).")
    return synthesis_result


# ── Phase 4: finalize ─────────────────────────────────────────────────────────


def _finalize_flow(
    env: OrchestrationEnv,
    prompt: str,
    plan_result: _PlanResult,
    dag_state: _DagState,
    exec_result: _ExecResult,
    synthesis_result: dict | None,
    *,
    output_format: str,
    show_graph: bool,
) -> str:
    """Format output, write synthesis artifact, post team messages, and finalize run."""
    agent_results = exec_result.agent_results
    n_spawned = exec_result.n_spawned
    assignments = plan_result.assignments
    agent_ids = plan_result.agent_ids
    worker_models = dag_state.worker_models

    if output_format == "json":
        output = _format_result_json(agent_results, synthesis_result)
    else:
        output = _format_result_text(agent_results, synthesis_result, header_fn=_flow_header_fn)

    if synthesis_result:
        env.run.synthesis_path.write_text(synthesis_result["response"])

    if env.team_data:
        _post_results_to_team(env.team_data, agent_results, agent_ids, synthesis_result)

    # "agents" must cover every id that "operations" (below) can reference —
    # operations is built from agent_results, which already includes
    # reactively spawned nodes (spawned=True), so agents has to walk the same
    # ground truth rather than only the fixed-size plan-time assignments, or
    # a spawned op's id resolves to nothing in UI/Studio agent lookups.
    agents_meta = [
        {
            "id": agent_ids[i],
            "name": agent_ids[i],
            "model": worker_models[i],
            "artifact_dir": str(env.run.agent_artifact_dir(agent_ids[i])),
            "spawned": False,
        }
        for i in range(len(assignments))
    ]
    agents_meta.extend(
        {
            "id": r["agent_id"],
            "name": r.get("assignee") or r["name"],
            "model": r.get("model", ""),
            "artifact_dir": str(env.run.agent_artifact_dir(r["agent_id"])),
            "spawned": True,
        }
        for r in agent_results
        if r.get("spawned")
    )

    finalize_orchestration(
        env,
        kind="flow",
        prompt=prompt,
        extras={
            "agents": agents_meta,
            "operations": [
                {
                    "id": r["id"],
                    "agent_id": r["agent_id"],
                    "control": False,
                    "spawned": r.get("spawned", False),
                    "depends_on": r.get("depends_on") or [],
                }
                for r in agent_results
            ],
        },
    )

    if show_graph:
        from lionagi.operations._visualize_graph import visualize_graph

        with contextlib.suppress(Exception):
            visualize_graph(
                env.builder,
                title=f"Flow DAG — {len(assignments)} assignments (+{n_spawned} spawned)",
                save_path=str(env.run.dag_image_path),
            )

    return output


# ── Public entry points ───────────────────────────────────────────────────────


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
    workers_str: str | None = None,
    max_ops: int = 0,
    dry_run: bool = False,
    show_graph: bool = False,
    reactive_spec: str = "all",
    fast: bool = False,
    playbook_name: str | None = None,
    playbook_artifacts: dict | None = None,
    invocation_id: str | None = None,
    project: str | None = None,
    pack: str | None = None,
    resume_checkpoint: dict | None = None,
    allow_degraded_context: bool = False,
    notify: str | None = None,
    **legacy_kwargs,
) -> tuple[str, str]:
    """Returns (output, terminal_status)."""
    if "max_agents" in legacy_kwargs and max_ops == 0:
        max_ops = legacy_kwargs.pop("max_agents")
    elif "max_agents" in legacy_kwargs:
        legacy_kwargs.pop("max_agents")
    if legacy_kwargs:
        raise TypeError(f"_run_flow() got unexpected keyword arguments: {list(legacy_kwargs)}")

    _started_at = time.time()
    _invocation_kind = "play" if playbook_name else "flow"

    # The checkpoint's own "config" replays THIS call's kwargs verbatim on
    # --resume (dry_run/show_graph excluded deliberately — those are
    # presentation flags for the CURRENT invocation, not "what happened").
    # Built unconditionally: a resumed run's own checkpoint must be just as
    # resumable as the first one.
    _checkpoint_config = {
        "model_spec": model_spec,
        "with_synthesis": with_synthesis,
        "synthesis_model": synthesis_model,
        "max_concurrent": max_concurrent,
        "yolo": yolo,
        "bypass": bypass,
        "verbose": verbose,
        "effort": effort,
        "theme": theme,
        "output_format": output_format,
        "save_dir": save_dir,
        "team_name": team_name,
        "team_attach": team_attach,
        "cwd": cwd,
        "timeout": timeout,
        "agent_name": agent_name,
        "bare": bare,
        "workers_str": workers_str,
        "max_ops": max_ops,
        "reactive_spec": reactive_spec,
        "fast": fast,
        "playbook_name": playbook_name,
        "playbook_artifacts": playbook_artifacts,
        "invocation_id": invocation_id,
        "project": project,
        "pack": pack,
    }

    env = await setup_orchestration(
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
        total_budget=timeout,
        pack=pack,
    )

    _orc_model, _orc_provider = parse_orchestrator_provider(env.default_model_spec)

    artifact_contract = None
    if playbook_artifacts is not None or (
        agent_name is not None and getattr(env.orc_profile, "artifact_defaults", None) is not None
    ):
        from lionagi.state.artifact_verifier import resolve_artifact_contract

        agent_defaults = (
            getattr(env.orc_profile, "artifact_defaults", None) if agent_name is not None else None
        )
        artifact_contract = resolve_artifact_contract(
            playbook_artifacts=playbook_artifacts,
            agent_defaults=agent_defaults,
        )

    # Every flow run stamps its own run_id into node_metadata so a later
    # --resume can resolve this session back to a checkpoint; a resumed run
    # additionally links to the run it resumed from.
    _extra_node_metadata: dict = {"run_id": env.run.run_id}
    if resume_checkpoint is not None and resume_checkpoint.get("session_id"):
        _extra_node_metadata["resumed_from"] = resume_checkpoint["session_id"]

    await start_live_persist(
        env,
        invocation_kind=_invocation_kind,
        playbook_name=playbook_name,
        agent_name=agent_name,
        artifacts_path=str(env.run.artifact_root),
        invocation_id=invocation_id,
        model=_orc_model,
        provider=_orc_provider,
        effort=env.effort,
        project=project,
        artifact_contract=artifact_contract,
        extra_node_metadata=_extra_node_metadata,
    )

    inner_kw = dict(
        env=env,
        with_synthesis=with_synthesis,
        synthesis_model=synthesis_model,
        max_concurrent=max_concurrent,
        output_format=output_format,
        team_name=team_name,
        team_attach=team_attach,
        workers_str=workers_str,
        max_ops=max_ops,
        dry_run=dry_run,
        show_graph=show_graph,
        reactive_spec=reactive_spec,
        resume_checkpoint=resume_checkpoint,
        allow_degraded_context=allow_degraded_context,
        checkpoint_config=_checkpoint_config,
    )
    _terminal_status = "completed"
    result: str = ""
    try:
        if timeout:
            with move_on_after(timeout) as cancel_scope:
                result = await _run_flow_inner(model_spec, prompt, **inner_kw)
            if cancel_scope.cancelled_caught:
                _terminal_status = "timed_out"
                raise LionTimeoutError(f"Flow timed out after {timeout}s")
        else:
            result = await _run_flow_inner(model_spec, prompt, **inner_kw)
    except BaseException as exc:
        _terminal_status = classify_exception(exc)
        raise
    finally:
        with CancelScope(shield=True):
            effective_status = await stop_live_persist(env, status=_terminal_status)
            if effective_status != _terminal_status:
                _terminal_status = effective_status
            import time as _time

            from lionagi.cli.status import _classify

            # ended_at/notify_status are meaningful regardless of whether an
            # invocation_id is tracked — an invocation-less --notify run must
            # still fire (the caller asked for it), just with a null id.
            _ended_at = _time.time()
            _notify_status = _terminal_status
            if invocation_id:
                from lionagi.state.db import StateDB

                try:
                    (
                        inv_status,
                        inv_rc,
                        inv_rs,
                        inv_ev,
                        inv_meta,
                    ) = await _resolve_invocation_terminal_flow(
                        invocation_id, fallback_status=_terminal_status
                    )
                    _notify_status = inv_status
                    async with StateDB() as _inv_db:
                        await _inv_db.update_invocation(invocation_id, ended_at=_ended_at)
                        await _inv_db.update_status(
                            "invocation",
                            invocation_id,
                            new_status=inv_status,
                            reason_code=inv_rc,
                            reason_summary=inv_rs,
                            evidence_refs=inv_ev,
                            source="executor",
                            actor=invocation_id,
                            metadata=inv_meta,
                        )
                except Exception:
                    import logging as _logging

                    _logging.getLogger("lionagi.cli").exception(
                        "Failed to finalize invocation %s", invocation_id
                    )

            # Fire the terminal-notify hook exactly once, after the
            # invocation's terminal status is as final as it gets here —
            # never lets a hook failure affect the run's own status.
            _, _notify_exit_class, _ = _classify("invocation", _notify_status)
            await fire_terminal_notify(
                invocation_id=invocation_id,
                kind=_invocation_kind,
                playbook=playbook_name,
                status=_notify_status,
                save_dir=save_dir,
                cwd=cwd or os.getcwd(),
                exit_class=_notify_exit_class,
                started_at=_started_at,
                ended_at=_ended_at,
                override_command=notify,
                project_dir=cwd,
            )
            for _br in env.session.branches:
                await _br.mdls.shutdown()

    return result, _terminal_status


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
    workers_str: str | None = None,
    max_ops: int = 0,
    dry_run: bool = False,
    show_graph: bool = False,
    reactive_spec: str = "all",
    resume_checkpoint: dict | None = None,
    allow_degraded_context: bool = False,
    checkpoint_config: dict | None = None,
) -> str:
    """Sequence the flow phases: plan → [dry-run] → build → execute → synthesize → finalize."""
    t0 = time.monotonic()

    if resume_checkpoint is not None:
        # Resume: replay the persisted plan verbatim — no planner LLM call.
        # dep_indices are already 0-based positions (persisted, not the raw
        # depends_on ordinal refs), so _earlier_dep_indices is skipped
        # entirely, not just its LLM-facing caller.
        plan_entries = resume_checkpoint.get("plan") or []
        if not plan_entries:
            raise FlowResumeError("Checkpoint has an empty plan — nothing to resume.")
        assignments = [
            TaskAssignment(
                **{k: v for k, v in entry.items() if k not in ("agent_id", "dep_indices")}
            )
            for entry in plan_entries
        ]
        agent_ids: list[str] = [entry["agent_id"] for entry in plan_entries]
        dep_indices = [list(entry.get("dep_indices") or []) for entry in plan_entries]
        # Replay the naming bookkeeping so a role that reactively spawns
        # again post-resume gets a name that doesn't collide with one
        # already used in the run being resumed (build_worker_branch
        # re-registers each restored agent_id into _all_names itself, via
        # the explicit_name path _build_dag already takes below).
        for ta in assignments:
            env._name_counts[ta.assignee] = env._name_counts.get(ta.assignee, 0) + 1
        t_plan = 0.0
        progress(f"Resumed plan: {len(assignments)} assignments (planner skipped).")
    else:
        roster = available_roles()
        budget_note = ""
        if max_ops > 0:
            budget_note = (
                f"BUDGET: at most {max_ops} ops total, INCLUDING any reactively "
                "spawned follow-ups — plan tightly. "
            )
        guidance = (
            f"{role_roster(env.default_model_spec)}\n\n{mode_roster()}\n\n"
            f"{budget_note}{team_guidance(team_attach or team_name)}"
        )

        progress("Planning DAG...")
        assignments = await plan(
            env.orc_branch, prompt, roles=roster, dag=True, guidance=guidance, max_tasks=max_ops
        )
        if not assignments:
            # Fail loud rather than silently exiting 0 with no work done.
            _warn("Orchestrator returned no assignments; retrying once with a sharper instruction.")
            assignments = await plan(
                env.orc_branch,
                prompt,
                roles=roster,
                dag=True,
                guidance=guidance + " Return ONLY the assignments list — do not perform the task.",
                max_tasks=max_ops,
            )
        if not assignments:
            raise FlowPlanError(
                "Orchestrator produced no usable plan (an empty TaskAssignment list) after a "
                "retry. This commonly happens when the task prompt embeds imperative "
                "multi-section instructions that pull the model into executing the task "
                "instead of decomposing it — prefer a declarative task statement, and run "
                "with --verbose to inspect the raw response."
            )

        # Defensive cap: a runaway orchestrator emitting hundreds of assignments
        # would spawn hundreds of branches/iModels. Truncate (don't crash).
        if len(assignments) > 200:
            _warn(f"Plan has {len(assignments)} assignments; truncating to 200.")
            assignments = assignments[:200]

        t_plan = time.monotonic() - t0

        agent_ids = [env.assign_name(ta.assignee) for ta in assignments]

        dep_indices = [_earlier_dep_indices(ta.depends_on, i) for i, ta in enumerate(assignments)]

    # --workers overrides model only; --bare also drops profiles (distinct behaviors).
    pool = [s.strip() for s in workers_str.split(",")] if workers_str else []

    dag_lines = []
    for i, ta in enumerate(assignments):
        deps = f" ← {','.join(str(j + 1) for j in dep_indices[i])}" if dep_indices[i] else ""
        dag_lines.append(f"{i + 1}:{ta.assignee}{deps}")
    progress(f"Plan done ({t_plan:.1f}s): {len(assignments)} assignments — {' | '.join(dag_lines)}")

    if dry_run:
        lines = [f"Plan ({len(assignments)} assignments):", ""]
        for i, ta in enumerate(assignments):
            deps = (
                f"  depends_on: {', '.join(str(j + 1) for j in dep_indices[i])}"
                if dep_indices[i]
                else ""
            )
            lines.append(f"  {i + 1}. [{ta.assignee}] {ta.task[:120]}")
            if deps:
                lines.append(deps)
            if ta.exit_criteria:
                lines.append(f"    exit: {ta.exit_criteria[:100]}")
        lines.append("")
        lines.append("Model + modes resolution:")
        for i, ta in enumerate(assignments):
            override = pool[i % len(pool)] if pool else None
            if override:
                modes = [] if env.bare else resolve_modes(ta.assignee, ta.modes or None, env.pack)
                mode_str = f"  modes={modes}" if modes else ""
                lines.append(f"  {agent_ids[i]}: {override} (workers){mode_str}")
                continue
            if env.bare:
                lines.append(f"  {agent_ids[i]}: {model_spec} (bare)")
                continue
            rm, rp = resolve_worker_spec(ta.assignee)
            cfg = role_config(ta.assignee, env.pack)
            if rp:
                # A user profile supplies its own body — casts modes don't apply
                # (profile shadows casts; ADR-0074 follow-up makes them compose).
                model, src, modes = rm, "profile", []
            elif cfg and cfg.model:
                model, src = cfg.model, "pack"
                modes = resolve_modes(ta.assignee, ta.modes or None, env.pack)
            else:
                model, src = model_spec, "default"
                modes = resolve_modes(ta.assignee, ta.modes or None, env.pack)
            mode_str = f"  modes={modes}" if modes else ""
            lines.append(f"  {agent_ids[i]}: {model} ({src}){mode_str}")
        return "\n".join(lines)

    if team_attach:
        from ..team import _load_team

        try:
            env.team_data = _load_team(team_attach)
            progress(
                f"Team '{team_attach}' attached ({env.team_data['id']}, "
                f"{len(env.team_data.get('messages', []))} prior msgs)"
            )
        except FileNotFoundError:
            env.team_data = _create_fanout_team(team_attach, agent_ids)
            progress(f"Team '{team_attach}' created ({env.team_data['id']})")
    elif team_name:
        env.team_data = _create_fanout_team(team_name, agent_ids)
        progress(f"Team '{team_name}' created ({env.team_data['id']})")

    budget_preambles: dict[int, str] = {}
    if env.total_budget and assignments:
        share = int(env.total_budget / len(assignments))
        deadline = time.time() + env.total_budget
        for i in range(len(assignments)):
            budget_preambles[i] = _format_budget_preamble(
                op_index=i + 1,
                num_ops=len(assignments),
                op_budget_seconds=share,
                deadline_epoch=deadline,
            )

    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=agent_ids,
        dep_indices=dep_indices,
        pool=pool,
        budget_preambles=budget_preambles,
    )

    dag_state = await _build_dag(env, prompt, plan_result, reactive_spec=reactive_spec)

    if resume_checkpoint is not None:
        _apply_checkpoint_precompletion(
            env,
            plan_result,
            dag_state,
            resume_checkpoint.get("ops") or {},
            allow_degraded_context=allow_degraded_context,
            checkpoint_spawned=resume_checkpoint.get("spawned") or None,
        )
        checkpoint_plan = resume_checkpoint["plan"]
    else:
        checkpoint_plan = [
            {**assignments[i].model_dump(), "agent_id": agent_ids[i], "dep_indices": dep_indices[i]}
            for i in range(len(assignments))
        ]

    exec_result = await _execute_dag(
        env,
        plan_result,
        dag_state,
        max_concurrent=max_concurrent,
        max_ops=max_ops,
        checkpoint_prompt=prompt,
        checkpoint_plan=checkpoint_plan,
        checkpoint_config=checkpoint_config,
        checkpoint_ops_seed=resume_checkpoint.get("ops") if resume_checkpoint is not None else None,
        checkpoint_flow_context=(
            resume_checkpoint.get("flow_context") if resume_checkpoint is not None else None
        ),
    )

    synthesis_result = None
    if (with_synthesis or exec_result.n_spawned) and exec_result.agent_results:
        synthesis_result = await _synthesize(
            env,
            prompt,
            plan_result,
            dag_state,
            exec_result,
            synthesis_model=synthesis_model,
            model_spec=model_spec,
        )

    output = _finalize_flow(
        env,
        prompt,
        plan_result,
        dag_state,
        exec_result,
        synthesis_result,
        output_format=output_format,
        show_graph=show_graph,
    )

    t_total = time.monotonic() - t0
    progress(f"\nTotal: {t_total:.1f}s")

    return output


async def _resume_flow(
    target: str,
    *,
    allow_degraded_context: bool = False,
    dry_run: bool = False,
    show_graph: bool = False,
    notify: str | None = None,
) -> tuple[str, str]:
    """Resolve a checkpointed run/session id and replay it through _run_flow.

    dry_run/show_graph come from the CURRENT invocation, not the checkpoint —
    they are presentation flags, not part of what already happened. Every
    other _run_flow kwarg replays the persisted config verbatim. notify is
    also a current-invocation override — like dry_run/show_graph it steers
    presentation of this replay, not the plan that already happened.
    """
    _run_dir, checkpoint = await resolve_checkpoint_target(target)
    config = dict(checkpoint.get("config") or {})
    config["dry_run"] = dry_run
    config["show_graph"] = show_graph
    if notify is not None:
        config["notify"] = notify
    return await _run_flow(
        prompt=checkpoint.get("prompt", ""),
        resume_checkpoint=checkpoint,
        allow_degraded_context=allow_degraded_context,
        **config,
    )
