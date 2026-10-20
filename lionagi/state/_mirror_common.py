# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared write-side helpers for the transcript mirrors (Claude Code, Codex).

Both mirrors tail an external tool's transcript into StateDB, so status
reconciliation and lineage linking are identical once the session id is known.

This module also owns the bounded-preview + source-pointer codec: mirror rows
store a bounded display preview in ``messages.content`` plus a versioned byte
pointer into the source transcript in ``messages.node_metadata.mirror_source``,
so the pointer allows deterministic recovery of the full content without
duplicating it in sqlite. See ``docs/internals/state-transcript-mirrors.md`` for the
normative contract.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from lionagi.ln import compute_hash

if TYPE_CHECKING:
    from .db import StateDB

__all__ = (
    "MIRROR_PROVIDER_ERROR_KEY",
    "reconcile_status",
    "link_lineage",
    "MirrorKind",
    "MirrorSourcePointer",
    "SourceLine",
    "ResolutionStatus",
    "ResolvedContent",
    "canonical_json",
    "content_sha256",
    "bound_mirror_content",
    "resolve_mirrored_content",
    "MIRROR_WRITE_CHUNK",
    "write_mirrored_messages",
    "name_answered_results",
)

MirrorKind = Literal["claude_jsonl", "codex_jsonl"]

_POINTER_KIND = "mirror_jsonl_v1"
MIRROR_PROVIDER_ERROR_KEY = "mirror_provider_error"

_RETRYABLE_PROVIDER_ERROR_KINDS = frozenset({"rate_limit", "server_error"})

# Function-name preview never exceeds this even when the char budget is larger.
_FUNCTION_PREVIEW_CAP = 128

# Messages per transaction when a mirror pass writes a batch: bounds how long one batch holds the
# writer while keeping a string-id progression's rewrite to once per chunk.
MIRROR_WRITE_CHUNK = 500


class MirrorSourcePointer(TypedDict):
    pointer_kind: Literal["mirror_jsonl_v1"]
    source_kind: MirrorKind
    source_path: str
    source_offset: int
    source_byte_count: int
    source_sha256: str
    source_session_uid: str
    message_id: str
    content_sha256: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class SourceLine:
    value: dict[str, object]
    source_path: str
    source_offset: int
    source_byte_count: int
    source_sha256: str


ResolutionStatus = Literal["legacy", "resolved", "preview"]


@dataclass(frozen=True, slots=True)
class ResolvedContent:
    content: dict[str, object]
    status: ResolutionStatus
    reason: str | None = None


