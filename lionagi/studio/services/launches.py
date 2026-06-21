# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Launch service — backs POST /api/launches."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from lionagi.state.db import StateDB

from .. import config
from ..registry import studio_route
from ..scheduler.subprocess import build_argv
from ..services.schedules import (
    _svc_validate_action_model,
    _svc_validate_extra_args,
    _svc_validate_identifier,
    _svc_validate_prompt,
)

_log = logging.getLogger(__name__)

_LAUNCH_VALID_KINDS = frozenset({"agent", "flow", "fanout", "play", "engine"})

_detached_tasks: set[asyncio.Task] = set()
_user_cancelled: set[str] = set()

_launch_semaphore: asyncio.Semaphore | None = None


class TooManyLaunchesError(Exception):
    """Raised when the in-flight launch count reaches the configured cap."""


def _get_semaphore() -> asyncio.Semaphore:
    """Return (creating on first call) the module-level admission semaphore."""
    global _launch_semaphore  # noqa: PLW0603
    if _launch_semaphore is None:
        _launch_semaphore = asyncio.Semaphore(config.MAX_LAUNCHES)
    return _launch_semaphore


def _validate_request(data: dict[str, Any]) -> None:
    """Raise ValueError if *data* fails security or structural checks."""
    kind = data.get("action_kind") or ""
    if kind not in _LAUNCH_VALID_KINDS:
        raise ValueError(
            f"action_kind {kind!r} is not supported for on-demand launches. "
            f"Valid kinds: {sorted(_LAUNCH_VALID_KINDS)}"
        )
    _svc_validate_action_model(data.get("action_model"))
    _svc_validate_prompt(data.get("action_prompt"))
    _svc_validate_identifier(data.get("action_agent"), "action_agent")
    _svc_validate_identifier(data.get("action_project"), "action_project")
    _svc_validate_identifier(data.get("action_playbook"), "action_playbook")
    _svc_validate_extra_args(data.get("action_extra_args"))
    if kind == "engine":
        if not (data.get("action_engine_def") or "").strip():
            raise ValueError(
                "action_engine_def (an engine definition id or name) is required "
                "for engine launches"
            )
        if not (data.get("action_prompt") or "").strip():
            raise ValueError("action_prompt (the engine spec) is required for engine launches")
        _svc_validate_identifier(data.get("action_engine_def"), "action_engine_def")


async def launch(data: dict[str, Any]) -> dict[str, Any]:
    """Validate *data*, record an invocation, spawn a detached process, return identifiers.

    Raises TooManyLaunchesError when the in-flight cap is reached.
    Returns ``{invocation_id, action_kind}``; session IDs appear in the DB only after the
    process starts.
    """
    _validate_request(data)

    schedule_dict: dict[str, Any] = {
        "action_kind": data["action_kind"],
        "action_model": data.get("action_model") or "",
        "action_prompt": data.get("action_prompt") or "",
        "action_agent": data.get("action_agent"),
        "action_playbook": data.get("action_playbook"),
        "action_project": data.get("action_project"),
        "action_extra_args": data.get("action_extra_args") or [],
    }
    if data["action_kind"] == "engine":
        defn = await _resolve_engine_def(data["action_engine_def"])
        # The saved definition supplies the engine kind (via the action_agent
        # slot), default model, and engine flags; the request supplies the spec
        # prompt and may override the model.
        schedule_dict["action_agent"] = defn["kind"]
        if not schedule_dict["action_model"]:
            schedule_dict["action_model"] = defn.get("model") or ""
        opts: dict[str, Any] = dict(defn.get("options") or {})
        for k in ("max_depth", "max_agents"):
            if defn.get(k) is not None:
                opts[k] = defn[k]
        schedule_dict["action_engine_options"] = opts
    argv, tmp_path = build_argv(schedule_dict, {})

    sem = _get_semaphore()
    if sem.locked():
        raise TooManyLaunchesError(
            f"Maximum concurrent launches ({config.MAX_LAUNCHES}) reached. "
            "Retry when an existing launch completes."
        )
    await sem.acquire()

    inv_id = uuid.uuid4().hex[:12]
    now = time.time()

    try:
        async with StateDB() as db:
            await db.create_invocation(
                {
                    "id": inv_id,
                    "skill": f"launch:{data['action_kind']}",
                    "plugin": "studio_launch",
                    "prompt": data.get("action_prompt") or data.get("action_playbook"),
                    "started_at": now,
                    "status": "running",
                }
            )

        task = asyncio.create_task(
            _spawn_detached(argv, inv_id, tmp_path=tmp_path),
            name=f"launch-{inv_id}",
        )
    except BaseException:
        sem.release()
        raise

    task.add_done_callback(lambda _t: sem.release())
    _detached_tasks.add(task)
    task.add_done_callback(_detached_tasks.discard)

    return {
        "invocation_id": inv_id,
        "action_kind": data["action_kind"],
    }


