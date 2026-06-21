# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0020 invocations service — backs /api/invocations endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Query

from lionagi._errors import NotFoundError
from lionagi.state.db import DEFAULT_DB_PATH, StateDB

from ..registry import studio_route
from ._io import parse_json_col as _parse_json_col


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
                "status_reason_code": r.get("status_reason_code"),
                "status_reason_summary": r.get("status_reason_summary"),
                "status_evidence_refs": _parse_json_col(r.get("status_evidence_refs")),
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
        # ADR-0021: structured outcomes alongside child sessions for the invocation detail page.
        artifacts = await db.list_artifacts_for_invocation(invocation_id)
    return {
        "id": row["id"],
        "skill": row["skill"],
        "plugin": row.get("plugin"),
        "prompt": row.get("prompt"),
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "status": row["status"],
        "status_reason_code": row.get("status_reason_code"),
        "status_reason_summary": row.get("status_reason_summary"),
        "status_evidence_refs": _parse_json_col(row.get("status_evidence_refs")),
        "session_count": row.get("session_count", 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "node_metadata": node_meta,
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
    """Common artifact projection — decodes JSON content columns so the frontend gets real objects."""
    raw_content = row.get("content")
    if isinstance(raw_content, str):
        parsed = _parse_json_col(raw_content)
        content = parsed if not isinstance(parsed, str) else None
    else:
        content = raw_content
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


@studio_route("/invocations/", method="GET", area="invocations", name="list_invocations")
async def list_invocations_route(
    skill: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = await list_invocations(skill=skill, status=status, limit=limit, offset=offset)
    return {
        "invocations": rows,
        "limit": limit,
        "offset": offset,
        # We don't compute total separately — the page is the slice the
        # client asked for. Studio's pagination uses has_next instead of
        # absolute totals.
        "has_next": len(rows) == limit,
    }


@studio_route(
    "/invocations/{invocation_id}", method="GET", area="invocations", name="get_invocation"
)
async def get_invocation_route(invocation_id: str) -> dict[str, Any]:
    data = await get_invocation(invocation_id)
    if data is None:
        raise NotFoundError(f"Invocation '{invocation_id}' not found")
    return data


@studio_route(
    "/artifacts/{artifact_id}",
    method="GET",
    area="invocations",
    tags=["artifacts"],
    name="get_artifact",
)
async def get_artifact_route(artifact_id: str) -> dict[str, Any]:
    data = await get_artifact(artifact_id)
    if data is None:
        raise NotFoundError(f"Artifact '{artifact_id}' not found")
    return data


# Convenience: sessions don't have their own router-level artifacts endpoint
# (sessions.router predates ADR-0021). This sub-route under /api/artifacts
# keeps the artifact concern in one place.
@studio_route(
    "/artifacts/by-session/{session_id}", method="GET", area="invocations", tags=["artifacts"]
)
async def list_for_session(session_id: str) -> dict[str, Any]:
    rows = await list_artifacts_for_session(session_id)
    return {"artifacts": rows}
