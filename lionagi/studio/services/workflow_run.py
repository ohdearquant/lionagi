# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run a compiled WorkflowDef through lionagi's Session.flow, persisted like any other run.

Does not reuse `_orchestration.setup_orchestration_persist`/`teardown_persist` verbatim: those
close a process-wide shared StateDB singleton, which would tear down every concurrent run's
connection on this long-lived server. This module opens its own request-scoped connection instead.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from typing import Any, Literal

__all__ = (
    "run_workflow_def",
    "WorkflowNotFoundError",
    "cancel_in_process_run",
    "CancelDelivery",
)

# What a cancellation request achieved, as three separate facts rather than a
# boolean that has to stand for all of them. See cancel_in_process_run.
CancelDelivery = Literal["not_hosted_here", "stopped", "still_stopping"]


class WorkflowNotFoundError(Exception):
    """No WorkflowDef with the given id."""


# Run id -> the asyncio task driving that run, for runs hosted in THIS process.
# An in-process run has no OS process of its own, so there is no pid to signal;
# cancelling the task is the only thing that actually stops the work. Scoped to
# one process on purpose: a run hosted elsewhere is absent here, and absence is
# the correct answer for this process rather than a reason to guess.
_IN_PROCESS_RUNS: dict[str, asyncio.Task[Any]] = {}


async def cancel_in_process_run(run_id: str, *, timeout: float = 5.0) -> CancelDelivery:
    """Cancel a workflow run hosted in this process, waiting for it to unwind.

    Waits for the task so the run's own ``CancelledError`` handler can persist
    its terminal status before the caller re-reads the row, up to *timeout*.

    The three outcomes are kept apart because they call for different claims:

    ``not_hosted_here``
        This process is not running it, so nothing was cancelled. A caller
        must treat this as "not cancelled" rather than as success: marking
        such a row cancelled would report a stop that never happened.
    ``stopped``
        Delivered, and the task finished unwinding inside *timeout*.
    ``still_stopping``
        Delivered, but the task had not finished unwinding when the wait
        expired. The work may well still be running. Collapsing this into
        ``stopped`` is what let a timed-out cancellation report as a
        completed one, since waiting does not raise when it gives up.
    """
    task = _IN_PROCESS_RUNS.get(run_id)
    if task is None or task.done():
        return "not_hosted_here"
    if task is asyncio.current_task():
        # Self-cancel would deadlock on the wait below, and a run cancelling
        # itself through the operator path is not a case that should exist.
        return "not_hosted_here"
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    return "stopped" if done else "still_stopping"


