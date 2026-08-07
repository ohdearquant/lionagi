# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Strict stdio MCP bridge for real Studio Operator application tools.

The bridge exposes a deliberately small capability set. Read tools return
bounded projections, UI tools only append durable ADR-0083 effects, and the
single launch tool creates a durable proposal then waits for the daemon's
authenticated human-decision path. It never accepts a URL, filesystem path,
shell command, or raw endpoint.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .cancel_run import CANCEL_RUN_DESCRIPTION, CancelRunInput, cancel_run
from .redact import scrub_text
from .rename_session import RENAME_SESSION_DESCRIPTION, RenameSessionInput, rename_session
from .resume_run import RESUME_RUN_DESCRIPTION, ResumeRunInput, resume_run
from .run_findings import RunFindingsInput, run_findings
from .run_progress import RunProgressInput, run_progress
from .store import OperatorStore

OperatorSpace = Literal[
    "mission",
    "designer",
    "library",
    "history",
    "schedules",
    "system",
]


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RecentRunsInput(_StrictInput):
    limit: int = Field(default=10, ge=1, le=20)
    status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None


class ListSchedulesInput(_StrictInput):
    limit: int = Field(default=20, ge=1, le=50)
    enabled: bool | None = None


class ListAgentsInput(_StrictInput):
    limit: int = Field(default=50, ge=1, le=200)


class ListPlaybooksInput(_StrictInput):
    limit: int = Field(default=50, ge=1, le=200)


class RunStatsInput(_StrictInput):
    window: Literal["24h", "7d"] = "24h"


class CurrentViewInput(_StrictInput):
    """No arguments: the view is a property of the turn, not of the request."""


class NavigateInput(_StrictInput):
    space: OperatorSpace = Field(
        description=(
            "Studio space to open. Use 'history' for the first-class Fleet/run-history view."
        )
    )
    status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None


class PrefillScheduleInput(_StrictInput):
    name: str = Field(min_length=1, max_length=160)
    cron: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=32_768)
    description: str = Field(default="", max_length=1_000)

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        from lionagi.studio.services.schedules import _svc_validate_cron_expr

        _svc_validate_cron_expr(value, required=True)
        return value


class LaunchPlaybookInput(_StrictInput):
    playbook: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    note: str = Field(default="", max_length=500)


class _NavigateEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["navigate"] = "navigate"
    space: OperatorSpace
    params: dict[str, str]


class _PrefillEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["prefill"] = "prefill"
    form: Literal["schedule"] = "schedule"
    values: dict[str, str]


_TOOL_MODELS: dict[str, type[BaseModel]] = {
    "list_recent_runs": RecentRunsInput,
    "run_stats": RunStatsInput,
    "get_current_view": CurrentViewInput,
    "list_schedules": ListSchedulesInput,
    "list_agents": ListAgentsInput,
    "list_playbooks": ListPlaybooksInput,
    "navigate": NavigateInput,
    "prefill_schedule": PrefillScheduleInput,
    "launch_playbook": LaunchPlaybookInput,
    "run_progress": RunProgressInput,
    "run_findings": RunFindingsInput,
    "cancel_run": CancelRunInput,
    "resume_run": ResumeRunInput,
    "rename_session": RenameSessionInput,
}

