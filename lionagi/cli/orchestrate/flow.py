# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Reactive DAG flow: orchestrator plans TaskAssignments, self-expanding execution."""

from __future__ import annotations

import asyncio as _asyncio
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi._errors import EmptyOutgoingContentError, LionError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.casts.emission import SpawnRequest, TaskAssignment
from lionagi.ln.concurrency import CancelScope, move_on_after
from lionagi.orchestration import normalize_dep_indices, plan, role_node_builder
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger

from .._agent_depth import stamp_worker_depth
from .._logging import progress
from .._logging import warn as _warn
from .._providers import parse_model_spec
from .._util import classify_exception
from ._checkpoint import CheckpointWriter, FlowResumeError, resolve_checkpoint_target
from ._common import (
    _build_worker_operate_node,
    _create_fanout_team,
    _format_result_json,
    _format_result_text,
    _post_results_to_team,
    retarget_artifact_section,
)
from ._notify import register_flow_notify_scope, unregister_flow_notify_scope
from ._orchestration import (
    EFFORT_MAP,
    OrchestrationEnv,
    available_roles,
    build_worker_branch,
    finalize_orchestration,
    make_help_coordinator,
    mode_roster,
    parse_orchestrator_provider,
    register_branch_hook,
    resolve_modes,
    resolve_worker_spec,
    role_config,
    role_roster,
    setup_orchestration,
    start_live_persist,
    stop_live_persist,
    team_guidance,
    team_history_context,
    worker_is_cli,
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


async def _persist_node_metadata_patch(db, session_id: str, patch: dict) -> None:
    """Merge *patch* into the session's node_metadata rather than replacing
    the column outright, so fields written out-of-band by other callers
    (e.g. the kill-sweep's unverifiable-pid markers) survive ordinary flow
    progress writes instead of being reset by them."""
    row = await db.get_session(session_id)
    existing = row.get("node_metadata") if row else None
    if not isinstance(existing, dict):
        existing = {}
    await db.update_session(session_id, node_metadata=json.dumps({**existing, **patch}))


# ── Artifact-contract text — shared by planned legs and spawned nodes ─────────
# Shared by _build_dag and _execute_dag's decorate_instruction closure so
# both use one namespacing rule instead of two copies drifting apart.


def _leg_artifact_entries(node_id: str, role_defaults: dict | None) -> list[dict]:
    """Namespace a role's declared artifact_defaults under *node_id*'s own subdirectory."""
    if not role_defaults:
        return []
    entries: list[dict] = []
    for entry in role_defaults.get("expected", []):
        eid = entry.get("id", "")
        epath = entry.get("path", "")
        entries.append(
            {
                **entry,
                "id": f"{node_id}__{eid}",
                "path": f"{node_id}/{epath}",
                "required": entry.get("required", True),
                "source": "role_default",
            }
        )
    return entries


def _retarget_spawn_prompt(
    branch,
    artifact_dir,
    *,
    workspace_assigned: bool = True,
) -> None:
    """Rewrite a spawned clone's artifact directive.

    Provider-aware wording avoids claiming an unassigned working directory.
    """
    msgs = getattr(branch, "msgs", None)
    if msgs is None:
        _warn(f"spawned worker prompt not retargeted to {artifact_dir}: branch has no messages")
        return
    sys_msg = msgs.system
    if sys_msg is None:
        return
    current = sys_msg.content.system_message or ""
    if not isinstance(current, str):
        _warn(f"spawned worker prompt not retargeted to {artifact_dir}: prompt is not text")
        return
    updated = retarget_artifact_section(
        current,
        artifact_dir,
        workspace_assigned=workspace_assigned,
    )
    if updated != current:
        msgs.set_system(msgs.create_system(system=updated))


def _artifact_directive(run, node_id: str, leg_expected: list[dict]) -> str:
    """Compose the artifact-directory (+ REQUIRED-file, when declared) instruction text."""
    note = f"Your artifact directory: {run.agent_artifact_dir(node_id)}/ — write output files here."
    if leg_expected:
        required_paths = ", ".join(e["path"].split("/", 1)[1] for e in leg_expected)
        note += (
            f" REQUIRED: write {required_paths} in that directory — the run "
            "is marked failed if it is missing at completion."
        )
    return note


# ── Control poller (ADR-0069 D1–D3: session-control transport) ──────────────
# `li o ctl pause|resume|msg` enqueues a session_controls row from a separate
# process; this poller is the only consumer, verb-specific apply/stamp order.

_CONTROL_POLL_INTERVAL = 2.0

# Sentinel: apply ran but no finalize write landed. The poller must stop the
# tick here rather than let later controls overtake it in the DB.
_CONTROL_UNSTAMPED = "unstamped"

# ── Escalation mirror linking — attributes an escalated leg's CLI transcript
# back to this run instead of leaving it an unlinked, misattributed session.
# The transcript mirror may run in another process and lag behind this run's
# own completion, so the link write gets a few bounded retries rather than
# firing once and giving up.
_ESCALATION_LINK_RETRIES = 5
_ESCALATION_LINK_RETRY_INTERVAL = 1.0

# ── Team lifecycle (done-signal / wakeup rounds / quiescence) ───────────────
# Driven by ReactiveExecutor's on_op_complete hook, not a poll loop (which
# would race the executor's task-group teardown) — see TeamLifecycleCoordinator.


async def _apply_session_control(db, executor, row: dict) -> str | None:
    """Apply one session_controls row against *executor*. Returns the
    finalize result, or None if left untouched (mid-apply from a prior
    poller crash). Never raises — failures are recorded as rejected."""
    control_id = row["id"]
    verb = row["verb"]
    # Set only once this poller has actually claimed the row. Every finalize
    # below passes it, so a write from this function can never land on an
    # outcome somebody else recorded while the apply was in flight — including
    # a hand resolution, which is the one outcome here that nothing can rebuild.
    # It stays None for the idempotent verbs, which take no claim and want the
    # unconditional write.
    claim: str | None = None
    try:
        if verb == "pause":
            executor.pause()
            return await _finalize_applied(db, control_id)

        if verb == "resume":
            executor.resume()
            return await _finalize_applied(db, control_id)

        if verb == "message":
            if str(row.get("result") or "").startswith("applying"):
                # A consumer stamped this row and did not finish; leave it
                # untouched — re-attempting could double-inject the message.
                # Prefix rather than equality because a claim may name its
                # owner ('applying:<run id>'), and an equality check would stop
                # recognising exactly the claims that identify who holds them.
                return None
            claim = await db.mark_session_control_applying(control_id)
            if claim is None:
                # The row above said unclaimed; this says it is claimed now.
                # Only the stamp that actually wrote may go on to inject, since
                # the losing side proceeding is the double-injection the check
                # above exists to prevent, arrived at by a different route.
                return None

            from lionagi.operations.node import Operation as _Operation  # noqa: PLC0415
            from lionagi.protocols.types import EventStatus as _EventStatus  # noqa: PLC0415

            has_pending_op = any(
                isinstance(node, _Operation) and node.execution.status == _EventStatus.PENDING
                for node in executor.graph.internal_nodes.values()
            )
            if not has_pending_op:
                result = "rejected:no-pending-ops"
                if not await db.finalize_session_control(
                    control_id, result=result, expect_claim=claim
                ):
                    return None
                return result

            from lionagi.libs.nested import deep_update  # noqa: PLC0415

            payload = row.get("payload") or {}
            existing = executor.context.content.get("operator_messages", [])
            entry = {"ts": time.time(), "text": payload.get("text", "")}
            deep_update(executor.context.content, {"operator_messages": [*existing, entry]})
            return await _finalize_applied(db, control_id, claim)

        # 'stop' is schema-reserved for a later slice (checkpoint writer);
        # reject other verbs loudly instead of polling them forever.
        result = f"rejected:unsupported-verb:{verb}"
        await db.finalize_session_control(control_id, result=result, expect_claim=claim)
        return result
    except Exception as exc:  # noqa: BLE001 — the poller must never crash the run
        result = f"rejected:error:{exc}"[:500]
        logger.warning("control %s (%s) failed to apply: %s", control_id, verb, exc)
        try:
            if not await db.finalize_session_control(control_id, result=result, expect_claim=claim):
                # The claim moved while this apply was failing, so the row
                # already carries somebody else's outcome. Recording our error
                # over it would replace a settled answer with a worse one.
                return None
        except Exception:  # noqa: BLE001
            # Still pending: signal the poller to end the tick so a later
            # control isn't overtaken by this one re-applying next tick.
            return _CONTROL_UNSTAMPED
        return result


async def _finalize_applied(db, control_id: str, claim: str | None = None) -> str | None:
    """Stamp 'applied' after a successful apply; on finalize failure, retry
    once then return the unstamped sentinel for the next poller tick.

    Returns None when *claim* is given and the row no longer carries it: the
    apply happened, but somebody else has since recorded the outcome, and
    theirs stands. Callers read None as "left untouched by us"."""
    for _ in range(2):
        try:
            if await db.finalize_session_control(control_id, result="applied", expect_claim=claim):
                return "applied"
            return None
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
        # empty leg still taints the flow's terminal status.
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
                # A "completed" child may still carry COMPLETED_GATE_REJECTED
                # (a gate rejected mid-DAG and short-circuited its dependent
                # subtree) — surface that at the invocation level too, or it
                # flattens back to a plain clean-pass COMPLETED_OK and the
                # distinction this reason code exists for is lost.
                gate_rejected = [
                    s
                    for s in sessions
                    if str(s.get("status_reason_code") or "") == RunReasons.COMPLETED_GATE_REJECTED
                ]
                if gate_rejected:
                    gate_metadata = dict(metadata)
                    gate_metadata["gate_rejected_session_ids"] = [
                        s["id"] for s in gate_rejected if s.get("id")
                    ]
                    return (
                        "completed",
                        RunReasons.COMPLETED_GATE_REJECTED,
                        "Flow completed successfully, but a gate rejected "
                        "mid-DAG in at least one child session and its "
                        "dependent subtree was short-circuited instead of "
                        "running against the rejected baseline.",
                        [{"kind": "session", "id": s["id"]} for s in gate_rejected if s.get("id")],
                        gate_metadata,
                    )
                # A "completed" child may still carry COMPLETED_FINALIZE_ERROR
                # (a guarded best-effort teardown step failed) — surface that
                # degraded reason at the invocation level rather than hiding it.
                degraded = [
                    s
                    for s in sessions
                    if str(s.get("status_reason_code") or "") == RunReasons.COMPLETED_FINALIZE_ERROR
                ]
                if degraded:
                    degraded_metadata = dict(metadata)
                    degraded_metadata["finalize_error_session_ids"] = [
                        s["id"] for s in degraded if s.get("id")
                    ]
                    return (
                        "completed",
                        RunReasons.COMPLETED_FINALIZE_ERROR,
                        "Flow completed successfully, but at least one child "
                        "session recorded a non-output finalize error (a "
                        "best-effort teardown step failed after that "
                        "child's own DAG already produced its result).",
                        [{"kind": "session", "id": s["id"]} for s in degraded if s.get("id")],
                        degraded_metadata,
                    )
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


def _fallback_notify_reason(status: str) -> str:
    """Reason code for a best-effort terminal-notify envelope emitted when
    invocation finalization itself raised before resolving a reason (see
    `_run_flow`'s finally block) -- *status* here is the flow's own
    already-computed terminal status, not a value read back from the
    (never-committed) invocation row.

    The mapping deliberately matches what `_resolve_invocation_terminal_flow`
    would have returned for the same status, so a consumer sees the same cause
    for the same run whether or not finalization failed. In particular an
    aborted flow is a SIGINT cancellation."""
    from lionagi.state.reasons import RunReasons

    return {
        "completed": RunReasons.COMPLETED_OK,
        "completed_empty": RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
        "failed": RunReasons.FAILED_EXCEPTION,
        "timed_out": RunReasons.TIMED_OUT_DEADLINE,
        "aborted": RunReasons.CANCELLED_SIGINT,
        "cancelled": RunReasons.CANCELLED_SYSTEM,
    }.get(status, RunReasons.FAILED_EXCEPTION)


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
    # agent_id → its own worker branch (role_base is one-per-role and can't
    # address a specific named instance for team-lifecycle wakeup rounds)
    # and agent_id → messenger-bound, so a round-injected node mirrors its
    # planned leg's actions= wiring. Populated in _build_dag's per-leg loop.
    worker_branches: dict[str, object] = field(default_factory=dict)
    messenger_bound: dict[str, bool] = field(default_factory=dict)


@dataclass
class _ExecResult:
    """Execution output: collected agent responses and spawn count."""

    agent_results: list[dict]
    n_spawned: int
    t_exec_elapsed: float
    escalated_agent_ids: list[str] = field(default_factory=list)


# ── Phase 1: build DAG ────────────────────────────────────────────────────────


def _deps_from_built_graph(builder, label_by_node: dict[str, str]) -> dict[str, list[str]]:
    """Read each node's incoming edges out of the graph the executor walks.

    A dependency list re-derived from what the planner declared is a statement
    about the *input* to the build step, not an observation of its *output*:
    the builder can add or omit edges, the executor waits on every incoming
    edge whatever its label, and nothing downstream would notice the two
    disagreeing. Reading the graph makes the reported structure and the
    executed one the same object.

    `label_by_node` names the nodes a reader already has a name for — plan
    steps, by their 1-based ordinal, matching how deps have always been shown.
    A head outside it is a node that has no plan ordinal because it did not
    exist at plan time; it is named by the spawn id stamped on it (the same id
    its own result record carries), falling back to the raw node id so an edge
    is never dropped for want of a name.
    """
    graph = builder.get_graph()
    nodes = getattr(graph, "internal_nodes", None)
    mapping = getattr(graph, "node_edge_mapping", None) or {}

    def _name(head_id: str) -> str:
        known = label_by_node.get(head_id)
        if known is not None:
            return known
        node = nodes.get(head_id) if nodes is not None else None
        stamped = node.metadata.get("spawn_id") if node is not None else None
        return stamped or head_id

    deps: dict[str, list[str]] = {}
    for node_id, slots in mapping.items():
        names: list[str] = []
        for head_id in (slots.get("in") or {}).values():
            name = _name(str(head_id))
            if name not in names:
                names.append(name)
        deps[str(node_id)] = names
    return deps


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
    worker_branches: dict[str, object] = {}
    worker_messenger_bound: dict[str, bool] = {}
    spawn_assignees = sorted({ta.assignee for ta in assignments})

    # The plan is the run's own statement of which workers it will have, made
    # before any of them is built. Recording it here is what lets finalization
    # notice a worker that was launched without being given a directory.
    for agent_id in agent_ids:
        env.expect_worker(agent_id)

    for i, ta in enumerate(assignments):
        w_branch, w_model, w_profile, messenger_bound = await build_worker_branch(
            env,
            agent_id=agent_ids[i],
            role=ta.assignee,
            model_override=pool[i % len(pool)] if pool else None,
            explicit_name=agent_ids[i],
            grant_spawn=_may_spawn(ta.assignee),
            spawn_assignees=spawn_assignees,
            modes=ta.modes or None,
        )
        worker_branches[agent_ids[i]] = w_branch
        worker_messenger_bound[agent_ids[i]] = messenger_bound
        worker_models.append(w_model)
        role_base.setdefault(ta.assignee, w_branch)

        # Fold this leg's OWN declared artifact contract (profile first, else
        # the casts role's artifact_defaults) into the flow-wide contract,
        # namespaced under this leg's own artifact subdirectory (ADR-0064 D3).
        if ta.assignee in role_artifact_defaults:
            role_defaults = role_artifact_defaults[ta.assignee]
        else:
            role_defaults = w_profile.artifact_defaults if w_profile else None
            if not role_defaults:
                from lionagi.casts.pattern import Role as _Role

                with contextlib.suppress(ValueError):
                    role_defaults = _Role.load(ta.assignee).artifact_defaults
            role_artifact_defaults[ta.assignee] = role_defaults
        leg_expected = _leg_artifact_entries(agent_ids[i], role_defaults)
        role_artifact_entries.extend(leg_expected)

        ctx: list = [{"original_task": prompt}]
        artifact_note = _artifact_directive(env.run, agent_ids[i], leg_expected)
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
            # Attached-team history (if any) rides in operation context, not
            # the system prompt — see team_history_context's docstring for why.
            history_ctx = team_history_context(
                env.team_data, agent_ids[i], messenger_bound=messenger_bound
            )
            if history_ctx:
                ctx.append(history_ctx)
        w_effort = env.effort
        if not env.bare and w_profile and w_profile.effort:
            w_effort = w_profile.effort
        if w_effort:
            ctx.append({"effort_guidance": EFFORT_MAP.get(w_effort, "")})

        instruction = budget_preambles.get(i, "") + ta.task
        dep_nodes = [node_ids[j] for j in dep_indices[i]]
        node = _build_worker_operate_node(
            env.builder,
            branch=w_branch,
            depends_on=dep_nodes,
            instruction=instruction,
            context=ctx,
            messenger_bound=messenger_bound,
        )
        node_ids.append(node)

    known_nodes = set(node_ids)
    # Observed from the graph just built, not re-derived from the plan that
    # asked for it — see _deps_from_built_graph.
    graph_deps = _deps_from_built_graph(
        env.builder, {str(node_ids[i]): str(i + 1) for i in range(len(assignments))}
    )
    deps_by_node = {
        node_ids[i]: graph_deps.get(str(node_ids[i]), []) for i in range(len(assignments))
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
                "depends_on": deps_by_node[node_ids[i]],
            }
            for i in range(len(assignments))
        ],
    }
    env._finalize_extras = early_graph
    ctx_lp = getattr(env, "_live_persist", None)
    if ctx_lp and ctx_lp.get("db"):
        with contextlib.suppress(Exception):
            _markers = ctx_lp.get("identity_markers") or {}
            await _persist_node_metadata_patch(
                ctx_lp["db"], ctx_lp["session_id"], {**early_graph, **_markers}
            )

    # Persist the per-leg role/profile artifact declarations (ADR-0064 D3),
    # validated eagerly; must reach the session row directly, not just
    # env._live_persist — see docs/internals/cli.md for the write-class split
    # with reactively spawned nodes' append-only write in _execute_dag.
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
        worker_branches=worker_branches,
        messenger_bound=worker_messenger_bound,
    )


