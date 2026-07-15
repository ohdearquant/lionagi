# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fan-out execution: decompose → parallel workers → optional synthesis."""

from __future__ import annotations

import os
import time

from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import CancelScope, create_task_group, move_on_after
from lionagi.orchestration import plan
from lionagi.orchestration.prompts import SYNTHESIS_INSTRUCTION
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger

from .._agent_depth import stamp_worker_depth
from .._logging import log_error, progress, warn
from .._providers import parse_model_spec
from .._util import classify_exception
from ._common import (
    _build_worker_operate_node,
    _create_fanout_team,
    _format_result_json,
    _format_result_text,
    _post_results_to_team,
)
from ._notify import register_flow_notify_scope, unregister_flow_notify_scope
from ._orchestration import (
    OrchestrationEnv,
    available_roles,
    build_worker_branch,
    finalize_orchestration,
    parse_orchestrator_provider,
    role_roster,
    setup_orchestration,
    start_live_persist,
    stop_live_persist,
    team_history_context,
    worker_is_cli,
)


async def _run_fanout(
    model_spec: str,
    prompt: str,
    *,
    num_workers: int = 3,
    workers_str: str | None = None,
    with_synthesis: bool = False,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    max_concurrent: int = 0,
    yolo: bool = False,
    bypass: bool = False,
    verbose: bool = False,
    effort: str | None = None,
    theme: str | None = None,
    output_format: str = "text",
    save_dir: str | None = None,
    team_name: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    agent_name: str | None = None,
    fast: bool = False,
    playbook_name: str | None = None,
    invocation_id: str | None = None,
    project: str | None = None,
    pack: str | None = None,
    notify: str | None = None,
) -> tuple[str, str]:
    """Three-phase fan-out: decompose → fan out → synthesize.

    Returns ``(result, terminal_status)`` — mirrors `_run_flow`'s contract so
    the completion-trust gate's `completed_empty` (and any other status the
    teardown path settles on) reaches the caller's exit code instead of being
    silently dropped in favour of a hardcoded success.
    """
    stamp_worker_depth()
    _started_at = time.time()
    env = await setup_orchestration(
        pattern_name="Fanout",
        model_spec=model_spec,
        agent_name=agent_name,
        save_dir=save_dir,
        cwd=cwd,
        yolo=yolo,
        bypass=bypass,
        verbose=verbose,
        effort=effort,
        theme=theme,
        bare=False,
        fast=fast,
        pack=pack,
    )
    _shared: dict = {}

    # Persist the orchestrator default model + effort on the session row.
    # Per-worker model is written branch-side when build_worker_branch runs.
    _orc_model, _orc_provider = parse_orchestrator_provider(env.default_model_spec)
    await start_live_persist(
        env,
        invocation_kind="fanout",
        playbook_name=playbook_name,
        agent_name=agent_name,
        artifacts_path=str(env.run.artifact_root),
        invocation_id=invocation_id,
        model=_orc_model,
        provider=_orc_provider,
        effort=env.effort,
        project=project,
    )

    # Session-scoped: stop_live_persist terminalizes only the session; invocation
    # records are finalized externally and would never fire.
    _notify_scope_name: str | None = None
    if notify:
        _notify_scope_name = register_flow_notify_scope(
            override=notify,
            entity_kind="session",
            entity_id=str(env.session.id),
            invocation_id=invocation_id,
            flow_kind="fanout",
            playbook=playbook_name,
            save_dir=save_dir,
            cwd=cwd or os.getcwd(),
            started_at=_started_at,
        )

    inner_kw = dict(
        env=env,
        num_workers=num_workers,
        workers_str=workers_str,
        with_synthesis=with_synthesis,
        synthesis_model=synthesis_model,
        synthesis_prompt=synthesis_prompt,
        max_concurrent=max_concurrent,
        output_format=output_format,
        team_name=team_name,
        _shared=_shared,
    )

    # ADR-0057: distinguish timed_out / aborted / cancelled / failed.
    _terminal_status = "completed"
    result: str = ""
    try:
        if timeout:
            with move_on_after(timeout) as cancel_scope:
                result = await _run_fanout_inner(model_spec, prompt, **inner_kw)
            if cancel_scope.cancelled_caught:
                _terminal_status = "timed_out"
                n_saved = len(_shared.get("saved_workers", []))
                msg = f"Fanout timed out after {timeout}s"
                if n_saved:
                    msg += f" ({n_saved} worker results already saved to {env.run.artifact_root})"
                log_error(msg)
                raise LionTimeoutError(msg)
        else:
            result = await _run_fanout_inner(model_spec, prompt, **inner_kw)
        if _shared.get("artifact_failures"):
            _terminal_status = "failed"
    except BaseException as exc:
        _terminal_status = classify_exception(exc)
        raise
    finally:
        with CancelScope(shield=True):
            effective_status = await stop_live_persist(env, status=_terminal_status)
            if effective_status != _terminal_status:
                _terminal_status = effective_status
            # Unregister after stop_live_persist fires the terminal transition.
            unregister_flow_notify_scope(_notify_scope_name)
            for _br in env.session.branches:
                await _br.mdls.shutdown()

    return result, _terminal_status


