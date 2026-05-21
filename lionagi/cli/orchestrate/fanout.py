# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fan-out execution: decompose → parallel workers → optional synthesis."""

from __future__ import annotations

import time

from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import move_on_after
from lionagi.operations.fields import Instruct

from .._agents import AgentProfile
from .._logging import log_error, progress
from .._providers import parse_model_spec
from ._common import (
    AGENT_REQUEST_FIELDS,
    AgentRequest,
    _create_fanout_team,
    _format_result_json,
    _format_result_text,
    _post_results_to_team,
)
from ._orchestration import (
    OrchestrationEnv,
    build_worker_branch,
    finalize_orchestration,
    resolve_worker_spec,
    setup_orchestration,
    start_live_persist,
    stop_live_persist,
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
) -> str:
    """Three-phase fan-out: decompose → fan out → synthesize."""
    env = setup_orchestration(
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
    )
    _shared: dict = {}

    # ADR-0022: orchestrator default model + effort on the session row.
    # Per-worker model is written branch-side when build_worker_branch runs.
    from .._providers import parse_model_spec as _parse_model_spec
    _orc_ms = (
        _parse_model_spec(env.default_model_spec) if env.default_model_spec else None
    )
    _orc_provider = None
    if _orc_ms and "/" in _orc_ms.model:
        _orc_provider = _orc_ms.model.split("/", 1)[0]
    await start_live_persist(
        env,
        invocation_kind="fanout",
        playbook_name=playbook_name,
        agent_name=agent_name,
        artifacts_path=str(env.run.artifact_root),
        invocation_id=invocation_id,
        model=_orc_ms.model if _orc_ms else None,
        provider=_orc_provider,
        effort=env.effort,
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

    # ADR-0025: distinguish timed_out / aborted / cancelled / failed.
    _terminal_status = "completed"
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
            return result
        return await _run_fanout_inner(model_spec, prompt, **inner_kw)
    except KeyboardInterrupt:
        _terminal_status = "aborted"
        raise
    except LionTimeoutError:
        # Set in the move_on_after branch above; preserve through any
        # re-raise so finally writes the correct terminal status.
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
        # Shield teardown from outer cancellation so iModel executors are
        # always closed; see lionagi/cli/agent.py for the full rationale.
        import anyio

        with anyio.CancelScope(shield=True):
            await stop_live_persist(env, status=_terminal_status)
            for _br in env.session.branches:
                await _br.mdls.shutdown()


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
    """Inner fanout logic (no timeout wrapper)."""
    t0 = time.monotonic()

    # Resolve worker profiles up-front — fanout pre-computes worker names
    # from profile names so teams can be created before Phase 1.
    worker_profiles: list[AgentProfile | None] = []
    if workers_str:
        worker_model_list: list[str] = []
        for token in (s.strip() for s in workers_str.split(",")):
            wmodel, wprofile = resolve_worker_spec(token)
            worker_model_list.append(wmodel)
            worker_profiles.append(wprofile)
    else:
        ms = parse_model_spec(model_spec)
        worker_model_list = [ms.model] * num_workers
        worker_profiles = [None] * num_workers

    # Pre-compute names so teams can be materialized before Phase 1.
    worker_names: list[str] = []
    for i, wp in enumerate(worker_profiles):
        if wp and wp.name:
            base = wp.name
            count = sum(
                1 for n in worker_names if n == base or n.startswith(f"{base}-")
            )
            worker_names.append(f"{base}-{count + 1}" if count > 0 else base)
        else:
            worker_names.append(f"worker-{i + 1}")

    if team_name:
        env.team_data = _create_fanout_team(team_name, worker_names)
        progress(
            f"Team '{team_name}' created ({env.team_data['id']}): {', '.join(worker_names)}"
        )

    if _shared is not None:
        _shared["session"] = env.session

    # ── Phase 1: Orchestrator decomposes task ─────────────────────────
    worker_descriptions = []
    for i, wm in enumerate(worker_model_list):
        wp = worker_profiles[i] if i < len(worker_profiles) else None
        if wp and wp.name:
            worker_descriptions.append(
                f"{worker_names[i]} (role: {wp.name}, model: {wm})"
            )
        else:
            worker_descriptions.append(f"{worker_names[i]} (model: {wm})")
    roster_guidance = "; ".join(worker_descriptions)

    root = env.builder.add_operation(
        "operate",
        branch=env.orc_branch,
        instruct=Instruct(
            instruction=(
                f"Generate {len(worker_model_list)} agent requests. "
                f"{AgentRequest.DECOMPOSITION_INSTRUCTION}"
            ),
            context={"user_prompt": prompt},
            guidance=(
                f"Available workers: {roster_guidance}. {AgentRequest.DECOMPOSITION_DISCIPLINE}"
            ),
        ),
        field_models=[AGENT_REQUEST_FIELDS],
        reason=True,
    )

    progress(
        f"Phase 1: Orchestrator decomposing task into {len(worker_model_list)} agent requests..."
    )

    result1 = await env.session.flow(env.builder.get_graph())
    t_decompose = time.monotonic() - t0

    root_result = result1.get("operation_results", {}).get(root)
    agents = getattr(root_result, "agents", None) or []

    if not agents:
        return "Orchestrator produced no agent requests."

    progress(f"Phase 1 done ({t_decompose:.1f}s): {len(agents)} requests generated.")

    # ── Phase 2: Fan out ──────────────────────────────────────────────
    default_ms = parse_model_spec(model_spec)
    fanned_nodes: list[str] = []
    fanned_labels: list[str] = []

    for i, a in enumerate(agents):
        # Pick the model: orchestrator override > profile > default
        wprofile = worker_profiles[i] if i < len(worker_profiles) else None
        desired_model = (
            a.model or (wprofile.model if wprofile else None) or default_ms.model
        )
        wname = worker_names[i]

        w_branch, w_model, _ = build_worker_branch(
            env,
            agent_id=wname,
            role=wprofile.name if wprofile else f"worker-{i + 1}",
            model_override=desired_model,
            explicit_name=wname,
        )

        worker_context = [
            {"overall_task": prompt},
            a.instruct.context or "",
        ]
        node = env.builder.add_operation(
            "operate",
            branch=w_branch,
            depends_on=[root],
            instruction=a.instruct.instruction,
            guidance=a.instruct.guidance,
            context=worker_context,
        )
        fanned_nodes.append(node)
        fanned_labels.append(w_model)

    labels = ", ".join(fanned_labels)
    progress(f"Phase 2: Fanning out to {len(fanned_nodes)} workers: [{labels}]")

    t1 = time.monotonic()
    conc = max_concurrent if max_concurrent > 0 else len(fanned_nodes)
    result2 = await env.session.flow(
        env.builder.get_graph(),
        max_concurrent=conc,
        verbose=env.verbose,
    )
    t_fanout = time.monotonic() - t1

    # Collect results
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

    # ── Incremental save: persist worker responses as files ──────────
    for wr in worker_results:
        (env.run.artifact_root / f"worker_{wr['worker']}.md").write_text(wr["response"])
    progress(f"Saved {len(worker_results)} worker results to {env.run.artifact_root}")
    if _shared is not None:
        _shared["saved_workers"] = worker_results

    # ── Phase 3: Synthesis ────────────────────────────────────────────
    synthesis_result = None
    if with_synthesis and contexts:
        synth_spec = synthesis_model or model_spec
        synth_label = str(parse_model_spec(synth_spec))

        progress(f"Phase 3: Synthesis [{synth_label}]...")

        synth_instruction = synthesis_prompt or (
            f"Synthesize the following {len(contexts)} worker responses "
            f"into a cohesive analysis.\n\n"
            f"Original task: {prompt}"
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

    # ── Output ────────────────────────────────────────────────────────
    if output_format == "json":
        output = _format_result_json(worker_results, synthesis_result)
    else:
        output = _format_result_text(worker_results, synthesis_result)

    # ── Save synthesis to artifact_root ──────────────────────────────
    if synthesis_result:
        env.run.synthesis_path.write_text(synthesis_result["response"])
    progress(f"Saved to {env.run.artifact_root}")

    # ── Post to team ─────────────────────────────────────────────────
    if env.team_data:
        _post_results_to_team(
            env.team_data, worker_results, worker_names, synthesis_result
        )
        progress(
            f"\nTeam '{env.team_data['name']}' ({env.team_data['id']}): "
            f"{len(worker_results)} results posted."
        )
        progress(f"  li team receive -t {env.team_data['id']} --as orchestrator")
        progress(f"  li team show {env.team_data['id']}")

    # ── Persist branches + manifest + hints ──────────────────────────
    finalize_orchestration(
        env,
        kind="fanout",
        prompt=prompt,
        extras={
            "workers": fanned_labels,
            "synthesis_model": (
                synthesis_result["model"] if synthesis_result else None
            ),
        },
    )

    t_total = time.monotonic() - t0
    progress(f"\nTotal: {t_total:.1f}s")

    return output
