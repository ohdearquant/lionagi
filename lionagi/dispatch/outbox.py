# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable dispatch outbox core (ADR-0092 slice 1).

Durability and delivery are separate guarantees: an outbox row persists in
``state.db`` independent of any consumer's liveness (durability); a surviving
producer — the Studio daemon's scheduler tick — re-attempts the configured
notify template until it succeeds, backs off, or exhausts ``max_attempts``
(delivery). The transport is a shell command template (ADR-0085 §5 shape):
best-effort, argv-safe, no specific messaging CLI baked in — the command is
configuration.

Argv-safety (binding rider): ``payload`` and ``deliver_to`` are substituted as
whole argv elements, never string-interpolated into a shell command line, and
the template always runs via ``exec`` (no shell), so shell metacharacters in
either value are inert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.types import JSON

from lionagi.session.signal import DispatchSignal
from lionagi.state.reasons import DispatchReasons
from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition

__all__ = (
    "DEFAULT_MAX_ATTEMPTS",
    "NOTIFY_TIMEOUT_SECONDS",
    "ack_dispatch",
    "backoff_seconds",
    "deliver_due_dispatches",
    "enqueue_dispatch",
    "get_dispatch",
    "list_dispatches",
    "purge_dispatch",
    "purge_dispatches",
    "resolve_notify_template",
    "retry_dispatch",
)

_log = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 8
NOTIFY_TIMEOUT_SECONDS = 10.0

# Terminal dispatch_outbox statuses (ADR-0059 D1's six-value CHECK minus the
# two in-flight ones). Used as the default status filter for purge_dispatches
# when the caller supplies no explicit status.
_TERMINAL_DISPATCH_STATUSES = ("delivered", "acked", "dead_letter", "expired")

_BASE_BACKOFF_SECONDS = 30
_MAX_BACKOFF_SECONDS = 1800

# A claim on a row (pending/delivering -> delivering) advances next_attempt_at
# by this lease so overlapping scans within the transport's execution window
# cannot re-claim it; a scan only revisits a still-`delivering` row once the
# lease has lapsed (crash recovery), and by then the guarded attempt-counter
# CAS in transition() ensures only one claimant ever wins per attempt.
_CLAIM_LEASE_SECONDS = NOTIFY_TIMEOUT_SECONDS + 5.0

_PAYLOAD_TOKEN = "{payload}"  # noqa: S105 -- template placeholder, not a credential
_DELIVER_TO_TOKEN = "{deliver_to}"  # noqa: S105 -- template placeholder, not a credential


def backoff_seconds(attempt: int) -> float:
    """``min(30 * 2**attempt, 1800)`` seconds (ADR-0092 spec-gate ruling 3, no jitter)."""
    return min(_BASE_BACKOFF_SECONDS * (2**attempt), _MAX_BACKOFF_SECONDS)


def resolve_notify_template(project_dir: str | Path | None = None) -> list[str] | None:
    """Read the ``dispatch.notify_template`` argv list from .lionagi/settings.yaml, or None."""
    from lionagi.agent.settings import load_settings

    settings = load_settings(project_dir)
    dispatch_cfg = settings.get("dispatch") if isinstance(settings, dict) else None
    template = dispatch_cfg.get("notify_template") if isinstance(dispatch_cfg, dict) else None
    if (
        not isinstance(template, list)
        or not template
        or not all(isinstance(x, str) for x in template)
    ):
        return None
    return template


def _render_notify_argv(template: list[str], *, payload_json: str, deliver_to: str) -> list[str]:
    """Substitute exact-match placeholder tokens as whole argv elements (never partial-string)."""
    rendered: list[str] = []
    for part in template:
        if part == _PAYLOAD_TOKEN:
            rendered.append(payload_json)
        elif part == _DELIVER_TO_TOKEN:
            rendered.append(deliver_to)
        else:
            rendered.append(part)
    return rendered


