# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fan-out execution: decompose → parallel workers → optional synthesis."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time

from lionagi._errors import CLIInputRefusedError, EmptyOutgoingContentError, LionError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import (
    CancelScope,
    cancelled_exc_classes,
    create_task_group,
    move_on_after,
)
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import LionMessenger
from lionhub.orchestration import plan
from lionhub.orchestration.prompts import SYNTHESIS_INSTRUCTION

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
    _hand_mcp_servers,
    attribute_worker_build_failure,
    available_roles,
    build_worker_branch,
    finalize_orchestration,
    mode_roster,
    parse_orchestrator_provider,
    register_branch_hook,
    resolve_modes,
    role_roster,
    setup_orchestration,
    start_live_persist,
    stop_live_persist,
    team_history_context,
    worker_is_cli,
)


class FanoutPlanError(LionError):
    """Orchestrator failed to produce a usable plan."""


# Rendered in place of a failed leg's output. Distinct from "(no response)",
# which means the operation completed but returned nothing: a failed leg must
# never read as a quiet success, and the synthesis context must be able to
# exclude it rather than synthesize over a placeholder as if it were content.
FAILED_WORKER_MARKER = "(worker failed: no output)"
FAILED_SYNTHESIS_MARKER = "(synthesis failed: no output)"


def _is_assignment_shaped_synthesis(value: object) -> bool:
    """True when the entire response is a planner-style assignment payload."""
    text = str(value).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced is not None:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return False

    if isinstance(payload, dict) and isinstance(payload.get("assignments"), list):
        assignments = payload["assignments"]
    elif isinstance(payload, list):
        assignments = payload
    elif isinstance(payload, dict):
        assignments = [payload]
    else:
        return False
    return bool(assignments) and all(
        isinstance(item, dict) and "task" in item and "assignee" in item for item in assignments
    )


async def _fresh_synthesis_branch(env: OrchestrationEnv, name: str = "synthesis"):
    """Build a clean synthesizer branch without the planner conversation."""
    from lionagi.agent import AgentSpec, create_agent

    spec = AgentSpec.compose(
        "synthesizer",
        pack=env.pack if env.pack is not None else "default",
        grant_emissions=False,
    )
    # The worker set, not the factory's own discovery: see setup_orchestration.
    # The model is a copy of the orchestrator's, whose request carries the run's
    # whole set, job control included, so the worker set is handed over the way
    # build_worker_branch hands it to a worker: as the whole set.
    worker_servers = getattr(env, "mcp_servers", None)
    chat_model = env.orc_branch.chat_model.copy()
    chat_model.endpoint.config.kwargs.pop("account_connectors", None)
    _hand_mcp_servers(
        chat_model,
        worker_servers,
        label="synthesis",
        exclusive=worker_servers is not None,
    )
    if not env.effort and chat_model.endpoint.config.kwargs.get("ignore_user_config"):
        # An empty set left this copy loading no user config, so the effort the
        # orchestrator's model derived from that config goes too, as it does for an
        # orchestrator handed an empty set (setup_orchestration).
        from lionagi.agent.factory import _canonical_provider
        from lionagi.service.providers import PROVIDER_EFFORT_KWARG

        effort_kwarg = PROVIDER_EFFORT_KWARG.get(
            _canonical_provider(chat_model.endpoint.config.provider)
        )
        if effort_kwarg:
            chat_model.endpoint.config.kwargs.pop(effort_kwarg, None)
    branch = await create_agent(
        spec,
        load_settings=False,
        chat_model=chat_model,
        resolved_mcp_servers=worker_servers,
    )
    branch.name = name
    env.session.include_branches(branch)
    if env._live_persist:
        register_branch_hook(env._live_persist, branch)
    return branch


