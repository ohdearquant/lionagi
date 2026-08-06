from __future__ import annotations

import asyncio
import base64
import json
import time
import unicodedata
from collections.abc import Mapping
from typing import Any

import aiosqlite
from fastapi import HTTPException, Query
from pydantic import BaseModel

from lionagi._errors import NotFoundError, ValidationError
from lionagi.state.claude_mirror import session_db_id
from lionagi.state.db import SESSION_TERMINAL_STATUSES

from ..registry import studio_route
from ._db import open_db as _open_db
from ._db import require_file_store, store_exists, store_path
from ._io import parse_json_col as _parse_json_col
from .artifact_verification import resolve_artifact_verification

SESSION_DONE_STABLE_SECS = 60.0

# Studio name validation bound (application_mcp.py:77 uses the same cap).
MAX_SESSION_LABEL_LENGTH = 160


def resolve_session_display_name(row: Mapping[str, Any]) -> str:
    """The single priority chain for every served session display name.

    ``user_label`` (the Studio rename) wins over the system-written
    ``name``, which wins over ``agent_name``, which wins over
    ``playbook_name``. Blank candidates (None, empty, or whitespace-only)
    are treated as absent, so a session nobody has renamed resolves exactly
    as it did before ``user_label`` existed. The last resort is an 8-char id
    prefix, matching the fallback every existing consumer already used.
    """
    for key in ("user_label", "name", "agent_name", "playbook_name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(row.get("id") or "")[:8]


def _normalize_session_label(raw: str) -> str | None:
    """Trim, cap length, and reject control characters in a rename request.

    A value that is empty after trimming means "clear the label" and
    returns ``None`` rather than raising -- clearing is not an error case.
    """
    trimmed = raw.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_SESSION_LABEL_LENGTH:
        raise ValidationError(
            f"label must be at most {MAX_SESSION_LABEL_LENGTH} characters after trimming"
        )
    if any(unicodedata.category(ch) == "Cc" for ch in trimmed):
        raise ValidationError("label must not contain control characters")
    return trimmed


def _parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    meta = _parse_json_col(raw)
    return meta if isinstance(meta, dict) else None


def _base_role(instance_name: str) -> str:
    """The role behind an instance name: "advisor-2" → "advisor".

    Only a purely numeric suffix is an instance counter — a role that itself
    ends in a word ("explorer-deep") is left alone.
    """
    stem, sep, suffix = instance_name.rpartition("-")
    if sep and stem and suffix.isdigit():
        return stem
    return instance_name


def _graph_from_metadata(raw: str | None) -> dict[str, Any] | None:
    """Build a DAG graph from session node_metadata (agents + operations)."""
    if not raw:
        return None
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    early_graph = meta.get("early_graph")
    if isinstance(early_graph, dict) and early_graph.get("nodes"):
        # Compiled workflow-exec graph already carries authored node ids +
        # edges in this shape — pass through, no re-derivation.
        return early_graph
    agents = meta.get("agents") or []
    operations = meta.get("operations") or []
    if not operations:
        return None
    agent_map = {a["id"]: a for a in agents if isinstance(a, dict) and "id" in a}
    nodes = []
    edges = []
    for op in operations:
        if not isinstance(op, dict) or "id" not in op:
            continue
        agent = agent_map.get(op.get("agent_id", ""), {})
        depends_on = op.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        nodes.append(
            {
                "id": op["id"],
                "label": op["id"],
                # Base role when recorded; older rows only carry the instance
                # name (e.g. "advisor-2"), whose numeric suffix is trimmed so
                # the role line never repeats the label above it.
                "role": agent.get("role") or _base_role(agent.get("name", "")),
                "assignment": agent.get("model", ""),
                "prompt": "",
                "capacity": 1,
                "timeout": None,
                "inputs": depends_on,
                "outputs": [],
            }
        )
        for dep in depends_on:
            edges.append(
                {
                    "id": f"e-{dep}-{op['id']}",
                    "source": dep,
                    "target": op["id"],
                    "mode": "simple",
                }
            )
    return {"nodes": nodes, "edges": edges} if nodes else None


def _format_message(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": _parse_json_col(row["content"]),
        "sender": row["sender"],
        "timestamp": row["created_at"],
        "lion_class": row["lion_class_str"] or "",
    }


# A listing whose SQL carries no LIMIT examines every session, every branch and
# every progression regardless of how few rows the caller asked for -- appending
# LIMIT to that statement does not help, because a limit bounds rows returned
# and not rows examined. So the page is chosen first, from an indexed scan of
# `sessions` alone, and only that page is joined against branches/progressions.
# Callers that want a whole-store answer must ask for it a page at a time.
MAX_SESSION_PAGE = 500


# SQLite LIKE's own wildcards, '%' and '_', are otherwise live inside a
# contains-filter value: a search for "50%" would match every row instead of
# rows containing the literal substring "50%". Escaping is applied to every
# LIKE operand this module builds, not just search — a stray '%'/'_' in a
# playbook-name filter has the same bug.
_LIKE_ESCAPE_CHAR = "\\"


def _escape_like(value: str) -> str:
    return (
        value.replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2)
        .replace("%", f"{_LIKE_ESCAPE_CHAR}%")
        .replace("_", f"{_LIKE_ESCAPE_CHAR}_")
    )


