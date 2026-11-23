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
import sqlite3
import time
import uuid
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from lionagi.ln import compute_hash
from lionhub.orchestration.state_store import open_db as _open_db
from lionhub.orchestration.state_store import require_file_store, store_path
from lionhub.serve.daemon.launch_access import (
    DECISION_TOKEN_HEADER,
    DecisionPrincipal,
    resolve_decision_capability,
)

from ..registry import studio_route

APPROVAL_TTL_SECONDS = 5 * 60
"""Lifetime of a proposed approval when the server and the request set none."""

APPROVAL_MAX_TTL_SECONDS = 60 * 60
"""Longest lifetime a request may ask for when the server sets no cap."""

# Server settings, read at each proposal so a restart is not needed to change them.
_TTL_ENV = "LIONAGI_STUDIO_APPROVAL_TTL_SECONDS"
_MAX_TTL_ENV = "LIONAGI_STUDIO_APPROVAL_MAX_TTL_SECONDS"

_STATUSES = frozenset({"pending", "granted", "consumed", "expired", "denied"})

# Header identifying an operator/service principal. Mere presence (any non-empty value)
# disqualifies the caller from grant/deny -- there is no "correct" value that passes.
_SERVICE_PRINCIPAL_HEADER = "x-lionagi-operator-principal"

# Who decided an approval, when the grant or deny request carried a verified decision
# capability: the five fields the Operator records on its proposals. NULL means the request
# carried none, and rows written before these columns existed stay NULL.
_PRINCIPAL_COLUMNS = (
    "decision_principal_id",
    "decision_epoch",
    "decision_method",
    "decision_origin",
    "decision_scope",
)

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
  expires_at    REAL    NOT NULL,
  decision_principal_id TEXT,
  decision_epoch        TEXT,
  decision_method       TEXT,
  decision_origin       TEXT,
  decision_scope        TEXT
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
  hmac_sig              TEXT,
  decision_principal_id TEXT,
  decision_epoch        TEXT,
  decision_method       TEXT,
  decision_origin       TEXT,
  decision_scope        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_evidence_sequence
  ON approval_evidence(sequence);
CREATE INDEX IF NOT EXISTS idx_approval_evidence_approval
  ON approval_evidence(approval_id);
"""


async def _ensure_table(db: Any) -> None:
    await db.executescript(_ENSURE_TABLE_SQL)
    await _add_principal_columns(db, "approvals")


async def _ensure_evidence_table(db: Any) -> None:
    await db.executescript(_ENSURE_EVIDENCE_TABLE_SQL)
    await _add_principal_columns(db, "approval_evidence")


async def _add_principal_columns(db: Any, table: str) -> None:
    """Give a table created before the decision principal columns those columns, as NULL."""
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    present = {row[1] for row in rows}
    for name in _PRINCIPAL_COLUMNS:
        if name in present:
            continue
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
        except sqlite3.OperationalError as exc:
            # Another connection added it between the read above and this statement.
            if "duplicate column" not in str(exc).lower():
                raise


def compute_params_hash(params: dict[str, Any]) -> str:
    """sha256 over canonical (sorted-keys, separator-stable) JSON of *params*."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return compute_hash(canonical, max_bytes=None)


def _compute_content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return compute_hash(canonical, max_bytes=None)


def _compute_chain_hash(content_hash: str, previous_hash: str) -> str:
    return compute_hash(content_hash + previous_hash, max_bytes=None)


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