async def _exec_notify_template(
    template: list[str],
    *,
    payload_json: str,
    deliver_to: str,
    timeout: float = NOTIFY_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Run the notify template argv-exec (never through a shell); returns (success, error)."""
    argv = _render_notify_argv(template, payload_json=payload_json, deliver_to=deliver_to)
    # If the template does not place the payload inline, feed it on stdin so
    # templates that read the body from stdin still receive it.
    needs_stdin = _PAYLOAD_TOKEN not in template
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin_bytes = payload_json.encode() if needs_stdin else b""
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(stdin_bytes),
            timeout=timeout,
        )
    except TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return False, f"notify template timed out after {timeout}s: {argv[0]!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"notify template execution error: {exc}"

    if proc.returncode != 0:
        err = stderr_bytes.decode(errors="replace").strip() or f"exit {proc.returncode}"
        return False, err[:2000]
    return True, ""


async def enqueue_dispatch(
    db: Any,
    *,
    kind: str,
    deliver_to: str,
    body: dict | None = None,
    dedup_key: str | None = None,
    ack_required: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    expires_at: float | None = None,
    session_id: str | None = None,
    schedule_run_id: str | None = None,
) -> str:
    """Insert a pending dispatch_outbox row; returns the dispatch id.

    Idempotent on ``dedup_key``: a re-enqueue with the same key returns the
    existing row's id rather than inserting a duplicate.

    ``max_attempts`` bounds delivery whether or not ``ack_required`` — an
    ack-required row that keeps sending successfully but never gets acked
    still exhausts at ``max_attempts`` sends (``dead_letter``, distinct
    ``DEAD_LETTER_ACK_TIMEOUT`` reason) rather than re-delivering forever.
    ``expires_at`` is an *additional*, optional bound honored on top of
    ``max_attempts``; it is not required for ``ack_required`` rows to be
    bounded.
    """
    now = time.time()
    dispatch_id = uuid.uuid4().hex
    ack_token = uuid.uuid4().hex if ack_required else None

    signal = DispatchSignal(
        dispatch_id=dispatch_id,
        kind=kind,
        deliver_to=deliver_to,
        attempt=0,
        ack_token=ack_token,
        body=body or {},
    )
    payload_dict = signal.to_dict(mode="json")

    async with db._tx() as conn:
        if dedup_key is not None:
            existing = (
                (
                    await conn.execute(
                        text("SELECT id FROM dispatch_outbox WHERE dedup_key = :dk"),
                        {"dk": dedup_key},
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return existing["id"]

        await conn.execute(
            text(
                "INSERT INTO dispatch_outbox "
                "(id, kind, deliver_to, payload, dedup_key, status, attempt, "
                " max_attempts, next_attempt_at, ack_required, ack_token, "
                " session_id, schedule_run_id, last_error, created_at, expires_at, updated_at) "
                "VALUES (:id, :kind, :deliver_to, :payload, :dedup_key, 'pending', 0, "
                " :max_attempts, :next_attempt_at, :ack_required, :ack_token, "
                " :session_id, :schedule_run_id, NULL, :created_at, :expires_at, :updated_at)"
            ).bindparams(bindparam("payload", type_=JSON)),
            {
                "id": dispatch_id,
                "kind": kind,
                "deliver_to": deliver_to,
                "payload": payload_dict,
                "dedup_key": dedup_key,
                "max_attempts": max_attempts,
                "next_attempt_at": now,
                "ack_required": int(ack_required),
                "ack_token": ack_token,
                "session_id": session_id,
                "schedule_run_id": schedule_run_id,
                "created_at": now,
                "expires_at": expires_at,
                "updated_at": now,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO status_transitions "
                "(id, entity_type, entity_id, previous_status, status, "
                " reason_code, reason_summary, evidence_refs, source, actor, created_at, metadata) "
                "VALUES (:id, 'dispatch', :entity_id, NULL, 'pending', "
                " :reason_code, :reason_summary, :evidence_refs, 'system', :actor, :created_at, :metadata)"
            ).bindparams(
                bindparam("evidence_refs", type_=JSON),
                bindparam("metadata", type_=JSON),
            ),
            {
                "id": uuid.uuid4().hex,
                "entity_id": dispatch_id,
                "reason_code": DispatchReasons.PENDING_ENQUEUED,
                "reason_summary": f"enqueued kind={kind}",
                "evidence_refs": [],
                "actor": "enqueue_dispatch",
                "created_at": now,
                "metadata": {},
            },
        )

    return dispatch_id


async def get_dispatch(db: Any, dispatch_id: str) -> dict[str, Any] | None:
    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM dispatch_outbox WHERE id = :id"), {"id": dispatch_id}
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    out = dict(row)
    if isinstance(out.get("payload"), str):
        out["payload"] = json.loads(out["payload"])
    return out


async def list_dispatches(
    db: Any,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    async with db._read() as conn:
        if status:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM dispatch_outbox WHERE status = :status "
                            "ORDER BY created_at DESC LIMIT :lim"
                        ),
                        {"status": status, "lim": limit},
                    )
                )
                .mappings()
                .all()
            )
        else:
            rows = (
                (
                    await conn.execute(
                        text("SELECT * FROM dispatch_outbox ORDER BY created_at DESC LIMIT :lim"),
                        {"lim": limit},
                    )
                )
                .mappings()
                .all()
            )
    out = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("payload"), str):
            d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


async def deliver_due_dispatches(
    db: Any,
    *,
    now: float | None = None,
    notify_template: list[str] | None = None,
) -> dict[str, int]:
    """Scan due pending/delivering rows and attempt delivery. Called from the scheduler tick.

    Ack-required rows loop back to ``pending`` (not ``delivered``) on transport
    success, so the same due-scan re-attempts delivery with backoff until the
    consumer acks, the row expires, or ``max_attempts`` sends have gone out
    unacked (``dead_letter``, ``DEAD_LETTER_ACK_TIMEOUT``) — the default
    (``ack_required=0``) tier stops at ``delivered`` on first transport
    success. ``delivering`` rows are re-scanned for crash recovery, but a
    claim on one is only exclusive for the duration of a lease
    (``_CLAIM_LEASE_SECONDS``): the guarded attempt-counter CAS in
    ``transition()`` prevents two overlapping scans from both running the
    notify transport for the same attempt.

    Race hardening: the due-row snapshot below and each row's subsequent
    ``transition()`` calls are separate transactions, so an operator
    ``purge_dispatch``/``purge_dispatches`` call can delete a snapshotted row
    in between -- ``transition()`` raises ``LookupError`` when its target row
    no longer exists (see ``lionagi.state.transitions.transition``, which
    ``SELECT``s the row inside its own ``_tx()`` and raises if the ``SELECT``
    misses). That is caught per-row here so one concurrently purged row is
    skipped (logged at debug) rather than aborting delivery for the rest of
    the batch.
    """
    if now is None:
        now = time.time()
    if notify_template is None:
        notify_template = resolve_notify_template()

    counts = {"attempted": 0, "delivered": 0, "dead_letter": 0, "expired": 0, "retried": 0}

    async with db._read() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, kind, deliver_to, payload, attempt, max_attempts, "
                        "ack_required, expires_at, status FROM dispatch_outbox "
                        "WHERE status IN ('pending', 'delivering') AND next_attempt_at <= :now"
                    ),
                    {"now": now},
                )
            )
            .mappings()
            .all()
        )

    for row in rows:
        counts["attempted"] += 1
        dispatch_id = row["id"]
        try:
            await _deliver_one_due_row(
                db, row, now=now, notify_template=notify_template, counts=counts
            )
        except LookupError:
            _log.debug(
                "deliver_due_dispatches: dispatch %s vanished mid-scan (likely an "
                "operator purge racing this tick); skipping, continuing the batch.",
                dispatch_id,
            )

    return counts


async def _deliver_one_due_row(
    db: Any,
    row: Any,
    *,
    now: float,
    notify_template: list[str] | None,
    counts: dict[str, int],
) -> None:
    """Claim-and-deliver one due row (extracted so ``deliver_due_dispatches`` can catch a
    mid-scan ``LookupError`` per row without aborting the rest of the batch).

    Every early ``return`` below is a normal outcome for this row (expired,
    lost the claim race, dead-lettered, retried, ...). A row missing at
    ``transition()`` time (e.g. an operator purge raced this scan) instead
    propagates ``LookupError`` to the caller.
    """
    dispatch_id = row["id"]

    if row["expires_at"] is not None and row["expires_at"] <= now:
        result = await transition(
            db,
            TransitionRequest(
                entity_type="dispatch",
                entity_id=dispatch_id,
                from_state=row["status"],
                to_state="expired",
                reason=StateReason(
                    code=DispatchReasons.EXPIRED_DEADLINE,
                    summary="expires_at reached before delivery",
                ),
                actor=Actor(type="scheduler", id="dispatch_delivery_loop"),
                idempotency_key=f"expire:{dispatch_id}:{row['attempt']}",
            ),
        )
        if result.applied:
            counts["expired"] += 1
        return

    next_attempt = row["attempt"] + 1
    delivering = await transition(
        db,
        TransitionRequest(
            entity_type="dispatch",
            entity_id=dispatch_id,
            from_state=row["status"],
            to_state="delivering",
            reason=StateReason(
                code=DispatchReasons.DELIVERING_ATTEMPT,
                summary=f"attempt {next_attempt}",
            ),
            actor=Actor(type="scheduler", id="dispatch_delivery_loop"),
            idempotency_key=f"deliver:{dispatch_id}:{row['attempt']}",
        ),
        # A `delivering -> delivering` recovery claim is a same-state
        # match on status alone, so guard on the pre-claim attempt value
        # too: the atomic patch below bumps attempt as part of THIS
        # UPDATE, so a second overlapping claimant's guard misses and it
        # loses instead of also running the transport. The next_attempt_at
        # lease keeps a live (non-crashed) claim from being re-picked-up
        # by a later scan until the transport should plausibly be done.
        guard={"attempt": row["attempt"]},
        patch={"attempt": next_attempt, "next_attempt_at": now + _CLAIM_LEASE_SECONDS},
    )
    if not delivering.applied:
        # Already claimed this tick (or state moved) — skip.
        return

    raw_payload = row["payload"]
    payload_json = raw_payload if isinstance(raw_payload, str) else json.dumps(raw_payload)

    if notify_template is None:
        success, err = False, "no dispatch.notify_template configured"
    else:
        success, err = await _exec_notify_template(
            notify_template,
            payload_json=payload_json,
            deliver_to=row["deliver_to"],
        )

    if success:
        # ack_required rows loop back to pending awaiting the consumer's
        # ack_token, but that must still be bounded by max_attempts (the
        # ADR's boundedness contract applies to every send while awaiting
        # ack, not only to transport failures) — otherwise a successful
        # transport to a dead/non-acking consumer re-delivers forever.
        if row["ack_required"] and next_attempt >= row["max_attempts"]:
            result = await transition(
                db,
                TransitionRequest(
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    from_state="delivering",
                    to_state="dead_letter",
                    reason=StateReason(
                        code=DispatchReasons.DEAD_LETTER_ACK_TIMEOUT,
                        summary=f"{next_attempt} sends without ack (max_attempts exhausted)",
                    ),
                    actor=Actor(type="scheduler", id="dispatch_delivery_loop"),
                    idempotency_key=f"ack_timeout:{dispatch_id}:{next_attempt}",
                ),
            )
            if result.applied:
                counts["dead_letter"] += 1
            return

        to_state = "pending" if row["ack_required"] else "delivered"
        patch = (
            {"next_attempt_at": now + backoff_seconds(next_attempt)}
            if to_state == "pending"
            else None
        )
        result = await transition(
            db,
            TransitionRequest(
                entity_type="dispatch",
                entity_id=dispatch_id,
                from_state="delivering",
                to_state=to_state,
                reason=StateReason(
                    code=DispatchReasons.DELIVERED_TRANSPORT_OK,
                    summary="transport succeeded",
                ),
                actor=Actor(type="scheduler", id="dispatch_delivery_loop"),
                idempotency_key=f"delivered:{dispatch_id}:{next_attempt}",
            ),
            patch=patch,
        )
        if result.applied:
            counts["delivered"] += 1
        return

    async with db._tx() as conn:
        await conn.execute(
            text("UPDATE dispatch_outbox SET last_error = :err, updated_at = :now WHERE id = :id"),
            {"err": err, "now": now, "id": dispatch_id},
        )

    if next_attempt >= row["max_attempts"]:
        result = await transition(
            db,
            TransitionRequest(
                entity_type="dispatch",
                entity_id=dispatch_id,
                from_state="delivering",
                to_state="dead_letter",
                reason=StateReason(
                    code=DispatchReasons.DEAD_LETTER_MAX_ATTEMPTS,
                    summary=f"{next_attempt} attempts exhausted: {err}",
                ),
                actor=Actor(type="scheduler", id="dispatch_delivery_loop"),
                idempotency_key=f"dead_letter:{dispatch_id}:{next_attempt}",
            ),
        )
        if result.applied:
            counts["dead_letter"] += 1
        return

    backoff = backoff_seconds(next_attempt)
    result = await transition(
        db,
        TransitionRequest(
            entity_type="dispatch",
            entity_id=dispatch_id,
            from_state="delivering",
            to_state="pending",
            reason=StateReason(
                code=DispatchReasons.PENDING_RETRY_BACKOFF,
                summary=f"retry in {backoff:.0f}s: {err}",
            ),
            actor=Actor(type="scheduler", id="dispatch_delivery_loop"),
            idempotency_key=f"retry:{dispatch_id}:{next_attempt}",
        ),
    )
    if result.applied:
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE dispatch_outbox SET next_attempt_at = :nat WHERE id = :id"),
                {"nat": now + backoff, "id": dispatch_id},
            )
        counts["retried"] += 1


async def ack_dispatch(db: Any, dispatch_id: str, ack_token: str) -> bool:
    """Present ack_token for an ack_required row; transitions to 'acked'."""
    row = await get_dispatch(db, dispatch_id)
    if row is None:
        raise LookupError(f"dispatch {dispatch_id!r} not found")
    if not row["ack_required"]:
        raise ValueError(f"dispatch {dispatch_id!r} does not require ack (ack_required=0)")
    if row["ack_token"] != ack_token:
        raise ValueError("ack_token mismatch")

    result = await transition(
        db,
        TransitionRequest(
            entity_type="dispatch",
            entity_id=dispatch_id,
            from_state=row["status"],
            to_state="acked",
            reason=StateReason(
                code=DispatchReasons.ACKED_CONSUMER,
                summary="consumer presented ack_token",
            ),
            actor=Actor(type="operator", id="li_dispatch_ack"),
            idempotency_key=f"ack:{dispatch_id}",
        ),
    )
    return result.applied


async def retry_dispatch(db: Any, dispatch_id: str) -> bool:
    """Operator override: force an immediate retry of a dead_letter/expired row."""
    row = await get_dispatch(db, dispatch_id)
    if row is None:
        raise LookupError(f"dispatch {dispatch_id!r} not found")
    if row["status"] not in ("dead_letter", "expired"):
        raise ValueError(
            f"dispatch {dispatch_id!r} is status={row['status']!r}; "
            "retry only applies to dead_letter or expired rows"
        )

    now = time.time()
    result = await transition(
        db,
        TransitionRequest(
            entity_type="dispatch",
            entity_id=dispatch_id,
            from_state=row["status"],
            to_state="pending",
            reason=StateReason(
                code=DispatchReasons.PENDING_RETRY_BACKOFF,
                summary="operator-forced retry",
            ),
            actor=Actor(type="operator", id="li_dispatch_retry"),
            idempotency_key=f"operator_retry:{dispatch_id}:{now}",
        ),
        # Rider B: the status change and the attempt/next_attempt_at/last_error
        # reset are one guarded write, not two transactions — a crash between
        # separate writes would otherwise leave a 'pending' row with stale
        # exhausted accounting.
        patch={"attempt": 0, "next_attempt_at": now, "last_error": None},
    )
    return result.applied


async def purge_dispatch(db: Any, dispatch_id: str, *, actor: str = "li_dispatch_purge") -> bool:
    """Single-row guarded delete inside BEGIN IMMEDIATE (RIDER B: direct-DB write discipline).

    Accepts any status: naming an exact id is already a deliberate,
    non-bulk operator action, unlike ``purge_dispatches`` below which
    requires explicit criteria to guard against a bare mass-delete. Writes
    one ``admin_events`` row (action="dispatch_purge") on a successful
    delete so single-row purges are auditable like the bulk path
    (ADR-0059 delta 3 — the shipped adapter wrote none).
    """
    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status FROM dispatch_outbox WHERE id = :id"),
                    {"id": dispatch_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return False

    async with db._tx() as conn:
        result = await conn.execute(
            text("DELETE FROM dispatch_outbox WHERE id = :id"),
            {"id": dispatch_id},
        )
    deleted = (result.rowcount or 0) > 0
    if deleted:
        await db.insert_admin_event(
            action="dispatch_purge",
            target_id=dispatch_id,
            details={"dispatch_id": dispatch_id, "status": row["status"], "total": 1},
            actor=actor,
        )
    return deleted


async def purge_dispatches(
    db: Any,
    *,
    status: str | None = None,
    before: float | None = None,
    dry_run: bool = False,
    actor: str = "li_dispatch_purge",
) -> dict[str, Any]:
    """Bulk-delete ``dispatch_outbox`` rows matching explicit criteria.

    At least one of ``status`` or ``before`` is required — a bare call with
    neither raises ``ValueError`` so a mistaken invocation cannot delete the
    entire table. ``before`` filters on ``updated_at``.

    Status semantics (deliberately asymmetric):

    - An explicit ``status`` is honored exactly as given, including
      ``pending``/``delivering`` — naming an in-flight status is deliberate
      operator intent (e.g. force-clearing a stuck row), not an accident.
    - A status-less call (``status=None``, only ``before`` supplied) is
      scoped to the terminal statuses only
      (``delivered``/``acked``/``dead_letter``/``expired``): it can never
      implicitly sweep ``pending``/``delivering`` rows that a live
      scheduler tick may still claim or retry.

    This is an operator action distinct from the automatic retention sweep
    in ``lionagi.studio.services.db_maintenance.prune_old_data`` (which is
    always terminal-only, on separate success/dead-letter windows); this
    function is the one path that can touch a non-terminal row, and only
    when the caller names that status explicitly.

    Writes one ``admin_events`` row (action="dispatch_purge") per call,
    always, including ``dry_run`` calls (so a dry run leaves an inspectable
    record of what would have been deleted). ``status_transitions`` rows for
    purged ids are preserved, matching ``purge_dispatch`` and the automatic
    sweep — see their docstrings for why.

    Returns ``{"total": N, "dry_run": bool, <status>: count, ...}``.
    """
    if status is None and before is None:
        raise ValueError("purge_dispatches requires status and/or before criteria")

    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    if status is not None:
        where_clauses.append("status = :status")
        params["status"] = status
    else:
        # No explicit status: default to terminal-only so a --before-only
        # purge can never implicitly delete pending/delivering rows.
        term_placeholders = ", ".join(f":term{i}" for i in range(len(_TERMINAL_DISPATCH_STATUSES)))
        where_clauses.append(f"status IN ({term_placeholders})")
        for i, term_status in enumerate(_TERMINAL_DISPATCH_STATUSES):
            params[f"term{i}"] = term_status
    if before is not None:
        where_clauses.append("updated_at <= :before")
        params["before"] = before
    where_sql = " AND ".join(where_clauses)

    count_sql = text(
        f"SELECT status, COUNT(*) AS n FROM dispatch_outbox "  # noqa: S608
        f"WHERE {where_sql} GROUP BY status"
    )

    if dry_run:
        async with db._read() as conn:
            rows = (await conn.execute(count_sql, params)).mappings().all()
        counts_by_status = {r["status"]: r["n"] for r in rows}
        total = sum(counts_by_status.values())
    else:
        # Select the exact match set inside the write transaction, derive the
        # audit counts from it, and delete by those ids only. A criteria-based
        # second DELETE could remove a different set than the count saw (the
        # transaction is not a database-wide lock on PostgreSQL), and the
        # admin_events record must describe the rows actually deleted.
        async with db._tx() as conn:
            matched = (
                (
                    await conn.execute(
                        text(
                            f"SELECT id, status FROM dispatch_outbox WHERE {where_sql}"  # noqa: S608
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            counts_by_status = {}
            for r in matched:
                counts_by_status[r["status"]] = counts_by_status.get(r["status"], 0) + 1
            total = len(matched)
            matched_ids = [r["id"] for r in matched]
            for i in range(0, len(matched_ids), 500):
                chunk = matched_ids[i : i + 500]
                placeholders = ", ".join(f":id{j}" for j in range(len(chunk)))
                await conn.execute(
                    text(f"DELETE FROM dispatch_outbox WHERE id IN ({placeholders})"),  # noqa: S608
                    {f"id{j}": v for j, v in enumerate(chunk)},
                )

    await db.insert_admin_event(
        action="dispatch_purge",
        details={
            "status": status,
            "before": before,
            "dry_run": dry_run,
            "total": total,
            "counts_by_status": counts_by_status,
        },
        actor=actor,
    )
    return {"total": total, "dry_run": dry_run, **counts_by_status}
