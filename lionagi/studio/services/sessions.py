from __future__ import annotations

import asyncio
import base64
import json
import math
from dataclasses import dataclass
from typing import Any

import aiosqlite
from fastapi import HTTPException, Query

from lionagi._errors import NotFoundError
from lionagi.state.claude_mirror import session_db_id
from lionagi.state.db import SESSION_TERMINAL_STATUSES
from lionagi.state.session_naming import resolve_display_name

from ..operator.run_control import session_has_control_consumer
from ..registry import studio_route
from ._db import open_db as _open_db
from ._db import require_file_store, store_exists, store_path, table_columns
from ._io import parse_json_col as _parse_json_col
from .artifact_verification import resolve_artifact_verification

SESSION_DONE_STABLE_SECS = 60.0
SESSION_TAIL_BATCH = 500


def display_model(value: Any) -> Any:
    """A model column fit to show: the provider CLIs stamp ``<synthetic>`` on
    their internal bookkeeping turns and the mirror copies it verbatim — it is
    not a model name, so every projection drops it rather than rendering the
    literal angle brackets in a model chip."""
    return None if value == "<synthetic>" else value


def display_cost(value: Any, provider: Any) -> Any:
    """A cost column fit to show. Codex runs' spend is not actually tracked
    yet: the stored figure is derived from a pricing table known to be wrong,
    and a plausible-wrong dollar amount is worse than an honest unknown. The
    cost-visibility contract already reserves NULL for "never reported", so
    codex rows project as NULL until real tracking lands."""
    return None if provider == "codex" else value


def _parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    meta = _parse_json_col(raw)
    return meta if isinstance(meta, dict) else None


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
                "role": agent.get("name", ""),
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
MAX_SESSION_SEARCH_CANDIDATES = 10_000


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
        kinds: set[str] | None = None,
    ) -> None:
        self.playbook = playbook
        self.statuses = statuses
        self.project = project
        self.project_null = project_null
        self.tags = list(dict.fromkeys(tags)) if tags else None
        self.search = search
        self.kinds = kinds

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        # A mirrored CLI transcript attributed to the run that spawned it as
        # its engine (see claude_mirror.link_engine_child_session) duplicates
        # that canonical run in every listing; the pair collapses here. The
        # row itself stays readable by id.
        clauses.append("json_extract(s.node_metadata, '$.engine_parent_run_id') IS NULL")
        if self.playbook:
            clauses.append(
                "LOWER(COALESCE(s.playbook_name, '')) LIKE '%' || LOWER(?) || '%' "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}'"
            )
            params.append(_escape_like(self.playbook))
        if self.search:
            escaped = _escape_like(self.search)
            clauses.append(
                "s.id IN (SELECT recent.id FROM sessions recent "
                "ORDER BY recent.updated_at DESC, recent.id DESC LIMIT ?)"
            )
            params.append(MAX_SESSION_SEARCH_CANDIDATES)
            clauses.append(
                "(LOWER(COALESCE(s.name, '')) LIKE '%' || LOWER(?) || '%' "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                "OR LOWER(COALESCE(s.agent_name, '')) LIKE '%' || LOWER(?) || '%' "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}')"
            )
            params.extend([escaped, escaped])
        if self.statuses:
            ordered = sorted(self.statuses)
            placeholders = ",".join("?" for _ in ordered)
            # Legacy rows carry NULL status and read as "completed" everywhere else.
            null_clause = " OR s.status IS NULL" if "completed" in self.statuses else ""
            clauses.append(f"(s.status IN ({placeholders}){null_clause})")
            params.extend(ordered)
        if self.kinds:
            # Facet vocabulary: "show" covers both spellings the writers have
            # used for a show-driven play root. Legacy rows carry NULL
            # invocation_kind and read as plain agent runs everywhere else,
            # so the agent facet admits them too.
            expanded_set: set[str] = set()
            for kind in self.kinds:
                expanded_set.update({"show", "show-play"} if kind == "show" else {kind})
            expanded = sorted(expanded_set)
            placeholders = ",".join("?" for _ in expanded)
            null_clause = " OR s.invocation_kind IS NULL" if "agent" in self.kinds else ""
            clauses.append(f"(s.invocation_kind IN ({placeholders}){null_clause})")
            params.extend(expanded)
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
        return "WHERE " + " AND ".join(clauses), params

    def cursor_contract(self) -> dict[str, Any]:
        """Stable public-filter identity carried by an opaque page cursor."""
        return {
            "playbook": self.playbook,
            "statuses": sorted(self.statuses) if self.statuses else [],
            # Every filter where() applies belongs here. A cursor is only sound
            # against the query that produced it, and one minted under a kinds
            # filter, replayed under another, carries a keyset boundary from a
            # different row set: the page silently skips rows or comes back
            # short, with nothing to say it happened.
            "kinds": sorted(self.kinds) if self.kinds else [],
            "project": self.project,
            "project_null": self.project_null,
            "tags": sorted(self.tags) if self.tags else [],
            "search": self.search,
        }


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


# "cost" sorts unreported (NULL total_cost_usd) after every reported value,
# including a genuine $0.00 — `total_cost_usd IS NULL` evaluates to 0/1 and
# sorts ascending first, so reported rows (0) always precede unreported (1).
_SESSION_SORTS: dict[str, str] = {
    "recent": "s.updated_at DESC, s.id DESC",
    "cost": "s.total_cost_usd IS NULL, s.total_cost_usd DESC, s.updated_at DESC, s.id DESC",
}