def canonical_json(value: object) -> str:
    """Deterministic JSON for hashing: UTF-8, sorted keys, compact separators."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(content: object) -> str:
    return compute_hash(canonical_json(content), max_bytes=None)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    limit = max(limit, 0)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _bound_content(content: dict[str, object], max_chars: int) -> tuple[dict[str, object], bool]:
    """Apply the per-message-class preview rule the mirrors doc describes.

    Message classes are discriminated by which fields are present rather than
    an exact key-set match: ``RoledMessage.to_dict(mode="db")`` includes a
    content class's untouched-default fields (e.g. ActionResponse always
    carries ``arguments: {}``) and omits ``None`` fields, so the live shape
    varies per instance even though the mirror only ever populates a fixed
    subset. Fields outside the ones this function bounds are passed through
    unchanged, never dropped.
    """
    keys = set(content.keys())

    if keys == {"instruction"}:
        text, trunc = _truncate(str(content.get("instruction") or ""), max_chars)
        return {"instruction": text}, trunc

    if keys == {"assistant_response"}:
        text, trunc = _truncate(str(content.get("assistant_response") or ""), max_chars)
        return {"assistant_response": text}, trunc

    if "function" in keys and "output" in keys:
        # ActionResponse: function + output always present; action_request_id,
        # error and arguments are optional/default and carried through as-is.
        fn_cap = min(_FUNCTION_PREVIEW_CAP, max_chars)
        fn_preview, fn_trunc = _truncate(str(content.get("function") or ""), fn_cap)
        remaining = max(max_chars - len(fn_preview), 0)
        out_preview, out_trunc = _truncate(str(content.get("output") or ""), remaining)
        bounded = dict(content)
        bounded["function"] = fn_preview
        bounded["output"] = out_preview
        return bounded, (fn_trunc or out_trunc)

    if "function" in keys and "arguments" in keys:
        fn_cap = min(_FUNCTION_PREVIEW_CAP, max_chars)
        fn_preview, fn_trunc = _truncate(str(content.get("function") or ""), fn_cap)
        remaining = max(max_chars - len(fn_preview), 0)
        args_json = canonical_json(content.get("arguments"))
        args_prefix, args_trunc = _truncate(args_json, remaining)
        bounded = dict(content)
        bounded["function"] = fn_preview
        bounded["arguments"] = {"_mirror_preview": args_prefix, "_truncated": args_trunc}
        return bounded, (fn_trunc or args_trunc)

    # Unknown mirror-produced shape: fail bounded rather than persist unbounded.
    prefix, _ = _truncate(canonical_json(content), max_chars)
    return {"_mirror_preview": prefix, "_truncated": True}, True


def bound_mirror_content(
    content: dict[str, object],
    message_id: str,
    source_line: SourceLine,
    *,
    source_kind: MirrorKind,
    source_session_uid: str,
    max_preview_chars: int,
) -> tuple[dict[str, object], MirrorSourcePointer]:
    """Bound one message's full content to a display preview and build its pointer.

    Preview slicing counts Unicode code points; the pointer's offset/byte-count
    are the exact bytes of the source JSONL record.
    """
    if max_preview_chars < 0:
        raise ValueError(f"max_preview_chars must be >= 0, got {max_preview_chars}")

    preview, truncated = _bound_content(content, max_preview_chars)
    pointer: MirrorSourcePointer = {
        "pointer_kind": _POINTER_KIND,
        "source_kind": source_kind,
        "source_path": source_line.source_path,
        "source_offset": source_line.source_offset,
        "source_byte_count": source_line.source_byte_count,
        "source_sha256": source_line.source_sha256,
        "source_session_uid": source_session_uid,
        "message_id": message_id,
        "content_sha256": content_sha256(content),
        "truncated": truncated,
    }
    return preview, pointer


def resolve_mirrored_content(
    row: dict[str, Any],
    *,
    reconstruct: Callable[[dict[str, object], str, str], dict[str, object] | None] | None = None,
) -> ResolvedContent:
    """Recover a mirrored row's full content from its source pointer, verifying
    every step. Never raises; degrades to the stored
    preview with a stable ``reason`` on any mismatch -- a stale/moved/rotated
    source must never silently return content from a different file.

    ``reconstruct(parsed_record, source_session_uid, message_id)`` re-runs
    the owning adapter's mapper and returns the derived message's full
    content dict, or None if no derived message matches ``message_id``.
    Adapters aren't wired to call this yet; it's exercised directly by tests
    and available for later integration.
    """
    stored_content = row.get("content") or {}
    node_metadata = row.get("node_metadata") or {}
    pointer = node_metadata.get("mirror_source")

    if not pointer:
        return ResolvedContent(content=stored_content, status="legacy")

    def _preview(reason: str) -> ResolvedContent:
        return ResolvedContent(content=stored_content, status="preview", reason=reason)

    if pointer.get("pointer_kind") != _POINTER_KIND:
        return _preview("unsupported_pointer")

    try:
        offset = int(pointer["source_offset"])
        byte_count = int(pointer["source_byte_count"])
        path_str = str(pointer["source_path"])
    except (KeyError, TypeError, ValueError):
        return _preview("unsupported_pointer")

    if offset < 0 or byte_count < 0 or not Path(path_str).is_absolute():
        return _preview("unsupported_pointer")

    path = Path(path_str)
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read(byte_count)
    except FileNotFoundError:
        return _preview("source_missing")
    except OSError:
        return _preview("source_unreadable")

    if len(raw) != byte_count:
        return _preview("short_read")

    if compute_hash(raw, max_bytes=None) != pointer.get("source_sha256"):
        return _preview("source_replaced")

    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _preview("invalid_json")
    if not isinstance(record, dict):
        return _preview("invalid_json")

    if reconstruct is None:
        return _preview("no_reconstructor")

    source_session_uid = str(pointer.get("source_session_uid") or "")
    message_id = str(pointer.get("message_id") or "")
    try:
        reconstructed = reconstruct(record, source_session_uid, message_id)
    except Exception:
        return _preview("content_mismatch")

    if reconstructed is None:
        return _preview("no_message_match")

    if content_sha256(reconstructed) != pointer.get("content_sha256"):
        return _preview("content_mismatch")

    return ResolvedContent(content=reconstructed, status="resolved")


async def reconcile_status(
    db: StateDB,
    sid: str,
    *,
    now: float,
    live_window: float,
    actor: str,
) -> bool:
    """Align a mirrored session's status with its liveness and attested provider errors.
    Liveness keys off ``last_message_at``, never ``updated_at`` — see docs/internals/runtime.md.

    Returns ``True`` when an unchanged transcript needs no further status read:
    the row is absent, or it is idle and already terminal / was made terminal.
    Returns ``False`` while the session is live, and after a lost idle-status
    CAS, so the polling mirror keeps observing until one final idle transition
    succeeds.  The return value is process-local scheduling evidence, never a
    persisted liveness fact.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES
    from lionagi.state.reasons import RunReasons

    existing = await db.get_session(sid)
    if not existing:
        return True
    live = (now - float(existing.get("last_message_at") or 0.0)) <= live_window
    previous = existing.get("status")
    previous_terminal = previous in SESSION_TERMINAL_STATUSES
    if previous_terminal and not live:
        return True

    desired = "running" if live else "completed"
    reason_code = RunReasons.STARTED_OK if live else RunReasons.COMPLETED_OK
    reason_summary = "mirror session became idle"
    if not live:
        session_metadata = existing.get("node_metadata")
        marker = (
            session_metadata.get(MIRROR_PROVIDER_ERROR_KEY)
            if isinstance(session_metadata, dict)
            else None
        )
        if marker is None:
            progression_id = existing.get("progression_id")
            message_ids = await db.get_progression(progression_id) if progression_id else []
            final_message = await db.get_message(message_ids[-1]) if message_ids else None
            message_metadata = final_message.get("node_metadata") if final_message else None
            marker = (
                message_metadata.get(MIRROR_PROVIDER_ERROR_KEY)
                if isinstance(message_metadata, dict)
                else None
            )
        if isinstance(marker, dict):
            error_kind = str(marker.get("error") or "provider_error")
            status = marker.get("status")
            retryable = error_kind in _RETRYABLE_PROVIDER_ERROR_KINDS or (
                isinstance(status, int) and (status == 429 or status >= 500)
            )
            desired = "failed"
            reason_code = (
                RunReasons.FAILED_PROVIDER_RETRYABLE
                if retryable
                else RunReasons.FAILED_PROVIDER_NONRETRYABLE
            )
            reason_summary = f"mirror provider error: {error_kind}"

    if previous == desired:
        return not live

    reactivating = previous_terminal and desired == "running"
    if reactivating:
        reason_summary = "mirror session reactivated because transcript resumed within live_window"
    updated = await db.update_status(
        "session",
        sid,
        new_status=desired,
        reason_code=reason_code,
        reason_summary=reason_summary,
        evidence_refs=[{"kind": "session", "id": sid}],
        source="system",
        actor=actor,
        expected_statuses={previous},
        expected_updated_at=existing.get("updated_at"),
        override=reactivating,
        override_actor=actor if reactivating else None,
        override_justification=(
            "mirror session terminal reactivation: transcript resumed within live_window"
            if reactivating
            else None
        ),
        # A reactivated row is running again: the terminal stamps must not
        # survive, or every listing reads it as "running yet ended days ago"
        # and elapsed-time surfaces keep growing from the stale end mark. The
        # end's provenance is one of those stamps and clears with it, since a
        # row with no end cannot have an approximate one.
        extra_fields=(
            {"ended_at": None, "duration_ms": None, "ended_at_is_approximate": 0}
            if reactivating
            else None
        ),
    )
    return bool(updated) and not live


