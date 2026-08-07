# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Mirror Codex CLI/app rollouts (~/.codex/sessions/**/rollout-*.jsonl) into StateDB,
one lionagi message per conversation record, under deterministic ids.

Reads the enveloped rollout format, where each line is ``{type, timestamp, payload}``.
Rollouts written before 2025-09-20 use a flat format with no envelope, and mirror
nothing; that was measured over the whole local corpus rather than sampled, at 6 files
out of 29,652. Such a file still gets a session row carrying its counts, because a
rollout that mirrors nothing is the case the per-type count pair exists to expose and
a row is what there is to subtract against.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lionagi.protocols.messages.action_request import ActionRequest
from lionagi.protocols.messages.action_response import ActionResponse
from lionagi.protocols.messages.assistant_response import AssistantResponse
from lionagi.protocols.messages.instruction import Instruction

from ._mirror_common import SourceLine, bound_mirror_content

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
    "absorb_orchestrated_session",
    "absorb_orchestrated_backfill",
    "RecordTally",
    "turn_context",
    "SOURCE_KIND",
    "ID_FIELD",
    "SKIPPED_ORIGINATORS",
)

_log = logging.getLogger(__name__)

# Provenance value for a session this mirror wrote, as opposed to one lionagi ran.
SOURCE_KIND = "imported_codex"

# Rollout originators this mirror does NOT import. A headless ``codex exec``
# rollout is some orchestrator's run — lionagi's own agent legs foremost — and
# that run already persists a first-class session under its own name; importing
# the rollout as well puts a second "codex" session beside the agent's for the
# same piece of work. This mirror exists for the interactive Codex surfaces
# (desktop app, TUI, IDE), whose rollouts have no other representation here.
SKIPPED_ORIGINATORS = frozenset({"codex_exec"})

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

# Harness-injected context that codex delivers through the user role: repo
# instructions, skill definitions, environment and editor blocks, the interruption
# notice. None of it is something a person typed, and it is not a rare case — a
# 1,000-file stride over the whole local corpus found 1,433 of 2,427 user messages
# (59%) to be injection, so without this filter most of the mirrored "prompts"
# would be machine text.
#
# The list is what that census turned up, and is deliberately NOT treated as
# closed: codex adds forms over time and two of these were found only after a
# first pass called an earlier, shorter list exhaustive. An uncovered form leaves
# a mirrored response_item the count pair cannot account for, which is the backstop
# for the completeness this list cannot promise on its own.
_INJECTED_USER_PREFIXES = (
    "<recommended_plugins>",
    "<environment_context>",
    "<skill>",
    "<turn_aborted>",
    "# AGENTS.md instructions for ",
    "# Context from my IDE setup:",
    "# Files mentioned by the user:",
)

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