def _require_daemon_credential(request: Request) -> None:
    """Consume requires the caller to PRESENT the configured bearer token.

    Without one, every local process reaches the daemon, so a granted approval could be
    spent by a caller holding no credential at all; consuming is refused on an open daemon,
    as granting is. Checked here as well as by the admission middleware, so the route does
    not depend on the middleware's order or on the token mode it runs in.
    """
    token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    if not token:
        raise HTTPException(
            status_code=403,
            detail=(
                "consuming an approval requires a configured auth token "
                "(set LIONAGI_STUDIO_AUTH_TOKEN); refusing on an open daemon"
            ),
        )
    presented = (request.headers.get("authorization") or "").encode("utf-8", "surrogateescape")
    if not hmac.compare_digest(presented, f"Bearer {token}".encode("utf-8", "surrogateescape")):
        raise HTTPException(
            status_code=403, detail="consuming an approval requires the daemon's bearer token"
        )


def _decision_principal(request: Request) -> DecisionPrincipal | None:
    """Who a grant or deny request's decision capability resolves to, or None.

    Resolved as the Operator's decision routes resolve it: the ``X-Studio-Decision-Token``
    capability ``li studio`` paired the page with, presented from exactly one ``Origin``.
    None covers a request without one and a capability this daemon cannot verify (unknown,
    expired, revoked, from another start, origin or scope). It admits and refuses nothing:
    the bearer check in :func:`_require_human_principal` is what admits a grant or deny.
    """
    presented = request.headers.get(DECISION_TOKEN_HEADER)
    if not presented:
        return None
    origins = request.headers.getlist("origin")
    if len(origins) != 1:
        return None
    return resolve_decision_capability(presented, origin=origins[0])


def _principal_values(principal: DecisionPrincipal | None) -> tuple[str | None, ...]:
    """The decision principal columns for *principal*, all None for an unattributed decision."""
    if principal is None:
        return (None,) * len(_PRINCIPAL_COLUMNS)
    return (principal.id, principal.epoch, principal.method, principal.origin, principal.scope)


def _principal_payload(values: tuple[Any, ...]) -> dict[str, Any] | None:
    """The hashed evidence payload's view of the recorded principal columns.

    None when every column is empty, and the payload then leaves the key out, so an
    unattributed row hashes exactly as rows written before principals were recorded and
    those chains still verify. Any non-empty column puts all five in the payload, so
    adding, changing or clearing one afterwards breaks the row's content hash.
    """
    if all(value is None for value in values):
        return None
    return dict(zip(("id", "epoch", "method", "origin", "scope"), values, strict=True))


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
        **{name: row[name] for name in _PRINCIPAL_COLUMNS},
    }


async def _fetch_row(approval_id: str) -> dict[str, Any] | None:
    require_file_store()
    async with _open_db(store_path()) as db:
        await _ensure_table(db)
        cur = await db.execute(
            "SELECT id, action_kind, params_hash, session_id, status, "
            "proposed_at, granted_at, consumed_at, expires_at, "
            "decision_principal_id, decision_epoch, decision_method, decision_origin, "
            "decision_scope "
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
    principal: DecisionPrincipal | None = None,
) -> None:
    """Append one evidence row to the chain. Caller MUST already hold the write lock on *db* (a preceding `BEGIN IMMEDIATE`) so the tail read is race-free. *principal* is who decided a grant or deny, when the request carried a verified decision capability."""
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
    }
    principal_values = _principal_values(principal)
    principal_payload = _principal_payload(principal_values)
    if principal_payload is not None:
        payload["decision_principal"] = principal_payload
    content_hash = _compute_content_hash(payload)
    chain_hash = _compute_chain_hash(content_hash, previous_hash)

    key = os.getenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY")
    hmac_sig = _compute_hmac_sig(chain_hash, created_at, key) if key else None

    await db.execute(
        "INSERT INTO approval_evidence "
        "(id, sequence, event_type, approval_id, action_kind, status_from, status_to, "
        "params_hash, justification_class, justification_reason, created_at, "
        "content_hash, previous_hash, chain_hash, hmac_sig, "
        "decision_principal_id, decision_epoch, decision_method, decision_origin, "
        "decision_scope) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            *principal_values,
        ),
    )


