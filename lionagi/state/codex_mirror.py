# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Mirror Codex CLI/app rollouts (~/.codex/sessions/**/rollout-*.jsonl) into StateDB,
one lionagi message per conversation record, under deterministic ids."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lionagi.protocols.messages.action_request import ActionRequest
from lionagi.protocols.messages.action_response import ActionResponse
from lionagi.protocols.messages.assistant_response import AssistantResponse
from lionagi.protocols.messages.instruction import Instruction

if TYPE_CHECKING:
    from lionagi.protocols.messages.message import RoledMessage

    from .db import StateDB

__all__ = (
    "session_db_id",
    "session_meta",
    "messages_for_record",
    "mirror_session",
    "reconcile_session_status",
    "link_session_lineage",
)

# Distinct from the Claude mirror's namespace so the two mirrors can never derive
# the same StateDB id from the same-looking upstream uid.
_NS = uuid.UUID("9c4a7b21-6d8e-4f13-a05c-2e7b9d1f83a4")

# A rollout interleaves the model conversation (``response_item``) with UI
# telemetry (``event_msg``), which restates the same turns. Only the former is
# mirrored; mirroring both would double every message.
_CONVERSATION_RECORD = "response_item"

# Harness-injected context that codex prepends as a user turn. Measured against
# the local corpus: these two account for every non-prompt user message seen.
_INJECTED_USER_PREFIXES = ("<recommended_plugins>", "<environment_context>")

# Roles that carry conversation. ``developer`` is the system-instruction channel.
_MIRRORED_ROLES = frozenset({"user", "assistant"})


def _det(*parts: str) -> str:
    """Deterministic UUID for a logical entity (session/branch/message)."""
    return str(uuid.uuid5(_NS, "|".join(parts)))