_TOOL_DESCRIPTIONS = {
    "list_recent_runs": ("List at most 20 recent Studio runs as a redacted read-only projection."),
    "run_stats": (
        "Count runs over a whole window (24h or 7d) with per-status totals and "
        "completion rate. Use this for 'how many runs did I have', which "
        "list_recent_runs cannot answer because it only returns the newest 20."
    ),
    "get_current_view": (
        "Read the Studio view the human is on: space, route, selection and "
        "filters. Read-only. The 'source' field says how fresh the answer is: "
        "'live' means the browser reported this view after the instruction was "
        "sent, so it is where they are now; 'turn' means nothing newer has been "
        "reported, so it is where they were when they sent the instruction and "
        "they may have moved since."
    ),
    "list_schedules": (
        "List Studio schedules as a redacted read-only projection: trigger, "
        "next fire time, and recent health."
    ),
    "list_agents": ("List the agent profiles in the library, names and models only."),
    "list_playbooks": (
        "List playbook names available to launch_playbook. Call this before "
        "proposing a launch: launch_playbook needs an exact existing name."
    ),
    "navigate": (
        "Request a typed Studio navigation effect. The browser applies and "
        "acknowledges it; this tool does not claim that navigation completed. "
        "The canonical 'history' space opens Fleet/run history."
    ),
    "prefill_schedule": (
        "Request a typed schedule-form prefill for human review. This never creates a schedule."
    ),
    "launch_playbook": (
        "Propose launching one named Studio playbook. This blocks until the "
        "human explicitly allows or denies the exact durable proposal."
    ),
    "run_progress": (
        "Report how one run is going: status, op totals split into "
        "completed/running/failed/pending (they always sum to the total), "
        "which ops are running right now, elapsed time, and whether it has a "
        "graph. Accepts a run id, an id prefix, a name or playbook substring "
        "(minimum 3 characters), or 'current' for the run the human is "
        "looking at. An ambiguous reference returns candidates instead of "
        "guessing. Every number is a direct database read taken when this "
        "tool is called, not a live process feed -- the returned "
        "'freshness' field says so, the same honesty get_current_view uses "
        "for its own 'source' field."
    ),
    "run_findings": (
        "Report what one run produced: message tails, tool calls with their "
        "inferred outcomes, errors, and declared artifacts, bounded and "
        "redacted the same way the other read tools are. Filter by "
        "agent/branch name substring or a single 'kind' (messages, "
        "tool_calls, errors, artifacts) to narrow the response. Same "
        "reference vocabulary and ambiguity handling as run_progress. "
        "Tool-call outcomes are inferred from response content, not read "
        "from a structured status field the run itself stores, so treat "
        "them as a best-effort read; any section too large to return in "
        "full is capped and says so via its own 'truncated' flag."
    ),
    "cancel_run": CANCEL_RUN_DESCRIPTION,
    "resume_run": RESUME_RUN_DESCRIPTION,
    "rename_session": RENAME_SESSION_DESCRIPTION,
}

_TOOL_SCHEMAS = [
    {
        "name": name,
        "description": _TOOL_DESCRIPTIONS[name],
        "inputSchema": model.model_json_schema(),
    }
    for name, model in _TOOL_MODELS.items()
]


def _identity() -> tuple[OperatorStore, str, str]:
    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        raise RuntimeError("Studio application bridge is missing its durable turn identity")
    return OperatorStore(db_path), conversation_id, request_id


def public_project(value: Any) -> str | None:
    """Reduce a project to a leaf name so no filesystem layout is disclosed."""
    if not isinstance(value, str) or not value:
        return None
    if Path(value).is_absolute():
        return Path(value).name or "external-project"
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute():
        return windows_path.name or "external-project"
    return value[:160]