async def link_lineage(
    db: StateDB,
    *,
    child_sid: str,
    parent_sid: str,
    parent_uid: str,
    parent_event_uuid: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record that one mirrored session continues another, on the child's node_metadata.
    Idempotent: the lineage entry is rewritten wholesale rather than appended."""
    existing = await db.get_session(child_sid)
    if existing is None:
        return
    meta = dict(existing.get("node_metadata") or {})
    lineage: dict[str, Any] = {
        "parent_session_id": parent_sid,
        "parent_session_uid": parent_uid,
        "parent_event_uuid": parent_event_uuid,
    }
    if extra:
        lineage.update(extra)
    meta["lineage"] = lineage
    await db.set_session_provenance(child_sid, node_metadata=meta)


async def name_answered_results(db: StateDB, rows: Sequence[dict[str, Any]]) -> None:
    """Give each tool result row with no function name the name of the tool use it answers.

    A mirror's tool-name map holds the uses still waiting for a result, so a second result for a
    use already answered finds no name there, and its row, sharing the first result's id, would
    replace that row with an empty name. (A map loaded from an earlier release's key also holds
    answered uses, which stay until a repeat of their result consumes them.) The name comes from
    the use's own row instead: one in this batch, else the stored one. A result whose use is in
    neither keeps the empty name, and so does one whose stored row with that id is not a use. The
    batch's stored lookups are read together, one query per 500 ids, not one read per result."""
    # A stored row lends its name only when it is a use row, and the row is judged two ways. Both
    # mirrors write their rows through ``to_dict(mode="db")``, which records the class in
    # ``node_metadata.lion_class``; ``insert_messages`` resolves that to a ``message_types`` row
    # and the readers return it as ``lion_class_str``, so a mirrored use row reads back as
    # ``lionagi.protocols.messages.action_request.ActionRequest`` and a result row as
    # ``...action_response.ActionResponse``. A recorded class that is not ActionRequest is refused
    # outright: an ActionResponse whose ``output`` is None keeps its default ``arguments: {}``
    # once the omitted field is dropped, so it would pass a field-shape test. Rows with no
    # recorded class (an older or non-mirror writer; StateDB reads one back as its placeholder
    # type, ``__unknown__``) fall back to the field shape, as
    # ``_bound_content`` tells the classes apart: a use carries function and arguments without
    # output, a result carries output. The field test is kept for the unclassed row alone; a
    # recorded class always wins over it.
    use_class = "lionagi.protocols.messages.action_request.ActionRequest"
    unclassed = (None, "", "__unknown__")
    # Rows are told apart by their fields, as _bound_content does: a result carries both
    # function and output, a use function and arguments without output.
    requested: dict[str, Any] = {}
    for row in rows:
        content = row.get("content")
        if (
            isinstance(content, dict)
            and "function" in content
            and "arguments" in content
            and "output" not in content
        ):
            requested[row["id"]] = content["function"]
    unresolved: list[str] = []
    for row in rows:
        content = row.get("content")
        if not isinstance(content, dict) or "output" not in content or content.get("function"):
            continue
        link = content.get("action_request_id")
        if not isinstance(link, str) or not link:
            continue
        if requested.get(link) is None:
            unresolved.append(link)
    stored = await db.get_messages(list(dict.fromkeys(unresolved))) if unresolved else {}
    for row in rows:
        content = row.get("content")
        if not isinstance(content, dict) or "output" not in content or content.get("function"):
            continue
        link = content.get("action_request_id")
        if not isinstance(link, str) or not link:
            continue
        name = requested.get(link)
        if name is None:
            stored_row = stored.get(link)
            stored_content = stored_row.get("content") if stored_row is not None else None
            recorded_class = stored_row.get("lion_class_str") if stored_row is not None else None
            if (
                isinstance(stored_content, dict)
                and (recorded_class in unclassed or recorded_class == use_class)
                and "arguments" in stored_content
                and "output" not in stored_content
            ):
                name = stored_content.get("function")
        if isinstance(name, str) and name:
            content["function"] = name


async def write_mirrored_messages(
    db: StateDB,
    rows: Sequence[dict[str, Any]],
    *,
    progression_ids: Sequence[str],
    session_id: str,
    activity_at: float | None,
) -> None:
    """Persist one mirrored batch, rewriting a string-id progression once per chunk rather than
    once per message. The session's activity clock moves once, with the last chunk, as the
    per-message loop moved it once after its last message. Naming a repeated result
    (``name_answered_results``) is the mirror's job and runs before this, ahead of the
    source pointer's content hash."""
    for start in range(0, len(rows), MIRROR_WRITE_CHUNK):
        chunk = rows[start : start + MIRROR_WRITE_CHUNK]
        last = start + MIRROR_WRITE_CHUNK >= len(rows)
        await db.insert_messages(
            chunk,
            progression_ids=progression_ids,
            session_id=session_id if last else None,
            activity_at=activity_at if last else None,
        )


async def _prepare_mirrored_rows(
    db: StateDB,
    messages: Sequence[Any],
    message_sources: Sequence[SourceLine | None],
    *,
    source_kind: MirrorKind,
    source_session_uid: str,
    max_preview_chars: int | None,
) -> list[dict[str, Any]]:
    """The stored rows for one mirrored batch, ready for ``write_mirrored_messages``: each
    message in db form, its repeated results named, and, when a preview bound is set and the
    message has a source line, its content bounded behind a pointer to that line."""
    entries: list[tuple[dict[str, Any], SourceLine | None]] = []
    for m, src in zip(messages, message_sources, strict=False):
        md = m.to_dict(mode="db")
        entries.append((md, src))
    # Name each repeated result before its content is bounded: the source pointer's
    # content hash must cover the row as stored, name included, or a reader reconstructing
    # the unnamed ActionResponse would mismatch and fall back to the stored preview.
    await name_answered_results(db, [md for md, _ in entries])
    written: list[dict[str, Any]] = []
    for md, src in entries:
        if max_preview_chars is not None and src is not None:
            preview, pointer = bound_mirror_content(
                md["content"],
                md["id"],
                src,
                source_kind=source_kind,
                source_session_uid=source_session_uid,
                max_preview_chars=max_preview_chars,
            )
            md["content"] = preview
            nm = dict(md.get("node_metadata") or {})
            nm["mirror_source"] = pointer
            md["node_metadata"] = nm
        written.append(md)
    return written