def session_db_id(rollout_uid: str) -> str:
    """StateDB session id for a codex rollout id (stable across runs)."""
    return _det(rollout_uid, "session")


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _text_blocks(content: Any) -> str:
    """Flatten a codex content array (input_text/output_text blocks) to display text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts = []
    for b in content:
        if isinstance(b, dict):
            if b.get("text"):
                parts.append(str(b["text"]))
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(p for p in parts if p)


def _arguments(raw: Any) -> dict[str, Any]:
    """Coerce a tool-call argument payload to a dict; codex sends JSON text or a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"input": raw}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def session_meta(record: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the fields a mirrored session needs out of a ``session_meta`` record.

    ``id`` is the rollout's own identity and is always present; ``session_id`` is
    the thread it belongs to and is absent on older rollouts.
    """
    if record.get("type") != "session_meta":
        return None
    p = record.get("payload")
    if not isinstance(p, dict):
        return None
    return {
        "rollout_uid": str(p.get("id") or ""),
        "thread_uid": str(p["session_id"]) if p.get("session_id") else None,
        "parent_thread_uid": str(p["parent_thread_id"]) if p.get("parent_thread_id") else None,
        "forked_from_uid": str(p["forked_from_id"]) if p.get("forked_from_id") else None,
        "cwd": str(p["cwd"]) if p.get("cwd") else None,
        "originator": str(p["originator"]) if p.get("originator") else None,
        "cli_version": str(p["cli_version"]) if p.get("cli_version") else None,
        "timestamp": p.get("timestamp"),
    }


def _tool_pair_ids(rollout_uid: str, call_id: str, fallback: str) -> tuple[str, str]:
    """(request_id, response_id) for a tool exchange, linked by codex's call_id."""
    key = call_id or fallback
    return _det(rollout_uid, "toolreq", key), _det(rollout_uid, "toolresp", key)


def messages_for_record(
    record: dict[str, Any],
    rollout_uid: str,
    tool_names: dict[str, str],
) -> list[RoledMessage]:
    """Map one codex rollout record to ordered lionagi messages. ``tool_names`` is
    read/written in place so a tool output can label its ActionResponse."""
    if record.get("type") != _CONVERSATION_RECORD:
        return []
    p = record.get("payload")
    if not isinstance(p, dict):
        return []

    base = _ts(record.get("timestamp")) or 0.0
    kind = p.get("type")
    pid = str(p.get("id") or "")
    specs: list[tuple[str, Any]] = []

    if kind == "message":
        role = p.get("role")
        if role not in _MIRRORED_ROLES:
            return []  # developer turns are instruction plumbing, not conversation
        text = _text_blocks(p.get("content")).strip()
        if not text:
            return []
        if role == "user":
            if text.startswith(_INJECTED_USER_PREFIXES):
                return []
            mid = _det(rollout_uid, pid or f"user:{base}", "instr")
            specs.append(
                (
                    mid,
                    lambda mid, ts, text=text: Instruction(
                        id=mid, created_at=ts, content={"instruction": text}
                    ),
                )
            )
        else:
            mid = _det(rollout_uid, pid or f"asst:{base}", "text")
            specs.append(
                (
                    mid,
                    lambda mid, ts, text=text: AssistantResponse(
                        id=mid, created_at=ts, content={"assistant_response": text}
                    ),
                )
            )

    elif kind in ("function_call", "custom_tool_call", "tool_search_call"):
        call_id = str(p.get("call_id") or "")
        fn = str(p.get("name") or ("tool_search" if kind == "tool_search_call" else ""))
        args = _arguments(p.get("arguments") if kind != "custom_tool_call" else p.get("input"))
        if call_id:
            tool_names[call_id] = fn
        req_id, _ = _tool_pair_ids(rollout_uid, call_id, pid)
        specs.append(
            (
                req_id,
                lambda mid, ts, fn=fn, args=args: ActionRequest(
                    id=mid, created_at=ts, content={"function": fn, "arguments": args}
                ),
            )
        )

    elif kind in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
        call_id = str(p.get("call_id") or "")
        out = p.get("tools") if kind == "tool_search_output" else p.get("output")
        text = json.dumps(out, default=str) if kind == "tool_search_output" else _text_blocks(out)
        req_id, resp_id = _tool_pair_ids(rollout_uid, call_id, pid)
        fn = tool_names.get(call_id, "")
        specs.append(
            (
                resp_id,
                lambda mid, ts, fn=fn, text=text, req_id=req_id: ActionResponse(
                    id=mid,
                    created_at=ts,
                    content={
                        "function": fn,
                        "output": text,
                        "action_request_id": req_id,
                        "error": None,
                    },
                ),
            )
        )

    # reasoning summaries and agent_message routing records carry no display
    # value in the studio reader — skipped, as thinking blocks are for Claude.
    return [builder(mid, base + i * 1e-3) for i, (mid, builder) in enumerate(specs)]


async def mirror_session(
    db: StateDB,
    *,
    rollout_uid: str,
    records: list[dict[str, Any]],
    tool_names: dict[str, str],
    project: str | None = None,
    project_source: str | None = None,
    model: str | None = None,
    provider: str | None = "openai",
    name: str | None = None,
    status: str = "running",
    node_metadata: dict[str, Any] | None = None,
) -> int:
    """Idempotently write a batch of codex records for one rollout; returns msgs written.
    Live/idle transitions are owned by ``reconcile_session_status``, not this writer."""
    sid = session_db_id(rollout_uid)
    branch_id = _det(rollout_uid, "branch")
    bprog = _det(rollout_uid, "bprog")
    sprog = _det(rollout_uid, "sprog")

    messages: list[RoledMessage] = []
    for rec in records:
        messages.extend(messages_for_record(rec, rollout_uid, tool_names))

    existing = await db.get_session(sid)
    if existing is None and not messages:
        return 0

    first_ts = min((m.created_at for m in messages), default=None)
    last_ts = max((m.created_at for m in messages), default=None)
    created_at = (existing.get("created_at") if existing is not None else None) or first_ts

    await db.create_progression(sprog)
    await db.create_progression(bprog)
    if existing is None:
        await db.create_session(
            {
                "id": sid,
                "cc_session_id": rollout_uid,
                "created_at": created_at,
                "progression_id": sprog,
                "name": name or "Codex session",
                "status": status,
                "invocation_kind": "agent",
                "agent_name": "codex",
                "model": model,
                "provider": provider,
                "project": project,
                "project_source": project_source,
                "node_metadata": node_metadata,
                "started_at": first_ts,
                "updated_at": last_ts,
            }
        )
    else:
        cc_session_id = rollout_uid if existing.get("cc_session_id") is None else None
        provenance_project = project if project and not existing.get("project") else None
        if cc_session_id is not None or provenance_project is not None:
            await db.set_session_provenance(
                sid,
                cc_session_id=cc_session_id,
                project=provenance_project,
                project_source=project_source if provenance_project is not None else None,
            )
    await db.create_branch(
        {
            "id": branch_id,
            "created_at": created_at,
            "session_id": sid,
            "progression_id": bprog,
            "model": model,
            "provider": provider,
            "agent_name": "codex",
        }
    )

    for m in messages:
        md = m.to_dict(mode="db")
        await db.insert_message(md)
        await db.append_to_progression(bprog, md["id"])
        await db.append_to_progression(sprog, md["id"])

    if messages:
        await db.touch_session_activity(sid, at=last_ts)

    return len(messages)


async def reconcile_session_status(
    db: StateDB,
    rollout_uid: str,
    *,
    now: float,
    live_window: float,
) -> None:
    """Align a mirrored codex session's status with its live/idle state."""
    from ._mirror_common import reconcile_status

    await reconcile_status(
        db,
        session_db_id(rollout_uid),
        now=now,
        live_window=live_window,
        actor="codex-mirror-reconcile",
    )


async def link_session_lineage(
    db: StateDB,
    *,
    child_uid: str,
    parent_uid: str,
    relation: str = "thread",
) -> None:
    """Record that one codex rollout continues another (same thread, fork, or subagent).
    ``relation`` names which of the three, because the fix differs per kind."""
    from ._mirror_common import link_lineage

    await link_lineage(
        db,
        child_sid=session_db_id(child_uid),
        parent_sid=session_db_id(parent_uid),
        parent_uid=parent_uid,
        parent_event_uuid="",
        extra={"relation": relation},
    )
