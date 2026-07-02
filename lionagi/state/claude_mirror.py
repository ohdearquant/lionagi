# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Mirror Claude Code session transcripts (~/.claude/projects/*.jsonl) into StateDB.

A Claude Code session is just another writer to ``state.db``: each JSONL event
maps to one or more lionagi messages, written under a session/branch/progression
with deterministic ids so re-reading the same transcript never duplicates rows.
The studio SSE reader polls the same tables, so mirrored sessions stream live in
the dashboard and the VS Code extension with no studio-side change.
"""

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

# Fixed namespace so ids derived from a Claude session/event are stable across
# mirror restarts — the basis for idempotent, resumable writes.
_NS = uuid.UUID("5f1d6e2a-1c3b-4a5d-8e9f-0a1b2c3d4e5f")

# Only conversation-bearing events become messages; the rest is editor metadata.
_MESSAGE_TYPES = frozenset({"user", "assistant"})

# Slash-command invocations and local-command output wrap their text in these
# tags. They are editor machinery, not conversation — a real prompt never opens
# with one — so they are dropped to keep the mirrored transcript readable.
_COMMAND_NOISE_PREFIXES = ("<command-", "<local-command-")


def _det(*parts: str) -> str:
    """Deterministic UUID for a logical entity (session/branch/message/link)."""
    return str(uuid.uuid5(_NS, "|".join(parts)))


def session_db_id(session_uid: str) -> str:
    """StateDB session id for a Claude session uuid (stable across runs)."""
    return _det(session_uid, "session")


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _tool_result_text(content: Any) -> str:
    """Flatten a Claude tool_result payload (str | blocks | dict) to display text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text" or "text" in c:
                    parts.append(str(c.get("text", "")))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return content.get("text") or json.dumps(content, default=str)
    return str(content)


def messages_for_event(
    event: dict[str, Any],
    session_uid: str,
    tool_names: dict[str, str],
) -> list[RoledMessage]:
    """Map one Claude JSONL event to ordered lionagi messages.

    ``tool_names`` is read/written in place: tool_use blocks record their
    function name so the matching tool_result can label its ActionResponse.
    """
    etype = event.get("type")
    if etype not in _MESSAGE_TYPES or event.get("isMeta"):
        return []
    msg = event.get("message")
    if not isinstance(msg, dict):
        return []

    euid = str(event.get("uuid") or "")
    base = _ts(event.get("timestamp")) or 0.0
    content = msg.get("content")
    blocks: list[Any]
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content
    else:
        blocks = []

    # Each spec is (id, builder(id, created_at) -> message); built in order with
    # a micro-incremented timestamp so messages of one event stay ordered.
    specs: list[tuple[str, Any]] = []

    if etype == "user":
        text_parts: list[str] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text"):
                text_parts.append(b["text"])
            elif bt == "tool_result":
                tuid = str(b.get("tool_use_id") or "")
                out = _tool_result_text(b.get("content"))
                err = "error" if b.get("is_error") else None
                link = _det(session_uid, "toolreq", tuid) if tuid else None
                mid = _det(session_uid, "toolresp", tuid or euid)
                fn = tool_names.get(tuid, "")
                specs.append(
                    (
                        mid,
                        lambda mid, ts, fn=fn, out=out, link=link, err=err: ActionResponse(
                            id=mid,
                            created_at=ts,
                            content={
                                "function": fn,
                                "output": out,
                                "action_request_id": link,
                                "error": err,
                            },
                        ),
                    )
                )
        text = "".join(text_parts).strip()
        if text and not text.startswith(_COMMAND_NOISE_PREFIXES):
            mid = _det(session_uid, euid, "instr")
            specs.insert(
                0,
                (
                    mid,
                    lambda mid, ts, text=text: Instruction(
                        id=mid, created_at=ts, content={"instruction": text}
                    ),
                ),
            )

    elif etype == "assistant":
        buf: list[str] = []
        flush_n = 0

        def _flush() -> None:
            nonlocal flush_n
            txt = "".join(buf).strip()
            buf.clear()
            if not txt:
                return
            mid = _det(session_uid, euid, "text", str(flush_n))
            specs.append(
                (
                    mid,
                    lambda mid, ts, txt=txt: AssistantResponse(
                        id=mid, created_at=ts, content={"assistant_response": txt}
                    ),
                )
            )
            flush_n += 1

        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text"):
                buf.append(b["text"])
            elif bt == "tool_use":
                _flush()  # preserve text→tool ordering within the turn
                tuid = str(b.get("id") or "")
                fn = b.get("name") or ""
                args = b.get("input")
                if not isinstance(args, dict):
                    args = {} if args is None else {"value": args}
                if tuid:
                    tool_names[tuid] = fn
                mid = _det(session_uid, "toolreq", tuid or f"{euid}:{len(specs)}")
                specs.append(
                    (
                        mid,
                        lambda mid, ts, fn=fn, args=args: ActionRequest(
                            id=mid, created_at=ts, content={"function": fn, "arguments": args}
                        ),
                    )
                )
            # thinking blocks carry no display value in the studio reader — skip.
        _flush()

    return [builder(mid, base + i * 1e-3) for i, (mid, builder) in enumerate(specs)]


