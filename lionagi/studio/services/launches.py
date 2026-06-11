# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Launch service — backs POST /api/launches.

Fires an agent/flow/fanout/play/flow_yaml run immediately, reusing the
scheduler's validated argv-building path.  The spawned process runs detached;
the caller watches progress via the invocations and sessions APIs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from lionagi.state.db import StateDB

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
# The scheduler handles this safely inside its own coroutine; replicating that
# logic here would duplicate the temp-file lifetime management.  Callers who
# need flow_yaml should create a schedule with trigger_type=manual and call
# POST /api/schedules/{id}/trigger instead.
_LAUNCH_VALID_KINDS = frozenset({"agent", "flow", "fanout", "play"})

# The event loop keeps only weak references to tasks; a fire-and-forget task
# with no strong reference can be garbage-collected mid-flight.
_detached_tasks: set[asyncio.Task] = set()


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

    The spawned process runs detached.  Session IDs are only knowable after the
    process starts and writes to the DB, so they are not included in this
    response.
    """
    _validate_request(data)

    inv_id = uuid.uuid4().hex[:12]
    now = time.time()

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

    # build_argv requires the scheduler dict shape; map the launch fields onto it.
    schedule_dict: dict[str, Any] = {
        "action_kind": data["action_kind"],
        "action_model": data.get("action_model") or "",
        "action_prompt": data.get("action_prompt") or "",
        "action_agent": data.get("action_agent"),
        "action_playbook": data.get("action_playbook"),
        "action_project": data.get("action_project"),
        "action_extra_args": data.get("action_extra_args") or [],
    }

    # build_argv does its own validation pass; raise before spawning if it disagrees.
    argv, tmp_path = build_argv(schedule_dict, {})

    # Spawn detached — the HTTP handler must not block until the run finishes.
    # Fire-and-forget: the task is not awaited.  tmp_path is None for non-flow_yaml
    # kinds (validated above), so there is no temp-file lifetime issue.
    task = asyncio.create_task(
        _spawn_detached(argv, inv_id, tmp_path=tmp_path),
        name=f"launch-{inv_id}",
    )
    _detached_tasks.add(task)
    task.add_done_callback(_detached_tasks.discard)

    return {
        "invocation_id": inv_id,
        "action_kind": data["action_kind"],
    }


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
        # Server is going down with us; no terminal row update is possible.
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