async def _setup_run_persist(
    session: Any,
    *,
    invocation_kind: str,
    extra_node_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import psutil

    from lionagi.cli.orchestrate._orchestration import register_branch_hook
    from lionagi.state.db import StateDB

    db = StateDB()
    await db.open()

    session_id = str(session.id)
    session_dict = session.to_dict(mode="db")
    session_prog_id = str(uuid.uuid4())
    await db.create_progression(session_prog_id)

    # This workflow has no OS process of its own: it runs inside the long-lived
    # server. Recording the server's pid as the run's own would make cancelling
    # one workflow signal the server, and with it every other run it hosts, so
    # the host is recorded under keys the kill path does not read. Liveness
    # still bounds the run by the host's, which is the strongest true statement
    # available about an in-process run.
    host_pid = os.getpid()
    node_metadata = {
        **(session_dict.get("node_metadata") or {}),
        "process_identity_mode": "in_process",
        "host_pid": host_pid,
        "host_pid_create_time": psutil.Process(host_pid).create_time(),
        "pid_host": socket.gethostname(),
        "pid_boot_time": psutil.boot_time(),
        **(extra_node_metadata or {}),
    }
    await db.create_session(
        {
            "id": session_id,
            "created_at": session_dict["created_at"],
            "node_metadata": node_metadata,
            "name": session_dict.get("name"),
            "user": session_dict.get("user"),
            "progression_id": session_prog_id,
            "first_msg_id": None,
            "last_msg_id": None,
            "invocation_kind": invocation_kind,
            "status": "running",
            "started_at": time.time(),
        }
    )

    ctx: dict[str, Any] = {
        "db": db,
        "session": session,
        "session_id": session_id,
        "session_prog_id": session_prog_id,
        "branch_prog_ids": {},
        "hooks": [],
        "message_retry_queues": [],
    }
    session.observer.bind_db_persistence(session_id, db=db)
    for branch in session.branches:
        register_branch_hook(ctx, branch)
    return ctx


async def _teardown_run_persist(
    ctx: dict[str, Any] | None,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
) -> str:
    if ctx is None:
        return status

    from lionagi.cli._runs import _flush_pending_message_events, _teardown_common
    from lionagi.hooks import unroute_message_persistence

    db = ctx["db"]
    try:
        await _flush_pending_message_events(ctx)
        final_status = await _teardown_common(
            db,
            session_id=ctx["session_id"],
            session_prog_id=ctx["session_prog_id"],
            status=status,
            exception=exception,
            artifacts_path=None,
            artifact_contract=None,
        )
        for branch, handler in ctx.get("hooks", []):
            unroute_message_persistence(branch, handler)
        session_obj = ctx.get("session")
        if session_obj is not None:
            try:
                session_obj.observer.unbind_db_persistence()
            except Exception:  # noqa: BLE001, S110
                pass
        return final_status
    finally:
        await db.close()


async def run_workflow_def(
    def_id: str,
    inputs: dict[str, Any] | None = None,
    *,
    base_dir: str | None = None,
    _session: Any | None = None,
) -> dict[str, Any]:
    """Load, compile, and execute a WorkflowDef; return ``{run_id, status}``.
    Raises WorkflowNotFoundError (404) or WorkflowCompileError (422) on
    compile failure -- never a bare 500 for those two cases."""
    from lionagi.session.session import Session

    from . import engine_defs
    from .workflow_compile import WorkflowCompileError, compile_workflow_def, make_engine_operation
    from .workflow_defs import get_workflow_def

    defn = await get_workflow_def(def_id)
    if defn is None:
        raise WorkflowNotFoundError(f"Workflow definition {def_id!r} not found")

    spec = defn.get("spec_json")
    if not spec:
        raise WorkflowCompileError("workflow definition has no spec_json to run")

    async def _resolve_engine_def(ref: str) -> dict[str, Any] | None:
        found = await engine_defs.get_engine_def(ref)
        if found is None:
            found = await engine_defs.get_engine_def_by_name(ref)
        return found

    graph, _id_map = await compile_workflow_def(
        spec, resolve_engine_def=_resolve_engine_def, base_dir=base_dir
    )

    from .workflow_compile import build_early_graph

    early_graph = build_early_graph(spec)

    session = _session if _session is not None else Session()

    ctx = await _setup_run_persist(
        session,
        invocation_kind="flow",
        extra_node_metadata={
            "early_graph": early_graph,
            "workflow_def_id": def_id,
            "workflow_def_name": defn.get("name"),
        },
    )

    from lionagi.cli.orchestrate._orchestration import register_branch_hook

    # ctx must exist before the "engine" operation is registered: engine
    # sub-agent branches are born mid-run and need the same on_branch_created
    # seam as session.flow(), not the setup-time-only loop in _setup_run_persist.
    session.register_operation(
        "engine",
        make_engine_operation(session, on_branch_created=lambda b: register_branch_hook(ctx, b)),
    )

    status = "completed"
    exc: BaseException | None = None
    run_id = str(session.id)
    # Publish the driving task so the cancel path has something to act on.
    # Without this the run is unreachable: it has no pid of its own, so a
    # cancel would have nothing to signal and could only mark the row.
    run_task = asyncio.current_task()
    if run_task is not None:
        _IN_PROCESS_RUNS[run_id] = run_task
    try:
        from lionagi.engines.flow_signals import flow_progress_signals

        # Emit per-node lifecycle signals; run_workflow_def drives session.flow
        # directly (bypassing the engine, the usual signal source), so without
        # this RunDetail would show no node-progress rows.
        async with flow_progress_signals(session, graph) as on_progress:
            result = await session.flow(
                graph,
                context=inputs or {},
                on_progress=on_progress,
                # Flow-created clone branches are born after _setup_run_persist
                # already registered persistence for setup-time branches;
                # without this a clone's transcript never persists.
                on_branch_created=lambda b: register_branch_hook(ctx, b),
            )
        op_results = result.get("operation_results", {}) if isinstance(result, dict) else {}
        if any(isinstance(v, dict) and "error" in v for v in op_results.values()):
            status = "failed"
    except asyncio.CancelledError:
        # CancelledError is a BaseException and bypasses `except Exception`
        # below; record the run as cancelled before re-propagating.
        status = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        status = "failed"
        exc = e
    finally:
        # Unregister before teardown, not after: an await that raises in
        # teardown would otherwise leave a finished run listed as cancellable.
        _IN_PROCESS_RUNS.pop(run_id, None)
        await _teardown_run_persist(ctx, status=status, exception=exc)

    return {"run_id": run_id, "status": status}
