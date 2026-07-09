# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Server-side approval ledger for operator-proposed mutating actions.

An action is proposed (pending) -> a human grants or denies it -> the real
endpoint consumes the granted approval exactly once. Expiry and single-use
are enforced here, not by the caller's convention: a granted approval that
is expired, already consumed, or whose params don't hash-match the action
being executed is rejected.

Principal separation (grant/deny only): these two routes require the
request to carry no operator/service principal marker. The studio frontend
never sends that header (see api.ts fetchJson — only Authorization and
Content-Type are set), so a legitimate browser confirm click always passes.
Any caller presenting the marker -- including a future in-process operator
service context or a spawned driver CLI that learned an approval id -- is
rejected before the row is touched. The marker is additive to the existing
bearer-token gate, not a replacement for it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from lionagi.state.db import DEFAULT_DB_PATH

from ..registry import studio_route
from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)

APPROVAL_TTL_SECONDS = 5 * 60

_STATUSES = frozenset({"pending", "granted", "consumed", "expired", "denied"})

# Header a caller uses to identify itself as an operator/service principal
# rather than the human browser session. Its mere presence (any non-empty
# value) is disqualifying for grant/deny -- there is no "correct" value that
# passes, so a caller can't guess its way past the check.
_SERVICE_PRINCIPAL_HEADER = "x-lionagi-operator-principal"

# Mirrors the approvals table in schema.sql -- a defensive fallback so direct
# callers of this module (outside the studio app's lifespan, which applies
# the full StateDB schema on startup) never hit "no such table".
_ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
  id            TEXT    PRIMARY KEY,
  action_kind   TEXT    NOT NULL,
  params_hash   TEXT    NOT NULL,
  session_id    TEXT    REFERENCES sessions(id),
  status        TEXT    NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'granted', 'consumed', 'expired', 'denied')),
  proposed_at   REAL    NOT NULL,
  granted_at    REAL,
  consumed_at   REAL,
  expires_at    REAL    NOT NULL
);
"""


async def _ensure_table(db: Any) -> None:
    await db.executescript(_ENSURE_TABLE_SQL)


def compute_params_hash(params: dict[str, Any]) -> str:
    """sha256 over canonical (sorted-keys, separator-stable) JSON of *params*."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_service_principal(request: Request) -> bool:
    return bool(request.headers.get(_SERVICE_PRINCIPAL_HEADER))


def _require_human_principal(request: Request) -> None:
    """Grant/deny are reserved for the human principal: the browser session's
    bearer credential. Enforced positively — the caller must PRESENT the
    configured token — not by trusting callers to identify themselves. With no
    token configured there is no human credential to distinguish, so granting
    is unavailable entirely (fail closed) rather than open to any local caller.
    The service-marker header check stays as defense in depth."""
    if _is_service_principal(request):
        raise HTTPException(
            status_code=403,
            detail="approvals may only be granted or denied by the human operator principal",
        )
    token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    if not token:
        raise HTTPException(
            status_code=403,
            detail=(
                "approval granting requires a configured auth token "
                "(set LIONAGI_STUDIO_AUTH_TOKEN); refusing on an open daemon"
            ),
        )
    if request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(
            status_code=403,
            detail="approvals may only be granted or denied by the human operator principal",
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "action_kind": row["action_kind"],
        "params_hash": row["params_hash"],
        "session_id": row["session_id"],
        "status": row["status"],
        "proposed_at": row["proposed_at"],
        "granted_at": row["granted_at"],
        "consumed_at": row["consumed_at"],
        "expires_at": row["expires_at"],
    }


