# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Mirror Codex CLI/app rollouts (~/.codex/sessions/**/rollout-*.jsonl) into StateDB,
one lionagi message per conversation record, under deterministic ids.

Reads the enveloped rollout format, where each line is ``{type, timestamp, payload}``.
Rollouts written before 2025-09-20 use a flat format with no envelope, and mirror
nothing; that was measured over the whole local corpus rather than sampled, at 6 files
out of 29,652, and the caller reports a file it read records from and mirrored none of
rather than passing over it in silence.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
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
    "RecordTally",
    "turn_context",
    "SOURCE_KIND",
    "ID_FIELD",
)

# Provenance value for a session this mirror wrote, as opposed to one lionagi ran.
SOURCE_KIND = "imported_codex"

# Which of a rollout's identifiers ``cc_session_id`` holds. A rollout carries three,
# and the column that stores one of them says nothing about which — so the name is
# written beside the value rather than left to a future reader to infer.
ID_FIELD = "codex_rollout_id"

# Where the import provenance lives on a mirrored session's node_metadata.
_IMPORT_KEY = "codex_import"

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

# Per-turn model/effort/config. Retained rather than skipped: it is what makes the
# conversation records interpretable, and without it every mirrored turn is an
# unattributed quote — "which model, at what effort" is the question a future reader
# asks when deciding whether a run's output is usable.
_TURN_CONTEXT_RECORD = "turn_context"

# The turn_context fields carried onto each message. Measured over a whole-corpus
# stride sample: model and effort are present on every turn_context seen, turn_id on
# about two thirds (it postdates the older rollouts).
_TURN_FIELDS = ("model", "effort", "turn_id")


@dataclass(frozen=True)
class RecordTally:
    """Per-record-type counts from both sides of one file's import.

    Completeness is then a subtraction any consumer can do — ``seen`` minus
    ``mirrored``, per type — rather than a narrative the importer writes about
    itself. A self-report goes stale silently when the importer's behaviour
    changes; two numbers that disagree cannot.

    ``unparseable`` is deliberately its own count and never folded into a type's
    skip. A line that could not be read is not a line deliberately not mirrored,
    and rolling the two together makes the subtraction stop discriminating exactly
    where a corpus is damaged.
    """

    seen: dict[str, int] = field(default_factory=dict)
    mirrored: dict[str, int] = field(default_factory=dict)
    unparseable: int = 0

    def merged(self, other: RecordTally) -> RecordTally:
        """This tally plus another (successive passes over a growing file)."""
        seen = dict(self.seen)
        mirrored = dict(self.mirrored)
        for key, val in other.seen.items():
            seen[key] = seen.get(key, 0) + val
        for key, val in other.mirrored.items():
            mirrored[key] = mirrored.get(key, 0) + val
        return RecordTally(seen, mirrored, self.unparseable + other.unparseable)

    def as_provenance(self) -> dict[str, Any]:
        """The form written to a session row; keys stay stable for consumers."""
        return {
            "records_seen": dict(sorted(self.seen.items())),
            "messages_mirrored": dict(sorted(self.mirrored.items())),
            "records_unparseable": self.unparseable,
        }


def _import_block(source_path: str | None, tally: RecordTally) -> dict[str, Any]:
    """The provenance a mirrored session carries: where the row came from, which
    identifier it was keyed on, and both sides of the record counts."""
    block: dict[str, Any] = {"id_field": ID_FIELD, **tally.as_provenance()}
    if source_path:
        block["source_path"] = source_path
    return block


def _carried_tally(block: Any) -> RecordTally:
    """The tally already recorded on a session row, for merging with a new batch.

    A block written by an older version, or damaged, reads as an empty tally rather
    than raising — but an empty tally is not silently equivalent to a complete one,
    because the counts it merges into still have to match the file.
    """
    if not isinstance(block, dict):
        return RecordTally()
    seen = block.get("records_seen")
    mirrored = block.get("messages_mirrored")
    unparseable = block.get("records_unparseable")
    return RecordTally(
        dict(seen) if isinstance(seen, dict) else {},
        dict(mirrored) if isinstance(mirrored, dict) else {},
        unparseable if isinstance(unparseable, int) else 0,
    )


