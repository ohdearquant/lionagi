# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Server-side approval ledger for operator-proposed mutating actions.

Lifecycle: proposed (pending) -> a human grants or denies it -> the real endpoint consumes the granted approval exactly once, with a hash-chained evidence row per event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from lionagi.state.db import DEFAULT_DB_PATH

from ..registry import studio_route
from ._db import open_db as _open_db

_DB = str(DEFAULT_DB_PATH)

APPROVAL_TTL_SECONDS = 5 * 60

_STATUSES = frozenset({"pending", "granted", "consumed", "expired", "denied"})

# Header identifying an operator/service principal. Mere presence (any non-empty value)
# disqualifies the caller from grant/deny -- there is no "correct" value that passes.
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
CREATE INDEX IF NOT EXISTS idx_approvals_status
  ON approvals(status) WHERE status IN ('pending', 'granted');
CREATE INDEX IF NOT EXISTS idx_approvals_session
  ON approvals(session_id) WHERE session_id IS NOT NULL;
"""

EVIDENCE_EVENT_TYPES = frozenset({"proposed", "granted", "denied", "consumed", "expired"})
GENESIS_HASH = "0" * 64
REASON_CLASSES = ("security", "policy", "mistake", "other")

# Mirrors the approval_evidence table in schema.sql -- same defensive-fallback
# rationale as _ENSURE_TABLE_SQL above.
_ENSURE_EVIDENCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS approval_evidence (
  id                    TEXT    PRIMARY KEY,
  sequence              INTEGER NOT NULL,
  event_type            TEXT    NOT NULL
                        CHECK(event_type IN ('proposed', 'granted', 'denied', 'consumed', 'expired')),
  approval_id           TEXT    NOT NULL REFERENCES approvals(id),
  action_kind           TEXT    NOT NULL,
  status_from           TEXT,
  status_to             TEXT    NOT NULL,
  params_hash           TEXT    NOT NULL,
  justification_class   TEXT,
  justification_reason  TEXT,
  created_at            REAL    NOT NULL,
  content_hash          TEXT    NOT NULL,
  previous_hash         TEXT    NOT NULL,
  chain_hash            TEXT    NOT NULL,
  hmac_sig              TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_evidence_sequence
  ON approval_evidence(sequence);
CREATE INDEX IF NOT EXISTS idx_approval_evidence_approval
  ON approval_evidence(approval_id);
"""


async def _ensure_table(db: Any) -> None:
    await db.executescript(_ENSURE_TABLE_SQL)


async def _ensure_evidence_table(db: Any) -> None:
    await db.executescript(_ENSURE_EVIDENCE_TABLE_SQL)