async def _fetch_row(approval_id: str) -> dict[str, Any] | None:
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        cur = await db.execute(
            "SELECT id, action_kind, params_hash, session_id, status, "
            "proposed_at, granted_at, consumed_at, expires_at "
            "FROM approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cur.fetchone()
        return _row_to_dict(row) if row is not None else None


async def _lazy_expire(approval_id: str, row: dict[str, Any], *, now: float) -> dict[str, Any]:
    """CAS the row to 'expired' if it is past its TTL and still pending/granted."""
    if row["status"] not in ("pending", "granted") or now <= row["expires_at"]:
        return row
    async with _open_db(_DB) as db:
        await db.execute(
            "UPDATE approvals SET status = 'expired' WHERE id = ? AND status = ?",
            (approval_id, row["status"]),
        )
        await db.commit()
    refreshed = await _fetch_row(approval_id)
    return refreshed if refreshed is not None else row


async def get_approval(approval_id: str) -> dict[str, Any] | None:
    """Read an approval, resolving lazy expiry first."""
    row = await _fetch_row(approval_id)
    if row is None:
        return None
    return await _lazy_expire(approval_id, row, now=time.time())


async def create_approval(
    *, action_kind: str, params: dict[str, Any], session_id: str | None = None
) -> dict[str, Any]:
    """Propose a mutating action; returns the pending approval row."""
    now = time.time()
    approval_id = uuid.uuid4().hex
    params_hash = compute_params_hash(params)
    async with _open_db(_DB) as db:
        await _ensure_table(db)
        await db.execute(
            "INSERT INTO approvals "
            "(id, action_kind, params_hash, session_id, status, proposed_at, expires_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (approval_id, action_kind, params_hash, session_id, now, now + APPROVAL_TTL_SECONDS),
        )
        await db.commit()
    row = await _fetch_row(approval_id)
    assert row is not None  # just inserted
    return row


async def grant_approval(approval_id: str) -> dict[str, Any]:
    """CAS pending -> granted. Raises HTTPException on any non-pending state."""
    row = await get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"approval {approval_id!r} is not pending (status={row['status']!r})",
        )
    now = time.time()
    async with _open_db(_DB) as db:
        cur = await db.execute(
            "UPDATE approvals SET status = 'granted', granted_at = ? WHERE id = ? AND status = 'pending'",
            (now, approval_id),
        )
        await db.commit()
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail=f"approval {approval_id!r} was concurrently modified; retry",
            )
    updated = await _fetch_row(approval_id)
    assert updated is not None
    return updated


async def deny_approval(approval_id: str) -> dict[str, Any]:
    """CAS pending -> denied. Raises HTTPException on any non-pending state."""
    row = await get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"approval {approval_id!r} is not pending (status={row['status']!r})",
        )
    async with _open_db(_DB) as db:
        cur = await db.execute(
            "UPDATE approvals SET status = 'denied' WHERE id = ? AND status = 'pending'",
            (approval_id,),
        )
        await db.commit()
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail=f"approval {approval_id!r} was concurrently modified; retry",
            )
    updated = await _fetch_row(approval_id)
    assert updated is not None
    return updated


async def require_approval(
    approval_id: str, *, action_kind: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Validate a granted approval for exactly this action and consume it atomically.

    Not wired into any route yet -- a future mutating route calls this before
    performing its side effect, passing the same action_kind/params it is
    about to act on. Raises HTTPException (404/409/422) on any failure; the
    approval is consumed only when every check passes.
    """
    row = await get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if row["status"] == "expired":
        raise HTTPException(status_code=409, detail=f"approval {approval_id!r} has expired")
    if row["status"] != "granted":
        raise HTTPException(
            status_code=409,
            detail=f"approval {approval_id!r} is not granted (status={row['status']!r})",
        )
    if row["action_kind"] != action_kind:
        raise HTTPException(
            status_code=422,
            detail=(
                f"approval {approval_id!r} was granted for action_kind="
                f"{row['action_kind']!r}, not {action_kind!r}"
            ),
        )
    if row["params_hash"] != compute_params_hash(params):
        raise HTTPException(
            status_code=422,
            detail=f"approval {approval_id!r} params do not match the action being executed",
        )
    now = time.time()
    async with _open_db(_DB) as db:
        cur = await db.execute(
            "UPDATE approvals SET status = 'consumed', consumed_at = ? "
            "WHERE id = ? AND status = 'granted'",
            (now, approval_id),
        )
        await db.commit()
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail=f"approval {approval_id!r} was concurrently consumed; retry",
            )
    updated = await _fetch_row(approval_id)
    assert updated is not None
    return updated


# ---------------------------------------------------------------------------
# Route handlers — approvals area
# ---------------------------------------------------------------------------


class _CreateApprovalBody(BaseModel):
    action_kind: str = Field(..., min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


@studio_route("/approvals/", method="POST", area="approvals", name="create_approval")
async def create_approval_route(body: _CreateApprovalBody) -> dict[str, Any]:
    return await create_approval(
        action_kind=body.action_kind, params=body.params, session_id=body.session_id
    )


@studio_route("/approvals/{approval_id}", method="GET", area="approvals", name="get_approval")
async def get_approval_route(approval_id: str) -> dict[str, Any]:
    row = await get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    return row


@studio_route(
    "/approvals/{approval_id}/grant", method="POST", area="approvals", name="grant_approval"
)
async def grant_approval_route(approval_id: str, request: Request) -> dict[str, Any]:
    _require_human_principal(request)
    return await grant_approval(approval_id)


@studio_route(
    "/approvals/{approval_id}/deny", method="POST", area="approvals", name="deny_approval"
)
async def deny_approval_route(approval_id: str, request: Request) -> dict[str, Any]:
    _require_human_principal(request)
    return await deny_approval(approval_id)
