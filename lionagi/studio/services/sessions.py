from __future__ import annotations

import asyncio
import base64
import heapq
import json
import time
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


# What one session read is allowed to decode: three independent ceilings (rows, per-row size,
# total decoded), since none of them bounds the others.

# What one message payload may decode to, enforced in SQL before a parser runs. An oversized
# row is withheld (not dropped), and the read reports itself as bounded.
MAX_ACTION_CONTENT_CHARS = 1_048_576

# What one session read may decode in total, across every row it holds.
MAX_HYDRATED_CONTENT_CHARS = 64 * 1_048_576

# How many rows one session read may hold, across every reader. A withheld row decodes nothing,
# so without this a stream of withheld rows would cost nothing and run unbounded.
MAX_HYDRATED_ROWS = 50_000

# How many action rows one session detail pulls, newest first, across all branches; every
# aggregate derived from action messages reads this same set and reports when it binds.
MAX_HYDRATED_ACTION_MESSAGES = 20_000

# How many distinct file paths one session detail will collect, over the whole run. Generous
# enough that an ordinary session never meets it; reported when it does.
MAX_ACTION_FILE_PATHS = 5_000

# What the file-path union may weigh in bytes, since a row count alone says nothing about path
# length. Both ceilings report through the same cut flag.
MAX_ACTION_FILE_PATH_BYTES = 1_048_576

# How many action rows the file-path union will scan before stopping -- bounds the work of
# reaching an answer, not the answer itself.
MAX_ACTION_FILE_ROWS_SCANNED = 200_000

# Max length for an extracted action_request_id/action_response_id link, cut well below any real
# id so a writer cannot route a payload out through those keys.
MAX_ACTION_ID_CHARS = 256

# How much text SQLite may scan to recover a withheld row's link id -- separate from the decode
# ceiling because finding a short id still means parsing the whole document.
MAX_ACTION_ID_SCAN_CHARS = 16 * MAX_ACTION_CONTENT_CHARS

# Only a withheld row needs its ids extracted this way; a kept payload already carries them.
# json_valid guards non-JSON content, since json_extract would otherwise abort the whole query.
_ACTION_ID = (
    "CASE WHEN length(m.content) > ? AND length(m.content) <= ? THEN "
    "substr(json_extract(CASE WHEN json_valid(m.content) THEN m.content END, '$.{key}'), "
    f"1, {MAX_ACTION_ID_CHARS}) END"
)

_BOUNDED_CONTENT_COLUMNS = (
    "CASE WHEN length(m.content) > ? THEN NULL ELSE m.content END AS content, "
    "CASE WHEN length(m.content) > ? THEN 1 ELSE 0 END AS content_oversized, "
    "CASE WHEN length(m.content) > ? THEN 0 ELSE length(m.content) END AS content_length, "
    # A withheld row still needs to be pairable, so its link ids are extracted here without
    # decoding the payload -- bounded, or an unbounded slice would give back what was withheld.
    f"{_ACTION_ID.format(key='action_request_id')} AS action_request_id, "
    f"{_ACTION_ID.format(key='action_response_id')} AS action_response_id"
)


class _HydrationBudget:
    """Decode budget for one session read, shared across every reader instead of per-call."""

    __slots__ = ("exhausted", "remaining", "rows_remaining")

    def __init__(self, total: int | None = None, rows: int | None = None) -> None:
        # Read here, not as a default argument, so it reflects the current module constant
        # rather than freezing whatever it was when this file was imported.
        self.remaining = MAX_HYDRATED_CONTENT_CHARS if total is None else total
        self.rows_remaining = MAX_HYDRATED_ROWS if rows is None else rows
        self.exhausted = False

    def admits(self, chars: int) -> bool:
        """Charge one row against both the character and row allowances, or refuse it."""
        if chars > self.remaining or self.rows_remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= chars
        self.rows_remaining -= 1
        return True