async def _run_fanout_inner(
    model_spec: str,
    prompt: str,
    *,
    env: OrchestrationEnv,
    num_workers: int = 3,
    workers_str: str | None = None,
    with_synthesis: bool = False,
    synthesis_model: str | None = None,
    synthesis_prompt: str | None = None,
    max_concurrent: int = 0,
    output_format: str = "text",
    team_name: str | None = None,
    _shared: dict | None = None,
) -> str:
    """Inner fanout logic without timeout wrapper."""
    t0 = time.monotonic()

    roster = available_roles()
    progress(f"Phase 1: Orchestrator decomposing task into ≤{num_workers} assignments...")
    assignments = await plan(
        env.orc_branch,
        prompt,
        roles=roster,
        dag=False,
        guidance=role_roster(env.default_model_spec),
        max_tasks=num_workers,
    )
    t_decompose = time.monotonic() - t0
    if not assignments:
        return "Orchestrator produced no assignments."
    progress(f"Phase 1 done ({t_decompose:.1f}s): {len(assignments)} assignments generated.")

    # Heterogeneous models via --workers M1,M2 (assignment i uses pool[i % len]).
    pool = [s.strip() for s in workers_str.split(",")] if workers_str else []

    worker_names: list[str] = [env.assign_name(ta.assignee) for ta in assignments]

    if team_name:
        env.team_data = _create_fanout_team(team_name, worker_names)
        env.exchange = Exchange()
        env.messenger = LionMessenger(env.exchange)
        env.roster = {}
        # Mixed-provider teams (heterogeneous --workers pool) build one worker
        # branch at a time, so which teammates end up messenger-bound isn't
        # fully known until the whole loop below finishes. Resolve it here,
        # for every team member up front, so each worker's prompt can flag
        # CLI-provider teammates as unreachable via messenger regardless of
        # build order (worker_is_cli is a cheap, side-effect-free pre-pass —
        # no branch/iModel with real I/O is constructed).
        env.messenger_names = frozenset(
            wname
            for i, (wname, ta) in enumerate(zip(worker_names, assignments, strict=True))
            if not worker_is_cli(env, ta.assignee, pool[i % len(pool)] if pool else None)
        )
        progress(f"Team '{team_name}' created ({env.team_data['id']}): {', '.join(worker_names)}")

    if _shared is not None:
        _shared["session"] = env.session

    fanned_nodes: list[str] = []
    fanned_labels: list[str] = []

    for i, ta in enumerate(assignments):
        model_override = pool[i % len(pool)] if pool else None
        wname = worker_names[i]
        w_branch, w_model, _, messenger_bound = await build_worker_branch(
            env,
            agent_id=wname,
            role=ta.assignee,
            model_override=model_override,
            explicit_name=wname,
            modes=ta.modes or None,
        )
        ctx = [{"overall_task": prompt}]
        # Attached-team history (if any) rides in operation context, not the
        # system prompt — see team_history_context's docstring for why.
        history_ctx = team_history_context(env.team_data, wname, messenger_bound=messenger_bound)
        if history_ctx:
            ctx.append(history_ctx)
        node = _build_worker_operate_node(
            env.builder,
            branch=w_branch,
            instruction=ta.task,
            context=ctx,
            messenger_bound=messenger_bound,
        )
        fanned_nodes.append(node)
        fanned_labels.append(w_model)

    labels = ", ".join(fanned_labels)
    progress(f"Phase 2: Fanning out to {len(fanned_nodes)} workers: [{labels}]")

    t1 = time.monotonic()
    conc = max_concurrent if max_concurrent > 0 else len(fanned_nodes)
    graph = env.builder.get_graph()
    node_workers = {str(node_id): (i + 1, node_id) for i, node_id in enumerate(fanned_nodes)}
    saved_workers: dict[int, dict] = {}
    artifact_failures: dict[int, dict] = {}

    from lionagi.engines import PlanningEngine
    from lionagi.session.signal import NodeCompleted

    def _save_completed_worker(sig, _ctx) -> None:
        worker_entry = node_workers.get(sig.op_id)
        if worker_entry is None:
            return
        worker_number, node_id = worker_entry
        node = graph.internal_nodes.get(node_id)
        response = getattr(node, "response", None) if node is not None else None
        response_text = str(response) if response is not None else "(no response)"
        worker_result = {
            "worker": worker_number,
            "model": fanned_labels[worker_number - 1],
            "response": response_text,
            "time_ms": sig.elapsed * 1000,
        }
        artifact_path = env.run.artifact_root / f"worker_{worker_number}.md"
        try:
            artifact_path.write_text(response_text)
        except OSError as exc:
            artifact_failures[worker_number] = {
                "worker": worker_number,
                "path": str(artifact_path),
                "error": str(exc),
            }
            warn(f"Failed to save worker {worker_number} artifact to {artifact_path}: {exc}")
            if _shared is not None:
                _shared["artifact_failures"] = [
                    artifact_failures[i] for i in sorted(artifact_failures)
                ]
            return
        saved_workers[worker_number] = worker_result
        if _shared is not None:
            _shared["saved_workers"] = [saved_workers[i] for i in sorted(saved_workers)]

    env.session.observe(NodeCompleted, handler=_save_completed_worker)
    eng_run = PlanningEngine().new_run(session=env.session)
    try:
        if env.exchange is not None:
            async with create_task_group() as tg:
                tg.start_soon(env.exchange.run, 0.5)
                try:
                    result2 = await eng_run.run_dag(
                        graph,
                        max_concurrent=conc,
                        verbose=env.verbose,
                    )
                finally:
                    env.exchange.stop()
            # Route any final outbox sends left over after the last collect tick.
            await env.exchange.collect_all()
        else:
            result2 = await eng_run.run_dag(
                graph,
                max_concurrent=conc,
                verbose=env.verbose,
            )
    finally:
        env.session.observer.unobserve(_save_completed_worker)
    t_fanout = time.monotonic() - t1

    op_results = result2.get("operation_results", {})
    worker_results: list[dict] = []
    contexts: list[str] = []
    for i, nid in enumerate(fanned_nodes):
        res = op_results.get(nid)
        response_text = str(res) if res is not None else "(no response)"
        worker_results.append(
            {
                "worker": i + 1,
                "model": fanned_labels[i],
                "response": response_text,
                "time_ms": t_fanout * 1000,
            }
        )
        contexts.append(response_text)

    progress(f"Phase 2 done ({t_fanout:.1f}s).")

    progress(f"Saved {len(saved_workers)} worker results to {env.run.artifact_root}")
    if _shared is not None:
        _shared["saved_workers"] = [saved_workers[i] for i in sorted(saved_workers)]

    synthesis_result = None
    if with_synthesis and contexts:
        synth_spec = synthesis_model or model_spec
        synth_label = str(parse_model_spec(synth_spec))

        progress(f"Phase 3: Synthesis [{synth_label}]...")

        synth_instruction = (
            synthesis_prompt or f"{SYNTHESIS_INSTRUCTION}\n\nOriginal task: {prompt}"
        )

        synth_node = env.builder.add_operation(
            "operate",
            branch=env.orc_branch,
            depends_on=fanned_nodes,
            instruction=synth_instruction,
            context=contexts,
        )

        t2 = time.monotonic()
        result3 = await env.session.flow(env.builder.get_graph(), verbose=env.verbose)
        t_synth = time.monotonic() - t2

        synth_res = result3.get("operation_results", {}).get(synth_node)
        synthesis_result = {
            "model": synth_label,
            "response": str(synth_res) if synth_res is not None else "(no response)",
            "time_ms": t_synth * 1000,
        }

        progress(f"Phase 3 done ({t_synth:.1f}s).")

    if output_format == "json":
        output = _format_result_json(worker_results, synthesis_result)
    else:
        output = _format_result_text(worker_results, synthesis_result)

    if synthesis_result:
        env.run.synthesis_path.write_text(synthesis_result["response"])
    progress(f"Saved to {env.run.artifact_root}")

    if env.team_data:
        _post_results_to_team(env.team_data, worker_results, worker_names, synthesis_result)
        progress(
            f"\nTeam '{env.team_data['name']}' ({env.team_data['id']}): "
            f"{len(worker_results)} results posted."
        )
        progress(f"  li team receive -t {env.team_data['id']} --as orchestrator")
        progress(f"  li team show {env.team_data['id']}")

    finalize_orchestration(
        env,
        kind="fanout",
        prompt=prompt,
        extras={
            "workers": fanned_labels,
            "synthesis_model": (synthesis_result["model"] if synthesis_result else None),
        },
    )

    t_total = time.monotonic() - t0
    progress(f"\nTotal: {t_total:.1f}s")

    return output