def _parse_worker_pool(workers_str: str | None, *, num_workers: int) -> list[str]:
    """Parse model overrides and report entries excluded by the assignment cap."""
    pool = [spec.strip() for spec in workers_str.split(",")] if workers_str else []
    unused = len(pool) - num_workers
    if unused > 0:
        noun = "spec" if unused == 1 else "specs"
        warn(
            f"{len(pool)} worker model specs provided, but --num-workers caps fanout at "
            f"{num_workers} assignments; {unused} model {noun} will not be used."
        )
    return pool


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
    mcp_config: str | None = None,
    no_mcp_config: bool = False,
) -> tuple[str, str]:
    """Three-phase fan-out: decompose → fan out → synthesize.

    Returns ``(result, terminal_status)`` — mirrors `_run_flow`'s contract so
    the completion-trust gate's `completed_empty` (and any other status the
    teardown path settles on) reaches the caller's exit code instead of being
    silently dropped in favour of a hardcoded success.
    """
    from lionagi._cli_worker_context import _bind_cli_worker_recorder
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    with contextlib.ExitStack() as _worker_spawn_bindings:
        _worker_spawn_bindings.enter_context(_bind_cli_worker_recorder(None))
        if invocation_id is not None:
            # The same check `li o flow` makes, and for the same reason: an
            # unregistered id here would silently disable persistence while the
            # caller believes the run is grouped under a real invocation. Refused
            # before setup so nothing has been spawned when it fails.
            from .flow import FlowPlanError, _validate_flow_invocation

            try:
                await _validate_flow_invocation(invocation_id)
            except FlowPlanError as exc:
                raise FanoutPlanError(str(exc)) from exc

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
            mcp_config=mcp_config,
            no_mcp_config=no_mcp_config,
        )
        _shared: dict = {}

        # Persist the orchestrator default model + effort on the session row.
        # Per-worker model is written branch-side when build_worker_branch runs.
        _orc_model, _orc_provider = parse_orchestrator_provider(env.default_model_spec)
        from lionagi.state.lifecycle.notify_settings import (
            register_run_notify_outcome_scope,
            unregister_run_notify_outcome_scope,
        )

        _terminal_status = "completed"
        result: str = ""
        _pending_worker_cancel = None
        _notify_scope_name = None
        _notify_outcome_scope_name = None
        try:
            await start_live_persist(
                env,
                invocation_kind="fanout",
                playbook_name=playbook_name,
                # The profile the run resolved, not the one this call named: a call that
                # named neither an agent nor a model named none, and recording that
                # `agent_name` would leave the record unable to say what it ran under.
                agent_name=env.orc_profile_name,
                artifacts_path=str(env.run.artifact_root),
                invocation_id=invocation_id,
                model=_orc_model,
                provider=_orc_provider,
                effort=getattr(env, "orc_effort", None),
                project=project,
            )

            _worker_owner = (env._live_persist or {}).get("worker_spawn_owner")
            if (
                env._live_persist is not None
                and _worker_owner is None
                and env._live_persist.get("worker_spawns_unrecorded") is not True
            ):
                raise _WorkerSpawnAdmissionError("persisted orchestration has no worker owner")
            _worker_spawn_bindings.enter_context(
                _worker_owner.bind()
                if _worker_owner is not None
                else _bind_cli_worker_recorder(None)
            )
            # Session-scoped: stop_live_persist terminalizes only the session; invocation
            # records are finalized externally and would never fire.
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
                    entity_kind="session",
                    entity_id=str(env.session.id),
                    invocation_id=invocation_id,
                    flow_kind="fanout",
                    playbook=playbook_name,
                    save_dir=save_dir,
                    cwd=cwd or os.getcwd(),
                    started_at=_started_at,
                    on_rejection=_notify_override_refused,
                )

            # Bind this run into the notify.on_terminal handler at registration time so
            # a late outcome lands here or nowhere, never on a later run. Skipped when
            # --notify already owns this entity (a second override would double-fire).

            _notify_outcome_scope_name = (
                None
                if notify
                else register_run_notify_outcome_scope(
                    env.run,
                    entity_kind="session",
                    entity_id=str(env.session.id),
                    project_dir=cwd,
                )
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
            if timeout:
                with move_on_after(timeout) as cancel_scope:
                    result = await _run_fanout_inner(model_spec, prompt, **inner_kw)
                if cancel_scope.cancelled_caught:
                    _terminal_status = "timed_out"
                    n_saved = len(_shared.get("saved_workers", []))
                    msg = f"Fanout timed out after {timeout}s"
                    if n_saved:
                        msg += (
                            f" ({n_saved} worker results already saved to {env.run.artifact_root})"
                        )
                    log_error(msg)
                    raise LionTimeoutError(msg)
            else:
                result = await _run_fanout_inner(model_spec, prompt, **inner_kw)
            # Two distinct failure signals feed this decision: an operation that
            # raised (op_failures) and a worker artifact that could not be written
            # (artifact_failures). Either alone means the run did not deliver what
            # it reported, so neither may resolve to "completed".
            if _shared.get("artifact_failures") or _shared.get("op_failures"):
                _terminal_status = "failed"
        except BaseException as exc:
            _terminal_status = classify_exception(exc)
            raise
        finally:
            with CancelScope(shield=True):
                _worker_stop_ctx = env._live_persist
                try:
                    effective_status = await stop_live_persist(env, status=_terminal_status)
                except cancelled_exc_classes() as exc:
                    _pending_worker_cancel = exc
                    effective_status = (_worker_stop_ctx or {}).get("worker_spawn_teardown_status")
                    if effective_status is None:
                        effective_status = "unknown"
                if effective_status != _terminal_status:
                    _terminal_status = effective_status
                # Unregister after stop_live_persist fires the terminal transition.
                unregister_flow_notify_scope(_notify_scope_name)
                unregister_run_notify_outcome_scope(_notify_outcome_scope_name)
                for _br in env.session.branches:
                    await _br.mdls.shutdown()
                if _pending_worker_cancel is not None:
                    raise _pending_worker_cancel

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
    pool = _parse_worker_pool(workers_str, num_workers=num_workers)

    roster = available_roles()
    progress(f"Phase 1: Orchestrator decomposing task into ≤{num_workers} assignments...")
    try:
        assignments = await plan(
            env.orc_branch,
            prompt,
            roles=roster,
            dag=False,
            guidance=f"{role_roster(env.default_model_spec)}\n\n{mode_roster(env.pack)}",
            max_tasks=num_workers,
        )
    except EmptyOutgoingContentError:
        raise
    except (ValueError, CLIInputRefusedError) as exc:
        # plan() raises a bare ValueError when the orchestrator still
        # overshoots max_tasks after the cap was stated in guidance — mirror
        # `li o flow`'s FlowPlanError translation so this reaches the CLI's
        # clean-failure exit path instead of escaping as a raw traceback. A
        # CLIInputRefusedError is what a request the CLI cannot take raises at
        # construction, and it reaches here the same way.
        raise FanoutPlanError(str(exc)) from exc
    t_decompose = time.monotonic() - t0
    if not assignments:
        return "Orchestrator produced no assignments."

    # Validate the complete plan before creating any worker. The permissive
    # resolver remains available to flow and legacy callers, but fanout must
    # not claim success after silently stripping planner intent.
    planned_modes: list[list[str] | None] = []
    for index, assignment in enumerate(assignments, start=1):
        if not assignment.modes:
            planned_modes.append(None)
            continue
        try:
            planned_modes.append(
                resolve_modes(
                    assignment.assignee,
                    assignment.modes,
                    env.pack,
                    reject_invalid=True,
                )
            )
        except ValueError as exc:
            raise FanoutPlanError(f"assignment {index} has invalid modes: {exc}") from exc

    progress(f"Phase 1 done ({t_decompose:.1f}s): {len(assignments)} assignments generated.")

    worker_names: list[str] = [env.assign_name(ta.assignee) for ta in assignments]

    if team_name:
        env.team_data = _create_fanout_team(team_name, worker_names)
        env.exchange = Exchange()
        env.messenger = LionMessenger(env.exchange)
        env.roster = {}
        # Resolved up front (before the build loop below) so each worker's
        # prompt can flag CLI-provider teammates as messenger-unreachable
        # regardless of build order. worker_is_cli is a side-effect-free
        # pre-pass — no branch/iModel with real I/O is constructed.
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

    # The worker names are settled before any branch is built; recording them
    # here is what lets finalization notice one that never got a directory.
    for wname in worker_names:
        env.expect_worker(wname)

    for i, ta in enumerate(assignments):
        model_override = pool[i % len(pool)] if pool else None
        wname = worker_names[i]
        try:
            w_branch, w_model, _, messenger_bound = await build_worker_branch(
                env,
                agent_id=wname,
                role=ta.assignee,
                model_override=model_override,
                explicit_name=wname,
                modes=planned_modes[i],
            )
        except BaseException as exc:
            attribute_worker_build_failure(exc, agent_id=wname, role=ta.assignee)
            raise
        ctx = [{"overall_task": prompt}]
        # Attached-team history (if any) rides in operation context, not the
        # system prompt — see team_history_context's docstring for why.
        history_ctx = team_history_context(env.team_data, wname, messenger_bound=messenger_bound)
        if history_ctx:
            ctx.append(history_ctx)
        node = _build_worker_operate_node(
            env.builder,
            branch=w_branch,
            # Explicitly no dependencies: fanout workers are independent, and
            # the builder chains onto its current heads when this is None.
            depends_on=[],
            instruction=ta.task,
            context=ctx,
            messenger_bound=messenger_bound,
            node_id=wname,
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

    from lionagi.session.signal import NodeCompleted, NodeFailed
    from lionhub.orchestration.engines import PlanningEngine

    # Keyed by op_id so the same observer serves the fanned workers here and
    # the synthesis node later. A failed op has no entry in operation_results,
    # which is also what a completed-but-empty op looks like — this set is the
    # only record that distinguishes the two.
    failed_ops: set[str] = set()

    def _record_failed_op(sig, _ctx) -> None:
        failed_ops.add(sig.op_id)
        worker_entry = node_workers.get(sig.op_id)
        if worker_entry is not None:
            worker_number, _ = worker_entry
            warn(f"Worker {worker_number} failed after {sig.elapsed:.1f}s; its leg has no output.")
        if _shared is not None:
            _shared["op_failures"] = sorted(failed_ops)

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
    env.session.observe(NodeFailed, handler=_record_failed_op)
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
        env.session.observer.unobserve(_record_failed_op)
    t_fanout = time.monotonic() - t1

    op_results = result2.get("operation_results", {})
    worker_results: list[dict] = []
    contexts: list[str] = []
    for i, nid in enumerate(fanned_nodes):
        if str(nid) in failed_ops:
            # A failed leg is rendered with its marker but kept out of the
            # synthesis context: a placeholder read alongside real worker
            # output would be synthesized as if it were content.
            response_text = FAILED_WORKER_MARKER
        else:
            res = op_results.get(nid)
            response_text = str(res) if res is not None else "(no response)"
            contexts.append(response_text)
        worker_results.append(
            {
                "worker": i + 1,
                "model": fanned_labels[i],
                "response": response_text,
                "time_ms": t_fanout * 1000,
            }
        )

    progress(f"Phase 2 done ({t_fanout:.1f}s).")

    progress(f"Saved {len(saved_workers)} worker results to {env.run.artifact_root}")
    if _shared is not None:
        _shared["saved_workers"] = [saved_workers[i] for i in sorted(saved_workers)]

    synthesis_result = None
    synth_node = None
    synth_name = None
    if with_synthesis and not contexts:
        # An empty context means either "every worker failed" or "no worker ran
        # at all". Only the first one has failures to report, so name the
        # situation that actually happened.
        if fanned_nodes:
            warn("Every worker failed; there is no output to synthesize.")
        else:
            warn("No workers ran; there is no output to synthesize.")
    if with_synthesis and contexts:
        synth_spec = synthesis_model or env.default_model_spec
        synth_label = str(parse_model_spec(synth_spec))

        progress(f"Phase 3: Synthesis [{synth_label}]...")

        synth_instruction = (
            synthesis_prompt or f"{SYNTHESIS_INSTRUCTION}\n\nOriginal task: {prompt}"
        )

        # One name for the node, its branch and the persisted record: a queued
        # signal carries the node's reference id, a started or terminal signal
        # the branch name, and Studio joins node status by that name.
        synth_name = env.assign_name("synthesis")
        synth_branch = await _fresh_synthesis_branch(env, name=synth_name)
        synth_node = env.builder.add_operation(
            "operate",
            node_id=synth_name,
            branch=synth_branch,
            depends_on=fanned_nodes,
            instruction=synth_instruction,
            context=contexts,
        )

        t2 = time.monotonic()
        env.session.observe(NodeFailed, handler=_record_failed_op)
        try:
            # Synthesis runs through the engine for the same reason the worker
            # phase does: run_dag is what installs the node-lifecycle signal
            # bridge, so a failed synthesis reaches the observer above. Calling
            # session.flow directly emits no node signals at all, which leaves
            # the observer silent and renders the failure as an empty response.
            # The graph still carries the workers, since synthesis depends on
            # them, but they ran in the worker phase above and already have
            # their terminal events; signalling them here would record the
            # same work a second time.
            synth_graph = env.builder.get_graph()
            already_ran = {str(n.id) for n in synth_graph.internal_nodes.values()} - {
                str(synth_node)
            }
            result3 = await eng_run.run_dag(
                synth_graph,
                verbose=env.verbose,
                skip_signal_ops=already_ran,
            )
        finally:
            env.session.observer.unobserve(_record_failed_op)
        t_synth = time.monotonic() - t2

        synth_res = result3.get("operation_results", {}).get(synth_node)
        if str(synth_node) in failed_ops:
            synth_text = FAILED_SYNTHESIS_MARKER
        else:
            synth_text = str(synth_res) if synth_res is not None else "(no response)"
            if _is_assignment_shaped_synthesis(synth_text):
                failed_ops.add(str(synth_node))
                if _shared is not None:
                    _shared["op_failures"] = sorted(failed_ops)
                warn(
                    "Synthesis returned a planner assignment instead of an integrated "
                    "result; marking the synthesis leg failed."
                )
                synth_text = FAILED_SYNTHESIS_MARKER
        synthesis_result = {
            "model": synth_label,
            "response": synth_text,
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

    # Transitional v1 Session projection. Delete this writer only after
    # Run detail derives from ADR-0123's ExecutionPlan (the per-Run resolved
    # plan, not the planner output in casts/emission.py), PlanDelta and
    # OperationAttempt records, with existing client payload compatibility
    # verified. It is not the canonical graph or lifecycle authority.
    # Preserve the graph that actually ran, including failed workers and a
    # synthesis leg only when it was built. Every node is recorded under the
    # name its lifecycle signals carry, the synthesis leg included.
    node_agents = dict(zip(fanned_nodes, worker_names, strict=True))
    agents_meta = [
        {"id": name, "name": name, "model": model}
        for name, model in zip(worker_names, fanned_labels, strict=True)
    ]
    if synth_node is not None:
        node_agents[synth_node] = synth_name
        agents_meta.append(
            {"id": synth_name, "name": synth_name, "model": synthesis_result["model"]}
        )
    finalize_orchestration(
        env,
        kind="fanout",
        prompt=prompt,
        extras={
            "workers": fanned_labels,
            "synthesis_model": (synthesis_result["model"] if synthesis_result else None),
            "agents": agents_meta,
            "operations": [
                {
                    "id": agent_id,
                    "agent_id": agent_id,
                    "control": False,
                    "spawned": False,
                    "depends_on": list(worker_names) if node_id == synth_node else [],
                }
                for node_id, agent_id in node_agents.items()
            ],
        },
    )

    t_total = time.monotonic() - t0
    progress(f"\nTotal: {t_total:.1f}s")

    return output