def _format_message(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    """Shape one hydrated message row; requires `_BOUNDED_CONTENT_COLUMNS` in the query."""
    withheld = bool(row["content_oversized"])
    formatted = {
        "id": row["id"],
        "role": row["role"],
        "content": _parse_json_col(row["content"]),
        # The row is still listed with identity/timing intact; a reader must be able to tell
        # a withheld payload apart from a message that carried no content.
        "content_withheld": withheld,
        "sender": row["sender"],
        "timestamp": row["created_at"],
        "lion_class": row["lion_class_str"] or "",
    }
    if withheld:
        # Only on a withheld row: a kept payload already carries these ids, so lifting them
        # out there would widen the shape every reader sees for no reason.
        for key in ("action_request_id", "action_response_id"):
            value = row[key]
            if value:
                formatted[key] = str(value)
    return formatted


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
            clauses.append(f"(COALESCE(s.status, 'completed') IN ({placeholders}){null_clause})")
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
    "recent": "s.updated_at DESC",
    "cost": "s.total_cost_usd IS NULL, s.total_cost_usd DESC, s.updated_at DESC",
}


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
    require_file_store()
    if not store_exists():
        return []

    limit = max(1, min(int(limit), MAX_SESSION_PAGE))
    offset = max(0, int(offset))
    clause, params = (where or SessionFilter()).where()
    order_by = _SESSION_SORTS.get(sort, _SESSION_SORTS["recent"])

    async with _open_db(store_path()) as db:
        # run_tags is created lazily on first tag write, so a tag filter would
        # fail on a store that has never been tagged.
        if (where or SessionFilter()).tags:
            from .run_tags import _ensure_table

            await _ensure_table(db)
        approximate_end = await _approximate_end_selection(db, alias="s")
        cur = await db.execute(
            f"""
            WITH page AS (
                SELECT s.id AS page_id
                FROM sessions s
                {clause}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
            )
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
                COUNT(DISTINCT b.id) AS branch_count,
                COALESCE(SUM(
                    json_array_length(p.collection)
                ), 0) AS message_count
            FROM page
            JOIN sessions s ON s.id = page.page_id
            LEFT JOIN branches b ON b.session_id = s.id
            LEFT JOIN progressions p ON p.id = b.progression_id
            GROUP BY s.id
            ORDER BY {order_by}
            """,  # noqa: S608
            [*params, limit, offset],
        )
        rows = await cur.fetchall()

    return [
        {
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
        for row in rows
    ]


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


# Names "the newest end", so a branch whose first page can't afford a single row still stays in
# the cursor instead of reading as exhausted and losing its messages. No UUID can collide with it.
_NEWEST_ANCHOR = "@newest"


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
        if anchor == _NEWEST_ANCHOR:
            end = len(msg_ids)
        elif anchor not in msg_ids:
            raise MessageCursorError(
                f"message_cursor anchor not found in branch {branch_id!r} progression"
            )
        else:
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


def _resume_anchor(
    window_ids: list[str],
    present_ids: list[str],
    *,
    has_older: bool,
    next_anchor: str | None,
    current_anchor: str | None,
    budget_refused: bool,
) -> tuple[bool, str | None]:
    """Where the next page resumes: the oldest row actually delivered (not the window's own
    oldest id), or the same window again if the budget refused it outright; a window whose ids
    are simply absent from the store still advances, since nothing was owed for it."""
    if len(present_ids) == len(window_ids):
        return has_older, next_anchor
    if present_ids:
        return True, present_ids[0]
    if not budget_refused:
        return has_older, next_anchor
    return True, current_anchor or _NEWEST_ANCHOR


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
        # True when the action-message pass stopped at its bound, so the four
        # fields above describe the session's most recent action messages
        # rather than all of them: the counts are floors and the errors and
        # files are the ones that fell inside the window.
        "bounded": False,
    }


