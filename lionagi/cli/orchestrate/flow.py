# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Reactive DAG flow: orchestrator plans TaskAssignments → self-expanding execution.

Clean-break design (no bespoke plan models):

- **Plan** = a ``list[TaskAssignment]`` (casts coordination emission) the
  orchestrator emits — ``assignee`` names a role, ``depends_on`` (1-based step
  indices) forms the DAG. No ``FlowPlan``/``FlowAgent``/``FlowOp``.
- **Workers** are casts roles (``_orchestration.casts_role_system``) granted the
  ``SpawnRequest`` capability.
- **Execution** is ``session.flow(reactive=True)``: a worker that finds work
  beyond its assignment emits a ``SpawnRequest`` and a new op is injected into
  the *live* DAG — replacing the old halt → critic-verdict → re-plan → re-run
  loop with continuous self-expansion.
"""

from __future__ import annotations

import contextlib
import json
import time

from lionagi._errors import LionError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.casts.emission import SpawnRequest
from lionagi.ln.concurrency import move_on_after
from lionagi.orchestration import plan, role_node_builder

from .._logging import progress
from .._logging import warn as _warn
from .._providers import parse_model_spec
from ._common import _create_fanout_team, _format_result_json, _post_results_to_team
from ._orchestration import (
    EFFORT_MAP,
    OrchestrationEnv,
    available_roles,
    build_worker_branch,
    finalize_orchestration,
    mode_roster,
    resolve_modes,
    resolve_worker_spec,
    role_config,
    role_roster,
    setup_orchestration,
    start_live_persist,
    stop_live_persist,
    team_guidance,
)


class FlowPlanError(LionError):
    """Orchestrator failed to produce a usable plan (a non-empty TaskAssignment list).

    Surfaced as a non-zero CLI exit with the raw orchestrator response attached,
    instead of a silent ``return`` that exited 0 with no work done (#1236).
    """


def _raw_response_snippet(res, limit: int = 800) -> str:
    """Truncated repr of whatever the planner returned, for diagnostics."""
    text = (str(res).strip() if res is not None else "") or "(empty response)"
    if len(text) > limit:
        text = f"{text[:limit]}… [+{len(text) - limit} chars truncated]"
    return text


async def _persist_session_phase(env, phase: str) -> None:
    """Best-effort write of the live execution phase to the session row.

    Surfaced as the PHASE column in ``li monitor`` (#1235). Failures here must
    never interrupt flow execution — the phase marker is observability.
    """
    ctx = getattr(env, "_live_persist", None)
    if ctx and ctx.get("db"):
        with contextlib.suppress(Exception):
            await ctx["db"].update_session(ctx["session_id"], current_phase=phase)


# ── Budget preamble template ──────────────────────────────────────────────
#
# Injected at the START of each op's instruction when the orchestrator runs with
# a total timeout (--timeout / playbook timeout:). Workers see their share of
# the budget so they can pace reasoning and switch from research to writing
# before time runs out.

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
    """Format the BUDGET preamble for a single op."""
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
    """Aggregate child session statuses into a flow invocation terminal reason."""
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    async with StateDB() as db:
        sessions = await db.list_sessions_for_invocation(invocation_id)
        child_statuses = [str(s.get("status") or "") for s in sessions]
        evidence_refs = [{"kind": "session", "id": s["id"]} for s in sessions if s.get("id")]
        metadata: dict = {"child_statuses": child_statuses}

        # Precedence: timed_out > failed > aborted > cancelled > completed.
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
                    RunReasons.ABORTED_USER,
                    "Flow was aborted because at least one child session was aborted.",
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
                RunReasons.ABORTED_USER,
                "Flow was aborted by the user.",
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


# ── depends_on parsing ────────────────────────────────────────────────────
#
# TaskAssignment.depends_on carries 1-based step numbers (the orchestrator
# numbers assignments by list position). Only *earlier* steps can be wired as
# graph predecessors at build time; forward/invalid refs are dropped.


def _earlier_dep_indices(depends_on: list[str] | None, position: int) -> list[int]:
    """0-based indices of earlier assignments referenced by ``depends_on``."""
    out: list[int] = []
    for ref in depends_on or []:
        try:
            j = int(str(ref).strip()) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= j < position:
            out.append(j)
    return out


def _parse_reactive(spec: str | None) -> tuple[bool, set[str] | None]:
    """Parse ``--reactive`` into ``(reactive, spawn_roles)``.

    - ``off`` / ``none`` / ``false``     → ``(False, set())`` — flat batch DAG,
      no worker may spawn (cheapest, fully deterministic).
    - ``all`` / ``on`` / ``""`` / None   → ``(True, None)`` — every worker may
      grow the live DAG (maximally reactive; the default).
    - ``critic,evaluator`` (role list)   → ``(True, {roles})`` — only those
      roles are granted the SpawnRequest capability.
    """
    s = (spec or "all").strip().lower()
    if s in ("off", "none", "false", "no", "0"):
        return False, set()
    if s in ("all", "on", "true", "yes", "1", ""):
        return True, None
    roles = {r.strip() for r in spec.split(",") if r.strip()}
    return (True, roles) if roles else (True, None)


def _format_flow_result_text(
    agent_results: list[dict],
    synthesis_result: dict | None = None,
) -> str:
    lines = []
    for w in agent_results:
        deps = w.get("depends_on") or []
        dep_str = f"  deps: {', '.join(deps)}" if deps else ""
        tag = "  [spawned]" if w.get("spawned") else ""
        lines.append(f"{'═' * 60}")
        lines.append(f"  {w['id']} ({w['name']}){tag}  [{w['model']}]{dep_str}")
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
    reactive_spec: str = "all",
    fast: bool = False,
    playbook_name: str | None = None,
    playbook_artifacts: dict | None = None,
    invocation_id: str | None = None,
    project: str | None = None,
    **legacy_kwargs,
) -> tuple[str, str]:
    """Reactive DAG flow: orchestrator plans TaskAssignments → self-expanding execution.

    Returns ``(output, terminal_status)`` — ADR-0029 §7 lets the artifact
    contract verifier flip a clean ``completed`` into ``failed`` at teardown, so
    callers MUST use the returned status for the process exit code.

    ``max_ops`` caps total ops: the initial plan plus reactive spawns share one
    budget. ``max_agents`` is accepted as a deprecated alias.
    """
    if "max_agents" in legacy_kwargs and max_ops == 0:
        max_ops = legacy_kwargs.pop("max_agents")
    elif "max_agents" in legacy_kwargs:
        legacy_kwargs.pop("max_agents")
    if legacy_kwargs:
        raise TypeError(f"_run_flow() got unexpected keyword arguments: {list(legacy_kwargs)}")

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
        total_budget=timeout,
    )

    # ADR-0012/0022: playbook run → `play`, ad-hoc → `flow`. Surface the
    # orchestrator's default model + effort on the session row.
    _orc_ms = parse_model_spec(env.default_model_spec) if env.default_model_spec else None
    _orc_provider = None
    if _orc_ms and "/" in _orc_ms.model:
        _orc_provider = _orc_ms.model.split("/", 1)[0]

    # ADR-0029 §4-5: resolve the artifact contract (playbook overrides agent).
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

    await start_live_persist(
        env,
        invocation_kind="play" if playbook_name else "flow",
        playbook_name=playbook_name,
        agent_name=agent_name,
        artifacts_path=str(env.run.artifact_root),
        invocation_id=invocation_id,
        model=_orc_ms.model if _orc_ms else None,
        provider=_orc_provider,
        effort=env.effort,
        project=project,
        artifact_contract=artifact_contract,
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
        reactive_spec=reactive_spec,
    )
    # ADR-0025: distinguish timed_out / aborted / cancelled / failed.
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
    except KeyboardInterrupt:
        _terminal_status = "aborted"
        raise
    except (TimeoutError, LionTimeoutError):
        _terminal_status = "timed_out"
        raise
    except BaseException as exc:
        from lionagi.ln.concurrency import get_cancelled_exc_class

        if isinstance(exc, get_cancelled_exc_class()):
            _terminal_status = "cancelled"
        else:
            _terminal_status = "failed"
        raise
    finally:
        # Shield teardown from outer cancellation so iModel executors are always
        # closed; see lionagi/cli/agent.py for the full rationale.
        import anyio

        with anyio.CancelScope(shield=True):
            effective_status = await stop_live_persist(env, status=_terminal_status)
            if effective_status != _terminal_status:
                _terminal_status = effective_status
            if invocation_id:
                import time as _time

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
                    async with StateDB() as _inv_db:
                        await _inv_db.update_invocation(invocation_id, ended_at=_time.time())
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
    max_ops: int = 0,
    dry_run: bool = False,
    show_graph: bool = False,
    reactive_spec: str = "all",
) -> str:
    """Inner flow logic (no timeout wrapper)."""
    t0 = time.monotonic()
    run = env.run
    session = env.session
    builder = env.builder

    # ── Phase 0: Orchestrator plans the DAG as a list[TaskAssignment] ──
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
        # #1236: fail loud rather than exiting 0 with no work. One reinforced
        # retry, then raise with the raw response attached.
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

    # One deduplicated worker name per assignment (researcher, researcher-2, …).
    name_counts: dict[str, int] = {}
    agent_ids: list[str] = []
    for ta in assignments:
        name_counts[ta.assignee] = name_counts.get(ta.assignee, 0) + 1
        n = name_counts[ta.assignee]
        agent_ids.append(f"{ta.assignee}-{n}" if n > 1 else ta.assignee)

    dep_indices = [_earlier_dep_indices(ta.depends_on, i) for i, ta in enumerate(assignments)]

    dag_lines = []
    for i, ta in enumerate(assignments):
        deps = f" ← {','.join(str(j + 1) for j in dep_indices[i])}" if dep_indices[i] else ""
        dag_lines.append(f"{i + 1}:{ta.assignee}{deps}")
    progress(f"Plan done ({t_plan:.1f}s): {len(assignments)} assignments — {' | '.join(dag_lines)}")

    # ── Dry run: dump assignments and exit ────────────────────────────
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
            if env.bare:
                lines.append(f"  {agent_ids[i]}: {model_spec} (bare)")
                continue
            rm, rp = resolve_worker_spec(ta.assignee)
            cfg = role_config(ta.assignee)
            if rp:
                # A user profile supplies its own body — casts modes don't apply
                # (profile shadows casts; ADR-0074 follow-up makes them compose).
                model, src, modes = rm, "profile", []
            elif cfg and cfg.model:
                model, src = cfg.model, "pack"
                modes = resolve_modes(ta.assignee, ta.modes or None)
            else:
                model, src = model_spec, "default"
                modes = resolve_modes(ta.assignee, ta.modes or None)
            mode_str = f"  modes={modes}" if modes else ""
            lines.append(f"  {agent_ids[i]}: {model} ({src}){mode_str}")
        return "\n".join(lines)

    # ── Team setup ────────────────────────────────────────────────────
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
    team_data = env.team_data

    # ── Budget preambles (proportional split of a total timeout) ──────
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

    # ── Reactive mode (--reactive): who, if anyone, may grow the DAG ──
    reactive, spawn_roles = _parse_reactive(reactive_spec)

    def _may_spawn(role: str) -> bool:
        return reactive and (spawn_roles is None or role in spawn_roles)

    # ── Build one worker branch + op node per assignment ──────────────
    # A worker granted SpawnRequest may grow the live DAG. role_base maps a role
    # → a branch the reactive node_builder clones for spawned follow-ups.
    worker_models: list[str] = []
    node_ids: list[str] = []
    role_base: dict[str, object] = {}

    for i, ta in enumerate(assignments):
        w_branch, w_model, w_profile = build_worker_branch(
            env,
            agent_id=agent_ids[i],
            role=ta.assignee,
            explicit_name=agent_ids[i],
            grant_spawn=_may_spawn(ta.assignee),
            modes=ta.modes or None,
        )
        worker_models.append(w_model)
        role_base.setdefault(ta.assignee, w_branch)

        # Context: original task + artifact dir + upstream dirs + effort + team.
        ctx: list = [{"original_task": prompt}]
        artifact_note = (
            f"Your artifact directory: {run.agent_artifact_dir(agent_ids[i])}/ — "
            "write output files here."
        )
        if dep_indices[i]:
            ups = "; ".join(
                f"step {j + 1} ({agent_ids[j]}): {run.agent_artifact_dir(agent_ids[j])}/"
                for j in dep_indices[i]
            )
            artifact_note += f" Upstream deps: {ups}."
        ctx.append({"artifact_instructions": artifact_note})
        if team_data:
            ctx.append(
                {
                    "team": {
                        "id": team_data["id"],
                        "name": team_data["name"],
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
        node = builder.add_operation(
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

    # ── Early DAG snapshot for Studio ─────────────────────────────────
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
            await ctx_lp["db"].update_session(
                ctx_lp["session_id"], node_metadata=json.dumps(early_graph)
            )

    # ── Progress + segment + branch-status plumbing (Studio) ──────────
    # The DAG runs through the planning engine's run_dag (below), which tees a
    # NodeStarted / NodeCompleted / NodeFailed onto the session bus per node.
    # These three handlers OBSERVE the bus — persistence and Studio segments are
    # reactive subscriptions, not a threaded-through on_progress callback.
    _op_segments: list[dict] = []

    def _on_node_started(sig, _ctx):
        progress(f"  ▶ {sig.name} started")
        _update_branch_status(sig.name, "running")
        _record_segment(sig.op_id, sig.name, "running")

    def _on_node_completed(sig, _ctx):
        progress(f"  ✓ {sig.name} done ({sig.elapsed:.1f}s)")
        _update_branch_status(sig.name, "completed")
        _record_segment(sig.op_id, sig.name, "completed")

    def _on_node_failed(sig, _ctx):
        progress(f"  ✗ {sig.name} FAILED ({sig.elapsed:.1f}s)")
        _update_branch_status(sig.name, "failed")
        _record_segment(sig.op_id, sig.name, "failed")

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

    def _persist_segments():
        ctx = getattr(env, "_live_persist", None)
        if not ctx or not ctx.get("db"):
            return
        extras = getattr(env, "_finalize_extras", {}) or {}
        extras["segments"] = _op_segments
        env._finalize_extras = extras
        import asyncio as _aio

        async def _do():
            with contextlib.suppress(Exception):
                await ctx["db"].update_session(ctx["session_id"], node_metadata=json.dumps(extras))

        _aio.ensure_future(_do())

    def _update_branch_status(branch_name: str, new_status: str):
        ctx = getattr(env, "_live_persist", None)
        if not ctx or not ctx.get("db"):
            return
        branch = next((b for b in env.session.branches if b.name == branch_name), None)
        if not branch:
            return
        import asyncio as _aio

        async def _do():
            with contextlib.suppress(Exception):
                kw = {"status": new_status}
                if new_status == "running":
                    kw["started_at"] = time.time()
                elif new_status in ("completed", "failed"):
                    kw["ended_at"] = time.time()
                await ctx["db"].update_branch(str(branch.id), **kw)

        _aio.ensure_future(_do())

    # ── Execution (reactive self-expansion, or flat batch when off) ───
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

    import asyncio as _asyncio

    heartbeat_interval = 60
    max_idle_seconds = 600

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

    # Subscribe the Studio/segment handlers to the node-lifecycle bus, then run
    # the DAG through the planning engine (ADR-0075 §4). run_dag emits the node
    # signals the handlers above consume; the engine shares this session, so the
    # emission store and any other observers see the same events.
    from lionagi.engines import PlanningEngine
    from lionagi.session.signal import NodeCompleted, NodeFailed, NodeStarted

    session.observe(NodeStarted, handler=_on_node_started)
    session.observe(NodeCompleted, handler=_on_node_completed)
    session.observe(NodeFailed, handler=_on_node_failed)
    eng_run = PlanningEngine().new_run(session=session)

    t_exec = time.monotonic()
    _hb_task = _asyncio.ensure_future(_heartbeat_loop())
    try:
        dag_result = await eng_run.run_dag(
            builder.get_graph(),
            reactive=reactive,
            spawn_type=SpawnRequest if reactive else None,
            node_builder=role_node_builder(role_base) if reactive else None,
            max_spawn=max_spawn,
            max_concurrent=conc,
            verbose=env.verbose,
        )
    finally:
        _hb_task.cancel()
        with contextlib.suppress(_asyncio.CancelledError):
            await _hb_task
    t_exec_elapsed = time.monotonic() - t_exec

    op_results = dag_result.get("operation_results", {})
    n_spawned = dag_result.get("spawned_operations", 0)

    # ── Collect results (initial assignments + reactively spawned) ────
    agent_results: list[dict] = []

    def _record_result(result: dict) -> None:
        agent_results.append(result)
        with contextlib.suppress(OSError):
            agent_dir = run.agent_artifact_dir(result["agent_id"])
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

    # Reactively spawned nodes are in the result map but not in our plan.
    spawn_idx = 0
    for nid, res in op_results.items():
        if nid in known_nodes:
            continue
        spawn_idx += 1
        sid = f"spawn-{spawn_idx}"
        _record_result(
            {
                "id": sid,
                "agent_id": sid,
                "name": "spawned",
                "model": "",
                "depends_on": [],
                "spawned": True,
                "response": str(res) if res is not None else "(no response)",
                "time_ms": t_exec_elapsed * 1000,
            }
        )

    spawn_note = f" (+{n_spawned} spawned)" if n_spawned else ""
    progress(f"DAG done ({t_exec_elapsed:.1f}s){spawn_note}.")

    # ── Synthesis ─────────────────────────────────────────────────────
    synthesis_result = None
    if (with_synthesis or n_spawned) and agent_results:
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
        adirs = [str(run.agent_artifact_dir(a)) for a in agent_ids]
        team_synth_note = ""
        if team_data:
            team_synth_note = (
                f"\n\nTEAM MESSAGES: Review inter-agent messages (team {team_data['id']}) "
                "for coordination context not captured in artifacts."
            )

        synth_node = builder.add_operation(
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
        synth_result = await session.flow(builder.get_graph(), verbose=env.verbose)
        t_synth_elapsed = time.monotonic() - t_synth
        synth_res = synth_result.get("operation_results", {}).get(synth_node)
        synthesis_result = {
            "model": synth_label,
            "response": str(synth_res) if synth_res is not None else "(no response)",
            "time_ms": t_synth_elapsed * 1000,
        }
        progress(f"Synthesis done ({t_synth_elapsed:.1f}s).")

    # ── Output ────────────────────────────────────────────────────────
    if output_format == "json":
        output = _format_result_json(agent_results, synthesis_result)
    else:
        output = _format_flow_result_text(agent_results, synthesis_result)

    if synthesis_result:
        run.synthesis_path.write_text(synthesis_result["response"])

    if team_data:
        _post_results_to_team(team_data, agent_results, agent_ids, synthesis_result)

    # ── Persist branches + run manifest + hints ───────────────────────
    finalize_orchestration(
        env,
        kind="flow",
        prompt=prompt,
        extras={
            "agents": [
                {
                    "id": agent_ids[i],
                    "name": agent_ids[i],
                    "model": worker_models[i],
                    "artifact_dir": str(run.agent_artifact_dir(agent_ids[i])),
                }
                for i in range(len(assignments))
            ],
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
                builder,
                title=f"Flow DAG — {len(assignments)} assignments (+{n_spawned} spawned)",
                save_path=str(run.dag_image_path),
            )

    t_total = time.monotonic() - t0
    progress(f"\nTotal: {t_total:.1f}s")

    return output
