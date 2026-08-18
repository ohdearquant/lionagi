# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Studio service: read path for the engine_runs table."""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import Query

from lionagi._errors import NotFoundError, ValidationError
from lionagi.state.db import StateDB, state_db_known_absent
from lionagi.studio.operator.redact import cap_payload_by_bytes, redact_arguments

from ..registry import studio_route


def _parse_spec_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw or {}


_CURSOR_VERSION = 1
_SPEC_PREVIEW_BYTE_CAP = 2 * 1024
_SPEC_BYTE_CAP = 64 * 1024
_OUTCOME_BYTE_CAP = 8 * 1024


def _cursor_filters(
    *, kind: str | None, status: str | None, session_id: str | None
) -> list[str | None]:
    return [kind, status, session_id]


def _encode_cursor(
    row: dict[str, Any], *, kind: str | None, status: str | None, session_id: str | None
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "started_at": row["started_at"],
        "id": row["id"],
        "filters": _cursor_filters(kind=kind, status=status, session_id=session_id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    kind: str | None,
    status: str | None,
    session_id: str | None,
) -> tuple[float, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if payload.get("v") != _CURSOR_VERSION:
            raise ValueError("unsupported version")
        if payload.get("filters") != _cursor_filters(
            kind=kind, status=status, session_id=session_id
        ):
            raise ValueError("filters changed")
        started_at = payload["started_at"]
        run_id = payload["id"]
        if not isinstance(started_at, int | float) or not isinstance(run_id, str) or not run_id:
            raise ValueError("invalid anchor")
        return float(started_at), run_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("Invalid engine-run cursor") from exc


def _bounded_payload(value: Any, *, limit: int) -> Any:
    value = _parse_spec_json(value)
    redacted = redact_arguments(value)
    bounded, _ = cap_payload_by_bytes(redacted, limit=limit)
    return bounded


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    outcome = _bounded_payload(row.get("outcome_json"), limit=_OUTCOME_BYTE_CAP)
    signal_session_id = row.get("signal_session_id")
    parent_session_id = row.get("parent_session_id")
    legacy_session_id = row.get("session_id")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "invocation_id": row.get("invocation_id"),
        "signal_session_id": signal_session_id,
        "parent_session_id": parent_session_id,
        # Transitional display alias. Never calls a legacy parent the signal session.
        "session_id": signal_session_id or parent_session_id or legacy_session_id,
        "outcome": outcome or None,
        "has_output": bool(row.get("has_output")),
        "error_code": "legacy_error" if row.get("has_error") else None,
    }


async def list_engine_run_page(
    *,
    kind: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, input-free, deterministic summary page."""
    before_started_at: float | None = None
    before_id: str | None = None
    if cursor:
        before_started_at, before_id = _decode_cursor(
            cursor, kind=kind, status=status, session_id=session_id
        )
    if state_db_known_absent():
        return {"version": 1, "items": [], "next_cursor": None}
    async with StateDB() as db:
        rows = await db.list_engine_run_summaries(
            kind=kind,
            status=status,
            session_id=session_id,
            before_started_at=before_started_at,
            before_id=before_id,
            limit=limit + 1,
        )
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = (
        _encode_cursor(visible[-1], kind=kind, status=status, session_id=session_id)
        if has_more and visible
        else None
    )
    return {"version": 1, "items": [_summary(row) for row in visible], "next_cursor": next_cursor}


async def list_engine_runs(
    *,
    kind: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return engine run rows, newest-first, with optional filters."""
    if state_db_known_absent():
        return []

    async with StateDB() as db:
        rows = await db.list_engine_runs(
            kind=kind,
            status=status,
            session_id=session_id,
            limit=limit,
            offset=offset,
        )

    for row in rows:
        row["spec_json"] = _parse_spec_json(row.get("spec_json"))
    return rows


async def get_engine_run(run_id: str, *, include_spec: bool = True) -> dict[str, Any] | None:
    """Return a single engine run row as a dict, or None if not found."""
    if state_db_known_absent():
        return None

    async with StateDB() as db:
        row = await db.get_engine_run(run_id)

    if row is None:
        return None
    raw_spec = row.get("spec_json")
    row["spec_preview"] = _bounded_payload(raw_spec, limit=_SPEC_PREVIEW_BYTE_CAP)
    row["spec_json"] = _bounded_payload(raw_spec, limit=_SPEC_BYTE_CAP) if include_spec else None
    if row.get("outcome_json") is not None:
        row["outcome_json"] = _bounded_payload(row["outcome_json"], limit=_OUTCOME_BYTE_CAP)
    return row


@studio_route("/engine-runs/", method="GET", area="engine-runs", name="list_engine_runs")
async def list_engine_runs_route(
    kind: str | None = Query(default=None, description="Filter by engine kind."),
    status: str | None = Query(default=None, description="Filter by status."),
    session_id: str | None = Query(default=None, description="Filter by associated session id."),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, description="Opaque keyset cursor."),
) -> dict[str, Any]:
    """List engine-run summaries without stored input, newest-first."""
    return await list_engine_run_page(
        kind=kind,
        status=status,
        session_id=session_id,
        limit=limit,
        cursor=cursor,
    )


@studio_route("/engine-runs/{run_id}", method="GET", area="engine-runs", name="get_engine_run")
async def get_engine_run_route(
    run_id: str,
    include_spec: bool = Query(
        default=False, description="Reveal the redacted, byte-capped stored input."
    ),
) -> dict[str, Any]:
    """Return a single engine run row by id."""
    row = await get_engine_run(run_id, include_spec=include_spec)
    if row is None:
        raise NotFoundError(f"Engine run '{run_id}' not found")
    return row
