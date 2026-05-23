from __future__ import annotations

import json
from typing import Any

import aiosqlite

from lionagi.state.db import DEFAULT_DB_PATH

from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)

SESSION_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "timed_out", "aborted", "cancelled"}
)
SESSION_DONE_STABLE_SECS = 60.0


def _parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
        return meta if isinstance(meta, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


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
        nodes.append({
            "id": op["id"],
            "label": op["id"],
            "role": agent.get("name", ""),
            "assignment": agent.get("model", ""),
            "prompt": "",
            "capacity": 1,
            "timeout": None,
            "inputs": depends_on,
            "outputs": [],
        })
        for dep in depends_on:
            edges.append({
                "id": f"e-{dep}-{op['id']}",
                "source": dep,
                "target": op["id"],
                "mode": "simple",
            })
    return {"nodes": nodes, "edges": edges} if nodes else None


def _parse_json_col(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _format_message(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": _parse_json_col(row["content"]),
        "sender": row["sender"],
        "timestamp": row["created_at"],
        "lion_class": row["lion_class_str"] or "",
    }


async def list_sessions() -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []

    async with _open_db(_DB) as db:
        cur = await db.execute(
            """
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
                COUNT(DISTINCT b.id) AS branch_count,
                COALESCE(SUM(
                    json_array_length(p.collection)
                ), 0) AS message_count
            FROM sessions s
            LEFT JOIN branches b ON b.session_id = s.id
            LEFT JOIN progressions p ON p.id = b.progression_id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            """
        )
        rows = await cur.fetchall()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"] or 0.0,
            "branch_count": row["branch_count"],
            "message_count": row["message_count"],
            # F-A1-2 / F-A1-8 (ADR-0017): read status directly from column;
            # fall back to "completed" only for legacy rows where status is NULL.
            "status": row["status"] or "completed",
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            # ADR-0019: caller (runs service) feeds this to staleness_check.
            "last_message_at": row["last_message_at"],
            # ADR-0020: optional parent skill orchestration.
            "invocation_id": row["invocation_id"],
            # ADR-0022: provenance disclosure — resolved values.
            "model": row["model"],
            "provider": row["provider"],
            "effort": row["effort"],
            "agent_hash": row["agent_hash"],
            "playbook_name": row["playbook_name"],
            "agent_name": row["agent_name"],
            "invocation_kind": row["invocation_kind"],
            "show_topic": row["show_topic"],
            "show_play_name": row["show_play_name"],
            "source_kind": row["source_kind"] or "live",
            # ADR-0026: project detection.
            "project": row["project"],
            "project_source": row["project_source"],
        }
        for row in rows
    ]