def _source_mtime(source_path: str | None) -> float:
    """Last-resort time for a rollout carrying no readable record timestamp.

    Only reached when a file is unparseable enough that not one record yielded a
    time, which is itself a rollout worth having a row for.
    """
    try:
        return Path(source_path).stat().st_mtime  # type: ignore[arg-type]
    except (OSError, TypeError):
        return time.time()


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
    #
    # No attribution is written when no turn_context has been seen yet, which happens
    # for a prompt at the head of a resumed rollout — measured at 2 of 12,889 messages
    # over a live tree. Absent means the file had nothing to attribute to at that
    # point, not that attribution was lost; inventing one from the following turn
    # would state something the rollout does not.
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
    cwd: str | None = None,
    node_metadata: dict[str, Any] | None = None,
    source_path: str | None = None,
    turn: dict[str, Any] | None = None,
    unparseable: int = 0,
    event_sources: list[tuple[int, int, str]] | None = None,
    max_preview_chars: int | None = None,
) -> tuple[int, RecordTally]:
    """Idempotently write a batch of codex records for one rollout.

    Returns the messages written and the tally for this batch. ``turn`` is the
    turn_context carried in from the previous batch, so attribution survives a
    file being mirrored across several passes; it is updated in place as records
    are walked. ``source_path`` is the rollout file this batch came from, stamped
    into the session's provenance so any row resolves back to its file.

    ``event_sources`` is the per-record ``(byte_offset, byte_count, sha256)`` of
    each raw JSONL line in ``records`` (same order/length). When both it and
    ``max_preview_chars`` are given, every message's content is bounded via
    ``bound_mirror_content`` before it is written, with a resolvable source
    pointer on ``node_metadata.mirror_source``. Omitting them keeps the legacy
    unbounded write, for callers with no live rollout file behind the records.

    Live/idle transitions are owned by ``reconcile_session_status``, not this writer.
    """
    sid = session_db_id(rollout_uid)
    branch_id = _det(rollout_uid, "branch")
    bprog = _det(rollout_uid, "bprog")
    sprog = _det(rollout_uid, "sprog")

    seen: dict[str, int] = {}
    mirrored: dict[str, int] = {}
    messages: list[RoledMessage] = []
    message_sources: list[SourceLine | None] = []
    for idx, rec in enumerate(records):
        rtype = str(rec.get("type") or "<untyped>")
        seen[rtype] = seen.get(rtype, 0) + 1
        ctx = turn_context(rec)
        if ctx is not None and turn is not None:
            turn.clear()
            turn.update(ctx)
        produced = messages_for_record(rec, rollout_uid, tool_names, turn)
        if produced:
            mirrored[rtype] = mirrored.get(rtype, 0) + len(produced)
            src: SourceLine | None = None
            if event_sources is not None and idx < len(event_sources):
                offset, byte_count, sha = event_sources[idx]
                src = SourceLine(
                    value=rec,
                    source_path=source_path or "",
                    source_offset=offset,
                    source_byte_count=byte_count,
                    source_sha256=sha,
                )
            messages.extend(produced)
            message_sources.extend([src] * len(produced))
    tally = RecordTally(seen, mirrored, unparseable)

    existing = await db.get_session(sid)
    # A rollout none of whose records mirror still gets a row. That is the case
    # the count pair exists for: an injection form nobody enumerated, or a legacy
    # flat file this reader cannot parse, both look like "seen, never mirrored"
    # from the outside — but only if a row is there to subtract against. Writing
    # nothing would leave the one shape the accounting was built to expose as the
    # one shape it cannot see. Only a batch that read nothing at all is skipped.
    if existing is None and not records and not unparseable:
        return 0, tally

    msg_first = min((m.created_at for m in messages), default=None)
    msg_last = max((m.created_at for m in messages), default=None)
    # Both time columns are NOT NULL, and a fully filtered rollout has no message
    # times to take them from, so the records' own timestamps carry the row.
    rec_ts = [t for t in (_ts(rec.get("timestamp")) for rec in records) if t is not None]
    floor = min(rec_ts) if rec_ts else _source_mtime(source_path)
    first_ts = msg_first or floor
    last_ts = msg_last or (max(rec_ts) if rec_ts else floor)
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
                "artifacts_path": cwd,
                "node_metadata": meta,
                "started_at": first_ts,
                "updated_at": last_ts,
            }
        )
    else:
        cc_session_id = rollout_uid if existing.get("cc_session_id") is None else None
        provenance_project = project if project and not existing.get("project") else None
        provenance_artifacts_path = cwd if cwd and not existing.get("artifacts_path") else None
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
            artifacts_path=provenance_artifacts_path,
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

    for m, src in zip(messages, message_sources, strict=False):
        md = m.to_dict(mode="db")
        if max_preview_chars is not None and src is not None:
            preview, pointer = bound_mirror_content(
                md["content"],
                md["id"],
                src,
                source_kind="codex_jsonl",
                source_session_uid=rollout_uid,
                max_preview_chars=max_preview_chars,
            )
            md["content"] = preview
            nm = dict(md.get("node_metadata") or {})
            nm["mirror_source"] = pointer
            md["node_metadata"] = nm
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


async def absorb_orchestrated_session(db: StateDB, rollout_uid: str) -> bool:
    """Remove the row a previous version imported for a now-skipped rollout.

    Only rows this mirror wrote are deletable (``delete_imported_session``
    requires this mirror's exact source kind), so absorbing an id that a live
    run happens to own is a no-op rather than a data loss.
    """
    return await db.delete_imported_session(
        session_db_id(rollout_uid), require_source_kind=SOURCE_KIND
    )


async def absorb_orchestrated_backfill(db: StateDB) -> tuple[int, int]:
    """One sweep over already-imported rows, deleting those whose recorded
    originator is in ``SKIPPED_ORIGINATORS``; returns ``(removed, failed)``.

    The originator is read from the provenance each import wrote on its own row
    (``node_metadata.codex.originator``), so this reaches rows whose rollout
    files are older than the mirror's sweep window and would otherwise never be
    revisited. Only a string originator counts, and each row is handled in
    isolation: one malformed row must not stop the rest of the sweep. Rows with
    no recorded originator are left alone — absence of provenance is not
    evidence of orchestration.
    """
    removed = 0
    failed = 0
    for row in await db.sessions_by_source_kind(SOURCE_KIND):
        try:
            meta = row.get("node_metadata")
            if isinstance(meta, str):
                meta = json.loads(meta)
            if not isinstance(meta, dict):
                continue
            codex_block = meta.get("codex")
            originator = codex_block.get("originator") if isinstance(codex_block, dict) else None
            if isinstance(originator, str) and originator in SKIPPED_ORIGINATORS:
                if await db.delete_imported_session(row["id"], require_source_kind=SOURCE_KIND):
                    removed += 1
        except Exception:
            # The count alone says a row did not reconcile, never why. A
            # contended teardown gives up rather than waiting, so this path now
            # carries an ordinary, recurring cause that an operator watching the
            # retry warning has no other way to see.
            _log.exception("codex mirror: absorbing imported session %s failed", row.get("id"))
            failed += 1
    return removed, failed


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