async def mirror_session(
    db: StateDB,
    *,
    session_uid: str,
    events: list[dict[str, Any]],
    tool_names: dict[str, str],
    project: str | None = None,
    project_source: str | None = None,
    model: str | None = None,
    provider: str | None = "anthropic",
    name: str | None = None,
    status: str = "running",
) -> int:
    """Idempotently write a batch of Claude events for one session; returns msgs written.

    Re-calling with already-seen events is a no-op: message ids are deterministic
    (upsert), and progression appends dedupe. Creates the session/branch on first
    call with a rich row (project, model, agent_name) so it groups correctly in
    the runs explorer, with ``status`` as the initial status. Live/idle transitions
    are owned by ``reconcile_session_status``, not this writer.
    """
    sid = session_db_id(session_uid)
    branch_id = _det(session_uid, "branch")
    bprog = _det(session_uid, "bprog")
    sprog = _det(session_uid, "sprog")

    messages: list[RoledMessage] = []
    for ev in events:
        messages.extend(messages_for_event(ev, session_uid, tool_names))

    existing = await db.get_session(sid)
    if existing is None and not messages:
        return 0

    first_ts = min((m.created_at for m in messages), default=None)
    last_ts = max((m.created_at for m in messages), default=None)
    created_at = (existing.get("created_at") if existing is not None else None) or first_ts

    # Ensure the full scaffold (progressions -> session -> branch) exists on every
    # call, in dependency order. Each write is INSERT OR IGNORE, so once present
    # this is a no-op; if an earlier pass died mid-scaffold (e.g. the branch write
    # raised after the session row committed) the next pass repairs the partial
    # instead of skipping scaffolding just because the session row now exists.
    await db.create_progression(sprog)
    await db.create_progression(bprog)
    if existing is None:
        await db.create_session(
            {
                "id": sid,
                "created_at": created_at,
                "progression_id": sprog,
                "name": name or "Claude Code session",
                "status": status,
                "invocation_kind": "agent",
                "agent_name": "claude-code",
                "model": model,
                "provider": provider,
                "project": project,
                "project_source": project_source,
                "started_at": first_ts,
                "updated_at": last_ts,
            }
        )
    elif project and not existing.get("project"):
        # Backfill attribution for a session first mirrored before its cwd could
        # be attributed to a project. INSERT OR IGNORE never updates an existing
        # row, so without this an already-seen "(no project)" session stays that
        # way forever; this writes it without disturbing the liveness clock.
        await db.set_session_provenance(sid, project=project, project_source=project_source)
    await db.create_branch(
        {
            "id": branch_id,
            "created_at": created_at,
            "session_id": sid,
            "progression_id": bprog,
            "model": model,
            "provider": provider,
            "agent_name": "claude-code",
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
    session_uid: str,
    *,
    now: float,
    live_window: float,
) -> None:
    """Align a mirrored session's status with its live/idle state — both directions.

    A mirror session's ``completed`` means dormant, not terminal: when the
    transcript resumes, the next reconcile brings it back to ``running``. Liveness
    is judged by ``last_message_at`` — the timestamp of the newest mirrored
    message — so an idle session converges to ``completed`` before the reaper can
    mark it failed, and an active one shows ``running`` (a live spinner in studio
    and the VS Code extension). It must NOT read ``updated_at``: the status write
    below bumps ``updated_at``, so keying liveness off it would let a just-marked
    ``completed`` session read as fresh again on the next pass and oscillate back
    to ``running``.
    """
    from lionagi.state.reasons import RunReasons

    existing = await db.get_session(session_db_id(session_uid))
    if not existing:
        return
    live = (now - float(existing.get("last_message_at") or 0.0)) <= live_window
    desired = "running" if live else "completed"
    if existing.get("status") == desired:
        return
    await db.update_session(
        session_db_id(session_uid),
        status=desired,
        reason_code=RunReasons.STARTED_OK if desired == "running" else RunReasons.COMPLETED_OK,
    )


async def link_session_lineage(
    db: StateDB,
    *,
    child_uid: str,
    parent_uid: str,
    parent_event_uuid: str,
) -> None:
    """Record that one Claude session continues another (conversation lineage).

    A continued conversation (after compaction, ``--resume``, or a fresh window
    that picks up an earlier thread) starts a new transcript whose first message
    points, via ``parentUuid``, at the last message of the session it continues.
    When the mirror resolves that pointer to a different session it calls this to
    store a ``lineage`` link on the child's node_metadata, so studio and the VS
    Code extension can show the provenance and walk the chain back. Written
    without moving the liveness clock; idempotent (re-linking rewrites the same
    value).
    """
    child_sid = session_db_id(child_uid)
    existing = await db.get_session(child_sid)
    if existing is None:
        return
    meta = dict(existing.get("node_metadata") or {})
    meta["lineage"] = {
        "parent_session_id": session_db_id(parent_uid),
        "parent_session_uid": parent_uid,
        "parent_event_uuid": parent_event_uuid,
    }
    await db.set_session_provenance(child_sid, node_metadata=meta)