async def get_session(session_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None

    async with _open_db(_DB) as db:
        cur = await db.execute(
            # F-A1-4 (ADR-0017): include lifecycle columns in session detail
            # ADR-0022: include provenance columns (model/provider/effort/agent_hash)
            """SELECT id, name, created_at, updated_at,
                      playbook_name, agent_name, invocation_kind,
                      show_topic, show_play_name, artifacts_path, source_kind,
                      status, started_at, ended_at,
                      model, provider, effort, agent_hash, invocation_id,
                      node_metadata, project, project_source
               FROM sessions WHERE id = ?""",
            (session_id,),
        )
        session_row = await cur.fetchone()
        if not session_row:
            return None

        # Reverse lookup: find the play that references this session
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
        for br in branch_rows:
            messages = []
            prog_id = br["progression_id"]
            if prog_id:
                prog_cur = await db.execute(
                    "SELECT collection FROM progressions WHERE id = ?",
                    (prog_id,),
                )
                prog_row = await prog_cur.fetchone()
                if prog_row and prog_row["collection"]:
                    try:
                        msg_ids = json.loads(prog_row["collection"])
                    except (json.JSONDecodeError, TypeError):
                        msg_ids = []

                    if msg_ids:
                        placeholders = ",".join("?" for _ in msg_ids)
                        msg_cur = await db.execute(
                            f"""
                            SELECT m.id, m.created_at, m.content, m.sender, m.role,
                                   mt.lion_class AS lion_class_str
                            FROM messages m
                            LEFT JOIN message_types mt ON m.lion_class = mt.type_id
                            WHERE m.id IN ({placeholders})
                            """,  # noqa: S608
                            msg_ids,
                        )
                        msg_rows = await msg_cur.fetchall()
                        by_id = {r["id"]: _format_message(r) for r in msg_rows}
                        messages = [by_id[mid] for mid in msg_ids if mid in by_id]

            br_keys = br.keys()
            branches.append(
                {
                    "id": br["id"],
                    "name": br["name"],
                    "created_at": br["created_at"],
                    "messages": messages,
                    "model": br["model"],
                    "provider": br["provider"],
                    "agent_name": br["agent_name"],
                    "status": br["status"] if "status" in br_keys else None,
                    "started_at": br["started_at"] if "started_at" in br_keys else None,
                    "ended_at": br["ended_at"] if "ended_at" in br_keys else None,
                }
            )

    # F-A1-4 (ADR-0017): compute duration_ms from lifecycle timestamps
    started_at = session_row["started_at"]
    ended_at = session_row["ended_at"]
    duration_ms = (
        (ended_at - started_at) * 1000 if started_at is not None and ended_at is not None else None
    )

    return {
        "id": session_row["id"],
        "name": session_row["name"],
        "created_at": session_row["created_at"],
        "updated_at": session_row["updated_at"],
        "playbook_name": session_row["playbook_name"],
        "agent_name": session_row["agent_name"],
        "invocation_kind": session_row["invocation_kind"],
        "show_topic": session_row["show_topic"],
        "show_play_name": session_row["show_play_name"],
        "artifacts_path": session_row["artifacts_path"],
        "source_kind": session_row["source_kind"] or "live",
        "status": session_row["status"] or "completed",
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "source_show": source_show,
        "branches": branches,
        # ADR-0022: provenance disclosure — same fields exposed on list_sessions().
        "model": session_row["model"],
        "provider": session_row["provider"],
        "effort": session_row["effort"],
        "agent_hash": session_row["agent_hash"],
        "invocation_id": session_row["invocation_id"],
        # ADR-0026: project detection.
        "project": session_row["project"],
        "project_source": session_row["project_source"],
        "graph": _graph_from_metadata(session_row["node_metadata"]),
        "segments": (_parse_metadata(session_row["node_metadata"]) or {}).get("segments"),
    }


async def get_session_messages_after(session_id: str, after_ts: float) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []

    async with _open_db(_DB) as db:
        branch_cur = await db.execute(
            "SELECT id, progression_id FROM branches WHERE session_id = ?",
            (session_id,),
        )
        branch_rows = await branch_cur.fetchall()

        result = []
        for br in branch_rows:
            branch_id = br["id"]
            prog_id = br["progression_id"]
            if not prog_id:
                continue

            prog_cur = await db.execute(
                "SELECT collection FROM progressions WHERE id = ?",
                (prog_id,),
            )
            prog_row = await prog_cur.fetchone()
            if not prog_row or not prog_row["collection"]:
                continue

            try:
                msg_ids = json.loads(prog_row["collection"])
            except (json.JSONDecodeError, TypeError):
                continue

            if not msg_ids:
                continue

            placeholders = ",".join("?" for _ in msg_ids)
            msg_cur = await db.execute(
                f"""
                SELECT m.id, m.created_at, m.content, m.sender, m.role,
                       mt.lion_class AS lion_class_str
                FROM messages m
                LEFT JOIN message_types mt ON m.lion_class = mt.type_id
                WHERE m.id IN ({placeholders}) AND m.created_at > ?
                """,  # noqa: S608
                (*msg_ids, after_ts),
            )
            msg_rows = await msg_cur.fetchall()
            by_id = {r["id"]: r for r in msg_rows}

            for mid in msg_ids:
                if mid in by_id:
                    msg = _format_message(by_id[mid])
                    msg["branch_id"] = branch_id
                    result.append(msg)

    return result


async def session_exists(session_id: str) -> bool:
    if not DEFAULT_DB_PATH.exists():
        return False

    async with _open_db(_DB) as db:
        cur = await db.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
        return row is not None


async def get_session_stream_state(session_id: str) -> dict[str, Any] | None:
    """Scalar read for the SSE done-condition check — avoids the full get_session() round-trip."""
    if not DEFAULT_DB_PATH.exists():
        return None

    async with _open_db(_DB) as db:
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
    """Return True only when the session is in a terminal status AND has been stable >= 60s.

    Both conditions must hold — terminal status alone might be a transient write;
    stale time alone would close active sessions that haven't received messages recently.
    """
    if state is None:
        return False
    return (
        state.get("status") in SESSION_TERMINAL_STATUSES
        and now - float(state.get("updated_at") or 0.0) > SESSION_DONE_STABLE_SECS
    )