async def _resolve_engine_def(ref: str) -> dict[str, Any]:
    """Resolve *ref* (engine definition id, then name) or raise ValueError."""
    from . import engine_defs

    defn = await engine_defs.get_engine_def(ref)
    if defn is None:
        defn = await engine_defs.get_engine_def_by_name(ref)
    if defn is None:
        raise ValueError(f"Engine definition {ref!r} not found")
    return defn


async def shutdown_launches() -> None:
    """Cancel all in-flight launch tasks on shutdown; each task writes a terminal DB row before re-raising."""
    tasks = [t for t in list(_detached_tasks) if not t.done()]
    if not tasks:
        return
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _spawn_detached(argv: list[str], inv_id: str, *, tmp_path: str | None) -> None:
    """Spawn the process and update the invocation row when it exits."""
    from lionagi.state.reasons import RunReasons

    from ..scheduler.subprocess import spawn_and_wait

    try:
        exit_code, _stderr = await spawn_and_wait(argv, inv_id, tmp_path=tmp_path)
        if exit_code == 0:
            status, reason = "completed", RunReasons.COMPLETED_OK
        else:
            status, reason = "failed", RunReasons.FAILED_EXIT_NONZERO
    except asyncio.CancelledError:
        # Write a terminal row before propagating; distinguish user vs shutdown.
        is_user = inv_id in _user_cancelled
        _user_cancelled.discard(inv_id)
        reason_summary = (
            "Launch cancelled by user." if is_user else "Launch cancelled by server shutdown."
        )
        try:
            async with StateDB() as db:
                await db.update_invocation(inv_id, ended_at=time.time())
                await db.update_status(
                    "invocation",
                    inv_id,
                    new_status="cancelled",
                    reason_code=RunReasons.CANCELLED_SYSTEM,
                    reason_summary=reason_summary,
                    evidence_refs=[],
                    source="executor",
                    actor=inv_id,
                    metadata={},
                )
        except Exception:
            _log.exception(
                "Failed to record cancellation for launch invocation %s",
                inv_id,
            )
        raise
    except Exception:
        _log.exception("Detached launch failed for invocation %s", inv_id)
        status, reason = "failed", RunReasons.FAILED_EXCEPTION

    try:
        async with StateDB() as db:
            await db.update_invocation(inv_id, ended_at=time.time())
            await db.update_status(
                "invocation",
                inv_id,
                new_status=status,
                reason_code=reason,
                reason_summary=f"Detached launch {status}.",
                evidence_refs=[],
                source="executor",
                actor=inv_id,
                metadata={},
            )
    except Exception:
        _log.exception("Failed to update invocation %s after detached launch", inv_id)


class LaunchRequest(BaseModel):
    action_kind: str
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_project: str | None = None
    action_flow_yaml: str | None = None
    action_engine_def: str | None = None
    action_extra_args: list[str] | None = None


@studio_route("/launches/", method="POST", area="launches", status_code=202)
async def launch_run(body: LaunchRequest) -> dict[str, Any]:
    """Fire an orchestration run immediately; process runs detached.

    Returns invocation_id; monitor via GET /api/invocations/{id} and GET /api/sessions/{id}/signals.
    """
    try:
        return await launch(body.model_dump(exclude_none=True))
    except TooManyLaunchesError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@studio_route(
    "/invocations/{invocation_id}/cancel", method="POST", area="launches", status_code=202
)
async def cancel_launch(invocation_id: str) -> dict[str, Any]:
    """Cancel an in-flight launch: terminate its detached process; the task records a cancelled invocation row."""
    for t in list(_detached_tasks):
        if t.get_name() == f"launch-{invocation_id}" and not t.done():
            _user_cancelled.add(invocation_id)
            t.cancel()
            return {"invocation_id": invocation_id, "status": "cancelling"}
    raise HTTPException(
        status_code=404, detail=f"No in-flight launch for invocation {invocation_id!r}"
    )