async def verify_evidence_chain() -> dict[str, Any]:
    """Replay the evidence chain end to end and report whether it is intact."""
    require_file_store()
    errors: list[str] = []
    total = 0
    async with _open_db(store_path()) as db:
        await _ensure_evidence_table(db)
        cur = await db.execute(
            "SELECT id, sequence, event_type, approval_id, action_kind, status_from, "
            "status_to, params_hash, justification_class, justification_reason, "
            "created_at, content_hash, previous_hash, chain_hash, hmac_sig, "
            "decision_principal_id, decision_epoch, decision_method, decision_origin, "
            "decision_scope "
            "FROM approval_evidence ORDER BY sequence ASC"
        )
        rows = await cur.fetchall()

    key = os.getenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY")
    expected_previous = GENESIS_HASH
    expected_sequence = 1
    for row in rows:
        total += 1
        if row["sequence"] != expected_sequence:
            errors.append(
                f"sequence {row['sequence']}: expected sequence {expected_sequence} "
                "(non-contiguous)"
            )
        expected_sequence += 1
        payload = {
            "id": row["id"],
            "sequence": row["sequence"],
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
        principal_payload = _principal_payload(tuple(row[name] for name in _PRINCIPAL_COLUMNS))
        if principal_payload is not None:
            payload["decision_principal"] = principal_payload
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
    async with _open_db(store_path()) as db:
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


def _env_seconds(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value <= 0:
        raise HTTPException(
            status_code=500,
            detail=f"{name} must be a positive whole number of seconds, got {raw!r}",
        )
    return value


def approval_ttl_bounds() -> tuple[int, int]:
    """The server's default approval lifetime and the cap on a requested one, in seconds.

    ``LIONAGI_STUDIO_APPROVAL_TTL_SECONDS`` sets the default (five minutes when unset) and
    ``LIONAGI_STUDIO_APPROVAL_MAX_TTL_SECONDS`` the cap (one hour when unset, or the default
    when that is longer). A value that is not a positive whole number, or a default above an
    explicit cap, is refused with a 500 rather than replaced, so a misconfigured server
    proposes nothing instead of approvals that live longer or shorter than intended.
    """
    default = _env_seconds(_TTL_ENV, APPROVAL_TTL_SECONDS)
    explicit_cap = os.getenv(_MAX_TTL_ENV)
    if explicit_cap is None or not explicit_cap.strip():
        return default, max(default, APPROVAL_MAX_TTL_SECONDS)
    cap = _env_seconds(_MAX_TTL_ENV, APPROVAL_MAX_TTL_SECONDS)
    if default > cap:
        raise HTTPException(
            status_code=500,
            detail=f"{_TTL_ENV} ({default}) is longer than {_MAX_TTL_ENV} ({cap})",
        )
    return default, cap


async def create_approval(
    *,
    action_kind: str,
    params: dict[str, Any],
    session_id: str | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Propose a mutating action; returns the pending approval row.

    *ttl_seconds* asks for a lifetime other than the server's default; a value above the
    server's cap (see :func:`approval_ttl_bounds`) is refused with a 422, never shortened.
    """
    require_file_store()
    default_ttl, max_ttl = approval_ttl_bounds()
    if ttl_seconds is None:
        ttl = default_ttl
    elif isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"ttl_seconds must be a positive whole number, got {ttl_seconds!r}",
        )
    elif ttl_seconds > max_ttl:
        raise HTTPException(
            status_code=422,
            detail=f"ttl_seconds {ttl_seconds} exceeds this server's cap of {max_ttl} seconds",
        )
    else:
        ttl = ttl_seconds
    now = time.time()
    approval_id = uuid.uuid4().hex
    params_hash = compute_params_hash(params)
    async with _open_db(store_path()) as db:
        await _ensure_table(db)
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO approvals "
            "(id, action_kind, params_hash, session_id, status, proposed_at, expires_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (approval_id, action_kind, params_hash, session_id, now, now + ttl),
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


async def grant_approval(
    approval_id: str, *, principal: DecisionPrincipal | None = None
) -> dict[str, Any]:
    """CAS pending -> granted. Raises HTTPException on any non-pending state.

    *principal* is who granted it, recorded on the approval and its evidence row; None
    records an unattributed grant.
    """
    row = await get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"approval {approval_id!r} is not pending (status={row['status']!r})",
        )
    now = time.time()
    async with _open_db(store_path()) as db:
        await _ensure_table(db)
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "UPDATE approvals SET status = 'granted', granted_at = ?, "
            "decision_principal_id = ?, decision_epoch = ?, decision_method = ?, "
            "decision_origin = ?, decision_scope = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, *_principal_values(principal), approval_id),
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
            principal=principal,
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
    principal: DecisionPrincipal | None = None,
) -> dict[str, Any]:
    """CAS pending -> denied. Raises HTTPException on any non-pending state.

    *principal* is who denied it, recorded on the approval and its evidence row; None
    records an unattributed deny.
    """
    row = await get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"approval {approval_id!r} is not pending (status={row['status']!r})",
        )
    async with _open_db(store_path()) as db:
        await _ensure_table(db)
        await _ensure_evidence_table(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "UPDATE approvals SET status = 'denied', "
            "decision_principal_id = ?, decision_epoch = ?, decision_method = ?, "
            "decision_origin = ?, decision_scope = ? "
            "WHERE id = ? AND status = 'pending'",
            (*_principal_values(principal), approval_id),
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
            principal=principal,
        )
        await db.commit()
    updated = await _fetch_row(approval_id)
    assert updated is not None
    return updated


async def require_approval(
    approval_id: str, *, action_kind: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Validate a granted approval for exactly this action_kind/params and consume it atomically; consumed only when every check passes. Served over HTTP as ``POST /approvals/{id}/consume`` for an executor outside this process."""
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
    async with _open_db(store_path()) as db:
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
    # Strict so a string or a float is refused rather than coerced; the server's cap is
    # checked in create_approval, which owns the configured bounds.
    ttl_seconds: int | None = Field(default=None, gt=0, strict=True)


class _ConsumeBody(BaseModel):
    """The action an executor is about to run; it must match the granted approval exactly."""

    action_kind: str = Field(..., min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


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
        action_kind=body.action_kind,
        params=body.params,
        session_id=body.session_id,
        ttl_seconds=body.ttl_seconds,
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
    """Grant a pending approval; a verified ``X-Studio-Decision-Token`` records who did."""
    _require_human_principal(request)
    return await grant_approval(approval_id, principal=_decision_principal(request))


@studio_route(
    "/approvals/{approval_id}/consume", method="POST", area="approvals", name="consume_approval"
)
async def consume_approval_route(
    approval_id: str, body: _ConsumeBody, request: Request
) -> dict[str, Any]:
    """Use a granted approval once, for exactly the action and params it was granted for.

    Unlike grant and deny this takes no human principal: the executor that runs the
    action consumes it, and the params hash, the single-use status move and the expiry
    are what bind the consumption to the human's grant. It does take the daemon's bearer
    token, and is refused on a daemon with none configured.
    """
    _require_daemon_credential(request)
    return await require_approval(approval_id, action_kind=body.action_kind, params=body.params)


@studio_route(
    "/approvals/{approval_id}/deny", method="POST", area="approvals", name="deny_approval"
)
async def deny_approval_route(
    approval_id: str, request: Request, body: _DenyBody | None = None
) -> dict[str, Any]:
    """Deny a pending approval; a verified ``X-Studio-Decision-Token`` records who did."""
    _require_human_principal(request)
    justification_class = body.reason_class if body is not None else None
    justification_reason = body.reason if body is not None else None
    return await deny_approval(
        approval_id,
        justification_class=justification_class,
        justification_reason=justification_reason,
        principal=_decision_principal(request),
    )
