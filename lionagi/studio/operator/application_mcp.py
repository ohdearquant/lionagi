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


_TOOL_MODELS: dict[str, type[_StrictInput]] = {
    "list_recent_runs": RecentRunsInput,
    "navigate": NavigateInput,
    "prefill_schedule": PrefillScheduleInput,
    "launch_playbook": LaunchPlaybookInput,
}

_TOOL_DESCRIPTIONS = {
    "list_recent_runs": ("List at most 20 recent Studio runs as a redacted read-only projection."),
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


async def list_recent_runs(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RecentRunsInput.model_validate(arguments)
    from lionagi.studio.services.runs import list_runs

    rows = await list_runs(status=args.status, limit=args.limit, offset=0)

    def public_project(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        if Path(value).is_absolute():
            return Path(value).name or "external-project"
        windows_path = PureWindowsPath(value)
        if windows_path.is_absolute():
            return windows_path.name or "external-project"
        return value[:160]

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
    "navigate": navigate,
    "prefill_schedule": prefill_schedule,
    "launch_playbook": launch_playbook,
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
            return _tool_response(message_id, {"error": str(exc)}, error=True)
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
