# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run a compiled WorkflowDef through lionagi's Session.flow, persisted like any other run.

Deliberately does NOT reuse `lionagi.cli.orchestrate._orchestration.setup_orchestration_persist`
/ `teardown_persist` verbatim: those helpers open the connection via
`register_shared_db()`/`close_shared_db()`, a PROCESS-WIDE singleton meant for one-shot CLI
processes that open it once and exit. The Studio server is long-lived and can have several
workflow runs (or other StateDB users) in flight at once; teardown_persist's finally block
closing the *entire* shared registry would tear down every concurrent run's connection, not
just this one. This module opens and closes its own request-scoped StateDB connection instead,
while still reusing the session-row/branch-hook/teardown-reason logic those helpers are built on.
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

    from lionagi.cli._runs import _teardown_common
    from lionagi.hooks import unroute_message_persistence

    db = ctx["db"]
    try:
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
    _session: Any | None = None,
) -> dict[str, Any]:
    """Load, compile, and execute a WorkflowDef; return ``{run_id, status}``.

    ``run_id`` is the lionagi Session id — the same primary key GET
    /api/sessions/{id} and the Fleet/History list already read, so the run
    shows up exactly like any other flow run. Raises WorkflowNotFoundError
    (404) or WorkflowCompileError (422, carries node_id/edge_id) on failure
    to compile; never a bare 500 for those two cases.

    ``_session`` is a private testability seam (inject a Session with a
    mocked default branch to avoid real provider calls in tests); real
    callers (the run route) never pass it and get a fresh Session().
    """
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

    graph, _id_map = await compile_workflow_def(spec, resolve_engine_def=_resolve_engine_def)

    from .workflow_compile import build_early_graph

    early_graph = build_early_graph(spec)

    session = _session if _session is not None else Session()
    session.register_operation("engine", make_engine_operation(session))

    ctx = await _setup_run_persist(
        session,
        invocation_kind="flow",
        extra_node_metadata={
            "early_graph": early_graph,
            "workflow_def_id": def_id,
            "workflow_def_name": defn.get("name"),
        },
    )

    status = "completed"
    exc: BaseException | None = None
    try:
        from lionagi.cli.orchestrate._orchestration import register_branch_hook

        result = await session.flow(
            graph,
            context=inputs or {},
            # Flow-created clone branches (any op with a predecessor and no
            # explicit branch_id — see FlowExecutor._preallocate_all_branches)
            # are born AFTER _setup_run_persist already registered persistence
            # for the branches that existed at setup time. Without this, a
            # clone's transcript never persists even though the run-DAG
            # signals still render (those persist via the session-level
            # observer, not per-branch hooks).
            on_branch_created=lambda b: register_branch_hook(ctx, b),
        )
        op_results = result.get("operation_results", {}) if isinstance(result, dict) else {}
        if any(isinstance(v, dict) and "error" in v for v in op_results.values()):
            status = "failed"
    except asyncio.CancelledError:
        # A cancelled Studio request/task aborts session.flow with
        # CancelledError, which is a BaseException and so bypasses the
        # `except Exception` below. Record the run as cancelled (not the
        # optimistic "completed" default) before re-propagating the cancel.
        status = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        status = "failed"
        exc = e
    finally:
        await _teardown_run_persist(ctx, status=status, exception=exc)

    return {"run_id": str(session.id), "status": status}