class SessionFilter:
    """Filters the runs/sessions listings share, pushed into SQL so they select
    the page rather than discard rows after the whole store has been read."""

    def __init__(
        self,
        *,
        playbook: str | None = None,
        statuses: set[str] | None = None,
        project: str | None = None,
        project_null: bool = False,
        tags: list[str] | None = None,
        search: str | None = None,
    ) -> None:
        self.playbook = playbook
        self.statuses = statuses
        self.project = project
        self.project_null = project_null
        self.tags = list(dict.fromkeys(tags)) if tags else None
        self.search = search

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.playbook:
            clauses.append(
                "LOWER(COALESCE(s.playbook_name, '')) LIKE '%' || LOWER(?) || '%' "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}'"
            )
            params.append(_escape_like(self.playbook))
        if self.search:
            escaped = _escape_like(self.search)
            clauses.append(
                "(LOWER(COALESCE(s.user_label, '')) LIKE '%' || LOWER(?) || '%' "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                "OR LOWER(COALESCE(s.name, '')) LIKE '%' || LOWER(?) || '%' "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                "OR LOWER(COALESCE(s.agent_name, '')) LIKE '%' || LOWER(?) || '%' "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}')"
            )
            params.extend([escaped, escaped, escaped])
        if self.statuses:
            ordered = sorted(self.statuses)
            placeholders = ",".join("?" for _ in ordered)
            # Legacy rows carry NULL status and read as "completed" everywhere else.
            null_clause = " OR s.status IS NULL" if "completed" in self.statuses else ""
            clauses.append(f"(COALESCE(s.status, 'completed') IN ({placeholders}){null_clause})")
            params.extend(ordered)
        if self.project_null:
            clauses.append("s.project IS NULL")
        elif self.project:
            clauses.append("s.project = ?")
            params.append(self.project)
        if self.tags:
            placeholders = ",".join("?" for _ in self.tags)
            clauses.append(
                f"s.id IN (SELECT session_id FROM run_tags WHERE tag IN ({placeholders})"  # noqa: S608
                " GROUP BY session_id HAVING COUNT(DISTINCT tag) = ?)"
            )
            params.extend([*self.tags, len(self.tags)])
        if not clauses:
            return "", []
        return "WHERE " + " AND ".join(clauses), params