# ── Resume: pre-mark checkpoint-completed nodes ───────────────────────────────


def _reconstruct_spawned_nodes(
    env: OrchestrationEnv,
    plan_result: _PlanResult,
    dag_state: _DagState,
    checkpoint_ops: dict[str, dict],
    checkpoint_spawned: list[dict],
) -> None:
    """Rebuild reactively spawned nodes from a checkpoint into the fresh
    graph, pre-completed like a planned node. See docs/internals/cli.md for
    the three soundness checks (operation field, parent-terminal, spawn_id)
    each entry must pass before any node is added to the graph."""
    from uuid import UUID as _UUID

    from lionagi.operations.node import create_operation
    from lionagi.protocols.graph.edge import Edge
    from lionagi.protocols.types import EventStatus

    legacy = [e for e in checkpoint_spawned if not e.get("operation")]
    if legacy:
        ids = ", ".join(str(e.get("node_id", "?")) for e in legacy)
        raise FlowResumeError(
            f"Resume refused for reactively spawned node(s) [{ids}]: this "
            "checkpoint predates spawn-reconstruction support (no operation "
            "type recorded for them), so they cannot be rebuilt. Re-run the "
            "flow from scratch."
        )

    unrecognized = [
        e["node_id"] for e in checkpoint_spawned if e.get("status") not in ("completed", "failed")
    ]
    if unrecognized:
        raise FlowResumeError(
            "Resume refused for reactively spawned node(s) "
            f"[{', '.join(unrecognized)}]: checkpoint status is neither "
            "'completed' nor 'failed', so it cannot be safely replayed."
        )

    unstamped = [
        e["node_id"] for e in checkpoint_spawned if e.get("assignee") and not e.get("spawn_id")
    ]
    if unstamped:
        raise FlowResumeError(
            "Resume refused for reactively spawned node(s) "
            f"[{', '.join(unstamped)}]: recorded a role assignee but no "
            "spawn_id — role_node_builder stamps both together, so this "
            "checkpoint predates spawn_id capture (or is otherwise corrupt) "
            "and cannot be soundly rebuilt. Re-run the flow from scratch."
        )

    known_ids = {str(n) for n in dag_state.node_ids}
    candidate_ids = {e["node_id"] for e in checkpoint_spawned}
    terminal_planned_ids = {
        str(node_id)
        for agent_id, node_id in zip(plan_result.agent_ids, dag_state.node_ids, strict=True)
        if (checkpoint_ops.get(agent_id) or {}).get("status") in ("completed", "failed")
    }

    unsound = [
        f"{e['node_id']} (parent {e['parent_id']})"
        for e in checkpoint_spawned
        if e.get("parent_id")
        and e["parent_id"] not in candidate_ids
        and not (e["parent_id"] in known_ids and e["parent_id"] in terminal_planned_ids)
    ]
    if unsound:
        raise FlowResumeError(
            f"Resume refused for reactively spawned node(s) [{'; '.join(unsound)}]: "
            "the op that spawned them had not itself reached a checkpointed "
            "terminal state, so the spawn decision cannot be soundly replayed "
            "— resuming risks either duplicating or silently dropping that "
            "work. Re-run the flow from scratch."
        )

    graph = env.builder.get_graph()
    built: dict[str, Any] = {}
    for entry in checkpoint_spawned:
        node_id = entry["node_id"]
        assignee = entry.get("assignee")
        spawn_id = entry.get("spawn_id")
        metadata: dict[str, Any] = {}
        if assignee:
            metadata["assignee"] = assignee
        if spawn_id:
            metadata["spawn_id"] = spawn_id
            metadata["reference_id"] = spawn_id
        parameters: dict[str, Any] = {"instruction": entry.get("instruction") or ""}
        # context (e.g. a team round op's prior_team_messages) is optional —
        # only checkpoints written after CHECKPOINT_VERSION 2's context
        # capture carry it; older entries simply have none to restore.
        if entry.get("context") is not None:
            parameters["context"] = entry["context"]
        node = create_operation(
            entry["operation"],
            parameters=parameters,
            id=_UUID(node_id),
            metadata=metadata,
        )
        node.execution.status = (
            EventStatus.COMPLETED if entry["status"] == "completed" else EventStatus.FAILED
        )
        node.execution.response = entry.get("response")
        role_branch = dag_state.role_base.get(assignee) if assignee else None
        if role_branch is not None:
            node.branch_id = role_branch.id
        built[node_id] = node

    for node in built.values():
        graph.add_node(node)
    for entry in checkpoint_spawned:
        parent_id = entry.get("parent_id")
        if not parent_id:
            continue
        parent_uuid = _UUID(parent_id) if parent_id in known_ids else built[parent_id].id
        graph.add_edge(Edge(head=parent_uuid, tail=built[entry["node_id"]].id, label=["spawn"]))


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
    pre-completed seam short-circuits them. A pending op with inherit_context
    is refused unless allow_degraded_context is passed (v1 resume restores
    results-context only). checkpoint_spawned is rebuilt the same way — see
    _reconstruct_spawned_nodes."""
    from lionagi.protocols.types import EventStatus

    if checkpoint_spawned:
        _reconstruct_spawned_nodes(env, plan_result, dag_state, checkpoint_ops, checkpoint_spawned)

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
    checkpoint_spawned_seed: list[dict] | None = None,
    team_max_rounds: int = 2,
) -> _ExecResult:
    """Drive the planning engine over the DAG and collect per-agent results.
    checkpoint_config gates the checkpoint writer (opt-in); checkpoint_spawned_seed
    carries forward prior-checkpoint spawn entries so a flush before any NEW
    spawn doesn't overwrite `spawned` with `[]` and lose reconstructed work."""
    assignments = plan_result.assignments
    agent_ids = plan_result.agent_ids
    role_by_worker = {
        agent_id: assignment.assignee
        for agent_id, assignment in zip(agent_ids, assignments, strict=True)
    }

    reactive = dag_state.reactive
    spawn_roles = dag_state.spawn_roles
    node_ids = dag_state.node_ids
    known_nodes = dag_state.known_nodes
    known_node_strs = {str(n) for n in known_nodes}
    deps_by_node = dag_state.deps_by_node
    worker_models = dag_state.worker_models
    role_base = dag_state.role_base
    _op_segments = dag_state.op_segments

    # Shared out-of-band handle for the live executor, populated by
    # DependencyAwareExecutor.__init__; both the control poller and the
    # checkpoint writer's per-completion hook read from it.
    _executor_ref: dict[str, object] = {}
    _checkpoint_tasks: list = []
    _branch_status_tasks: list = []
    _escalation_link_tasks: list = []

    _checkpoint_writer: CheckpointWriter | None = None
    if checkpoint_config is not None:
        _ctx_lp = getattr(env, "_live_persist", None)
        _checkpoint_writer = CheckpointWriter(
            path=env.run.checkpoint_path,
            session_id=(_ctx_lp or {}).get("session_id") or "",
            prompt=checkpoint_prompt,
            plan=checkpoint_plan or [],
            config=checkpoint_config,
            # Seed with prior-checkpoint state (empty on a fresh run) so a
            # resume-of-a-resume can't silently lose context before the next flush.
            flow_context=dict(checkpoint_flow_context or {}),
            ops=dict(checkpoint_ops_seed or {}),
            spawned=list(checkpoint_spawned_seed or []),
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
    # Restored spawns already consumed spawn budget and exist as completed/
    # failed work; both the budget below and spawn accounting must count them.
    restored_spawn_count = len(checkpoint_spawned_seed or [])
    # --max-ops shares budget between initial plan + spawns; default cap of
    # 20 otherwise so an un-capped reactive run can't fan out unbounded.
    max_spawn = max(0, (max_ops - len(assignments) if max_ops > 0 else 20) - restored_spawn_count)
    # Resume must start the spawn-id ordinal sequence past whatever restored
    # spawns already used (MAX existing + 1, not count — crashes can leave
    # gaps) or a live spawn could reissue a restored spawn_id/artifact dir.
    _spawn_seq_start = 1
    for _entry in checkpoint_spawned_seed or []:
        _sid = _entry.get("spawn_id")
        if not _sid:
            continue
        _, _, _suffix = _sid.rpartition("-")
        if _suffix.isdigit():
            _spawn_seq_start = max(_spawn_seq_start, int(_suffix) + 1)

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
                _markers = ctx.get("identity_markers") or {}
                await _persist_node_metadata_patch(
                    ctx["db"], ctx["session_id"], {**extras, **_markers}
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

        _branch_status_tasks.append(_asyncio.ensure_future(_do()))

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
        """Fire-and-forget the checkpoint write for one op's outcome. sig.op_id
        (not sig.name, which a spawned clone can share with a planned node)
        routes to record() vs record_spawned() to avoid key collisions."""
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
            # Capture what resume needs to rebuild this node: operation type,
            # routed role, and instruction, read off the still-live graph
            # node. A lookup failure leaves these unset, which resume treats
            # as unreconstructable for this node alone (see flow.py's resume path).
            spawn_fields: dict[str, Any] = {"parent_id": sig.parent_id}
            with contextlib.suppress(Exception):
                from uuid import UUID as _UUID

                spawned_node = env.builder.get_graph().internal_nodes.get(_UUID(sig.op_id))
                if spawned_node is not None:
                    params = spawned_node.parameters
                    spawn_fields["operation"] = spawned_node.operation
                    spawn_fields["assignee"] = spawned_node.metadata.get("assignee")
                    spawn_fields["instruction"] = (
                        params.get("instruction")
                        if isinstance(params, dict)
                        else getattr(params, "instruction", None)
                    )
                    # role_node_builder stamps spawn_id unconditionally, so
                    # it's captured the same way regardless of assignee.
                    spawn_fields["spawn_id"] = spawned_node.metadata.get("spawn_id")
                    # context carries payload the generic `instruction` text
                    # doesn't (e.g. a team round op's prior_team_messages) —
                    # without it, resume reconstructs the node with only the
                    # boilerplate instruction and silently loses that data.
                    spawn_fields["context"] = (
                        params.get("context")
                        if isinstance(params, dict)
                        else getattr(params, "context", None)
                    )
            _checkpoint_tasks.append(
                _asyncio.ensure_future(
                    _checkpoint_writer.record_spawned(
                        sig.op_id,
                        status=status,
                        response=response,
                        flow_context=flow_ctx,
                        **spawn_fields,
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

    # ADR-0034 §4: run_dag drives the session bus; observers above consume the signals.
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

    # ADR-0069 D1: control poller, the only consumer of session_controls rows.
    # _executor_ref is populated synchronously by DependencyAwareExecutor's
    # __init__, so the window below is at most one event-loop tick.
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
                await _persist_node_metadata_patch(
                    ctx["db"], ctx["session_id"], {**extras, **_markers}
                )

        _asyncio.ensure_future(_do())

    # Only wired when team messaging + reactive mode are on and at least
    # one worker got a branch built (nothing to inject otherwise).
    _team_coordinator: Any = None
    if (
        env.team_data
        and getattr(env, "messenger", None) is not None
        and dag_state.reactive
        and dag_state.worker_branches
    ):
        from ._orchestration import make_team_lifecycle_coordinator

        _team_coordinator = make_team_lifecycle_coordinator(
            env.team_data["id"],
            agent_ids,
            dag_state.worker_branches,
            messenger_bound=dag_state.messenger_bound,
            max_rounds=team_max_rounds,
            exchange=getattr(env, "exchange", None),
            # env.team_data is the snapshot `_load_team`/`_create_fanout_team`
            # returned when this run attached/created the team, before this
            # run posted anything — its message count is exactly this run's
            # history boundary (0 for a freshly created team).
            message_boundary=len(env.team_data.get("messages", [])),
        )
        env.messenger.on("done", _team_coordinator.on_done)
        env.messenger.on("finished", _team_coordinator.on_finished)

    def _artifact_defaults_for_assignee(assignee: str | None) -> dict | None:
        role = role_by_worker.get(assignee, assignee)
        return dag_state.role_artifact_defaults.get(role) if role else None

    def _decorate_team_round_artifacts(operation: Any) -> None:
        """Tell a stamped team round about the contract its result will register."""
        assignee = operation.metadata.get("assignee")
        spawn_id = operation.metadata.get("spawn_id")
        if not spawn_id:
            return
        leg_expected = _leg_artifact_entries(spawn_id, _artifact_defaults_for_assignee(assignee))
        artifact_note = _artifact_directive(env.run, spawn_id, leg_expected)
        params = operation.parameters
        if not isinstance(params, dict):
            raise TypeError("team round operation parameters must be a dictionary")
        context = params.setdefault("context", [])
        context.append({"artifact_instructions": artifact_note})
        env.expect_worker(spawn_id)

    def _on_team_op_complete(node: Any) -> None:
        """ReactiveExecutor.on_op_complete callback: race-free inject() for
        team wakeup rounds. Called for every completed node."""
        if _team_coordinator is None:
            return
        executor = _executor_ref.get("executor")
        if executor is None:
            return
        try:
            state = _team_coordinator.check_round()
        except FileNotFoundError:
            return  # team file transiently unavailable — next node retries
        except Exception as e:  # noqa: BLE001 — never let a coordinator bug kill the run
            logger.warning("team round: check_round() failed: %s", e)
            return
        if not state.should_continue:
            return
        batch_size = sum(
            worker in _team_coordinator.worker_branches for worker in state.pending_targets
        )
        if batch_size and not executor.can_inject(batch_size):
            logger.warning(
                "team round: wakeup batch of %d exceeds remaining operation capacity",
                batch_size,
            )
            return
        try:
            new_ops = _team_coordinator.build_round_operations(state, prompt=checkpoint_prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("team round: build_round_operations() failed: %s", e)
            return
        injected = []
        for op in new_ops:
            _decorate_team_round_artifacts(op)
            if executor.inject(op, independent=True):
                injected.append(op)
            else:
                logger.warning(
                    "team round: inject() rejected op %s (flow no longer running)",
                    str(getattr(op, "id", op))[:8],
                )
        if injected:
            progress(
                f"  ↻ team round {_team_coordinator.rounds_run}: "
                f"woke {', '.join(sorted(state.pending_targets))}"
            )

    def _link_escalation_mirror(node: Any) -> None:
        """Attribute an escalation child's CLI transcript to this run.

        No-ops for anything that isn't an escalation child (no ``escalated_from``
        metadata) or that didn't run on a CLI engine (``provider_session_id`` unset —
        API-provider retries have nothing for the transcript mirror to link). The
        session uid a CLI engine reports is the same one the mirror keys its session
        row by, so it is the only thing this run and a detached mirror sweep share.
        """
        metadata = getattr(node, "metadata", None)
        parent_op_id = metadata.get("escalated_from") if metadata else None
        if not parent_op_id:
            return
        chat_model = getattr(getattr(node, "_branch", None), "chat_model", None)
        session_uid = getattr(chat_model, "provider_session_id", None) if chat_model else None
        if not session_uid:
            return
        ctx = getattr(env, "_live_persist", None)
        db = ctx.get("db") if ctx else None
        if db is None:
            return
        escalated_label = metadata.get("escalated_from_name") or parent_op_id[:8]
        display_name = f"escalation of {escalated_label}"
        project = getattr(env, "_project", None)
        project_source = "escalation_parent" if project else None

        async def _do() -> None:
            from lionagi.state.claude_mirror import link_escalation_session

            for attempt in range(_ESCALATION_LINK_RETRIES):
                if attempt:
                    await _asyncio.sleep(_ESCALATION_LINK_RETRY_INTERVAL)
                try:
                    linked = await link_escalation_session(
                        db,
                        session_uid=session_uid,
                        run_id=env.run.run_id,
                        name=display_name,
                        project=project,
                        project_source=project_source,
                        parent_op_id=parent_op_id,
                    )
                except Exception:  # noqa: BLE001 — a link failure must not affect the run
                    logger.exception(
                        "escalation mirror link failed for session %s", session_uid[:8]
                    )
                    return
                if linked:
                    return
            logger.warning(
                "escalation mirror link: no mirrored session for %s after %d retries",
                session_uid[:8],
                _ESCALATION_LINK_RETRIES,
            )

        _escalation_link_tasks.append(_asyncio.ensure_future(_do()))

    def _on_op_complete(node: Any) -> None:
        """ReactiveExecutor.on_op_complete callback: called for every completed node."""
        _link_escalation_mirror(node)
        _on_team_op_complete(node)

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

    def _decorate_spawn_instruction(req: SpawnRequest, spawn_id: str) -> str:
        """Give a reactively spawned node the same artifact-dir + REQUIRED
        text a planned leg gets, mirroring the block _build_dag composes."""
        role_defaults = dag_state.role_artifact_defaults.get(req.assignee) if req.assignee else None
        leg_expected = _leg_artifact_entries(spawn_id, role_defaults)
        note = _artifact_directive(env.run, spawn_id, leg_expected)
        # A node whose instruction has been composed is a worker this run
        # intends to have, whatever provider it turns out to run under.
        env.expect_worker(spawn_id)
        return f"{req.instruction}\n\n{note}"

    def _spawn_branch_setup(operation: Any, branch: Any) -> None:
        """Assign a spawned node its artifact destination.

        CLI branches receive it as a workspace; other providers get output-only wording.
        """
        spawn_id = operation.metadata.get("spawn_id") if operation is not None else None
        if not spawn_id:
            return
        artifact_dir = env.run.agent_artifact_dir(spawn_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        env.worker_artifact_dirs[spawn_id] = artifact_dir
        chat_model = getattr(branch, "chat_model", None)
        is_cli = bool(chat_model and getattr(chat_model, "is_cli", False))
        # The clone inherits the emitter's prompt. Retarget its destination and
        # only describe that destination as a workspace when one is assigned.
        _retarget_spawn_prompt(
            branch,
            artifact_dir,
            workspace_assigned=is_cli,
        )

        if not is_cli:
            return
        kwargs = chat_model.endpoint.config.kwargs
        kwargs["repo"] = artifact_dir
        project_root = str(Path(env.cwd).resolve()) if env.cwd else str(Path.cwd().resolve())
        add_dir = kwargs.setdefault("add_dir", [])
        if project_root not in add_dir:
            add_dir.append(project_root)

    t_exec = time.monotonic()
    _hb_task = _asyncio.ensure_future(_heartbeat_loop())
    _ctl_task = _asyncio.ensure_future(_control_poll_loop())
    _exchange = getattr(env, "exchange", None)
    _exch_task = _asyncio.ensure_future(_exchange.run(0.5)) if _exchange is not None else None
    _dag_cancelled = False
    try:
        dag_result = await eng_run.run_dag(
            env.builder.get_graph(),
            reactive=reactive,
            spawn_type=SpawnRequest if reactive else None,
            node_builder=(
                role_node_builder(
                    role_base,
                    decorate_instruction=_decorate_spawn_instruction,
                    start=_spawn_seq_start,
                )
                if reactive
                else None
            ),
            max_spawn=max_spawn,
            max_concurrent=conc,
            verbose=env.verbose,
            executor_ref=_executor_ref,
            context=checkpoint_flow_context,
            on_branch_created=lambda branch: (
                register_branch_hook(env._live_persist, branch) if env._live_persist else None
            ),
            spawn_branch_setup=_spawn_branch_setup if reactive else None,
            on_op_complete=_on_op_complete,
        )
    except _asyncio.CancelledError:
        _dag_cancelled = True
        raise
    finally:
        _hb_task.cancel()
        _ctl_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError):
            await _hb_task
        with contextlib.suppress(_asyncio.CancelledError):
            await _ctl_task
        if _exch_task is not None:
            _exchange.stop()
            with contextlib.suppress(_asyncio.CancelledError):
                await _exch_task
            # Route any final outbox sends left over after the last collect tick.
            await _exchange.collect_all()

        async def _drain_escalation_links_bounded() -> None:
            # An escalation-link row is nice-to-have attribution, not run
            # data — losing one to cancellation is acceptable, but a hung
            # link write (stuck DB call) blocking teardown indefinitely is
            # not. Give in-flight links a short grace period to land
            # normally, then cancel whatever is left and wait for it to
            # actually unwind before returning.
            with contextlib.suppress(Exception), move_on_after(2):
                await _asyncio.gather(*_escalation_link_tasks, return_exceptions=True)
            _survivors = [t for t in _escalation_link_tasks if not t.done()]
            for _t in _survivors:
                _t.cancel()
            if _survivors:
                # A survivor that itself swallows cancellation (or blocks in
                # sync code) would otherwise hang this await forever — bound
                # the wait too, and abandon whatever is still alive after it
                # rather than let a single stuck link task block teardown.
                with contextlib.suppress(Exception), move_on_after(2):
                    await _asyncio.gather(*_survivors, return_exceptions=True)
                _abandoned = [t for t in _survivors if not t.done()]
                if _abandoned:
                    _warn(
                        f"abandoned {len(_abandoned)} escalation-link task(s) "
                        "still alive after cancellation grace period"
                    )

        # Completion observers schedule persistence writes synchronously but the
        # writes themselves are async. Drain them while the live DB is still open.
        with CancelScope(shield=True):
            if _branch_status_tasks:
                with contextlib.suppress(Exception):
                    await _asyncio.gather(*_branch_status_tasks, return_exceptions=True)
            if _checkpoint_tasks:
                with contextlib.suppress(Exception):
                    await _asyncio.gather(*_checkpoint_tasks, return_exceptions=True)
            if _escalation_link_tasks:
                if _dag_cancelled:
                    # Cancellation already landed while run_dag() was running,
                    # so go straight to the bounded path.
                    await _drain_escalation_links_bounded()
                else:
                    # Bounded by _ESCALATION_LINK_RETRIES * _ESCALATION_LINK_RETRY_INTERVAL
                    # (a few seconds worst case) in the happy path — the outer
                    # db/session teardown (teardown_persist) only runs after this
                    # function returns, so draining here, not firing untracked, is
                    # what keeps a late retry from writing into a store this run
                    # has already closed.
                    #
                    # A cancellation can also land on *this* task after
                    # run_dag() already returned — the CancelScope shield above
                    # only stops anyio-mediated cancellation, not a direct
                    # cancel() on this task — so _dag_cancelled stays False and
                    # this is the await that would otherwise take it. gather()
                    # only settles its own cancellation once every child
                    # actually finishes, so a link task that swallows the one
                    # cancel it's handed (or blocks past it) would leave a bare
                    # `await gather(...)` parked here forever — asyncio.shield
                    # lets this await raise promptly instead of waiting on the
                    # children, which keep running in the background for
                    # _drain_escalation_links_bounded() to actually finish off.
                    # Re-raise afterward so the caller still observes the
                    # cancellation — unless the try body already raised a real
                    # exception, in which case that exception is what the
                    # caller was waiting on and must win; a cancellation that
                    # only landed during this teardown drain must not silently
                    # replace it.
                    _in_flight_exc = sys.exc_info()[1]
                    try:
                        with contextlib.suppress(Exception):
                            await _asyncio.shield(
                                _asyncio.gather(*_escalation_link_tasks, return_exceptions=True)
                            )
                    except _asyncio.CancelledError as _late_cancel:
                        await _drain_escalation_links_bounded()
                        if _in_flight_exc is not None:
                            raise _in_flight_exc from _late_cancel
                        raise
    t_exec_elapsed = time.monotonic() - t_exec

    op_results = dag_result.get("operation_results", {})
    # Includes restored spawns from a prior checkpoint generation, not just
    # this generation's — else a resume with zero NEW spawns would report
    # n_spawned=0 and skip the with_synthesis gate in _run_flow_inner.
    n_spawned = restored_spawn_count + dag_result.get("spawned_operations", 0)

    # Escalation backstop: an escalated leg (gave up via EscalationRequest
    # instead of producing a result) reads as a normal completed op_result
    # below without this — makes it loud at teardown. Spawned nodes aren't
    # in node_ids/agent_ids (fixed-size, plan-time only), so checked separately.
    graph_nodes = getattr(env.builder.get_graph(), "internal_nodes", {}) or {}
    # Re-read the edges now that the run is over: the durable record and the
    # Studio DAG outlive the terminal, so they get the final graph rather than
    # the plan-time one. Nodes absent from the graph (never built) keep their
    # plan-time entry — there is nothing to observe for them.
    final_deps = _deps_from_built_graph(
        env.builder, {str(node_ids[i]): str(i + 1) for i in range(len(assignments))}
    )
    escalated_op_ids = {str(x) for x in dag_result.get("escalated_operations", [])}
    escalated_evidence = [
        {"kind": "escalated_operation", "id": agent_ids[i], "label": assignments[i].assignee}
        for i in range(len(assignments))
        # node_ids holds Operation UUIDs, not strings, despite the `list[str]`
        # annotation -- compare on the string form so a planned (non-spawned)
        # escalated op is recognized here instead of falling through to the
        # spawned branch below and losing its assignee label.
        if str(node_ids[i]) in escalated_op_ids
    ]
    for spawned_nid in sorted(escalated_op_ids - known_node_strs):
        # Surface the stamped spawn_id (e.g. "spawn-3") instead of the
        # internal UUID, matching the artifact dirs/contract entries produced.
        graph_node = graph_nodes.get(spawned_nid)
        spawn_id = graph_node.metadata.get("spawn_id") if graph_node is not None else None
        evidence_id = spawn_id or spawned_nid
        escalated_evidence.append(
            {"kind": "escalated_operation", "id": evidence_id, "label": evidence_id}
        )
    escalated_agent_ids = [entry["id"] for entry in escalated_evidence]
    if escalated_evidence:
        # Merge, don't overwrite: a team-mode "blocked" help signal may
        # already have appended entries to env._escalated_evidence mid-run.
        prior_evidence = getattr(env, "_escalated_evidence", None) or []
        env._escalated_evidence = [*prior_evidence, *escalated_evidence]

    # Node-failure evidence: an operation's invoke() raised and
    # DependencyAwareExecutor recorded EventStatus.FAILED for it, but that
    # per-node failure was folded into completed_operations right alongside
    # genuine completions and never rolled up into the run's own status --
    # a run whose terminal (or any other) node died could still read as an
    # ordinary clean completion. Mirrors the escalation evidence above: name
    # the failed op(s) here, let stop_live_persist flip status/reason.
    failed_op_ids = {str(x) for x in dag_result.get("failed_operations", [])}
    failed_evidence = [
        {"kind": "failed_operation", "id": agent_ids[i], "label": assignments[i].assignee}
        for i in range(len(assignments))
        if str(node_ids[i]) in failed_op_ids
    ]
    for spawned_nid in sorted(failed_op_ids - known_node_strs):
        graph_node = graph_nodes.get(spawned_nid)
        spawn_id = graph_node.metadata.get("spawn_id") if graph_node is not None else None
        evidence_id = spawn_id or spawned_nid
        failed_evidence.append(
            {"kind": "failed_operation", "id": evidence_id, "label": evidence_id}
        )
    if failed_evidence:
        prior_failed_evidence = getattr(env, "_failed_operation_evidence", None) or []
        env._failed_operation_evidence = [*prior_failed_evidence, *failed_evidence]

    # Lost-node evidence: a node in the fixed initial plan (node_ids /
    # assignments, indexed 1:1) whose result never landed in
    # operation_results at all -- distinct from a node that ran and failed
    # (already caught above) or was legitimately never meant to run. Every
    # other accounted-for fate is excluded explicitly: an edge-condition skip
    # is named in skipped_operations; a leg that gave up via EscalationRequest
    # is named in escalated_operations and already fails the run through the
    # escalated-evidence backstop above with its own, more specific reason;
    # a reactive/budget-refused spawn was never a planned node to begin with
    # (dropped_spawns never touches node_ids). A cancelled run never reaches
    # this line -- run_dag raises and the whole evidence block above is
    # skipped -- so no separate check is needed for it. What remains here is
    # a node the plan expected but execution never observed in any of those
    # ways; treat it as a failure with its own evidence rather than letting
    # it render as "(no response)" below.
    skipped_op_ids = {str(x) for x in dag_result.get("skipped_operations", [])}
    observed_op_ids = {str(k) for k in op_results}
    lost_evidence = [
        {"kind": "lost_operation", "id": agent_ids[i], "label": assignments[i].assignee}
        for i in range(len(assignments))
        if str(node_ids[i]) not in observed_op_ids
        and str(node_ids[i]) not in skipped_op_ids
        and str(node_ids[i]) not in escalated_op_ids
    ]
    if lost_evidence:
        prior_failed_evidence = getattr(env, "_failed_operation_evidence", None) or []
        env._failed_operation_evidence = [*prior_failed_evidence, *lost_evidence]

    # Lost-spawn evidence: mirrors the lost-node check above, but for the
    # reactive surface. spawned_ids (when the executor reports it) is the
    # roster of every node _accept_node actually accepted into the graph --
    # spawned_operations is only a running count and can't answer "which
    # nodes were accepted" on its own. An accepted spawn that reached a
    # terminal EventStatus (e.g. CANCELLED) without ever producing a result
    # is absent from operation_results, failed_operations, and
    # skipped_operations alike; without this check it would also read as a
    # clean completion. A spawn the executor refused to accept in the first
    # place (dropped_spawns) never enters spawned_ids, so it stays excluded
    # here exactly as dropped_spawns already is above.
    spawned_ids = {str(x) for x in dag_result.get("spawned_ids", [])}
    lost_spawn_ids = (
        spawned_ids - observed_op_ids - skipped_op_ids - escalated_op_ids - failed_op_ids
    )
    lost_spawn_evidence = []
    for spawned_nid in sorted(lost_spawn_ids):
        graph_node = graph_nodes.get(spawned_nid)
        spawn_id = graph_node.metadata.get("spawn_id") if graph_node is not None else None
        evidence_id = spawn_id or spawned_nid
        lost_spawn_evidence.append(
            {"kind": "lost_operation", "id": evidence_id, "label": evidence_id}
        )
    if lost_spawn_evidence:
        prior_failed_evidence = getattr(env, "_failed_operation_evidence", None) or []
        env._failed_operation_evidence = [*prior_failed_evidence, *lost_spawn_evidence]

    # Gate-reject evidence (issue #2860): a mid-DAG gate node returned a
    # REJECT verdict and the executor short-circuited its dependent subtree
    # to skipped rather than let it run against the rejected baseline. This
    # names the rejecting gate(s), not the (possibly many) skipped
    # dependents, so `stop_live_persist` can record a "completed but gate
    # rejected" reason instead of a plain clean-pass one.
    gate_rejected_op_ids = {str(x) for x in dag_result.get("gate_rejected_operations", [])}
    gate_rejected_evidence = [
        {"kind": "gate_rejected_operation", "id": agent_ids[i], "label": assignments[i].assignee}
        for i in range(len(assignments))
        # node_ids holds Operation UUIDs, not strings, despite the `list[str]`
        # annotation -- compare on the string form so a planned (non-spawned)
        # gate is recognized here instead of falling through to the spawned
        # branch below and losing its assignee label.
        if str(node_ids[i]) in gate_rejected_op_ids
    ]
    known_node_strs_for_gates = {str(n) for n in known_nodes}
    for spawned_nid in sorted(gate_rejected_op_ids - known_node_strs_for_gates):
        graph_node = graph_nodes.get(spawned_nid)
        spawn_id = graph_node.metadata.get("spawn_id") if graph_node is not None else None
        evidence_id = spawn_id or spawned_nid
        gate_rejected_evidence.append(
            {"kind": "gate_rejected_operation", "id": evidence_id, "label": evidence_id}
        )
    if gate_rejected_evidence:
        prior_gate_evidence = getattr(env, "_gate_rejected_evidence", None) or []
        env._gate_rejected_evidence = [*prior_gate_evidence, *gate_rejected_evidence]

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
                "depends_on": final_deps.get(str(nid), deps_by_node[nid]),
                "spawned": False,
                "response": str(res) if res is not None else "(no response)",
                "time_ms": t_exec_elapsed * 1000,
            }
        )

    # Reactively spawned nodes are in the result map but not in our plan —
    # recovered here from graph node metadata since plan-time arrays are
    # fixed-size and can't cover nodes injected mid-run via SpawnRequest.
    spawned_contract_entries: list[dict] = []

    # Pre-scan every builder-stamped spawn_id BEFORE assigning any fallback:
    # synthesis must never collide with an id role_node_builder already
    # allocated (completion order alone can't be trusted to hand out spawn-1).
    stamped_spawn_ids: set[str] = set()
    for nid in op_results:
        if nid in known_nodes:
            continue
        graph_node = graph_nodes.get(nid)
        stamped = graph_node.metadata.get("spawn_id") if graph_node is not None else None
        if stamped:
            stamped_spawn_ids.add(stamped)

    _fallback_seq = 0

    def _next_fallback_spawn_id() -> str:
        nonlocal _fallback_seq
        while True:
            _fallback_seq += 1
            candidate = f"spawn-{_fallback_seq}"
            if candidate not in stamped_spawn_ids:
                return candidate

    for nid, res in op_results.items():
        if nid in known_nodes:
            continue
        graph_node = graph_nodes.get(nid)
        assignee = graph_node.metadata.get("assignee") if graph_node is not None else None
        sid = graph_node.metadata.get("spawn_id") if graph_node is not None else None
        if not sid:
            if assignee:
                # role_node_builder stamps spawn_id unconditionally; reaching
                # here without one means that invariant broke upstream — fail
                # loudly rather than mint a fresh id that hides the defect.
                raise RuntimeError(
                    f"spawned node {nid!r} carries role assignee {assignee!r} "
                    "but no spawn_id — role_node_builder must stamp spawn_id "
                    "before the executor runs the node"
                )
            sid = _next_fallback_spawn_id()
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
                # A node injected mid-run has real predecessors; read them off
                # the graph like every other node rather than reporting none.
                "depends_on": final_deps.get(str(nid), []),
                "spawned": True,
                "response": str(res) if res is not None else "(no response)",
                "time_ms": t_exec_elapsed * 1000,
            }
        )

        # Record the spawned node's role-declared artifacts in the session
        # contract, namespaced under its own subdir — required entries stay
        # enforceable, not just observability, since the node received its
        # artifact directive before it was injected.
        if assignee:
            role_defaults = _artifact_defaults_for_assignee(assignee)
            spawned_contract_entries.extend(_leg_artifact_entries(sid, role_defaults))

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
    # Derived from agent_results, not the plan-time agent_ids array, so
    # reactively spawned nodes' own artifact dirs aren't omitted here.
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
    """Format output, write the synthesis artifact, then run best-effort teardown.

    The synthesis artifact write runs first and unguarded — it IS the run's
    output, so its failure is a real run failure, stashed on
    ``env._artifact_write_error`` for teardown to flip the terminal status to
    "failed" over. Everything after (team inbox post, branch snapshots, resume
    pointer, DAG graph image) is best-effort telemetry whose failure is caught
    and stashed on ``env._finalize_error`` instead, so it can never mask or be
    conflated with the artifact-write outcome.
    """
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
        try:
            env.run.synthesis_path.write_text(synthesis_result["response"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "flow finalize: writing the synthesis artifact failed; the run "
                "produced no output and cannot be reported as completed: %s",
                exc,
                exc_info=True,
            )
            env._artifact_write_error = {"error_class": type(exc).__name__, "error": str(exc)}

    # Each best-effort side effect below is guarded independently: one
    # raising (e.g. a stuck team-inbox file lock) must not skip the ones
    # after it (snapshot, resume pointer, graph image).
    finalize_errors: list[dict] = []

    def _guard_finalize_step(label: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "flow finalize step (%s) failed after the DAG already "
                "completed; DAG result is unaffected: %s",
                label,
                exc,
                exc_info=True,
            )
            finalize_errors.append(
                {"step": label, "error_class": type(exc).__name__, "error": str(exc)}
            )

    if env.team_data:
        _guard_finalize_step(
            "team_post",
            lambda: _post_results_to_team(
                env.team_data, agent_results, agent_ids, synthesis_result
            ),
        )

    def _snapshot_and_resume_pointer() -> None:
        # "agents" must cover every id "operations" (below) references, so it
        # walks agent_results (which includes spawned nodes), not just the
        # fixed-size plan-time assignments, or a spawned id resolves to nothing.
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

    _guard_finalize_step("snapshot", _snapshot_and_resume_pointer)

    if show_graph:

        def _write_graph_image() -> None:
            from lionagi.operations._visualize_graph import visualize_graph

            visualize_graph(
                env.builder,
                title=f"Flow DAG — {len(assignments)} assignments (+{n_spawned} spawned)",
                save_path=str(env.run.dag_image_path),
            )

        _guard_finalize_step("graph", _write_graph_image)

    if finalize_errors:
        if len(finalize_errors) == 1:
            env._finalize_error = {k: v for k, v in finalize_errors[0].items() if k != "step"}
        else:
            env._finalize_error = {
                "error_class": "MultipleFinalizeErrors",
                "error": "; ".join(
                    f"{e['step']}: {e['error_class']}: {e['error']}" for e in finalize_errors
                ),
            }

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
    team_max_rounds: int = 2,
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
    mcp_config: str | None = None,
    no_mcp_config: bool = False,
    **legacy_kwargs,
) -> tuple[str, str]:
    """Returns (output, terminal_status)."""
    stamp_worker_depth()

    if "max_agents" in legacy_kwargs and max_ops == 0:
        max_ops = legacy_kwargs.pop("max_agents")
    elif "max_agents" in legacy_kwargs:
        legacy_kwargs.pop("max_agents")
    if legacy_kwargs:
        raise TypeError(f"_run_flow() got unexpected keyword arguments: {list(legacy_kwargs)}")

    _started_at = time.time()
    _invocation_kind = "play" if playbook_name else "flow"

    # The checkpoint's own "config" replays THIS call's kwargs verbatim on
    # --resume (dry_run/show_graph excluded — presentation flags, not "what
    # happened"). Built unconditionally so a resumed run stays resumable.
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
        "team_max_rounds": team_max_rounds,
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
        mcp_config=mcp_config,
        no_mcp_config=no_mcp_config,
    )

    # `--notify` is compatibility sugar over the terminal-callback registry:
    # registered against this run's own entity, unregistered in `finally`
    # below. The handler fires from the same guarded lifecycle transition
    # that persists the terminal status — no direct notify call at teardown.
    _notify_scope_name: str | None = None
    _notify_entity_kind = "invocation" if invocation_id else "session"
    _notify_entity_id = invocation_id if invocation_id else str(env.session.id)
    if notify:
        from lionagi.state.lifecycle.notify_settings import (
            record_notify_rejection_to_run,
        )

        def _notify_override_refused(reason: str) -> None:
            # This run explicitly asked for a notifier and will not get one.
            # Recording it here is what keeps a refusal distinguishable from
            # never having configured one; both otherwise register nothing.
            record_notify_rejection_to_run(env.run, reason)

        _notify_scope_name = register_flow_notify_scope(
            override=notify,
            entity_kind=_notify_entity_kind,
            entity_id=_notify_entity_id,
            invocation_id=invocation_id,
            flow_kind=_invocation_kind,
            playbook=playbook_name,
            save_dir=save_dir,
            cwd=cwd or os.getcwd(),
            started_at=_started_at,
            on_rejection=_notify_override_refused,
        )

    # Bind this run into the notify.on_terminal handler at registration time so
    # a late outcome lands here or nowhere, never on a later run. Skipped when
    # --notify already owns this entity (a second override would double-fire).
    from lionagi.state.lifecycle.notify_settings import (
        register_run_notify_outcome_scope,
        unregister_run_notify_outcome_scope,
    )

    _notify_outcome_scope_name = (
        None
        if notify
        else register_run_notify_outcome_scope(
            env.run,
            entity_kind=_notify_entity_kind,
            entity_id=_notify_entity_id,
            project_dir=cwd,
        )
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

    # start_live_persist resolves `project` (explicit arg, or detect_project()
    # fallback) and stashes the RESOLVED value on env._project — what the
    # session row actually gets — for _execute_dag's escalation-mirror-link
    # hook, which runs deeper in the call chain and must inherit the same
    # project the run itself was attributed, not re-guess from cwd.
    await start_live_persist(
        env,
        invocation_kind=_invocation_kind,
        playbook_name=playbook_name,
        # The profile the run resolved, not the one this call named: a call that
        # named neither an agent nor a model named none, and recording that
        # `agent_name` would leave the record unable to say what it ran under.
        agent_name=env.orc_profile_name,
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
        team_max_rounds=team_max_rounds,
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

            # Terminal-notify no longer fires from a direct call here: the
            # session/invocation status writes below are guarded lifecycle
            # transitions that push through the terminal-callback registry.
            _ended_at = _time.time()
            if invocation_id:
                from lionagi.state.db import StateDB

                _invocation_previous_status = "unknown"
                # Populated once resolution succeeds, so the fallback below can
                # tell "resolution never produced an outcome" from "resolution
                # produced one and only the write failed".
                inv_status: str | None = None
                inv_rc: str | None = None
                try:
                    async with StateDB() as _status_db:
                        _invocation_row = await _status_db.get_invocation(invocation_id)
                    if _invocation_row and _invocation_row.get("status"):
                        _invocation_previous_status = str(_invocation_row["status"])
                    (
                        inv_status,
                        inv_rc,
                        inv_rs,
                        inv_ev,
                        inv_meta,
                    ) = await _resolve_invocation_terminal_flow(
                        invocation_id, fallback_status=_terminal_status
                    )
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
                    # The guarded update_status() above never committed, so the
                    # terminal-callback registry never fired for this entity;
                    # emit a best-effort envelope directly instead of silently
                    # dropping the notification. Prefer the invocation
                    # status/reason resolution already settled (it can differ
                    # from the flow's own coarser status, e.g. completed_empty)
                    # and fall back to the flow's own terminal status only when
                    # resolution never produced one.
                    try:
                        import uuid as _uuid

                        from lionagi.state.lifecycle.callbacks import (
                            DEFAULT_TERMINAL_CALLBACKS,
                            Correlation,
                            EntityRef,
                            RunTerminalEnvelope,
                        )

                        _notify_status = inv_status or _terminal_status
                        _notify_reason = inv_rc or _fallback_notify_reason(_notify_status)

                        await DEFAULT_TERMINAL_CALLBACKS.emit(
                            RunTerminalEnvelope(
                                event_id=str(_uuid.uuid4()),
                                entity=EntityRef(kind="invocation", id=invocation_id),
                                previous_status=_invocation_previous_status,
                                terminal_status=_notify_status,
                                reason_code=_notify_reason,
                                occurred_at=_ended_at,
                                correlation=Correlation(invocation_id=invocation_id),
                                durable=False,
                            )
                        )
                    except Exception:
                        _logging.getLogger("lionagi.cli").exception(
                            "Failed to emit fallback terminal notify for invocation %s",
                            invocation_id,
                        )

            unregister_flow_notify_scope(_notify_scope_name)
            unregister_run_notify_outcome_scope(_notify_outcome_scope_name)
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
    team_max_rounds: int = 2,
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
        # depends_on ordinal refs), so normalization is skipped
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
        # Replay the naming bookkeeping so a reactive spawn post-resume
        # doesn't collide with a name already used in the resumed run.
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
            f"{role_roster(env.default_model_spec)}\n\n{mode_roster(env.pack)}\n\n"
            f"{budget_note}{team_guidance(team_attach or team_name)}"
        )

        progress("Planning DAG...")
        try:
            assignments = await plan(
                env.orc_branch, prompt, roles=roster, dag=True, guidance=guidance, max_tasks=max_ops
            )
        except EmptyOutgoingContentError:
            raise
        except ValueError as exc:
            # plan() raises a bare ValueError when the orchestrator still
            # overshoots max_tasks after the cap was stated in guidance —
            # route it through the same clean-failure channel as every
            # other plan-time failure in this function.
            raise FlowPlanError(str(exc)) from exc
        if not assignments:
            # Fail loud rather than silently exiting 0 with no work done.
            _warn("Orchestrator returned no assignments; retrying once with a sharper instruction.")
            try:
                assignments = await plan(
                    env.orc_branch,
                    prompt,
                    roles=roster,
                    dag=True,
                    guidance=guidance
                    + " Return ONLY the assignments list — do not perform the task.",
                    max_tasks=max_ops,
                )
            except EmptyOutgoingContentError:
                raise
            except ValueError as exc:
                raise FlowPlanError(str(exc)) from exc
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

        try:
            dep_indices = normalize_dep_indices(assignments)
        except ValueError as exc:
            raise FlowPlanError(str(exc)) from exc

    # --workers overrides model only; --bare also drops profiles (distinct behaviors).
    pool = [s.strip() for s in workers_str.split(",")] if workers_str else []

    dag_lines = []
    for i, ta in enumerate(assignments):
        deps = f" ← {','.join(str(j + 1) for j in dep_indices[i])}" if dep_indices[i] else ""
        dag_lines.append(f"{i + 1}:{ta.assignee}{deps}")
    # Says "as declared" because that is all it can say: no node exists yet for
    # any of these assignments, so this line is the planner's input to the build
    # and not an observation of the graph that will run.
    progress(
        f"Plan done ({t_plan:.1f}s): {len(assignments)} assignments, dependencies as declared "
        f"by the planner (the run graph is not built yet) — {' | '.join(dag_lines)}"
    )

    if dry_run:
        lines = [
            f"Plan ({len(assignments)} assignments):",
            "Dependencies below are the ones the planner declared. A dry run builds no run",
            "graph, so nothing here has been checked against the structure that would run.",
            "",
        ]
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
                # (profile shadows casts; ADR-0043 follow-up makes them compose).
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

    if env.team_data:
        env.exchange = Exchange()
        env.messenger = LionMessenger(env.exchange)
        env.messenger.on("help", make_help_coordinator(env))
        env.roster = {}
        # Mixed-provider teams build one worker branch at a time, so which
        # teammates end up messenger-bound isn't known until _build_dag's
        # loop finishes. Resolve it here up front (worker_is_cli is a cheap,
        # side-effect-free pre-pass) so build order can't affect the prompt.
        env.messenger_names = frozenset(
            agent_ids[i]
            for i, ta in enumerate(assignments)
            if not worker_is_cli(env, ta.assignee, pool[i % len(pool)] if pool else None)
        )

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
        checkpoint_spawned_seed=(
            resume_checkpoint.get("spawned") if resume_checkpoint is not None else None
        ),
        team_max_rounds=team_max_rounds,
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
    dry_run/show_graph/notify come from the CURRENT invocation (presentation
    overrides); every other _run_flow kwarg replays the persisted config."""
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