async def _fetch_messages_by_ids(
    db: aiosqlite.Connection,
    msg_ids: list[str],
    *,
    budget: _HydrationBudget,
) -> list[dict[str, Any]]:
    """Hydrate message rows for msg_ids, chunked under SQLite's bound-variable limit, spending
    from the same shared request budget as every other reader."""
    if not msg_ids:
        return []
    chunks = [msg_ids[start : start + 500] for start in range(0, len(msg_ids), 500)]

    # Pass one decides who is paid for, reading sizes only, walked from the newest end in the
    # caller's own order -- an IN(...) query returns rows in planner order, not request order.
    admitted: list[str] = []
    for chunk in reversed(chunks):
        if budget.exhausted:
            break
        placeholders = ",".join("?" for _ in chunk)
        cur = await db.execute(
            f"SELECT m.id, length(m.content) AS content_length "  # noqa: S608
            f"FROM messages m WHERE m.id IN ({placeholders})",
            chunk,
        )
        sizes = {row["id"]: int(row["content_length"] or 0) for row in await cur.fetchall()}
        for msg_id in reversed(chunk):
            if msg_id not in sizes:
                continue
            # Charge what pass two will actually hand back, not the row's raw size: an oversized
            # row is withheld (content NULL) and costs nothing in characters.
            charged = 0 if sizes[msg_id] > MAX_ACTION_CONTENT_CHARS else sizes[msg_id]
            if not budget.admits(charged):
                break
            admitted.append(msg_id)

    # Pass two hydrates exactly what pass one paid for, so no payload crosses
    # the boundary without a charge behind it.
    rows_by_id: dict[str, dict[str, Any]] = {}
    for start in range(0, len(admitted), 500):
        chunk = admitted[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        cur = await db.execute(
            f"""
            SELECT m.id, m.created_at, m.sender, m.role,
                   {_BOUNDED_CONTENT_COLUMNS},
                   mt.lion_class AS lion_class_str
            FROM messages m
            LEFT JOIN message_types mt ON m.lion_class = mt.type_id
            WHERE m.id IN ({placeholders})
            """,  # noqa: S608
            [
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_ID_SCAN_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_ID_SCAN_CHARS,
                *chunk,
            ],
        )
        async for row in cur:
            data = dict(row)
            data.pop("content_length", None)
            rows_by_id[data["id"]] = _format_message(data)
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
    db: aiosqlite.Connection,
    ids_by_branch: dict[str, list[str]],
    *,
    limit: int,
    budget: _HydrationBudget,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Hydrate at most *limit* action rows across the whole session, newest first, as one
    selection over every branch (not a per-branch share, which would depend on visit order);
    two passes so the cap applies before payloads are read. Returns (messages_by_branch, bounded)."""
    empty: dict[str, list[dict[str, Any]]] = {branch_id: [] for branch_id in ids_by_branch}
    any_ids = any(ids_by_branch.values())
    if not any_ids or limit <= 0:
        return empty, any_ids
    if budget.exhausted:
        # The display windows already spent the total. Say so rather than
        # reading a first row for free.
        return empty, True
    class_placeholders = ",".join("?" for _ in _ACTION_LION_CLASSES)
    cur = await db.execute(
        f"SELECT type_id FROM message_types WHERE lion_class IN ({class_placeholders})",  # noqa: S608
        _ACTION_LION_CLASSES,
    )
    type_ids = [row["type_id"] for row in await cur.fetchall()]
    if not type_ids:
        return empty, False
    type_placeholders = ",".join("?" for _ in type_ids)

    # A heap of the newest `limit` seen so far, so what is held is the size of the answer,
    # not of the whole progression.
    newest: list[tuple[float, str]] = []
    candidates = 0
    for msg_ids in ids_by_branch.values():
        for chunk_start in range(0, len(msg_ids), 500):
            chunk = msg_ids[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            # `+m.lion_class` disqualifies the lion_class index so the planner probes the id primary
            # key for the IN list instead of rescanning every action-class row per chunk.
            cur = await db.execute(
                f"""
                SELECT m.id, m.created_at
                FROM messages m
                WHERE m.id IN ({placeholders}) AND +m.lion_class IN ({type_placeholders})
                """,  # noqa: S608
                [*chunk, *type_ids],
            )
            async for row in cur:
                candidates += 1
                key = (row["created_at"] or 0.0, row["id"])
                if len(newest) < limit:
                    heapq.heappush(newest, key)
                elif key > newest[0]:
                    heapq.heapreplace(newest, key)

    # Oldest to newest, so the hydration budget -- which pays from the end of the list it is
    # handed -- pays for the newest of the selection first.
    ordered_ids = [message_id for _, message_id in sorted(newest)]
    messages = await _fetch_messages_by_ids(db, ordered_ids, budget=budget)
    hydrated = {message["id"]: message for message in messages}
    # Message ids are unique to one branch, so a selected row belongs to exactly one
    # progression and this splits the selection without duplicating it.
    by_branch = {
        branch_id: [hydrated[mid] for mid in msg_ids if mid in hydrated]
        for branch_id, msg_ids in ids_by_branch.items()
    }
    # Three ways this describes less than what is there: the cap dropped older rows, the budget
    # stopped hydration short, or a payload was withheld for its size.
    bounded = (
        candidates > limit
        or len(messages) < len(ordered_ids)
        or any(message["content_withheld"] for message in messages)
    )
    return by_branch, bounded


_FILE_TOOL_NAMES = frozenset(
    {
        "read",
        "read_file",
        "write",
        "write_file",
        "edit",
        "edit_file",
        "multiedit",
        "notebookedit",
    }
)


def _action_file_path(function: Any, arguments: Any) -> str | None:
    """The file an action request touched, or None -- shared by the per-branch stats and the
    run-wide file union so both agree on what counts as a file call."""
    arguments = arguments if isinstance(arguments, dict) else {}
    tool_name = str(function or "").lower().replace("-", "_").rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    if tool_name and tool_name not in _FILE_TOOL_NAMES:
        return None
    file_path = arguments.get("file_path") or arguments.get("path")
    return file_path if isinstance(file_path, str) and file_path else None


async def _fetch_action_file_paths(
    db: aiosqlite.Connection, ids_by_branch: dict[str, list[str]]
) -> tuple[list[str], bool]:
    """Every file this session's action requests touched, over the whole run -- a union, not the
    newest slice the hydrated set uses, since a reference can resolve against an old name.
    Decodes nothing (SQLite extracts short fields only) and reports whether it was cut short."""
    class_placeholders = ",".join("?" for _ in _ACTION_LION_CLASSES)
    cur = await db.execute(
        f"SELECT type_id, lion_class FROM message_types WHERE lion_class IN ({class_placeholders})",  # noqa: S608
        _ACTION_LION_CLASSES,
    )
    type_rows = await cur.fetchall()
    type_ids = [row["type_id"] for row in type_rows]
    if not type_ids:
        # No action rows to union, and nothing was cut reaching that answer.
        return [], False
    type_placeholders = ",".join("?" for _ in type_ids)
    # Only a request carries the fields a path is read from; an oversized response withheld
    # nothing the union wanted, so it must not count as a cut.
    request_type_ids = {
        row["type_id"]
        for row in type_rows
        if _short_lion_class(row["lion_class"]) == "ActionRequest"
    }

    paths: set[str] = set()
    path_bytes = 0
    rows_scanned = 0
    omitted_oversized = False

    def at_ceiling() -> bool:
        return (
            len(paths) >= MAX_ACTION_FILE_PATHS
            or path_bytes >= MAX_ACTION_FILE_PATH_BYTES
            or rows_scanned >= MAX_ACTION_FILE_ROWS_SCANNED
        )

    def union_was_cut() -> bool:
        # An omitted oversized row is a cut with no counter behind it, so it's checked separately.
        return at_ceiling() or omitted_oversized

    for msg_ids in ids_by_branch.values():
        if at_ceiling():
            break
        for chunk_start in range(0, len(msg_ids), 500):
            if at_ceiling():
                break
            chunk = msg_ids[chunk_start : chunk_start + 500]
            # Charged before the query, against the ids handed to it rather than the rows it
            # gives back, so a progression cannot pass through free by having its rows filtered.
            rows_scanned += len(chunk)
            placeholders = ",".join("?" for _ in chunk)
            cur = await db.execute(
                f"""
                -- json_valid beside the length check, for the reason given at
                -- _ACTION_ID: json_extract raises on malformed content and
                -- takes the whole read down with it, and a row that did not
                -- come from this application is a state the store is allowed
                -- to be in. Short-circuits, so the extraction still never runs
                -- on oversized payloads.
                SELECT CASE WHEN length(m.content) <= ? AND json_valid(m.content)
                            THEN json_extract(m.content, '$.function') END AS fn,
                       CASE WHEN length(m.content) <= ? AND json_valid(m.content)
                            THEN json_extract(m.content, '$.arguments.file_path') END AS file_path,
                       CASE WHEN length(m.content) <= ? AND json_valid(m.content)
                            THEN json_extract(m.content, '$.arguments.path') END AS path,
                       length(m.content) > ? AS oversized,
                       m.lion_class AS type_ref
                FROM messages m
                WHERE m.id IN ({placeholders}) AND +m.lion_class IN ({type_placeholders})
                """,  # noqa: S608
                [
                    MAX_ACTION_CONTENT_CHARS,
                    MAX_ACTION_CONTENT_CHARS,
                    MAX_ACTION_CONTENT_CHARS,
                    MAX_ACTION_CONTENT_CHARS,
                    *chunk,
                    *type_ids,
                ],
            )
            async for row in cur:
                # Selected rather than filtered, so the row still arrives and can be reported;
                # the CASE guards keep extraction off oversized content, only short fields cross.
                if row["oversized"]:
                    if row["type_ref"] in request_type_ids:
                        omitted_oversized = True
                    continue
                found = _action_file_path(
                    row["fn"], {"file_path": row["file_path"], "path": row["path"]}
                )
                if found and found not in paths:
                    paths.add(found)
                    path_bytes += len(found.encode())
                if at_ceiling():
                    break
    # Equality, not the count alone: a run sitting exactly on a ceiling reports bounded, which
    # is the safe direction for a set used to decide whether a name is a file.
    return sorted(paths), union_was_cut()


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
        file_path = _action_file_path(function, arguments)
        if file_path:
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
                "SELECT id, name, created_at, progression_id, model, provider, agent_name, status, started_at, ended_at FROM branches WHERE session_id = ? ORDER BY created_at",
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

        progression_ids: dict[str, list[str]] = {}
        for br in branch_rows:
            ids: list[str] = []
            prog_id = br["progression_id"]
            if prog_id:
                prog_cur = await db.execute(
                    "SELECT collection FROM progressions WHERE id = ?",
                    (prog_id,),
                )
                prog_row = await prog_cur.fetchone()
                if prog_row and prog_row["collection"]:
                    try:
                        ids = json.loads(prog_row["collection"])
                    except (json.JSONDecodeError, TypeError):
                        ids = []
            progression_ids[br["id"]] = ids

        # One budget for the whole read, not one per branch or reader, so it isn't multiplied by
        # branch count. Display windows spend from it first; the action aggregate can degrade
        # gracefully (it already reports itself as bounded), so it spends what's left.
        content_budget = _HydrationBudget()

        window_by_branch: dict[str, tuple[list[dict[str, Any]], bool]] = {}
        for br in branch_rows:
            branch_id = br["id"]
            # Window from the tail: offset/cursor 0 = the newest page,
            # each page further back prepends older history.
            window_ids, has_older, next_anchor = _window_message_ids(
                progression_ids[branch_id],
                branch_id=branch_id,
                limit=message_limit,
                cursor_anchors=cursor_anchors,
                legacy_offset=message_offset if cursor_anchors is None else 0,
            )
            window_messages = await _fetch_messages_by_ids(db, window_ids, budget=content_budget)
            by_id = {m["id"]: m for m in window_messages}
            present = [by_id[mid] for mid in window_ids if mid in by_id]
            has_older, next_anchor = _resume_anchor(
                window_ids,
                [m["id"] for m in present],
                has_older=has_older,
                next_anchor=next_anchor,
                current_anchor=cursor_anchors.get(branch_id) if cursor_anchors else None,
                # Request-wide rather than window-specific: erring toward asking again cannot
                # lose a row, but erring toward advancing can.
                budget_refused=content_budget.exhausted,
            )
            if next_anchor:
                next_branch_anchors[branch_id] = next_anchor
            window_by_branch[branch_id] = (present, has_older)

        # Row count gets its own ceiling too, spent over the session rather than per branch, so
        # which rows survive depends only on when they happened.
        action_by_branch, action_messages_bounded = await _fetch_action_messages(
            db,
            progression_ids,
            limit=MAX_HYDRATED_ACTION_MESSAGES,
            budget=content_budget,
        )

        for br in branch_rows:
            branch_id = br["id"]
            full_msg_ids = progression_ids[branch_id]
            message_total = len(full_msg_ids)
            messages, has_older = window_by_branch[branch_id]

            role_counts = await _fetch_role_counts(db, full_msg_ids)
            first_message_at, last_message_at = await _fetch_message_bounds(db, full_msg_ids)
            action_messages = action_by_branch[branch_id]
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

            br_keys = br.keys()
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

        full_stats["bounded"] = action_messages_bounded
        # Read over every action row, not the hydrated slice: a file union can't be an honest
        # floor the way the counts beside it are, since it decides whether a name is a file at all.
        full_stats["files"], files_bounded = await _fetch_action_file_paths(db, progression_ids)
        # Its own flag rather than folding into `bounded`, so a caller resolving a file
        # reference knows this union specifically was cut.
        full_stats["files_bounded"] = files_bounded
        message_next_cursor = (
            _encode_message_cursor(session_id, message_limit, next_branch_anchors)
            if next_branch_anchors
            else None
        )

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
        "message_stats": full_stats,
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


async def get_session_messages_after(
    session_id: str,
    after_ts: float,
    after_id: str | None = None,
    after_branch: str | None = None,
) -> list[dict[str, Any]]:
    """Poll-friendly, bounded tail read for the SSE stream/signals endpoints. Joins via json_each
    instead of an IN (...) clause to stay under SQLite's bound-variable limit, and cursors on the
    full sort key ``(created_at, id, branch_id)`` so it can resume mid-group rather than take a
    whole timestamp-sharing group unbounded; ``after_id``/``after_branch`` are optional for
    backward compatibility with the old timestamp-only cursor."""
    if not store_exists():
        return []

    # A bare timestamp keeps the old exclusive read; a full cursor resumes at the row after the
    # one it names, letting a group sharing one timestamp be cut in the middle.
    if after_id is None or after_branch is None:
        cursor_sql = "m.created_at > ?"
        cursor_params: tuple[Any, ...] = (after_ts,)
    else:
        cursor_sql = (
            "(m.created_at > ?"
            " OR (m.created_at = ? AND m.id > ?)"
            " OR (m.created_at = ? AND m.id = ? AND b.id > ?))"
        )
        cursor_params = (after_ts, after_ts, after_id, after_ts, after_id, after_branch)

    async with _open_db(store_path()) as db:
        cur = await db.execute(
            f"""
            SELECT m.id, m.created_at, m.sender, m.role,
                   {_BOUNDED_CONTENT_COLUMNS},
                   mt.lion_class AS lion_class_str, b.id AS branch_id
            FROM branches b
            JOIN progressions p ON p.id = b.progression_id
            JOIN json_each(p.collection) je ON 1=1
            JOIN messages m ON m.id = je.value
            LEFT JOIN message_types mt ON m.lion_class = mt.type_id
            WHERE b.session_id = ? AND {cursor_sql}
            ORDER BY m.created_at, m.id, b.id
            LIMIT ?
            """,  # noqa: S608
            (
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_ID_SCAN_CHARS,
                MAX_ACTION_CONTENT_CHARS,
                MAX_ACTION_ID_SCAN_CHARS,
                session_id,
                *cursor_params,
                # The loop below stops at the row budget, so this is exactly what it could ever
                # take. Without it ORDER BY sorts the whole remaining stream on every page.
                MAX_HYDRATED_ROWS + 1,
            ),
        )

        budget = _HydrationBudget()
        result: list[dict[str, Any]] = []
        # Iterated rather than fetchall'd: a budget can only bound what has not
        # been read yet.
        async for row in cur:
            # Charged on every row including the first, so a row taken is always a row paid for.
            admitted = budget.admits(int(row["content_length"] or 0))
            if not admitted:
                # The sort key is the whole cursor, so cutting here skips nothing, even inside
                # a run of rows sharing a timestamp.
                if result:
                    break
                # Nothing taken yet -- stopping now would leave the cursor unmoved and the next
                # poll would read the same row forever, so this one row is let through anyway.
            msg = _format_message(row)
            msg["branch_id"] = row["branch_id"]
            result.append(msg)
            if not admitted:
                break
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
    sort: str = Query(
        default="recent",
        description="Sort order: 'recent' (default) or 'cost' (highest reported spend first)",
    ),
) -> dict[str, Any]:
    """One page of sessions. The response always reports `total` and
    `truncated` so a bounded answer can never be mistaken for a complete one."""
    if sort not in _SESSION_SORTS:
        raise HTTPException(status_code=422, detail="sort must be one of: recent, cost")
    sessions = await list_sessions(limit=limit, offset=offset, sort=sort)
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
        after_id: str | None = None
        after_branch: str | None = None
        last_heartbeat = time.monotonic()

        while True:
            cursor = (after_ts, after_id, after_branch)
            messages = await get_session_messages_after(
                session_id, after_ts, after_id, after_branch
            )

            if messages:
                for msg in messages:
                    yield f"data: {json.dumps(msg)}\n\n"
                # Rows come back in cursor order, so the last one is the resume position; the
                # newest timestamp alone would name a whole group rather than a specific row.
                last = messages[-1]
                after_ts = last.get("timestamp") or last.get("created_at") or after_ts
                after_id = last.get("id")
                after_branch = last.get("branch_id")
                last_heartbeat = time.monotonic()
                # A page may have left rows behind the budget; loop immediately (skipping the
                # done check) to drain them, but only while the cursor is actually moving.
                if (after_ts, after_id, after_branch) != cursor:
                    continue

            if time.monotonic() - last_heartbeat >= 5.0:
                yield 'data: {"type":"heartbeat"}\n\n'
                last_heartbeat = time.monotonic()

            # Reached only once the cursor stopped moving, so "done" describes the whole
            # stream rather than one page of it.
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
                # get_signals_after is itself page-limited, so a non-empty
                # batch does not mean the client is caught up to the tip —
                # loop again immediately instead of falling through to the
                # done-check below. Checking "done" here would let a
                # long-completed session's first (oldest) page read as the
                # whole stream and close the connection before the rest ever
                # sends.
                continue

            if time.monotonic() - last_heartbeat >= 5.0:
                yield 'data: {"type":"heartbeat"}\n\n'
                last_heartbeat = time.monotonic()

            state = await get_session_stream_state(session_id)
            if is_session_stream_done(state, now=time.time()):
                yield 'data: {"type":"done"}\n\n'
                return

            await asyncio.sleep(0.5)

    from ._sse import sse_response

    return sse_response(generate())
