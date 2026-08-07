# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Mirror Claude Code session transcripts (~/.claude/projects/*.jsonl) into StateDB,
one lionagi message per JSONL event, under deterministic ids. See docs/internals/runtime.md."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lionagi.protocols.messages.action_request import ActionRequest
from lionagi.protocols.messages.action_response import ActionResponse
from lionagi.protocols.messages.assistant_response import AssistantResponse
from lionagi.protocols.messages.instruction import Instruction

from ._mirror_common import SourceLine, bound_mirror_content

if TYPE_CHECKING:
    from lionagi.protocols.messages.message import RoledMessage

    from .db import StateDB

# Fixed namespace so ids derived from a Claude session/event are stable across
# mirror restarts — the basis for idempotent, resumable writes.
_NS = uuid.UUID("5f1d6e2a-1c3b-4a5d-8e9f-0a1b2c3d4e5f")

# Only conversation-bearing events become messages; the rest is editor metadata.
_MESSAGE_TYPES = frozenset({"user", "assistant"})

# Slash-command/local-command output wraps its text in these tags — editor
# machinery, not conversation — so it is dropped from the mirrored transcript.
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
    """Map one Claude JSONL event to ordered lionagi messages. ``tool_names`` is
    read/written in place so a matching tool_result can label its ActionResponse."""
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
    cwd: str | None = None,
    source_path: str | None = None,
    event_sources: list[tuple[int, int, str]] | None = None,
    max_preview_chars: int | None = None,
) -> int:
    """Idempotently write a batch of Claude events for one session; returns msgs written.
    Live/idle transitions are owned by ``reconcile_session_status``, not this writer.

    ``event_sources`` is the per-event ``(byte_offset, byte_count, sha256)`` of each
    raw JSONL line in ``events`` (same order/length), and ``source_path`` is the
    transcript file they came from. When both are given together with
    ``max_preview_chars``, every message's content is bounded via
    ``bound_mirror_content`` before it is written, with a resolvable source pointer
    on ``node_metadata.mirror_source``. Omitting them keeps the legacy unbounded
    write, for callers with no live transcript file behind the events.

    ``cwd`` is the transcript's own working directory -- the CLI's artifact root
    (issue #2848) -- written to ``artifacts_path`` on create, and backfilled on an
    existing row that lacks one without ever overwriting one that is already set.
    """
    sid = session_db_id(session_uid)
    branch_id = _det(session_uid, "branch")
    bprog = _det(session_uid, "bprog")
    sprog = _det(session_uid, "sprog")

    messages: list[RoledMessage] = []
    message_sources: list[SourceLine | None] = []
    for idx, ev in enumerate(events):
        produced = messages_for_event(ev, session_uid, tool_names)
        src: SourceLine | None = None
        if produced and event_sources is not None and idx < len(event_sources):
            offset, byte_count, sha = event_sources[idx]
            src = SourceLine(
                value=ev,
                source_path=source_path or "",
                source_offset=offset,
                source_byte_count=byte_count,
                source_sha256=sha,
            )
        messages.extend(produced)
        message_sources.extend([src] * len(produced))

    existing = await db.get_session(sid)
    if existing is None and not messages:
        return 0

    first_ts = min((m.created_at for m in messages), default=None)
    last_ts = max((m.created_at for m in messages), default=None)
    created_at = (existing.get("created_at") if existing is not None else None) or first_ts

    # Scaffold (progressions -> session -> branch) is INSERT OR IGNORE and re-run
    # every call, so a prior partial-scaffold failure self-repairs — see docs/internals/runtime.md.
    await db.create_progression(sprog)
    await db.create_progression(bprog)
    if existing is None:
        await db.create_session(
            {
                "id": sid,
                "cc_session_id": session_uid,
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
                "artifacts_path": cwd,
                "started_at": first_ts,
                "updated_at": last_ts,
            }
        )
    else:
        cc_session_id = session_uid if existing.get("cc_session_id") is None else None
        provenance_project = project if project and not existing.get("project") else None
        provenance_artifacts_path = cwd if cwd and not existing.get("artifacts_path") else None
        if (
            cc_session_id is not None
            or provenance_project is not None
            or provenance_artifacts_path is not None
        ):
            # Backfill attribution for an already-seen session (INSERT OR IGNORE never
            # updates); writes without disturbing the liveness clock.
            await db.set_session_provenance(
                sid,
                cc_session_id=cc_session_id,
                project=provenance_project,
                project_source=project_source if provenance_project is not None else None,
                artifacts_path=provenance_artifacts_path,
            )
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

    for m, src in zip(messages, message_sources, strict=False):
        md = m.to_dict(mode="db")
        if max_preview_chars is not None and src is not None:
            preview, pointer = bound_mirror_content(
                md["content"],
                md["id"],
                src,
                source_kind="claude_jsonl",
                source_session_uid=session_uid,
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

    return len(messages)


async def reconcile_session_status(
    db: StateDB,
    session_uid: str,
    *,
    now: float,
    live_window: float,
) -> None:
    """Align a mirrored session's status with its live/idle state, both directions.
    Liveness keys off ``last_message_at``, never ``updated_at`` — see docs/internals/runtime.md."""
    from ._mirror_common import reconcile_status

    await reconcile_status(
        db,
        session_db_id(session_uid),
        now=now,
        live_window=live_window,
        actor="claude-mirror-reconcile",
    )


async def link_session_lineage(
    db: StateDB,
    *,
    child_uid: str,
    parent_uid: str,
    parent_event_uuid: str,
) -> None:
    """Record that one Claude session continues another (conversation lineage) via
    a ``lineage`` entry on the child's node_metadata. Idempotent; see docs/internals/runtime.md."""
    from ._mirror_common import link_lineage

    await link_lineage(
        db,
        child_sid=session_db_id(child_uid),
        parent_sid=session_db_id(parent_uid),
        parent_uid=parent_uid,
        parent_event_uuid=parent_event_uuid,
    )


async def link_escalation_session(
    db: StateDB,
    *,
    session_uid: str,
    run_id: str,
    name: str,
    project: str | None,
    project_source: str | None,
    parent_op_id: str,
) -> bool:
    """Attribute a mirrored CLI transcript to the run whose escalation spawned it.

    A flow escalation (``operations.flow._schedule_escalation``) retries a node on a
    higher-tier CLI engine as an in-session child op; the engine's own transcript is
    mirrored independently (by this module, possibly in another process) under a
    session uid the mirror has no way to connect back to the run. The escalation call
    site learns that uid once the child op's branch reports it
    (``branch.chat_model.provider_session_id``) and calls this to stamp the link —
    overwriting the mirror's cwd-guessed ``project`` and first-prompt-derived ``name``,
    which are wrong for an escalation leg by construction: its transcript's ``cwd`` is
    the leg's scratch workspace, not a project, and its first prompt is the injected
    escalation guidance, not a task description. The mirror never revisits either
    field once a session row exists, so this write is never clobbered by a later sweep.

    Returns ``False`` if the mirror hasn't created the session row yet — the escalation
    call site is expected to retry for a bounded window rather than lose the link,
    since which side writes first is a race with no fixed winner.
    """
    sid = session_db_id(session_uid)
    existing = await db.get_session(sid)
    if existing is None:
        return False
    meta = dict(existing.get("node_metadata") or {})
    meta["escalated_from_session"] = parent_op_id
    fields: dict[str, Any] = {
        "name": name,
        "run_id": run_id,
        "node_metadata": json.dumps(meta),
    }
    # An unresolved run project is not evidence the mirror's cwd guess is wrong —
    # leave it alone rather than overwrite a real (if imprecise) value with NULL.
    if project:
        fields["project"] = project
        fields["project_source"] = project_source
    await db.update_session(sid, **fields)
    return True
