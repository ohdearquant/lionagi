# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Dependency-free stdio MCP permission bridge for Claude Code.

Claude invokes ``request_permission`` before gated native tool work. The helper
persists the exact tool/input as an Operator proposal and polls the shared
StateDB until the daemon's authenticated human-decision endpoint records allow
or deny. Nothing is auto-approved in this subprocess.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from .store import OperatorStore

_TOOL_SCHEMA = {
    "name": "request_permission",
    "description": "Ask the Lion Studio human operator to allow or deny gated work.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input", "tool_use_id"],
        "additionalProperties": False,
    },
}


def _risk(tool_name: str) -> str:
    lowered = tool_name.lower()
    if any(part in lowered for part in ("bash", "command", "terminal", "execute")):
        return "execute"
    return "mutate"


async def request_permission(arguments: dict[str, Any]) -> dict[str, Any]:
    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        return {
            "behavior": "deny",
            "message": "Studio permission bridge is missing its durable turn identity",
        }

    tool_name = arguments.get("tool_name")
    tool_input = arguments.get("input")
    tool_use_id = arguments.get("tool_use_id")
    if (
        not isinstance(tool_name, str)
        or not tool_name
        or not isinstance(tool_input, dict)
        or not isinstance(tool_use_id, str)
        or not tool_use_id
    ):
        return {"behavior": "deny", "message": "Invalid permission request"}

    command = {
        "toolName": tool_name,
        "input": tool_input,
        "toolUseId": tool_use_id,
    }
    store = OperatorStore(db_path)
    stable = store.canonical_hash(
        {
            "requestId": request_id,
            "toolUseId": tool_use_id,
            "toolName": tool_name,
            "command": command,
        }
    )
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type="provider_permission",
        command=command,
        risk=_risk(tool_name),
        summary=f"Allow {tool_name} for this Operator turn",
        idempotency_key=f"provider:{stable}",
    )
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= time.time():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in {"confirmed", "succeeded"}:
            return {"behavior": "allow", "updatedInput": tool_input}
        if status in {"cancelled", "expired", "failed", "conflict"}:
            # Name the deciding party precisely: the human at the Studio
            # prompt, not "the operator" (which is what the model reading
            # this transcript calls itself). An expiry is nobody's decision.
            if status == "expired":
                message = (
                    "The permission request expired before the human at the "
                    "Studio prompt decided; raise it again if still needed"
                )
            else:
                message = "The human at the Studio permission prompt declined this tool request"
            return {"behavior": "deny", "message": message}
        await asyncio.sleep(0.1)


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
                "serverInfo": {"name": "studio-permission", "version": "1"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {"tools": [_TOOL_SCHEMA]},
        }
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "request_permission":
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "content": [{"type": "text", "text": "Unknown permission tool"}],
                    "isError": True,
                },
            }
        try:
            decision = await request_permission(params.get("arguments") or {})
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(decision, sort_keys=True, separators=(",", ":")),
                        }
                    ]
                },
            }
        except Exception:  # noqa: BLE001
            # The provider gets a stable denial; local paths/DB errors are never
            # reflected into its prompt or the browser. The protocol has no
            # third verdict, so the message carries the distinction: this is
            # an outage, not a human "no" — retrying later is legitimate.
            decision = {
                "behavior": "deny",
                "message": (
                    "Studio permission service is temporarily unavailable; "
                    "this is not a human denial — retry shortly"
                ),
            }
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(decision, sort_keys=True),
                        }
                    ],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
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
            rendered = json.dumps(response, sort_keys=True, separators=(",", ":"))
            sys.stdout.write(rendered + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_main())