async def list_recent_runs(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RecentRunsInput.model_validate(arguments)
    from lionagi.studio.services.runs import list_runs

    rows = await list_runs(status=args.status, limit=args.limit, offset=0)

    projected = [
        {
            "id": row.get("id"),
            "agentName": row.get("agent_name"),
            "status": row.get("status"),
            "project": public_project(row.get("project")),
            "startedAt": row.get("started_at"),
            "endedAt": row.get("ended_at"),
            "href": f"/runs/{row.get('id')}",
        }
        for row in rows[: args.limit]
        if isinstance(row.get("id"), str)
    ]
    return {"runs": projected, "count": len(projected), "bounded": True}


async def run_stats(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RunStatsInput.model_validate(arguments)
    from lionagi.studio.services.stats import get_activity_stats

    stats = await get_activity_stats(args.window)
    buckets = stats.get("buckets") or []
    totals = {key: 0 for key in ("completed", "failed", "cancelled", "running")}
    for bucket in buckets:
        for key in totals:
            value = bucket.get(key)
            if isinstance(value, int):
                totals[key] += value
    return {
        "window": stats.get("window"),
        "total": stats.get("total"),
        "byStatus": totals,
        "completionRate": stats.get("completion_rate"),
    }


async def get_current_view(arguments: dict[str, Any]) -> dict[str, Any]:
    CurrentViewInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()
    turn = await store.get_turn(request_id)
    context = turn.get("context")
    context = context if isinstance(context, dict) else None
    source = "turn"

    # The turn's context is frozen at submit, so it is only the freshest answer
    # until the human moves. Prefer a view the SAME PAGE observed later in its
    # own count of the views it has seen.
    #
    # Both halves of that are load-bearing. Server arrival order cannot stand in
    # for the count: a report the browser saw before the instruction can arrive
    # after it, and ordering by arrival would present a view from before the
    # question as the answer to it, labelled live. Nor can a wall clock, which
    # can step backwards and leave a stale view holding the higher number. And a
    # count from a different page cannot be compared at all: two tabs on one
    # conversation are looking at two different pages, they count
    # independently, and only the page the instruction came from can say where
    # the human is.
    #
    # When the turn names no observer or no count there is nothing to compare
    # against, so the honest answer is the turn's own snapshot rather than a
    # freshness claim that cannot be supported.
    turn_seq = (context or {}).get("observationSeq")
    turn_observer = (context or {}).get("observerId")
    if isinstance(turn_seq, int) and isinstance(turn_observer, str):
        reported, reported_seq = await store.get_view(conversation_id, turn_observer)
        if reported is not None and isinstance(reported_seq, int) and reported_seq > turn_seq:
            context, source = reported, "live"

    if context is None:
        return {"known": False}
    return {
        "known": True,
        "space": context.get("space"),
        "route": context.get("route"),
        "project": public_project(context.get("project")),
        "selection": context.get("selection"),
        "filters": context.get("filters"),
        # "turn" means nothing observed later than the instruction has been
        # reported, so the human may have moved since. "live" means this is
        # where they are.
        #
        # The observation count that decided this is deliberately NOT returned.
        # It counts what one page has seen and means nothing outside that page,
        # so a bare number here could only invite a comparison that is not
        # valid -- which is the defect this whole mechanism was built to remove.
        "source": source,
    }


async def list_schedules(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListSchedulesInput.model_validate(arguments)
    from lionagi.studio.services.schedules import list_schedules as _list_schedules

    rows = await _list_schedules(enabled=args.enabled)
    projected = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "enabled": bool(row.get("enabled")),
            "triggerType": row.get("trigger_type"),
            "cron": row.get("cron_expr"),
            "intervalSec": row.get("interval_sec"),
            "actionKind": row.get("action_kind"),
            "nextFireAt": row.get("next_fire_at"),
            "lastFiredAt": row.get("last_fired_at"),
            "lastStatus": row.get("last_status"),
            "consecutiveFailures": row.get("consecutive_failures"),
            "project": public_project(row.get("project")),
        }
        for row in rows[: args.limit]
        if isinstance(row.get("id"), str)
    ]
    return {"schedules": projected, "count": len(projected), "bounded": True}


async def list_agents(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListAgentsInput.model_validate(arguments)
    from lionagi.studio.services.agents import list_agents as _list_agents
    from lionagi.studio.services.redaction import demo_mode_enabled, project_agent_fields

    rows = await anyio.to_thread.run_sync(_list_agents)
    redact = demo_mode_enabled()
    projected = []
    for row in rows[: args.limit]:
        if not isinstance(row.get("name"), str):
            continue
        safe = project_agent_fields(row, redact=redact)
        projected.append(
            {
                "name": row.get("name"),
                "provider": safe.get("provider") or None,
                "model": safe.get("model") or None,
                "description": (safe.get("description") or "")[:500] or None,
            }
        )
    return {"agents": projected, "count": len(projected), "bounded": True}


async def list_playbooks(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListPlaybooksInput.model_validate(arguments)
    from lionagi.studio.services.playbooks import list_playbooks as _list_playbooks

    rows = await anyio.to_thread.run_sync(_list_playbooks)
    projected = [
        {
            "name": row.get("name"),
            "description": (row.get("description") or "")[:500] or None,
        }
        for row in rows[: args.limit]
        if isinstance(row.get("name"), str)
    ]
    return {"playbooks": projected, "count": len(projected), "bounded": True}


async def navigate(arguments: dict[str, Any]) -> dict[str, Any]:
    args = NavigateInput.model_validate(arguments)
    if args.status is not None and args.space not in {"mission", "history"}:
        raise ValueError("status is only valid for mission/history run navigation")
    params = {"status": args.status} if args.status is not None else {}
    effect = _NavigateEffect(space=args.space, params=params).model_dump()
    store, conversation_id, request_id = _identity()
    frame = await store.append_effect(conversation_id, request_id, effect)
    if frame is None:
        raise RuntimeError("The Operator turn ended before the UI effect was persisted")
    stored = frame["payload"]["effect"]
    return {
        "status": "pending",
        "effectId": stored["id"],
        "kind": stored["kind"],
    }


async def prefill_schedule(arguments: dict[str, Any]) -> dict[str, Any]:
    args = PrefillScheduleInput.model_validate(arguments)
    effect = _PrefillEffect(
        values={
            "name": args.name,
            "cron": args.cron,
            "prompt": args.prompt,
            "description": args.description,
        }
    ).model_dump()
    store, conversation_id, request_id = _identity()
    frame = await store.append_effect(conversation_id, request_id, effect)
    if frame is None:
        raise RuntimeError("The Operator turn ended before the UI effect was persisted")
    stored = frame["payload"]["effect"]
    return {
        "status": "pending",
        "effectId": stored["id"],
        "kind": stored["kind"],
    }


def _redacted_launch_result(proposal: dict[str, Any]) -> dict[str, Any]:
    raw = proposal.get("result")
    result = raw if isinstance(raw, dict) else {}
    allowed = {
        key: result[key]
        for key in ("invocation_id", "run_id", "href", "action_kind")
        if isinstance(result.get(key), (str, int, float, bool))
    }
    return {
        "status": proposal["status"],
        "proposalId": proposal["id"],
        "result": allowed,
        "errorCode": proposal.get("errorCode"),
    }


async def resolve_playbook_version(playbook: str) -> str:
    """Fingerprint the exact playbook content without exposing its host path."""
    from lionagi.studio.services.playbooks import fingerprint_playbook

    return await anyio.to_thread.run_sync(fingerprint_playbook, playbook)


async def launch_playbook(arguments: dict[str, Any]) -> dict[str, Any]:
    args = LaunchPlaybookInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()
    target_version = await resolve_playbook_version(args.playbook)
    command = {
        "action_kind": "play",
        "action_playbook": args.playbook,
    }
    stable = store.canonical_hash(
        {
            "requestId": request_id,
            "tool": "launch_playbook",
            "command": command,
            "targetVersion": target_version,
        }
    )
    summary = f"Launch playbook '{args.playbook}'"
    if args.note:
        summary += f" — {args.note}"
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type="launch",
        command=command,
        risk="execute",
        summary=summary,
        idempotency_key=f"operator-app:{stable}",
        target_version=target_version,
    )
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= time.time():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in {"succeeded", "failed", "cancelled", "expired", "conflict"}:
            return _redacted_launch_result(proposal)
        await asyncio.sleep(0.1)


_TOOL_HANDLERS = {
    "list_recent_runs": list_recent_runs,
    "run_stats": run_stats,
    "get_current_view": get_current_view,
    "list_schedules": list_schedules,
    "list_agents": list_agents,
    "list_playbooks": list_playbooks,
    "navigate": navigate,
    "prefill_schedule": prefill_schedule,
    "launch_playbook": launch_playbook,
    "run_progress": run_progress,
    "run_findings": run_findings,
    "cancel_run": cancel_run,
    "resume_run": resume_run,
    "rename_session": rename_session,
}


async def _dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    if message_id is None:
        return None
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": requested or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "studio-operator", "version": "1"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {"tools": _TOOL_SCHEMAS},
        }
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return _tool_response(message_id, {"error": "Unknown Operator tool"}, error=True)
        try:
            result = await handler(params.get("arguments") or {})
            return _tool_response(message_id, result)
        except ValidationError as exc:
            return _tool_response(
                message_id,
                {
                    "error": "Invalid Operator tool arguments",
                    "details": exc.errors(
                        include_url=False,
                        include_context=False,
                        include_input=False,
                    ),
                },
                error=True,
            )
        except ValueError as exc:
            return _tool_response(message_id, {"error": scrub_text(str(exc))}, error=True)
        except Exception:  # noqa: BLE001
            return _tool_response(
                message_id,
                {"error": "Studio Operator application service is unavailable"},
                error=True,
            )
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _tool_response(
    message_id: Any,
    value: dict[str, Any],
    *,
    error: bool = False,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(value, sort_keys=True, separators=(",", ":")),
                }
            ],
            **({"isError": True} if error else {}),
        },
    }


async def _main() -> None:
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            return
        try:
            message = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        response = await _dispatch(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_main())