async def count_sessions(where: SessionFilter | None = None) -> int:
    """Total matching sessions, without reading a single branch or progression."""
    require_file_store()
    if not store_exists():
        return 0
    clause, params = (where or SessionFilter()).where()
    async with _open_db(store_path()) as db:
        cur = await db.execute(
            f"SELECT COUNT(*) AS n FROM sessions s {clause}",  # noqa: S608
            params,
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def list_sessions(
    *,
    limit: int = MAX_SESSION_PAGE,
    offset: int = 0,
    where: SessionFilter | None = None,
) -> list[dict[str, Any]]:
    """One page of sessions, newest first. Cost is proportional to `limit`, not
    to the size of the store."""
    require_file_store()
    if not store_exists():
        return []

    limit = max(1, min(int(limit), MAX_SESSION_PAGE))
    offset = max(0, int(offset))
    clause, params = (where or SessionFilter()).where()

    async with _open_db(store_path()) as db:
        # run_tags is created lazily on first tag write, so a tag filter would
        # fail on a store that has never been tagged.
        if (where or SessionFilter()).tags:
            from .run_tags import _ensure_table

            await _ensure_table(db)
        cur = await db.execute(
            f"""
            WITH page AS (
                SELECT s.id AS page_id
                FROM sessions s
                {clause}
                ORDER BY s.updated_at DESC
                LIMIT ? OFFSET ?
            )
            SELECT
                s.id,
                s.name,
                s.user_label,
                s.created_at,
                s.updated_at,
                s.playbook_name,
                s.agent_name,
                s.invocation_kind,
                s.show_topic,
                s.show_play_name,
                s.artifacts_path,
                s.artifact_contract_json,
                s.artifact_verification_json,
                s.source_kind,
                s.status,
                s.started_at,
                s.ended_at,
                s.last_message_at,
                s.invocation_id,
                s.model,
                s.provider,
                s.effort,
                s.agent_hash,
                s.project,
                s.project_source,
                s.status_reason_code,
                s.status_reason_summary,
                s.node_metadata,
                s.input_tokens,
                s.output_tokens,
                s.total_cost_usd,
                s.num_turns,
                COUNT(DISTINCT b.id) AS branch_count,
                COALESCE(SUM(
                    json_array_length(p.collection)
                ), 0) AS message_count
            FROM page
            JOIN sessions s ON s.id = page.page_id
            LEFT JOIN branches b ON b.session_id = s.id
            LEFT JOIN progressions p ON p.id = b.progression_id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            """,  # noqa: S608
            [*params, limit, offset],
        )
        rows = await cur.fetchall()

    out = []
    for row in rows:
        entry = {
            "id": row["id"],
            "name": row["name"],
            "user_label": row["user_label"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"] or 0.0,
            "node_metadata": row["node_metadata"],
            "branch_count": row["branch_count"],
            "message_count": row["message_count"],
            # ADR-0057: read status directly from column;
            # fall back to "completed" only for legacy rows where status is NULL.
            "status": row["status"] or "completed",
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            # Caller (runs service) feeds this to staleness_check (ADR-0057 D6).
            "last_message_at": row["last_message_at"],
            # Optional parent skill orchestration.
            "invocation_id": row["invocation_id"],
            # Provenance disclosure — resolved values.
            "model": row["model"],
            "provider": row["provider"],
            "effort": row["effort"],
            "agent_hash": row["agent_hash"],
            "playbook_name": row["playbook_name"],
            "agent_name": row["agent_name"],
            "invocation_kind": row["invocation_kind"],
            "show_topic": row["show_topic"],
            "show_play_name": row["show_play_name"],
            "artifacts_path": row["artifacts_path"],
            "source_kind": row["source_kind"] or "live",
            "artifact_contract_json": _parse_json_col(row["artifact_contract_json"]),
            # Resolved, not passed through: a terminal session that was contracted
            # and holds no verdict reports that absence here exactly as the detail
            # route does. Returning the raw column instead would give the two
            # routes different answers for the same session, which is the
            # conflation this state exists to remove.
            #
            # artifacts_path is withheld deliberately, and the row does carry one.
            # Supplying it would enable the live-progress arm, which reads the
            # artifacts directory per row -- a filesystem walk for every running
            # session on a paginated list that Studio polls. Withholding it leaves
            # the two cheap arms intact (a stored verdict still wins, terminal
            # absence is still named) and declines only the live read, which
            # belongs to a single-session view.
            "artifact_verification_json": resolve_artifact_verification(
                _parse_json_col(row["artifact_verification_json"]),
                status=row["status"] or "completed",
                contract=_parse_json_col(row["artifact_contract_json"]),
                artifacts_path=None,
            ),
            # ADR-0063: project detection.
            "project": row["project"],
            "project_source": row["project_source"],
            # ADR-0057: denormalized status reason for the hot read path.
            "status_reason_code": row["status_reason_code"],
            "status_reason_summary": row["status_reason_summary"],
            # Run usage (populated at RunEnd). total_cost_usd=None means
            # unreported by the provider, never coerced to 0.0.
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_cost_usd": row["total_cost_usd"],
            "num_turns": row["num_turns"],
        }
        entry["display_name"] = resolve_session_display_name(entry)
        out.append(entry)
    return out


async def list_project_counts() -> list[dict[str, Any]]:
    """Per-project run counts via a cheap GROUP BY (no branch/message join)."""
    require_file_store()
    if not store_exists():
        return []
    async with _open_db(store_path()) as db:
        cur = await db.execute(
            """
            SELECT project,
                   COUNT(*) AS count,
                   MAX(updated_at) AS last_activity
            FROM sessions
            GROUP BY project
            """
        )
        rows = await cur.fetchall()
    return [
        {
            "project": row["project"],
            "count": row["count"],
            "last_activity": row["last_activity"],
        }
        for row in rows
    ]


# Long-lived sessions accumulate tens of thousands of messages; detail
# responses window from the tail to avoid freezing the client.
DEFAULT_MESSAGE_LIMIT = 200
MAX_MESSAGE_LIMIT = 1000


class MessageCursorError(ValueError):
    """A message_cursor is malformed, session-mismatched, or references a stale anchor."""


def _encode_message_cursor(session_id: str, limit: int, branch_anchors: dict[str, str]) -> str:
    payload = {"v": 1, "session_id": session_id, "limit": limit, "branch_anchors": branch_anchors}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_message_cursor(token: str, *, session_id: str, limit: int) -> dict[str, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception as exc:
        raise MessageCursorError(f"Malformed message_cursor: {token!r}") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise MessageCursorError(f"Unsupported message_cursor: {token!r}")
    if payload.get("session_id") != session_id:
        raise MessageCursorError("message_cursor belongs to a different session")
    if payload.get("limit") != limit:
        raise MessageCursorError("message_cursor does not match message_limit")
    anchors = payload.get("branch_anchors")
    if not isinstance(anchors, dict):
        raise MessageCursorError("message_cursor is missing branch_anchors")
    return anchors


def _window_message_ids(
    msg_ids: list[str],
    *,
    branch_id: str,
    limit: int,
    cursor_anchors: dict[str, str] | None,
    legacy_offset: int,
) -> tuple[list[str], bool, str | None]:
    """Return (window_ids, has_older, next_anchor); cursor_anchors=None means no
    cursor was passed, an anchor-less branch entry means that branch is exhausted."""
    if cursor_anchors is not None:
        anchor = cursor_anchors.get(branch_id)
        if anchor is None:
            return [], False, None
        if anchor not in msg_ids:
            raise MessageCursorError(
                f"message_cursor anchor not found in branch {branch_id!r} progression"
            )
        end = msg_ids.index(anchor)
    elif legacy_offset:
        total = len(msg_ids)
        end = max(0, total - legacy_offset)
    else:
        end = len(msg_ids)

    start = max(0, end - limit)
    window_ids = msg_ids[start:end]
    has_older = start > 0
    next_anchor = window_ids[0] if has_older and window_ids else None
    return window_ids, has_older, next_anchor


def _short_lion_class(lion_class: str) -> str:
    """Strip a fully-qualified lion_class path to its bare class name, so legacy
    short-name rows and canonical dotted-path rows compare equal."""
    return lion_class.rsplit(".", 1)[-1] if lion_class else lion_class


_ACTION_LION_CLASSES = (
    "lionagi.protocols.messages.action_request.ActionRequest",
    "lionagi.protocols.messages.action_response.ActionResponse",
    "ActionRequest",
    "ActionResponse",
)


def _init_message_stats() -> dict[str, Any]:
    return {
        "message_count": 0,
        "roles": {},
        "branches": {},
        "tool_call_count": 0,
        "error_count": 0,
        "errors": [],
        "files": [],
    }


async def _fetch_messages_by_ids(
    db: aiosqlite.Connection, msg_ids: list[str]
) -> list[dict[str, Any]]:
    """Hydrate message rows for msg_ids, chunked to stay under SQLite's bound-variable limit."""
    if not msg_ids:
        return []
    rows_by_id: dict[str, dict[str, Any]] = {}
    for chunk_start in range(0, len(msg_ids), 500):
        chunk = msg_ids[chunk_start : chunk_start + 500]
        placeholders = ",".join("?" for _ in chunk)
        cur = await db.execute(
            f"""
            SELECT m.id, m.created_at, m.content, m.sender, m.role,
                   mt.lion_class AS lion_class_str
            FROM messages m
            LEFT JOIN message_types mt ON m.lion_class = mt.type_id
            WHERE m.id IN ({placeholders})
            """,  # noqa: S608
            chunk,
        )
        for row in await cur.fetchall():
            rows_by_id[row["id"]] = _format_message(row)
    return [rows_by_id[mid] for mid in msg_ids if mid in rows_by_id]


async def _fetch_role_counts(db: aiosqlite.Connection, msg_ids: list[str]) -> dict[str, int]:
    """Role histogram over msg_ids via SQL GROUP BY — no message content is hydrated."""
    counts: dict[str, int] = {}
    if not msg_ids:
        return counts
    for chunk_start in range(0, len(msg_ids), 500):
        chunk = msg_ids[chunk_start : chunk_start + 500]
        placeholders = ",".join("?" for _ in chunk)
        cur = await db.execute(
            f"SELECT role, COUNT(*) AS n FROM messages WHERE id IN ({placeholders}) GROUP BY role",  # noqa: S608
            chunk,
        )
        for row in await cur.fetchall():
            role = row["role"] or ""
            if role:
                counts[role] = counts.get(role, 0) + row["n"]
    return counts


async def _fetch_message_bounds(
    db: aiosqlite.Connection, msg_ids: list[str]
) -> tuple[float | None, float | None]:
    """Return persisted timestamp bounds without hydrating message content."""
    if not msg_ids:
        return None, None
    cur = await db.execute(
        """SELECT MIN(m.created_at) AS first_message_at,
                  MAX(m.created_at) AS last_message_at
           FROM json_each(?) AS ids
           JOIN messages m ON m.id = ids.value""",
        (json.dumps(msg_ids),),
    )
    row = await cur.fetchone()
    if row is None:
        return None, None
    return row["first_message_at"], row["last_message_at"]


async def _fetch_action_messages(
    db: aiosqlite.Connection, msg_ids: list[str]
) -> list[dict[str, Any]]:
    """Hydrate only the ActionRequest/ActionResponse rows among msg_ids, in progression
    order — the only kinds tool/error/file aggregates need, keeping the pass cheap."""
    if not msg_ids:
        return []
    class_placeholders = ",".join("?" for _ in _ACTION_LION_CLASSES)
    cur = await db.execute(
        f"SELECT type_id, lion_class FROM message_types WHERE lion_class IN ({class_placeholders})",  # noqa: S608
        _ACTION_LION_CLASSES,
    )
    lion_class_by_type_id = {row["type_id"]: row["lion_class"] for row in await cur.fetchall()}
    if not lion_class_by_type_id:
        return []

    rows_by_id: dict[str, dict[str, Any]] = {}
    type_ids = list(lion_class_by_type_id)
    type_placeholders = ",".join("?" for _ in type_ids)
    for chunk_start in range(0, len(msg_ids), 500):
        chunk = msg_ids[chunk_start : chunk_start + 500]
        placeholders = ",".join("?" for _ in chunk)
        # `+m.lion_class` disqualifies the lion_class index so the planner probes
        # the id primary key for the IN list instead of rescanning every
        # action-class row in the whole table per chunk (minutes of I/O at scale).
        cur = await db.execute(
            f"""
            SELECT m.id, m.created_at, m.content, m.sender, m.role, m.lion_class
            FROM messages m
            WHERE m.id IN ({placeholders}) AND +m.lion_class IN ({type_placeholders})
            """,  # noqa: S608
            [*chunk, *type_ids],
        )
        for row in await cur.fetchall():
            data = dict(row)
            data["lion_class_str"] = lion_class_by_type_id.get(data.pop("lion_class"))
            rows_by_id[data["id"]] = _format_message(data)
    return [rows_by_id[mid] for mid in msg_ids if mid in rows_by_id]


def _branch_message_stats(
    message_count: int,
    roles: dict[str, int],
    action_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Full-branch stats over the full progression, never a display window."""
    from .runs import _detect_status

    response_by_id: dict[str, dict[str, Any]] = {
        m["id"]: m
        for m in action_messages
        if _short_lion_class(m.get("lion_class", "")) == "ActionResponse"
    }

    tool_call_count = 0
    error_count = 0
    errors: list[dict[str, Any]] = []
    files: set[str] = set()
    for m in action_messages:
        if _short_lion_class(m.get("lion_class", "")) != "ActionRequest":
            continue

        content = m.get("content") if isinstance(m.get("content"), dict) else {}
        tool_call_count += 1
        function = content.get("function") or ""
        arguments = content.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        tool_name = str(function).lower().replace("-", "_").rsplit("__", 1)[-1].rsplit(".", 1)[-1]
        if not tool_name or tool_name in {
            "read",
            "read_file",
            "write",
            "write_file",
            "edit",
            "edit_file",
            "multiedit",
            "notebookedit",
        }:
            file_path = arguments.get("file_path") or arguments.get("path")
            if isinstance(file_path, str) and file_path:
                files.add(file_path)

        response_id = content.get("action_response_id")
        response_msg = response_by_id.get(response_id) if response_id else None
        output_text = ""
        if response_msg and isinstance(response_msg.get("content"), dict):
            output_text = str(response_msg["content"].get("output", ""))
        status, _exit_code = _detect_status(output_text, function)
        if status == "error":
            error_count += 1
            errors.append(
                {
                    "function": function,
                    "sender": m.get("sender", ""),
                    "timestamp": m.get("timestamp"),
                    "output": output_text,
                }
            )

    return {
        "message_count": message_count,
        "roles": roles,
        "tool_call_count": tool_call_count,
        "error_count": error_count,
        "errors": errors,
        "files": sorted(files),
    }


async def get_session(
    session_id: str,
    *,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    message_offset: int = 0,
    message_cursor: str | None = None,
) -> dict[str, Any] | None:
    require_file_store()
    if not store_exists():
        return None

    message_limit = max(1, min(message_limit, MAX_MESSAGE_LIMIT))
    message_offset = max(0, message_offset)
    cursor_anchors = (
        _decode_message_cursor(message_cursor, session_id=session_id, limit=message_limit)
        if message_cursor
        else None
    )

    async with _open_db(store_path()) as db:
        cur = await db.execute(
            # Include lifecycle and provenance columns (model/provider/effort/agent_hash).
            """SELECT id, name, user_label, created_at, updated_at,
                      playbook_name, agent_name, invocation_kind,
                      show_topic, show_play_name, artifacts_path,
                      artifact_contract_json, artifact_verification_json,
                      source_kind, status, started_at, ended_at, last_message_at,
                      model, provider, effort, agent_hash, invocation_id,
                      node_metadata, project, project_source,
                      status_reason_code, status_reason_summary, status_evidence_refs,
                      input_tokens, output_tokens, total_cost_usd, num_turns
               FROM sessions WHERE id = ?""",
            (session_id,),
        )
        session_row = await cur.fetchone()
        if not session_row:
            return None

        play_cur = await db.execute(
            """SELECT sh.topic AS show_topic, p.name AS play_name
               FROM plays p
               JOIN shows sh ON sh.id = p.show_id
               WHERE p.session_id = ?
               LIMIT 1""",
            (session_id,),
        )
        play_row = await play_cur.fetchone()
        source_show = (
            {"topic": play_row["show_topic"], "play_name": play_row["play_name"]}
            if play_row
            else None
        )

        try:
            branch_cur = await db.execute(
                "SELECT id, name, created_at, progression_id, model, provider, agent_name, status, started_at, ended_at,"
                " input_tokens, output_tokens, total_cost_usd, num_turns"
                " FROM branches WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            )
        except Exception:
            branch_cur = await db.execute(
                "SELECT id, name, created_at, progression_id, model, provider, agent_name FROM branches WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            )
        branch_rows = await branch_cur.fetchall()

        branches = []
        full_stats = _init_message_stats()
        next_branch_anchors: dict[str, str] = {}
        for br in branch_rows:
            branch_id = br["id"]
            full_msg_ids: list[str] = []
            message_total = 0
            prog_id = br["progression_id"]
            if prog_id:
                prog_cur = await db.execute(
                    "SELECT collection FROM progressions WHERE id = ?",
                    (prog_id,),
                )
                prog_row = await prog_cur.fetchone()
                if prog_row and prog_row["collection"]:
                    try:
                        full_msg_ids = json.loads(prog_row["collection"])
                    except (json.JSONDecodeError, TypeError):
                        full_msg_ids = []
                    message_total = len(full_msg_ids)

            # Window from the tail: offset/cursor 0 = the newest page,
            # each page further back prepends older history.
            window_ids, has_older, next_anchor = _window_message_ids(
                full_msg_ids,
                branch_id=branch_id,
                limit=message_limit,
                cursor_anchors=cursor_anchors,
                legacy_offset=message_offset if cursor_anchors is None else 0,
            )
            if next_anchor:
                next_branch_anchors[branch_id] = next_anchor

            window_messages = await _fetch_messages_by_ids(db, window_ids)
            by_id = {m["id"]: m for m in window_messages}
            messages = [by_id[mid] for mid in window_ids if mid in by_id]

            role_counts = await _fetch_role_counts(db, full_msg_ids)
            first_message_at, last_message_at = await _fetch_message_bounds(db, full_msg_ids)
            action_messages = await _fetch_action_messages(db, full_msg_ids)
            # message_count is the DB role-aggregate, not message_total: a
            # progression can reference ids whose row was pruned, so the two can diverge.
            message_count = sum(role_counts.values())
            branch_stats = _branch_message_stats(message_count, role_counts, action_messages)

            full_stats["message_count"] += branch_stats["message_count"]
            for role, count in branch_stats["roles"].items():
                full_stats["roles"][role] = full_stats["roles"].get(role, 0) + count
            full_stats["branches"][branch_id] = {
                "message_count": branch_stats["message_count"],
                "roles": branch_stats["roles"],
            }
            full_stats["tool_call_count"] += branch_stats["tool_call_count"]
            full_stats["error_count"] += branch_stats["error_count"]
            full_stats["errors"].extend(branch_stats["errors"])
            full_stats["files"].extend(branch_stats["files"])

            br_keys = br.keys()
            branch_started_at = br["started_at"] if "started_at" in br_keys else None
            branch_ended_at = br["ended_at"] if "ended_at" in br_keys else None
            branch_duration_ms = (
                (branch_ended_at - branch_started_at) * 1000
                if branch_started_at is not None and branch_ended_at is not None
                else None
            )
            branches.append(
                {
                    "id": branch_id,
                    "name": br["name"],
                    "created_at": br["created_at"],
                    "messages": messages,
                    "message_total": message_total,
                    "message_offset": message_offset,
                    "message_limit": message_limit,
                    "message_window_count": len(messages),
                    "messages_truncated": message_total > len(messages),
                    "message_has_older": has_older,
                    "message_stats": full_stats["branches"][branch_id],
                    "first_message_at": first_message_at,
                    "last_message_at": last_message_at,
                    "model": br["model"],
                    "provider": br["provider"],
                    "agent_name": br["agent_name"],
                    "status": br["status"] if "status" in br_keys else None,
                    "started_at": branch_started_at,
                    "ended_at": branch_ended_at,
                    # Run usage (populated at BRANCH_END). total_cost_usd=None
                    # means unreported by the provider, never coerced to 0.0.
                    "input_tokens": br["input_tokens"] if "input_tokens" in br_keys else None,
                    "output_tokens": br["output_tokens"] if "output_tokens" in br_keys else None,
                    "total_cost_usd": br["total_cost_usd"] if "total_cost_usd" in br_keys else None,
                    "num_turns": br["num_turns"] if "num_turns" in br_keys else None,
                    "duration_ms": branch_duration_ms,
                }
            )

        full_stats["files"] = sorted(set(full_stats["files"]))
        message_next_cursor = (
            _encode_message_cursor(session_id, message_limit, next_branch_anchors)
            if next_branch_anchors
            else None
        )

    started_at = session_row["started_at"]
    ended_at = session_row["ended_at"]
    duration_ms = (
        (ended_at - started_at) * 1000 if started_at is not None and ended_at is not None else None
    )
    status = session_row["status"] or "completed"
    artifact_contract = _parse_json_col(session_row["artifact_contract_json"])
    stored_verification = _parse_json_col(session_row["artifact_verification_json"])

    artifact_verification = resolve_artifact_verification(
        stored_verification,
        status=status,
        contract=artifact_contract,
        artifacts_path=session_row["artifacts_path"],
    )

    detail = {
        "id": session_row["id"],
        "name": session_row["name"],
        "user_label": session_row["user_label"],
        "created_at": session_row["created_at"],
        "updated_at": session_row["updated_at"],
        "playbook_name": session_row["playbook_name"],
        "agent_name": session_row["agent_name"],
        "invocation_kind": session_row["invocation_kind"],
        "show_topic": session_row["show_topic"],
        "show_play_name": session_row["show_play_name"],
        "artifacts_path": session_row["artifacts_path"],
        "artifact_contract_json": artifact_contract,
        "artifact_verification_json": artifact_verification,
        "source_kind": session_row["source_kind"] or "live",
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        # Run usage (populated at RunEnd). total_cost_usd=None means
        # unreported by the provider, never coerced to 0.0.
        "input_tokens": session_row["input_tokens"],
        "output_tokens": session_row["output_tokens"],
        "total_cost_usd": session_row["total_cost_usd"],
        "num_turns": session_row["num_turns"],
        # Full-session aggregate, not derived from the windowed page.
        "last_message_at": session_row["last_message_at"],
        "source_show": source_show,
        "branches": branches,
        "message_limit": message_limit,
        "message_cursor": message_cursor,
        "message_next_cursor": message_next_cursor,
        "message_stats": full_stats,
        # Provenance disclosure — same fields exposed on list_sessions().
        "model": session_row["model"],
        "provider": session_row["provider"],
        "effort": session_row["effort"],
        "agent_hash": session_row["agent_hash"],
        "invocation_id": session_row["invocation_id"],
        # ADR-0063: project detection.
        "project": session_row["project"],
        "project_source": session_row["project_source"],
        # ADR-0057: status reason surfaced on detail (drives the failure banner).
        "status_reason_code": session_row["status_reason_code"],
        "status_reason_summary": session_row["status_reason_summary"],
        "status_evidence_refs": _parse_json_col(session_row["status_evidence_refs"]),
        "graph": _graph_from_metadata(session_row["node_metadata"]),
        "segments": (_parse_metadata(session_row["node_metadata"]) or {}).get("segments"),
        # Resume lineage: the session this run was resumed from, so the UI can
        # present attempts of one run together instead of as strangers.
        "resumed_from": (_parse_metadata(session_row["node_metadata"]) or {}).get("resumed_from"),
        # Raw node_metadata (carries pid/pid_create_time) so callers like
        # get_run()'s liveness check can find the recorded pid.
        "node_metadata": session_row["node_metadata"],
    }
    detail["display_name"] = resolve_session_display_name(detail)
    return detail


async def get_session_by_cc_id(cc_uid: str) -> dict[str, Any] | None:
    """Return a mirrored Claude Code session, including legacy unbackfilled rows."""
    require_file_store()
    if not store_exists():
        return None

    async with _open_db(store_path()) as db:
        cur = await db.execute(
            "SELECT id FROM sessions WHERE cc_session_id = ? LIMIT 1",
            (cc_uid,),
        )
        row = await cur.fetchone()

    return await get_session(row["id"] if row else session_db_id(cc_uid))


async def get_session_messages_after(session_id: str, after_ts: float) -> list[dict[str, Any]]:
    """Poll-friendly tail read for the SSE stream/signals endpoints. Joins via
    json_each rather than binding every message id into an IN (...) clause,
    which would blow past SQLite's 999 bound-variable limit at scale."""
    if not store_exists():
        return []

    async with _open_db(store_path()) as db:
        cur = await db.execute(
            """
            SELECT m.id, m.created_at, m.content, m.sender, m.role,
                   mt.lion_class AS lion_class_str, b.id AS branch_id
            FROM branches b
            JOIN progressions p ON p.id = b.progression_id
            JOIN json_each(p.collection) je ON 1=1
            JOIN messages m ON m.id = je.value
            LEFT JOIN message_types mt ON m.lion_class = mt.type_id
            WHERE b.session_id = ? AND m.created_at > ?
            ORDER BY m.created_at
            """,
            (session_id, after_ts),
        )
        rows = await cur.fetchall()

    result = []
    for row in rows:
        msg = _format_message(row)
        msg["branch_id"] = row["branch_id"]
        result.append(msg)
    return result


async def session_exists(session_id: str) -> bool:
    require_file_store()
    if not store_exists():
        return False

    async with _open_db(store_path()) as db:
        cur = await db.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
        return row is not None


async def get_session_stream_state(session_id: str) -> dict[str, Any] | None:
    """Scalar read for the SSE done-condition check — avoids the full get_session() round-trip."""
    if not store_exists():
        return None

    async with _open_db(store_path()) as db:
        cur = await db.execute(
            "SELECT updated_at, status FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "updated_at": row["updated_at"] or 0.0,
        "status": row["status"] or "completed",  # NULL → "completed" for legacy rows
    }


def is_session_stream_done(state: dict[str, Any] | None, *, now: float) -> bool:
    """True only when the session is terminal AND has been stable >= 60s
    (terminal alone may be a transient write; stale time alone risks closing active sessions)."""
    if state is None:
        return False
    return (
        state.get("status") in SESSION_TERMINAL_STATUSES
        and now - float(state.get("updated_at") or 0.0) > SESSION_DONE_STABLE_SECS
    )


# ---------------------------------------------------------------------------
# Route handlers — sessions area
# ---------------------------------------------------------------------------


@studio_route("/sessions/", method="GET", area="sessions", name="list_sessions")
async def list_sessions_route(
    limit: int = Query(
        default=MAX_SESSION_PAGE,
        ge=1,
        le=MAX_SESSION_PAGE,
        description=f"Rows to return, newest first (max {MAX_SESSION_PAGE})",
    ),
    offset: int = Query(default=0, ge=0, description="Rows to skip, newest first"),
) -> dict[str, Any]:
    """One page of sessions. The response always reports `total` and
    `truncated` so a bounded answer can never be mistaken for a complete one."""
    sessions = await list_sessions(limit=limit, offset=offset)
    total = await count_sessions()
    return {
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": offset + len(sessions) < total,
    }


@studio_route("/sessions/{session_id}", method="GET", area="sessions", name="get_session")
async def get_session_route(
    session_id: str,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    message_offset: int = 0,
    message_cursor: str | None = None,
) -> dict[str, Any]:
    try:
        session = await get_session(
            session_id,
            message_limit=message_limit,
            message_offset=message_offset,
            message_cursor=message_cursor,
        )
    except MessageCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise NotFoundError(f"Session '{session_id}' not found")
    return session


class RenameSessionRequest(BaseModel):
    label: str


@studio_route("/sessions/{session_id}", method="PUT", area="sessions", name="rename_session")
async def rename_session_route(session_id: str, body: RenameSessionRequest) -> dict[str, Any]:
    """Set or clear a session's user-owned display label.

    Whitespace-only input clears the label (the display falls back to the
    existing name/agent_name/playbook_name/id chain) rather than erroring.
    An unknown session_id 404s before any write, matching the GET route's
    wording (``sessions.py`` GET handler above).
    """
    require_file_store()
    if not await session_exists(session_id):
        raise NotFoundError(f"Session '{session_id}' not found")

    normalized = _normalize_session_label(body.label)

    # A plain field write, not routed through StateDB.update_session(): that
    # generic updater unconditionally bumps updated_at, which would make a
    # rename masquerade as activity -- reordering session lists and, for a
    # running session, resetting the staleness clock a phantom-session sweep
    # depends on. A label edit is not activity.
    async with _open_db(store_path()) as db:
        await db.execute(
            "UPDATE sessions SET user_label = ? WHERE id = ?",
            (normalized, session_id),
        )
        await db.commit()

        cur = await db.execute(
            "SELECT id, name, user_label, agent_name, playbook_name FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()

    display_name = resolve_session_display_name(dict(row)) if row else session_id[:8]
    return {
        "session_id": session_id,
        "user_label": normalized,
        "display_name": display_name,
    }


@studio_route(
    "/sessions/{session_id}/stream",
    method="GET",
    area="sessions",
    name="stream_session",
    response_class=None,
)
async def stream_session_route(session_id: str):
    # Pre-flight 404 guard: without it a non-existent session silently
    # returns no messages and waits 60s before "done" with no indication.
    if not await session_exists(session_id):
        raise NotFoundError(f"Session '{session_id}' not found")

    async def generate():
        after_ts: float = 0.0
        last_heartbeat = time.monotonic()

        while True:
            messages = await get_session_messages_after(session_id, after_ts)

            if messages:
                for msg in messages:
                    yield f"data: {json.dumps(msg)}\n\n"
                    ts = msg.get("timestamp") or msg.get("created_at")
                    if ts and ts > after_ts:
                        after_ts = ts
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= 5.0:
                yield 'data: {"type":"heartbeat"}\n\n'
                last_heartbeat = time.monotonic()

            state = await get_session_stream_state(session_id)
            if is_session_stream_done(state, now=time.time()):
                yield 'data: {"type":"done"}\n\n'
                return

            await asyncio.sleep(0.5)

    from ._sse import sse_response

    return sse_response(generate())


# ---------------------------------------------------------------------------
# Route handlers — signals area (lives here; both areas share this module)
# ---------------------------------------------------------------------------


@studio_route(
    "/sessions/{session_id}/signals",
    method="GET",
    area="sessions",
    name="stream_signals",
    response_class=None,
)
async def stream_signals(session_id: str) -> Any:
    # Pre-flight 404 guard before opening the stream (ADR-0076).
    if not await session_exists(session_id):
        raise NotFoundError(f"Session '{session_id}' not found")

    from . import signals as signals_svc

    async def generate():
        after_seq: int = 0
        last_heartbeat = time.monotonic()

        while True:
            rows = await signals_svc.get_signals_after(session_id, after_seq)

            if rows:
                for row in rows:
                    # _PAYLOAD_BYTE_CAP (session/observer.py) caps the payload
                    # column only; the row envelope adds overhead so frames can exceed it.
                    yield f"data: {json.dumps(row)}\n\n"
                    if row["seq"] > after_seq:
                        after_seq = row["seq"]
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= 5.0:
                yield 'data: {"type":"heartbeat"}\n\n'
                last_heartbeat = time.monotonic()

            state = await get_session_stream_state(session_id)
            if is_session_stream_done(state, now=time.time()):
                yield 'data: {"type":"done"}\n\n'
                return

            await asyncio.sleep(0.5)

    from ._sse import sse_response

    return sse_response(generate())
