# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Launch service — backs POST /api/launches."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from lionagi.state.db import StateDB

from .. import config
from ..scheduler.subprocess import build_argv
from ..services.schedules import (
    _svc_validate_action_model,
    _svc_validate_extra_args,
    _svc_validate_identifier,
    _svc_validate_prompt,
)

_log = logging.getLogger(__name__)

# flow_yaml is excluded from on-demand launches: the inline YAML would need to
# be written to a temp file whose lifetime must outlive the HTTP request handler.
# Callers who need flow_yaml should create a schedule with trigger_type=manual
# and call POST /api/schedules/{id}/trigger instead.
_LAUNCH_VALID_KINDS = frozenset({"agent", "flow", "fanout", "play"})

# The event loop keeps only weak references to tasks; a fire-and-forget task
# with no strong reference can be garbage-collected mid-flight.
_detached_tasks: set[asyncio.Task] = set()

# Admission cap (config.MAX_LAUNCHES): a slot is acquired in launch() before
# the invocation row is created and released when the detached task completes,
# so a burst of concurrent POSTs cannot over-admit past the cap.
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


async def launch(data: dict[str, Any]) -> dict[str, Any]:
    """Validate *data*, record an invocation, spawn the process, return identifiers.

    Returns a dict with:
      - ``invocation_id``: created before spawn; use GET /api/invocations/{id}
        to watch status and find child sessions once they appear.
      - ``action_kind``: the normalised kind that was fired.

    Raises TooManyLaunchesError when the in-flight cap is reached.
    The spawned process runs detached.  Session IDs are only knowable after the
    process starts and writes to the DB, so they are not included in this
    response.
    """
    _validate_request(data)

    # Build and validate argv BEFORE creating the DB row.  build_argv does its
    # own validation pass; if it raises, no invocation row is left stranded.
    schedule_dict: dict[str, Any] = {
        "action_kind": data["action_kind"],
        "action_model": data.get("action_model") or "",
        "action_prompt": data.get("action_prompt") or "",
        "action_agent": data.get("action_agent"),
        "action_playbook": data.get("action_playbook"),
        "action_project": data.get("action_project"),
        "action_extra_args": data.get("action_extra_args") or [],
    }
    argv, tmp_path = build_argv(schedule_dict, {})

    sem = _get_semaphore()
    if sem.locked():
        raise TooManyLaunchesError(
            f"Maximum concurrent launches ({config.MAX_LAUNCHES}) reached. "
            "Retry when an existing launch completes."
        )
    # The slot must be taken here, not inside the spawned task: deferring the
    # acquire would let a burst of concurrent POSTs all pass the check above
    # before any task runs.  locked() was False and nothing yields between the
    # check and the acquire on a single event loop, so this never blocks.
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

        # Spawn detached — the HTTP handler must not block until the run finishes.
        task = asyncio.create_task(
            _spawn_detached(argv, inv_id, tmp_path=tmp_path),
            name=f"launch-{inv_id}",
        )
    except BaseException:
        sem.release()
        raise

    # Release on task completion (a done callback fires even if the task is
    # cancelled before its coroutine ever runs, where an in-task release would not).
    task.add_done_callback(lambda _t: sem.release())
    _detached_tasks.add(task)
    task.add_done_callback(_detached_tasks.discard)

    return {
        "invocation_id": inv_id,
        "action_kind": data["action_kind"],
    }


async def shutdown_launches() -> None:
    """Cancel all in-flight detached launch tasks and await their completion.

    Called from the app lifespan on shutdown.  Each task's CancelledError
    handler writes a terminal DB row before re-raising, so invocation rows do
    not stay stuck in 'running'.
    """
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
        # Server is shutting down — write a terminal row before propagating.
        try:
            async with StateDB() as db:
                await db.update_invocation(inv_id, ended_at=time.time())
                await db.update_status(
                    "invocation",
                    inv_id,
                    new_status="cancelled",
                    reason_code=RunReasons.CANCELLED_SYSTEM,
                    reason_summary="Launch cancelled by server shutdown.",
                    evidence_refs=[],
                    source="executor",
                    actor=inv_id,
                    metadata={},
                )
        except Exception:
            _log.exception(
                "Failed to record cancellation for launch invocation %s during shutdown",
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