def compute_params_hash(params: dict[str, Any]) -> str:
    """sha256 over canonical (sorted-keys, separator-stable) JSON of *params*."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compute_content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compute_chain_hash(content_hash: str, previous_hash: str) -> str:
    return hashlib.sha256((content_hash + previous_hash).encode("utf-8")).hexdigest()


def _compute_hmac_sig(chain_hash: str, created_at: float, key: str) -> str:
    message = (chain_hash + str(created_at)).encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _is_service_principal(request: Request) -> bool:
    return bool(request.headers.get(_SERVICE_PRINCIPAL_HEADER))


def _require_human_principal(request: Request) -> None:
    """Grant/deny require the caller to PRESENT the configured bearer token; with none configured, granting is unavailable entirely (fail closed)."""
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
    presented = request.headers.get("authorization") or ""
    if not hmac.compare_digest(presented, f"Bearer {token}"):
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


async def _write_evidence(
    db: Any,
    *,
    event_type: str,
    approval_id: str,
    action_kind: str,
    status_from: str | None,
    status_to: str,
    params_hash: str,
    justification_class: str | None = None,
    justification_reason: str | None = None,
) -> dict[str, Any]:
    """Append one evidence row to the chain. Caller MUST already hold the write lock on *db* (a preceding `BEGIN IMMEDIATE`) so the tail read is race-free."""
    cur = await db.execute(
        "SELECT sequence, chain_hash FROM approval_evidence ORDER BY sequence DESC LIMIT 1"
    )
    tail = await cur.fetchone()
    sequence = (tail["sequence"] + 1) if tail is not None else 1
    previous_hash = tail["chain_hash"] if tail is not None else GENESIS_HASH

    evidence_id = uuid.uuid4().hex
    created_at = time.time()
    payload = {
        "id": evidence_id,
        "event_type": event_type,
        "approval_id": approval_id,
        "action_kind": action_kind,
        "status_from": status_from,
        "status_to": status_to,
        "params_hash": params_hash,
        "justification_class": justification_class,
        "justification_reason": justification_reason,
        "created_at": created_at,
    }
    content_hash = _compute_content_hash(payload)
    chain_hash = _compute_chain_hash(content_hash, previous_hash)

    key = os.getenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY")
    hmac_sig = _compute_hmac_sig(chain_hash, created_at, key) if key else None

    await db.execute(
        "INSERT INTO approval_evidence "
        "(id, sequence, event_type, approval_id, action_kind, status_from, status_to, "
        "params_hash, justification_class, justification_reason, created_at, "
        "content_hash, previous_hash, chain_hash, hmac_sig) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            sequence,
            event_type,
            approval_id,
            action_kind,
            status_from,
            status_to,
            params_hash,
            justification_class,
            justification_reason,
            created_at,
            content_hash,
            previous_hash,
            chain_hash,
            hmac_sig,
        ),
    )
    return {
        "id": evidence_id,
        "sequence": sequence,
        "event_type": event_type,
        "approval_id": approval_id,
        "action_kind": action_kind,
        "status_from": status_from,
        "status_to": status_to,
        "params_hash": params_hash,
        "justification_class": justification_class,
        "justification_reason": justification_reason,
        "created_at": created_at,
        "content_hash": content_hash,
        "previous_hash": previous_hash,
        "chain_hash": chain_hash,
        "hmac_sig": hmac_sig,
    }


async def verify_evidence_chain() -> dict[str, Any]:
    """Replay the evidence chain end to end and report whether it is intact."""
    errors: list[str] = []
    total = 0
    async with _open_db(_DB) as db:
        await _ensure_evidence_table(db)
        cur = await db.execute(
            "SELECT id, sequence, event_type, approval_id, action_kind, status_from, "
            "status_to, params_hash, justification_class, justification_reason, "
            "created_at, content_hash, previous_hash, chain_hash, hmac_sig "
            "FROM approval_evidence ORDER BY sequence ASC"
        )
        rows = await cur.fetchall()

    key = os.getenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY")
    expected_previous = GENESIS_HASH
    expected_sequence = 1
    for row in rows:
        total += 1
        # The sequence column is untrusted metadata: the true order is fixed by the
        # previous_hash -> chain_hash linkage below. A legitimate chain is always
        # 1, 2, ..., N (each row takes tail+1, starting at 1), so pin sequence to its
        # cryptographically-enforced position. Without this, renumbering every row by a
        # uniform offset (1, 2 -> 101, 102) leaves all hash columns intact and slips
        # past chain-continuity, forging a valid-looking but relabelled audit trail.
        if row["sequence"] != expected_sequence:
            errors.append(
                f"sequence {row['sequence']}: expected contiguous sequence {expected_sequence}"
            )
        payload = {
            "id": row["id"],
            "event_type": row["event_type"],
            "approval_id": row["approval_id"],
            "action_kind": row["action_kind"],
            "status_from": row["status_from"],
            "status_to": row["status_to"],
            "params_hash": row["params_hash"],
            "justification_class": row["justification_class"],
            "justification_reason": row["justification_reason"],
            "created_at": row["created_at"],
        }
        recomputed_content = _compute_content_hash(payload)
        if recomputed_content != row["content_hash"]:
            errors.append(f"sequence {row['sequence']}: content hash mismatch")
        if row["previous_hash"] != expected_previous:
            errors.append(f"sequence {row['sequence']}: previous_hash breaks chain continuity")
        recomputed_chain = _compute_chain_hash(recomputed_content, row["previous_hash"])
        if recomputed_chain != row["chain_hash"]:
            errors.append(f"sequence {row['sequence']}: chain hash mismatch")
        if key:
            # A signed chain must not accept unsigned rows: stripping hmac_sig
            # from a tampered row would otherwise downgrade to chain-only checks.
            if row["hmac_sig"] is None:
                errors.append(f"sequence {row['sequence']}: hmac signature missing")
            else:
                expected_sig = _compute_hmac_sig(row["chain_hash"], row["created_at"], key)
                if not hmac.compare_digest(expected_sig, row["hmac_sig"]):
                    errors.append(f"sequence {row['sequence']}: hmac signature mismatch")
        expected_previous = row["chain_hash"]
        expected_sequence += 1

    return {
        "valid": not errors,
        "total_entries": total,
        "verified_at": time.time(),
        "errors": errors,
    }


async def _lazy_expire(approval_id: str, row: dict[str, Any], *, now: float) -> dict[str, Any]:
    """CAS the row to 'expired' if it is past its TTL and still pending/granted."""
    if row["status"] not in ("pending", "granted") or now <= row["expires_at"]:
        return row
    async with _open_db(_DB) as db:
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "UPDATE approvals SET status = 'expired' WHERE id = ? AND status = ?",
            (approval_id, row["status"]),
        )
        if cur.rowcount == 1:
            await _write_evidence(
                db,
                event_type="expired",
                approval_id=approval_id,
                action_kind=row["action_kind"],
                status_from=row["status"],
                status_to="expired",
                params_hash=row["params_hash"],
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
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO approvals "
            "(id, action_kind, params_hash, session_id, status, proposed_at, expires_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (approval_id, action_kind, params_hash, session_id, now, now + APPROVAL_TTL_SECONDS),
        )
        await _write_evidence(
            db,
            event_type="proposed",
            approval_id=approval_id,
            action_kind=action_kind,
            status_from=None,
            status_to="pending",
            params_hash=params_hash,
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
        await _ensure_table(db)
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "UPDATE approvals SET status = 'granted', granted_at = ? WHERE id = ? AND status = 'pending'",
            (now, approval_id),
        )
        if cur.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"approval {approval_id!r} was concurrently modified; retry",
            )
        await _write_evidence(
            db,
            event_type="granted",
            approval_id=approval_id,
            action_kind=row["action_kind"],
            status_from=row["status"],
            status_to="granted",
            params_hash=row["params_hash"],
        )
        await db.commit()
    updated = await _fetch_row(approval_id)
    assert updated is not None
    return updated


async def deny_approval(
    approval_id: str,
    *,
    justification_class: str | None = None,
    justification_reason: str | None = None,
) -> dict[str, Any]:
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
        await _ensure_table(db)
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "UPDATE approvals SET status = 'denied' WHERE id = ? AND status = 'pending'",
            (approval_id,),
        )
        if cur.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"approval {approval_id!r} was concurrently modified; retry",
            )
        await _write_evidence(
            db,
            event_type="denied",
            approval_id=approval_id,
            action_kind=row["action_kind"],
            status_from=row["status"],
            status_to="denied",
            params_hash=row["params_hash"],
            justification_class=justification_class,
            justification_reason=justification_reason,
        )
        await db.commit()
    updated = await _fetch_row(approval_id)
    assert updated is not None
    return updated


async def require_approval(
    approval_id: str, *, action_kind: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Validate a granted approval for exactly this action_kind/params and consume it atomically. Not wired into any route yet; consumed only when every check passes."""
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
        await _ensure_table(db)
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "UPDATE approvals SET status = 'consumed', consumed_at = ? "
            "WHERE id = ? AND status = 'granted'",
            (now, approval_id),
        )
        if cur.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"approval {approval_id!r} was concurrently consumed; retry",
            )
        await _write_evidence(
            db,
            event_type="consumed",
            approval_id=approval_id,
            action_kind=action_kind,
            status_from=row["status"],
            status_to="consumed",
            params_hash=row["params_hash"],
        )
        await db.commit()
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


class _DenyBody(BaseModel):
    """Typed justification for a deny. Optional at the route: a body-less
    deny is still valid (the human clicked "deny", no reason required), but
    a body that IS sent must carry a real reason -- empty/whitespace-only
    text is rejected."""

    reason_class: Literal["security", "policy", "mistake", "other"]
    reason: str = Field(..., min_length=1)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be blank")
        return v


@studio_route("/approvals/", method="POST", area="approvals", name="create_approval")
async def create_approval_route(body: _CreateApprovalBody) -> dict[str, Any]:
    return await create_approval(
        action_kind=body.action_kind, params=body.params, session_id=body.session_id
    )


@studio_route(
    "/approvals/evidence/verify",
    method="GET",
    area="approvals",
    name="verify_approval_evidence",
)
async def verify_approval_evidence_route() -> dict[str, Any]:
    return await verify_evidence_chain()


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
async def deny_approval_route(
    approval_id: str, request: Request, body: _DenyBody | None = None
) -> dict[str, Any]:
    _require_human_principal(request)
    justification_class = body.reason_class if body is not None else None
    justification_reason = body.reason if body is not None else None
    return await deny_approval(
        approval_id,
        justification_class=justification_class,
        justification_reason=justification_reason,
    )