MAX_SESSION_OFFSET = 10_000


class SessionListCursorError(ValueError):
    """A session-list cursor is malformed or does not match the requested query."""


@dataclass(frozen=True)
class SessionPage:
    items: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool


def _encode_session_list_cursor(row: dict[str, Any], *, where: SessionFilter, sort: str) -> str:
    payload = {
        "v": 1,
        "sort": sort,
        "filters": where.cursor_contract(),
        "updated_at": float(row.get("updated_at") or 0.0),
        "id": row["id"],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_session_list_cursor(
    token: str, *, where: SessionFilter, sort: str
) -> tuple[float, str]:
    if not token or len(token) > 8_192:
        raise SessionListCursorError("Malformed session list cursor")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception as exc:
        raise SessionListCursorError("Malformed session list cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise SessionListCursorError("Unsupported session list cursor")
    if payload.get("sort") != sort or payload.get("filters") != where.cursor_contract():
        raise SessionListCursorError("Session list cursor does not match the requested query")
    updated_at = payload.get("updated_at")
    session_id = payload.get("id")
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(updated_at)
        or not isinstance(session_id, str)
        or not session_id
    ):
        raise SessionListCursorError("Malformed session list cursor")
    return float(updated_at), session_id


def _append_where(clause: str, predicate: str) -> str:
    return f"{clause} AND {predicate}" if clause else f"WHERE {predicate}"


def _session_page_sql(clause: str, order_by: str, approximate_end: str) -> str:
    """Correlated aggregates preserve the indexed session scan and its LIMIT.

    approximate_end is chosen from two literals by _approximate_end_selection
    and never comes from a caller.
    """
    return f"""
        SELECT
          s.id,
          s.name,
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
          {approximate_end},
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
          s.total_cost_usd,
          s.input_tokens,
          s.output_tokens,
          (SELECT COUNT(*) FROM branches b WHERE b.session_id = s.id) AS branch_count,
          COALESCE((
              SELECT SUM(json_array_length(p.collection))
              FROM branches b
              JOIN progressions p ON p.id = b.progression_id
              WHERE b.session_id = s.id
          ), 0) AS message_count
        FROM sessions s
        {clause}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """  # noqa: S608 -- both fragments are module-owned SQL


async def list_sessions_page(
    *,
    limit: int = MAX_SESSION_PAGE,
    offset: int = 0,
    cursor: str | None = None,
    where: SessionFilter | None = None,
    sort: str = "recent",
) -> SessionPage:
    """Read a stable page without regrouping or re-sorting the selected rows."""
    require_file_store()
    if not store_exists():
        return SessionPage([], None, False)

    limit = max(1, min(int(limit), MAX_SESSION_PAGE))
    offset = max(0, min(int(offset), MAX_SESSION_OFFSET))
    selected_filter = where or SessionFilter()
    clause, params = selected_filter.where()
    order_by = _SESSION_SORTS.get(sort, _SESSION_SORTS["recent"])
    if cursor is not None:
        if sort != "recent":
            raise SessionListCursorError("Session list cursors currently require recent sort")
        updated_at, session_id = _decode_session_list_cursor(
            cursor, where=selected_filter, sort=sort
        )
        clause = _append_where(
            clause,
            "(s.updated_at < ? OR (s.updated_at = ? AND s.id < ?))",
        )
        params.extend([updated_at, updated_at, session_id])
        offset = 0

    async with _open_db(store_path()) as db:
        if selected_filter.tags:
            from .run_tags import _ensure_table

            await _ensure_table(db)
        approximate_end = await _approximate_end_selection(db, alias="s")
        cur = await db.execute(
            _session_page_sql(clause, order_by, approximate_end),
            [*params, limit, offset],
        )
        rows = await cur.fetchall()
        has_more = False
        if len(rows) == limit:
            more_cur = await db.execute(
                f"SELECT 1 FROM sessions s {clause} ORDER BY {order_by} LIMIT 1 OFFSET ?",  # noqa: S608
                [*params, offset + limit],
            )
            has_more = await more_cur.fetchone() is not None

    items = [_format_session_summary(row) for row in rows]
    next_cursor = (
        _encode_session_list_cursor(items[-1], where=selected_filter, sort=sort)
        if has_more and items and sort == "recent"
        else None
    )
    return SessionPage(items, next_cursor, has_more)


async def _approximate_end_selection(db: Any, *, alias: str = "") -> str:
    """How to read the approximate-end flag from the store in front of us.

    A store written before this column existed has no approximate ends
    recorded, so a constant zero is the honest answer for it rather than a
    degraded one: it is exactly what the version that wrote the store reported
    for every row. Naming the column unconditionally would instead fail the
    whole read, and these connections cannot migrate the store to avoid that.
    """
    prefix = f"{alias}." if alias else ""
    if "ended_at_is_approximate" in await table_columns(db, "sessions"):
        return f"{prefix}ended_at_is_approximate"
    return "0 AS ended_at_is_approximate"


async def list_sessions(
    *,
    limit: int = MAX_SESSION_PAGE,
    offset: int = 0,
    where: SessionFilter | None = None,
    sort: str = "recent",
) -> list[dict[str, Any]]:
    """One page of sessions, newest first (or highest-cost first). Cost is
    proportional to `limit`, not to the size of the store."""
    return (await list_sessions_page(limit=limit, offset=offset, where=where, sort=sort)).items


def _format_session_summary(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        # Displayed name prefers structured identity (playbook/show/agent)
        # over the raw, possibly prompt-derived value stored on the row
        # — see resolve_display_name().
        "name": resolve_display_name(dict(row)),
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
        # Carried here as well as on the detail route: an end time that was
        # inferred rather than recorded reads as measured wherever the
        # qualifier is missing, and the two routes describing the same session
        # differently is the conflation this flag exists to remove.
        "ended_at_is_approximate": bool(row["ended_at_is_approximate"]),
        # Caller (runs service) feeds this to staleness_check (ADR-0057 D6).
        "last_message_at": row["last_message_at"],
        # Optional parent skill orchestration.
        "invocation_id": row["invocation_id"],
        # Provenance disclosure — resolved values.
        "model": display_model(row["model"]),
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
        # Cost-visibility contract: NULL means the provider never reported
        # a cost for this session (unknown), never coerced to 0.0 (free).
        "total_cost_usd": display_cost(row["total_cost_usd"], row["provider"]),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
    }


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
            WHERE json_extract(node_metadata, '$.engine_parent_run_id') IS NULL
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
MAX_LEGACY_MESSAGE_OFFSET = 10_000
_PROGRESSION_READ_CHUNK = 16 * 1024


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


def _encode_message_window_cursor(
    session_id: str,
    limit: int,
    branch_positions: dict[str, dict[str, Any]],
) -> str:
    payload = {
        "v": 2,
        "session_id": session_id,
        "limit": limit,
        "branch_positions": branch_positions,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_message_window_cursor(
    token: str, *, session_id: str, limit: int
) -> tuple[int, dict[str, dict[str, Any]] | dict[str, str]]:
    if not token or len(token) > 64 * 1024:
        raise MessageCursorError("Malformed message_cursor")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception as exc:
        raise MessageCursorError("Malformed message_cursor") from exc
    if not isinstance(payload, dict):
        raise MessageCursorError("Malformed message_cursor")
    version = payload.get("v")
    if payload.get("session_id") != session_id:
        raise MessageCursorError("message_cursor belongs to a different session")
    if payload.get("limit") != limit:
        raise MessageCursorError("message_cursor does not match message_limit")
    if version == 1:
        anchors = payload.get("branch_anchors")
        if not isinstance(anchors, dict):
            raise MessageCursorError("message_cursor is missing branch_anchors")
        return 1, anchors
    if version != 2:
        raise MessageCursorError("Unsupported message_cursor")
    positions = payload.get("branch_positions")
    if not isinstance(positions, dict):
        raise MessageCursorError("message_cursor is missing branch_positions")
    for branch_id, position in positions.items():
        if not isinstance(branch_id, str) or not isinstance(position, dict):
            raise MessageCursorError("Malformed message_cursor branch position")
        end = position.get("end")
        anchor = position.get("anchor")
        if (
            isinstance(end, bool)
            or not isinstance(end, int)
            or end < 1
            or not isinstance(anchor, str)
            or not anchor
        ):
            raise MessageCursorError("Malformed message_cursor branch position")
    return 2, positions


class SessionStreamCursorError(ValueError):
    """A live-message cursor is malformed or belongs to another session."""


def _encode_session_stream_cursor(session_id: str, created_at: float, message_id: str) -> str:
    payload = {
        "v": 1,
        "session_id": session_id,
        "created_at": created_at,
        "message_id": message_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_session_stream_cursor(token: str, *, session_id: str) -> tuple[float, str]:
    if not token or len(token) > 4_096:
        raise SessionStreamCursorError("Malformed session stream cursor")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception as exc:
        raise SessionStreamCursorError("Malformed session stream cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise SessionStreamCursorError("Unsupported session stream cursor")
    if payload.get("session_id") != session_id:
        raise SessionStreamCursorError("Session stream cursor belongs to a different session")
    created_at = payload.get("created_at")
    message_id = payload.get("message_id")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(created_at)
        or not isinstance(message_id, str)
        or not message_id
    ):
        raise SessionStreamCursorError("Malformed session stream cursor")
    return float(created_at), message_id


@dataclass(frozen=True)
class _ProgressionToken:
    value: str
    start: int


@dataclass(frozen=True)
class _ProgressionWindow:
    ids: list[str]
    has_older: bool
    next_position: dict[str, Any] | None


def _reverse_json_string_tokens(
    raw: bytes, *, absolute_start: int
) -> tuple[list[_ProgressionToken], bool]:
    """Decode complete JSON strings from an array suffix, newest first.

    The suffix may start in the middle of its oldest string. In that case the
    complete newer tokens are returned with ``False`` so the caller can prepend
    another bounded chunk and retry. Progression collections are arrays of
    strings by schema contract; any other token is treated as corruption.
    """
    tokens: list[_ProgressionToken] = []
    i = len(raw) - 1
    whitespace = b" \t\r\n"
    while i >= 0:
        while i >= 0 and (raw[i] in whitespace or raw[i] in (ord(","), ord("]"))):
            i -= 1
        if i < 0:
            return tokens, False
        if raw[i] == ord("["):
            return tokens, True
        if raw[i] != ord('"'):
            raise ValueError("progression collection is not a JSON string array")
        closing = i
        i -= 1
        opening = -1
        while i >= 0:
            if raw[i] == ord('"'):
                backslashes = 0
                j = i - 1
                while j >= 0 and raw[j] == ord("\\"):
                    backslashes += 1
                    j -= 1
                if backslashes % 2 == 0:
                    opening = i
                    break
            i -= 1
        if opening < 0:
            return tokens, False
        try:
            value = json.loads(raw[opening : closing + 1].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid message id in progression collection") from exc
        if not isinstance(value, str):
            raise ValueError("progression collection is not a JSON string array")
        tokens.append(_ProgressionToken(value=value, start=absolute_start + opening))
        i = opening - 1
    return tokens, False


async def _read_progression_window(
    db: aiosqlite.Connection,
    progression_id: str,
    *,
    total_bytes: int,
    end: int | None,
    limit: int,
    legacy_offset: int,
) -> _ProgressionWindow:
    """Read one tail window without materializing the progression JSON blob."""
    collection_end = max(1, total_bytes - 1)
    end = collection_end if end is None else end
    if end < 1 or end > collection_end:
        raise MessageCursorError("message_cursor position is outside the progression")
    needed = limit + legacy_offset + 1
    start = end
    tokens: list[_ProgressionToken] = []
    reached_array_start = False
    while len(tokens) < needed and not reached_array_start and start > 0:
        start = max(0, start - _PROGRESSION_READ_CHUNK)
        cur = await db.execute(
            "SELECT substr(CAST(collection AS BLOB), ?, ?) AS fragment "
            "FROM progressions WHERE id = ?",
            (start + 1, end - start, progression_id),
        )
        row = await cur.fetchone()
        fragment = bytes(row["fragment"] or b"") if row else b""
        tokens, reached_array_start = _reverse_json_string_tokens(fragment, absolute_start=start)
    newest_first = tokens[:needed]
    ordered = list(reversed(newest_first))
    page_end = max(0, len(ordered) - legacy_offset)
    page_start = max(0, page_end - limit)
    page = ordered[page_start:page_end]
    has_older = page_start > 0 or (not reached_array_start and bool(page))
    next_position = {"end": page[0].start, "anchor": page[0].value} if has_older and page else None
    return _ProgressionWindow(
        ids=[token.value for token in page],
        has_older=has_older,
        next_position=next_position,
    )


async def _validate_progression_position(
    db: aiosqlite.Connection,
    progression_id: str,
    *,
    end: int,
    anchor: str,
    total_bytes: int,
) -> None:
    if end < 1 or end >= total_bytes:
        raise MessageCursorError("message_cursor position is outside the progression")
    read_size = min(_PROGRESSION_READ_CHUNK, total_bytes - end)
    while read_size <= total_bytes - end:
        cur = await db.execute(
            "SELECT substr(CAST(collection AS BLOB), ?, ?) AS fragment "
            "FROM progressions WHERE id = ?",
            (end + 1, read_size, progression_id),
        )
        row = await cur.fetchone()
        fragment = bytes(row["fragment"] or b"") if row else b""
        try:
            decoded = fragment.decode("utf-8")
            value, _ = json.JSONDecoder().raw_decode(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            if read_size == total_bytes - end:
                break
            read_size = min(read_size * 2, total_bytes - end)
            continue
        if value == anchor:
            return
        break
    raise MessageCursorError("message_cursor anchor no longer matches the progression")


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


async def _get_session_statistics_from_db(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    exists_cur = await db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
    if not await exists_cur.fetchone():
        return None
    branch_cur = await db.execute(
        "SELECT id, progression_id FROM branches WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    )
    branch_rows = await branch_cur.fetchall()
    full_stats = _init_message_stats()
    branch_results: dict[str, dict[str, Any]] = {}
    for branch in branch_rows:
        msg_ids: list[str] = []
        progression_id = branch["progression_id"]
        if progression_id:
            progression_cur = await db.execute(
                "SELECT collection FROM progressions WHERE id = ?",
                (progression_id,),
            )
            progression_row = await progression_cur.fetchone()
            if progression_row and progression_row["collection"]:
                try:
                    decoded = json.loads(progression_row["collection"])
                    msg_ids = decoded if isinstance(decoded, list) else []
                except (json.JSONDecodeError, TypeError):
                    msg_ids = []
        role_counts = await _fetch_role_counts(db, msg_ids)
        first_message_at, last_message_at = await _fetch_message_bounds(db, msg_ids)
        action_messages = await _fetch_action_messages(db, msg_ids)
        branch_stats = _branch_message_stats(
            sum(role_counts.values()), role_counts, action_messages
        )
        branch_id = branch["id"]
        branch_results[branch_id] = {
            "message_total": len(msg_ids),
            "message_stats": {
                "message_count": branch_stats["message_count"],
                "roles": branch_stats["roles"],
            },
            "first_message_at": first_message_at,
            "last_message_at": last_message_at,
        }
        full_stats["message_count"] += branch_stats["message_count"]
        for role, count in branch_stats["roles"].items():
            full_stats["roles"][role] = full_stats["roles"].get(role, 0) + count
        full_stats["branches"][branch_id] = branch_results[branch_id]["message_stats"]
        full_stats["tool_call_count"] += branch_stats["tool_call_count"]
        full_stats["error_count"] += branch_stats["error_count"]
        full_stats["errors"].extend(branch_stats["errors"])
        full_stats["files"].extend(branch_stats["files"])
    full_stats["files"] = sorted(set(full_stats["files"]))
    return {
        "session_id": session_id,
        "message_stats_loaded": True,
        "message_stats": full_stats,
        "branches": branch_results,
    }


async def get_session_statistics(session_id: str) -> dict[str, Any] | None:
    """Load exact lifetime aggregates separately from the bounded detail page."""
    require_file_store()
    if not store_exists():
        return None
    async with _open_db(store_path()) as db:
        return await _get_session_statistics_from_db(db, session_id)


async def _pause_is_held(db: Any, session_id: str) -> bool:
    """Whether this run's pause gate is held, or queued to be.

    Read from the control transport rather than remembered by whoever clicked.
    A client-local flag does not survive a reload, and what it leaves behind is
    the one combination an operator cannot recover from: a still-paused run
    offering Pause and refusing Resume as "not paused".

    The answer is the verb of the newest pause or resume row that still counts
    for anything -- one already applied, or one queued and waiting for the
    poller. A rejected row never held a gate, and a resume releases the pause
    before it, so ordering by when each was written and taking the first is the
    whole rule.
    """
    cur = await db.execute(
        """SELECT verb FROM session_controls
           WHERE session_id = ?
             AND verb IN ('pause', 'resume')
             AND (result IS NULL OR result = 'applied')
           ORDER BY created_at DESC, id DESC
           LIMIT 1""",
        (session_id,),
    )
    row = await cur.fetchone()
    return row is not None and row["verb"] == "pause"


async def get_session(
    session_id: str,
    *,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    message_offset: int = 0,
    message_cursor: str | None = None,
    include_stats: bool = False,
) -> dict[str, Any] | None:
    require_file_store()
    if not store_exists():
        return None

    message_limit = max(1, min(message_limit, MAX_MESSAGE_LIMIT))
    message_offset = max(0, min(message_offset, MAX_LEGACY_MESSAGE_OFFSET))
    cursor_version = 0
    cursor_positions: dict[str, dict[str, Any]] | dict[str, str] | None = None
    if message_cursor:
        cursor_version, cursor_positions = _decode_message_window_cursor(
            message_cursor,
            session_id=session_id,
            limit=message_limit,
        )

    async with _open_db(store_path()) as db:
        # The page, continuation anchors, and both live high-water marks must
        # describe one read snapshot. The connection stays open only for this
        # request; the broker owns the reusable live-read resource.
        await db.execute("BEGIN")
        approximate_end = await _approximate_end_selection(db)
        cur = await db.execute(
            # Include lifecycle and provenance columns (model/provider/effort/agent_hash).
            # The one interpolated name is chosen from two literals by the
            # helper above and never comes from a caller.
            f"""SELECT id, name, created_at, updated_at,
                      playbook_name, agent_name, invocation_kind,
                      show_topic, show_play_name, artifacts_path,
                      artifact_contract_json, artifact_verification_json,
                      source_kind, status, started_at, ended_at,
                      {approximate_end}, last_message_at,
                      model, provider, effort, agent_hash, invocation_id, run_id,
                      node_metadata, project, project_source,
                      status_reason_code, status_reason_summary, status_evidence_refs,
                      total_cost_usd, input_tokens, output_tokens, duration_ms
               FROM sessions WHERE id = ?""",  # noqa: S608
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
        pause_is_held = await _pause_is_held(db, session_id)

        try:
            branch_cur = await db.execute(
                """SELECT b.id, b.name, b.created_at, b.progression_id,
                          b.model, b.provider, b.agent_name, b.status,
                          b.started_at, b.ended_at,
                          length(CAST(p.collection AS BLOB)) AS collection_bytes
                   FROM branches b
                   LEFT JOIN progressions p ON p.id = b.progression_id
                   WHERE b.session_id = ? ORDER BY b.created_at""",
                (session_id,),
            )
        except Exception:
            branch_cur = await db.execute(
                """SELECT b.id, b.name, b.created_at, b.progression_id,
                          b.model, b.provider, b.agent_name,
                          length(CAST(p.collection AS BLOB)) AS collection_bytes
                   FROM branches b
                   LEFT JOIN progressions p ON p.id = b.progression_id
                   WHERE b.session_id = ? ORDER BY b.created_at""",
                (session_id,),
            )
        branch_rows = await branch_cur.fetchall()

        branches = []
        next_branch_positions: dict[str, dict[str, Any]] = {}
        high_water_messages: list[dict[str, Any]] = []
        for br in branch_rows:
            branch_id = br["id"]
            prog_id = br["progression_id"]
            collection_bytes = int(br["collection_bytes"] or 2)
            window_ids: list[str] = []
            has_older = False
            next_position: dict[str, Any] | None = None
            if prog_id and cursor_version == 1:
                # Compatibility for an in-flight v1 anchor. New responses issue
                # byte-position cursors, so the unbounded lookup ages out after
                # one continuation instead of remaining the normal path.
                #
                # A branch the anchor map does not name has nothing left to
                # page, and reading its collection to discover that costs
                # exactly as much as reading it to serve a page. So the anchor
                # decides first: the whole-collection decode is spent only on
                # branches that are actually owed one, and a cursor naming no
                # branch at all -- which every v1 client can send, for every
                # session -- reads nothing.
                anchor = cursor_positions.get(branch_id) if cursor_positions else None
                if anchor is not None:
                    prog_cur = await db.execute(
                        "SELECT collection FROM progressions WHERE id = ?",
                        (prog_id,),
                    )
                    prog_row = await prog_cur.fetchone()
                    full_msg_ids: list[str] = []
                    if prog_row and prog_row["collection"]:
                        try:
                            full_msg_ids = json.loads(prog_row["collection"])
                        except (json.JSONDecodeError, TypeError):
                            full_msg_ids = []
                    window_ids, has_older, next_anchor = _window_message_ids(
                        full_msg_ids,
                        branch_id=branch_id,
                        limit=message_limit,
                        cursor_anchors=cursor_positions,  # type: ignore[arg-type]
                        legacy_offset=0,
                    )
                    if next_anchor:
                        anchor_index = full_msg_ids.index(next_anchor)
                        prefix = json.dumps(full_msg_ids[:anchor_index], separators=(",", ":"))
                        next_position = {"end": len(prefix.encode()) - 1, "anchor": next_anchor}
            elif prog_id:
                end: int | None = None
                if cursor_version == 2:
                    position = cursor_positions.get(branch_id) if cursor_positions else None
                    if position is None:
                        window = _ProgressionWindow([], False, None)
                    else:
                        end = int(position["end"])
                        await _validate_progression_position(
                            db,
                            prog_id,
                            end=end,
                            anchor=str(position["anchor"]),
                            total_bytes=collection_bytes,
                        )
                        window = await _read_progression_window(
                            db,
                            prog_id,
                            total_bytes=collection_bytes,
                            end=end,
                            limit=message_limit,
                            legacy_offset=0,
                        )
                else:
                    window = await _read_progression_window(
                        db,
                        prog_id,
                        total_bytes=collection_bytes,
                        end=None,
                        limit=message_limit,
                        legacy_offset=message_offset,
                    )
                window_ids = window.ids
                has_older = window.has_older
                next_position = window.next_position
            if next_position:
                next_branch_positions[branch_id] = next_position

            window_messages = await _fetch_messages_by_ids(db, window_ids)
            by_id = {m["id"]: m for m in window_messages}
            messages = [by_id[mid] for mid in window_ids if mid in by_id]
            if cursor_version == 0 and message_offset == 0 and messages:
                high_water_messages.append(messages[-1])

            br_keys = br.keys()
            branches.append(
                {
                    "id": branch_id,
                    "name": br["name"],
                    "created_at": br["created_at"],
                    "messages": messages,
                    # Exact lifetime fields are intentionally deferred. Keeping
                    # their compatibility keys with null values makes absence
                    # visible instead of silently redefining them from a window.
                    "message_total": None,
                    "message_offset": message_offset,
                    "message_limit": message_limit,
                    "message_window_count": len(messages),
                    "messages_truncated": has_older or bool(message_cursor),
                    "message_has_older": has_older,
                    "message_stats": None,
                    "first_message_at": None,
                    "last_message_at": None,
                    "model": display_model(br["model"]),
                    "provider": br["provider"],
                    "agent_name": br["agent_name"],
                    "status": br["status"] if "status" in br_keys else None,
                    "started_at": (
                        br["started_at"]
                        if "started_at" in br_keys and br["started_at"] is not None
                        else br["created_at"]
                    ),
                    "ended_at": br["ended_at"] if "ended_at" in br_keys else None,
                }
            )

        message_next_cursor = (
            _encode_message_window_cursor(session_id, message_limit, next_branch_positions)
            if next_branch_positions
            else None
        )
        signal_cursor = 0
        try:
            signal_cur = await db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM session_signals WHERE session_id = ?",
                (session_id,),
            )
            signal_row = await signal_cur.fetchone()
            signal_cursor = int(signal_row["seq"] or 0) if signal_row else 0
        except aiosqlite.OperationalError:
            signal_cursor = 0
        message_stream_cursor = None
        if high_water_messages:
            newest = max(
                high_water_messages,
                key=lambda message: (float(message.get("timestamp") or 0.0), message["id"]),
            )
            message_stream_cursor = _encode_session_stream_cursor(
                session_id,
                float(newest.get("timestamp") or 0.0),
                newest["id"],
            )
        lifetime_stats = (
            await _get_session_statistics_from_db(db, session_id) if include_stats else None
        )
        if lifetime_stats:
            by_branch = lifetime_stats["branches"]
            for branch in branches:
                exact = by_branch.get(branch["id"])
                if exact:
                    branch.update(exact)

    started_at = session_row["started_at"]
    ended_at = session_row["ended_at"]
    ended_at_is_approximate = bool(session_row["ended_at_is_approximate"])
    duration_ms = None if ended_at_is_approximate else session_row["duration_ms"]
    # Only reconstruct from a measured end. Deriving one from an approximate
    # ended_at hands back a number that reads as measured, which is the whole
    # thing the flag exists to prevent.
    if (
        duration_ms is None
        and not ended_at_is_approximate
        and started_at is not None
        and ended_at is not None
    ):
        duration_ms = (ended_at - started_at) * 1000
    status = session_row["status"] or "completed"
    artifact_contract = _parse_json_col(session_row["artifact_contract_json"])
    stored_verification = _parse_json_col(session_row["artifact_verification_json"])

    artifact_verification = resolve_artifact_verification(
        stored_verification,
        status=status,
        contract=artifact_contract,
        artifacts_path=session_row["artifacts_path"],
    )

    return {
        "id": session_row["id"],
        # Same resolution as list_sessions() — structured identity beats
        # the raw, possibly prompt-derived stored name.
        "name": resolve_display_name(dict(session_row)),
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
        "ended_at_is_approximate": ended_at_is_approximate,
        "duration_ms": duration_ms,
        # Full-session aggregate, not derived from the windowed page.
        "last_message_at": session_row["last_message_at"],
        "source_show": source_show,
        "branches": branches,
        "message_limit": message_limit,
        "message_cursor": message_cursor,
        "message_next_cursor": message_next_cursor,
        "message_stats": lifetime_stats["message_stats"] if lifetime_stats else None,
        "message_stats_loaded": lifetime_stats is not None,
        "statistics_url": f"/api/sessions/{session_id}/statistics",
        "stream_cursors": {
            "messages": message_stream_cursor,
            "signals": signal_cursor,
        },
        # Provenance disclosure — same fields exposed on list_sessions().
        "model": display_model(session_row["model"]),
        "provider": session_row["provider"],
        "effort": session_row["effort"],
        "agent_hash": session_row["agent_hash"],
        "invocation_id": session_row["invocation_id"],
        # Whether a queued run control would ever reach a runner. Computed by
        # the admission path's own predicate rather than restated here, so a
        # client cannot offer a control this session's admission would refuse.
        "has_control_consumer": session_has_control_consumer(dict(session_row)),
        # Whether a pause is currently held on this run. Server-derived so it
        # survives a reload; see _pause_is_held.
        "pause_is_held": pause_is_held,
        # ADR-0063: project detection.
        "project": session_row["project"],
        "project_source": session_row["project_source"],
        # ADR-0057: status reason surfaced on detail (drives the failure banner).
        "status_reason_code": session_row["status_reason_code"],
        "status_reason_summary": session_row["status_reason_summary"],
        "status_evidence_refs": _parse_json_col(session_row["status_evidence_refs"]),
        # Cost-visibility contract: NULL means unreported, never coerced to 0.0.
        "total_cost_usd": display_cost(session_row["total_cost_usd"], session_row["provider"]),
        "input_tokens": session_row["input_tokens"],
        "output_tokens": session_row["output_tokens"],
        "graph": _graph_from_metadata(session_row["node_metadata"]),
        "segments": (_parse_metadata(session_row["node_metadata"]) or {}).get("segments"),
        # Raw node_metadata (carries pid/pid_create_time) so callers like
        # get_run()'s liveness check can find the recorded pid.
        "node_metadata": session_row["node_metadata"],
    }


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


async def _get_session_messages_after_db(
    db: Any,
    session_id: str,
    after_ts: float,
    after_id: str | None,
    *,
    limit: int = SESSION_TAIL_BATCH,
) -> list[dict[str, Any]]:
    cursor_clause = (
        "AND (m.created_at > ? OR (m.created_at = ? AND m.id > ?))"
        if after_id is not None
        else "AND m.created_at > ?"
    )
    params: tuple[Any, ...] = (
        (session_id, after_ts, after_ts, after_id, limit)
        if after_id is not None
        else (session_id, after_ts, limit)
    )
    cur = await db.execute(
        f"""
        SELECT m.id, m.created_at, m.content, m.sender, m.role,
               mt.lion_class AS lion_class_str, b.id AS branch_id
        FROM branches b
        JOIN progressions p ON p.id = b.progression_id
        JOIN json_each(p.collection) je ON 1=1
        JOIN messages m ON m.id = je.value
        LEFT JOIN message_types mt ON m.lion_class = mt.type_id
        WHERE b.session_id = ? {cursor_clause}
        ORDER BY m.created_at, m.id
        LIMIT ?
        """,  # noqa: S608 -- cursor_clause is a fixed internal fragment
        params,
    )
    rows = await cur.fetchall()
    result = []
    for row in rows:
        msg = _format_message(row)
        msg["branch_id"] = row["branch_id"]
        result.append(msg)
    return result


async def get_session_messages_after(
    session_id: str,
    after_ts: float,
    after_id: str | None = None,
    *,
    limit: int = SESSION_TAIL_BATCH,
) -> list[dict[str, Any]]:
    """Poll-friendly tail read for the SSE stream/signals endpoints. Joins via
    json_each rather than binding every message id into an IN (...) clause,
    which would blow past SQLite's 999 bound-variable limit at scale."""
    if not store_exists():
        return []

    async with _open_db(store_path()) as db:
        return await _get_session_messages_after_db(
            db,
            session_id,
            after_ts,
            after_id,
            limit=max(1, min(limit, SESSION_TAIL_BATCH)),
        )


async def _read_session_tail_tick(
    db: Any,
    session_id: str,
    message_cursor: tuple[float, str] | None,
    signal_cursor: int,
    *,
    read_messages: bool,
    read_signals: bool,
) -> Any:
    """Read one bounded live-tail tick on the broker's retained connection."""
    from . import signals as signals_svc
    from .tail_broker import TailRead

    messages: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    next_message_cursor = message_cursor
    next_signal_cursor = signal_cursor
    if read_messages:
        after_ts, after_id = message_cursor or (0.0, "")
        messages = await _get_session_messages_after_db(
            db,
            session_id,
            after_ts,
            after_id,
            limit=SESSION_TAIL_BATCH,
        )
        if messages:
            newest = messages[-1]
            next_message_cursor = (
                float(newest.get("timestamp") or 0.0),
                str(newest["id"]),
            )
    if read_signals:
        signals = await signals_svc._get_signals_after_db(
            db,
            session_id,
            signal_cursor,
            limit=SESSION_TAIL_BATCH,
        )
        if signals:
            next_signal_cursor = int(signals[-1]["seq"])

    cur = await db.execute(
        "SELECT updated_at, status FROM sessions WHERE id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    state = (
        {
            "updated_at": row["updated_at"] or 0.0,
            "status": row["status"] or "completed",
        }
        if row
        else None
    )
    return TailRead(
        messages=messages,
        signals=signals,
        state=state,
        message_cursor=next_message_cursor,
        signal_cursor=next_signal_cursor,
        messages_caught_up=len(messages) < SESSION_TAIL_BATCH,
        signals_caught_up=len(signals) < SESSION_TAIL_BATCH,
    )


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
    offset: int = Query(
        default=0,
        ge=0,
        le=MAX_SESSION_OFFSET,
        description=f"Compatibility rows to skip (max {MAX_SESSION_OFFSET}); prefer cursor",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque continuation cursor from the previous recent-sorted page",
    ),
    include_total: bool = Query(
        default=True,
        description="Compute the exact matching total; disable on latency-sensitive reads",
    ),
    sort: str = Query(
        default="recent",
        description="Sort order: 'recent' (default) or 'cost' (highest reported spend first)",
    ),
) -> dict[str, Any]:
    """One page of sessions. The response always reports `total` and
    `truncated` so a bounded answer can never be mistaken for a complete one."""
    if sort not in _SESSION_SORTS:
        raise HTTPException(status_code=422, detail="sort must be one of: recent, cost")
    if cursor is not None and offset:
        raise HTTPException(status_code=422, detail="cursor and offset are mutually exclusive")
    try:
        page = await list_sessions_page(
            limit=limit,
            offset=offset,
            cursor=cursor,
            sort=sort,
        )
    except SessionListCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    total = await count_sessions() if include_total else None
    return {
        "sessions": page.items,
        "total": total,
        "total_exact": include_total,
        "limit": limit,
        "offset": offset,
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "truncated": (offset + len(page.items) < total) if total is not None else page.has_more,
        "search": {"mode": "none", "bounded_slow_path": False},
    }


@studio_route("/sessions/{session_id}", method="GET", area="sessions", name="get_session")
async def get_session_route(
    session_id: str,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    message_offset: int = 0,
    message_cursor: str | None = None,
    include_stats: bool = Query(
        default=False,
        description="Include exact lifetime statistics inline; prefer the statistics endpoint",
    ),
) -> dict[str, Any]:
    try:
        session = await get_session(
            session_id,
            message_limit=message_limit,
            message_offset=message_offset,
            message_cursor=message_cursor,
            include_stats=include_stats,
        )
    except MessageCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise NotFoundError(f"Session '{session_id}' not found")
    return session


@studio_route(
    "/sessions/{session_id}/statistics",
    method="GET",
    area="sessions",
    name="get_session_statistics",
)
async def get_session_statistics_route(session_id: str) -> dict[str, Any]:
    statistics = await get_session_statistics(session_id)
    if statistics is None:
        raise NotFoundError(f"Session '{session_id}' not found")
    return statistics


@studio_route(
    "/sessions/{session_id}/stream",
    method="GET",
    area="sessions",
    name="stream_session",
    response_class=None,
)
async def stream_session_route(session_id: str, cursor: str | None = None):
    # Pre-flight 404 guard: without it a non-existent session silently
    # returns no messages and waits 60s before "done" with no indication.
    if not await session_exists(session_id):
        raise NotFoundError(f"Session '{session_id}' not found")

    try:
        start = _decode_session_stream_cursor(cursor, session_id=session_id) if cursor else None
    except SessionStreamCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def generate():
        from .tail_broker import subscribe_session_messages

        subscription = await subscribe_session_messages(session_id, start)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(subscription.next_event(), timeout=5.0)
                except asyncio.TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'
                    continue
                if event.kind == "data":
                    resume = event.resume_cursor
                    event_id = (
                        _encode_session_stream_cursor(session_id, resume[0], resume[1])
                        if isinstance(resume, tuple)
                        else ""
                    )
                    prefix = f"id: {event_id}\n" if event_id else ""
                    yield f"{prefix}data: {json.dumps(event.payload)}\n\n"
                elif event.kind == "resync":
                    resume = event.resume_cursor
                    resync_cursor = (
                        _encode_session_stream_cursor(session_id, resume[0], resume[1])
                        if isinstance(resume, tuple)
                        else None
                    )
                    yield f"data: {json.dumps({'type': 'resync', 'cursor': resync_cursor})}\n\n"
                    return
                elif event.kind == "done":
                    yield f"data: {json.dumps(event.payload)}\n\n"
                    return
        finally:
            await subscription.close()

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
async def stream_signals(session_id: str, after_seq: int = 0) -> Any:
    # Pre-flight 404 guard before opening the stream (ADR-0076).
    if not await session_exists(session_id):
        raise NotFoundError(f"Session '{session_id}' not found")

    async def generate():
        from .tail_broker import subscribe_session_signals

        subscription = await subscribe_session_signals(session_id, max(0, after_seq))
        try:
            while True:
                try:
                    event = await asyncio.wait_for(subscription.next_event(), timeout=5.0)
                except asyncio.TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'
                    continue
                if event.kind == "data":
                    event_id = str(event.resume_cursor or "")
                    prefix = f"id: {event_id}\n" if event_id else ""
                    yield f"{prefix}data: {json.dumps(event.payload)}\n\n"
                elif event.kind == "resync":
                    yield f"data: {json.dumps({'type': 'resync', 'after_seq': event.resume_cursor})}\n\n"
                    return
                elif event.kind == "done":
                    yield f"data: {json.dumps(event.payload)}\n\n"
                    return
        finally:
            await subscription.close()

    from ._sse import sse_response

    return sse_response(generate())
