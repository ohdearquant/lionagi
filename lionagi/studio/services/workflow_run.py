# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run a compiled WorkflowDef through lionagi's Session.flow, persisted like any other run.

Does not reuse `_orchestration.setup_orchestration_persist`/`teardown_persist` verbatim: those
close a process-wide shared StateDB singleton, which would tear down every concurrent run's
connection on this long-lived server. This module opens its own request-scoped connection instead.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

__all__ = ("run_workflow_def", "WorkflowNotFoundError")


class WorkflowNotFoundError(Exception):
    """No WorkflowDef with the given id."""


async def _setup_run_persist(
    session: Any,
    *,
    invocation_kind: str,
    extra_node_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from lionagi.cli.orchestrate._orchestration import register_branch_hook
    from lionagi.state.db import StateDB

    db = StateDB()
    await db.open()

    session_id = str(session.id)
    session_dict = session.to_dict(mode="db")
    session_prog_id = str(uuid.uuid4())
    await db.create_progression(session_prog_id)

    node_metadata = {
        **(session_dict.get("node_metadata") or {}),
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
        await _teardown_run_persist(ctx, status=status, exception=exc)

    return {"run_id": str(session.id), "status": status}
