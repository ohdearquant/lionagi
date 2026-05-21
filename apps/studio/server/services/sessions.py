from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

from lionagi.state.db import DEFAULT_DB_PATH

_DB = str(DEFAULT_DB_PATH)


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

    now = time.time()
    async with aiosqlite.connect(_DB) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        db.row_factory = aiosqlite.Row
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
            "status": "running" if (now - (row["updated_at"] or 0)) <= 60 else "completed",
            "playbook_name": row["playbook_name"],
            "agent_name": row["agent_name"],
            "invocation_kind": row["invocation_kind"],
            "show_topic": row["show_topic"],
            "show_play_name": row["show_play_name"],
            "source_kind": row["source_kind"] or "live",
        }
        for row in rows
    ]


async def get_session(session_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None

    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            """SELECT id, name, created_at, updated_at,
                      playbook_name, agent_name, invocation_kind,
                      show_topic, show_play_name, artifacts_path, source_kind
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

        branch_cur = await db.execute(
            "SELECT id, name, created_at, progression_id FROM branches WHERE session_id = ? ORDER BY created_at",
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
                            """,
                            msg_ids,
                        )
                        msg_rows = await msg_cur.fetchall()
                        by_id = {r["id"]: _format_message(r) for r in msg_rows}
                        messages = [by_id[mid] for mid in msg_ids if mid in by_id]

            branches.append(
                {
                    "id": br["id"],
                    "name": br["name"],
                    "created_at": br["created_at"],
                    "messages": messages,
                }
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
        "source_show": source_show,
        "branches": branches,
    }


async def get_session_messages_after(
    session_id: str, after_ts: float
) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []

    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row

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
                """,
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

    async with aiosqlite.connect(_DB) as db:
        cur = await db.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
        return row is not None