def turn_context(record: dict[str, Any]) -> dict[str, Any] | None:
    """The retained fields of a ``turn_context`` record, or None for other records."""
    if record.get("type") != _TURN_CONTEXT_RECORD:
        return None
    p = record.get("payload")
    if not isinstance(p, dict):
        return None
    out = {k: str(p[k]) for k in _TURN_FIELDS if p.get(k)}
    return out or None


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
    turn: dict[str, Any] | None = None,
) -> list[RoledMessage]:
    """Map one codex rollout record to ordered lionagi messages. ``tool_names`` is
    read/written in place so a tool output can label its ActionResponse.

    ``turn`` is the most recent ``turn_context`` seen before this record; it is
    stamped onto every message produced so a mirrored turn stays attributable to
    the model and effort that produced it.
    """
    if record.get("type") != _CONVERSATION_RECORD:
        return []
    p = record.get("payload")
    if not isinstance(p, dict):
        return []

    base = _ts(record.get("timestamp")) or 0.0
    kind = p.get("type")
    pid = str(p.get("id") or "")
    # Attribution travels with the message, not only with the session: a rollout can
    # change model or effort mid-thread, so a session-level value would misattribute
    # every turn before the switch.
    meta = {"codex_turn": dict(turn)} if turn else {}
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
                        id=mid, created_at=ts, content={"instruction": text}, metadata=meta
                    ),
                )
            )
        else:
            mid = _det(rollout_uid, pid or f"asst:{base}", "text")
            specs.append(
                (
                    mid,
                    lambda mid, ts, text=text: AssistantResponse(
                        id=mid, created_at=ts, content={"assistant_response": text}, metadata=meta
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
                    id=mid,
                    created_at=ts,
                    content={"function": fn, "arguments": args},
                    metadata=meta,
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
                    metadata=meta,
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
    source_path: str | None = None,
    turn: dict[str, Any] | None = None,
    unparseable: int = 0,
) -> tuple[int, RecordTally]:
    """Idempotently write a batch of codex records for one rollout.

    Returns the messages written and the tally for this batch. ``turn`` is the
    turn_context carried in from the previous batch, so attribution survives a
    file being mirrored across several passes; it is updated in place as records
    are walked. ``source_path`` is the rollout file this batch came from, stamped
    into the session's provenance so any row resolves back to its file.

    Live/idle transitions are owned by ``reconcile_session_status``, not this writer.
    """
    sid = session_db_id(rollout_uid)
    branch_id = _det(rollout_uid, "branch")
    bprog = _det(rollout_uid, "bprog")
    sprog = _det(rollout_uid, "sprog")

    seen: dict[str, int] = {}
    mirrored: dict[str, int] = {}
    messages: list[RoledMessage] = []
    for rec in records:
        rtype = str(rec.get("type") or "<untyped>")
        seen[rtype] = seen.get(rtype, 0) + 1
        ctx = turn_context(rec)
        if ctx is not None and turn is not None:
            turn.clear()
            turn.update(ctx)
        produced = messages_for_record(rec, rollout_uid, tool_names, turn)
        if produced:
            mirrored[rtype] = mirrored.get(rtype, 0) + len(produced)
            messages.extend(produced)
    tally = RecordTally(seen, mirrored, unparseable)

    existing = await db.get_session(sid)
    if existing is None and not messages:
        return 0, tally

    first_ts = min((m.created_at for m in messages), default=None)
    last_ts = max((m.created_at for m in messages), default=None)
    created_at = (existing.get("created_at") if existing is not None else None) or first_ts

    await db.create_progression(sprog)
    await db.create_progression(bprog)
    if existing is None:
        meta = dict(node_metadata or {})
        meta[_IMPORT_KEY] = _import_block(source_path, tally)
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
                "source_kind": SOURCE_KIND,
                "model": model,
                "provider": provider,
                "effort": (turn or {}).get("effort"),
                "project": project,
                "project_source": project_source,
                "node_metadata": meta,
                "started_at": first_ts,
                "updated_at": last_ts,
            }
        )
    else:
        cc_session_id = rollout_uid if existing.get("cc_session_id") is None else None
        provenance_project = project if project and not existing.get("project") else None
        # A file mirrored across several passes accumulates its counts, so the
        # subtraction a consumer does is against the whole file rather than the
        # last batch of it.
        meta = dict(existing.get("node_metadata") or {})
        meta[_IMPORT_KEY] = _import_block(
            source_path, _carried_tally(meta.get(_IMPORT_KEY)).merged(tally)
        )
        await db.set_session_provenance(
            sid,
            cc_session_id=cc_session_id,
            project=provenance_project,
            project_source=project_source if provenance_project is not None else None,
            node_metadata=meta,
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

    return len(messages), tally


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
