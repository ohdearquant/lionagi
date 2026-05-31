# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0020 invocations service.

Backs the /api/invocations endpoints. Reads from state.db's
``invocations`` and ``sessions`` tables.
"""

from __future__ import annotations

import json
from typing import Any

from lionagi.state.db import DEFAULT_DB_PATH, StateDB


async def list_invocations(
    *,
    skill: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        rows = await db.list_invocations(skill=skill, status=status, limit=limit, offset=offset)
    out: list[dict[str, Any]] = []
    for r in rows:
        node_meta = r.get("node_metadata")
        if isinstance(node_meta, str):
            try:
                node_meta = json.loads(node_meta)
            except json.JSONDecodeError:
                node_meta = None
        out.append(
            {
                "id": r["id"],
                "skill": r["skill"],
                "plugin": r.get("plugin"),
                "prompt": r.get("prompt"),
                "started_at": r["started_at"],
                "ended_at": r.get("ended_at"),
                "status": r["status"],
                "session_count": r.get("session_count", 0),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "node_metadata": node_meta,
                # ADR-0026: project provenance from the most-recently updated
                # child session.  NULL when the invocation has no sessions yet.
                "project": r.get("project"),
                "project_source": r.get("project_source"),
            }
        )
    return out


async def get_invocation(invocation_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        row = await db.get_invocation(invocation_id)
        if row is None:
            return None
        node_meta = row.get("node_metadata")
        if isinstance(node_meta, str):
            try:
                node_meta = json.loads(node_meta)
            except json.JSONDecodeError:
                node_meta = None
        sessions = await db.list_sessions_for_invocation(invocation_id)
        # ADR-0021: surface structured outcomes alongside child sessions
        # so the invocation detail page can render verdict / CI / gate
        # cards inline. Filesystem blobs (file_path) are included by
        # reference; the frontend renders them as JSON when the kind is
        # unknown.
        artifacts = await db.list_artifacts_for_invocation(invocation_id)
    return {
        "id": row["id"],
        "skill": row["skill"],
        "plugin": row.get("plugin"),
        "prompt": row.get("prompt"),
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "status": row["status"],
        "session_count": row.get("session_count", 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "node_metadata": node_meta,
        # Sessions ordered by creation; minimal projection — Studio's
        # session detail page is the authority for any single session.
        "sessions": [
            {
                "id": s["id"],
                "name": s.get("name"),
                "agent_name": s.get("agent_name"),
                "playbook_name": s.get("playbook_name"),
                "invocation_kind": s.get("invocation_kind"),
                "status": s.get("status"),
                "last_message_at": s.get("last_message_at"),
                "started_at": s.get("started_at"),
                "ended_at": s.get("ended_at"),
                # ADR-0022: model disclosure on the child sessions list.
                "model": s.get("model"),
                "effort": s.get("effort"),
            }
            for s in sessions
        ],
        "artifacts": [_serialize_artifact(a) for a in artifacts],
    }


def _serialize_artifact(row: dict[str, Any]) -> dict[str, Any]:
    """Common artifact projection for /api/invocations and /api/artifacts.

    SQLite returns JSON columns as strings; decode here so the frontend
    gets a real object instead of a doubly-encoded string.
    """
    content = row.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = None
    return {
        "id": row["id"],
        "invocation_id": row.get("invocation_id"),
        "session_id": row.get("session_id"),
        "kind": row["kind"],
        "name": row["name"],
        "created_at": row["created_at"],
        "content": content,
        "file_path": row.get("file_path"),
    }


# ── Artifacts (ADR-0021) ──────────────────────────────────────────────────────


async def list_artifacts_for_session(session_id: str) -> list[dict[str, Any]]:
    if not DEFAULT_DB_PATH.exists():
        return []
    async with StateDB() as db:
        rows = await db.list_artifacts_for_session(session_id)
    return [_serialize_artifact(r) for r in rows]


async def get_artifact(artifact_id: str) -> dict[str, Any] | None:
    if not DEFAULT_DB_PATH.exists():
        return None
    async with StateDB() as db:
        row = await db.get_artifact(artifact_id)
    return _serialize_artifact(row) if row else None
