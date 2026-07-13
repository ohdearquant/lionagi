# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0071 D3: the ``admit()`` admission seam.

Extracts the worker claim loop's admission predicate into one named,
StateDB-backed, unit-testable function: ``admit(row, worker, db) ->
AdmissionDecision``. This borrows ``Processor.handle_denied``'s
terminal-vs-deferred return *shape* only (``lionagi/protocols/generic/
processor.py``: ``True`` means terminal, ``False`` means deferred/re-enqueue),
never the ``Processor`` class itself -- ``Processor`` is ``asyncio.Queue``-backed
and in-process only, which would only ever see jobs submitted inside its own
process and is useless for a fleet of independent CLI processes claiming from
a shared ``schedule_runs`` table.

Conditions evaluated (ADR-0071 D3):

1. Capability match (``capabilities.worker_can_serve``) -- a mismatch defers
   (the row is left ``queued``, never faked; unchanged D4 behavior).
2. Concurrency-key block -- a matching key currently ``running`` (this pass
   or a prior one) defers the row to the next tick; unchanged D4 behavior.
3. Waiter cap (D-Cap) -- per ``concurrency_key``, at most
   ``key_concurrency * waiter_cap_multiplier`` rows may sit ``queued`` /
   ``retry_wait`` behind a running holder. Over cap is a terminal rejection
   unless the submission opted into deferred/parked semantics (D-Reject).
4. Duration guard (D6) -- a job declaring a ``max_duration_seconds`` at or
   above the worker's lease TTL is terminal-rejected: lease renewal is not
   yet shipped (ADR-0071 delta #5), so an admitted long-runner would just
   lose its lease mid-flight.

GPU/bench-window locks are never consulted here: ``admit()`` only ever reads
StateDB. Machine-local lock acquisition and arbitration stay a worker-side
execution responsibility (ADR-0071 D5's own stated limit, reaffirmed by D3).

``action_args["admission"]`` payload convention (documented shape inside the
existing free-form ``args``/``action_args`` dict, no schema change -- the
same style as D5's ``SeatSpec`` convention):

    {
        "max_duration_seconds": <float>,       # duration guard input
        "allow_deferred_over_cap": <bool>,     # opt out of terminal rejection
                                                # when the waiter cap is hit
        "notify": {
            "deliver_to": <str>,                # required, non-empty
            "kind": <str>,                      # optional, default "terminal_notify"
            "dedup_key": <str | None>,          # optional
        },
    }

A ``notify`` payload with a field of the wrong type (e.g. ``deliver_to`` as
an int) is dropped by ``notify_request()`` rather than surfaced -- it must
never crash the claim loop for a row that is already correctly skipped.

A claim-time
terminal rejection must surface observably even though the submitter is no
longer on the wire by then. ``worker.py``'s claim loop, on a terminal
``AdmissionDecision``, transitions the row ``queued -> skipped`` carrying the
reason and -- whenever ``notify_request()`` finds a notify payload -- emits a
``dispatch_outbox`` row via ``lionagi.dispatch.outbox.enqueue_dispatch``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.studio.scheduler import capabilities

__all__ = (
    "DEFAULT_KEY_CONCURRENCY",
    "DEFAULT_WAITER_CAP_MULTIPLIER",
    "AdmissionDecision",
    "WorkerCaps",
    "admit",
    "allows_deferred_over_cap",
    "declared_max_duration_seconds",
    "holder_is_running",
    "normalize_action_args",
    "notify_request",
    "waiter_ahead_count",
)

# D-Cap default: 2x the worker's configured concurrency for a key. A starting
# default, tunable per resource later, not a hard architectural limit.
DEFAULT_WAITER_CAP_MULTIPLIER = 2

# Today's claim loop only ever allows one running row per concurrency_key
# (a matching key currently running blocks any other claim outright), so the
# effective per-key concurrency is 1 until a future slice adds real per-key
# parallelism.
DEFAULT_KEY_CONCURRENCY = 1

_HOLDER_RUNNING_SQL = """
    SELECT 1 FROM schedule_runs
    WHERE concurrency_key = :key AND status = 'running'
    LIMIT 1
"""

_WAITER_AHEAD_WITH_EXCLUDE_SQL = """
    SELECT COUNT(*) AS n FROM schedule_runs
    WHERE concurrency_key = :key
      AND status IN ('queued', 'retry_wait')
      AND id != :exclude_id
      AND (queued_at < :before_queued_at
           OR (queued_at = :before_queued_at AND id < :exclude_id))
"""

_WAITER_COUNT_NO_EXCLUDE_SQL = """
    SELECT COUNT(*) AS n FROM schedule_runs
    WHERE concurrency_key = :key
      AND status IN ('queued', 'retry_wait')
"""


@dataclass(frozen=True)
class AdmissionDecision:
    """``admit()``'s return value. ``terminal`` mirrors ``Processor.handle_denied``'s
    convention: ``True`` = terminal (reject loud), ``False`` = deferred (leave
    ``queued``, retried next tick). ``terminal`` is only meaningful when
    ``admitted`` is ``False``."""

    admitted: bool
    terminal: bool = False
    reason_code: str | None = None
    reason_summary: str | None = None


@dataclass
class WorkerCaps:
    """Per-pass worker context ``admit()`` consults.

    ``claimed_keys`` accumulates concurrency_keys claimed earlier in the SAME
    ``claim_and_execute`` pass, so a key whose row already finished executing
    (and moved past ``running`` to a terminal status before the next
    candidate is examined) is still treated as an active holder for the rest
    of this pass -- see ``worker.py``'s ``claim_and_execute`` pass-local
    blocking behavior, unchanged from before this extraction.
    """

    advertised_capabilities: list[str] = field(default_factory=list)
    lease_ttl: float = 300.0
    waiter_cap_multiplier: int = DEFAULT_WAITER_CAP_MULTIPLIER
    key_concurrency: int = DEFAULT_KEY_CONCURRENCY
    claimed_keys: set[str] = field(default_factory=set)


def normalize_action_args(value: Any) -> dict[str, Any]:
    """Normalize a row's ``action_args`` column: a JSON string on SQLite, a
    native dict on Postgres, or already-parsed. Malformed/absent input
    normalizes to ``{}`` rather than raising -- admission checks degrade to
    "no declared opts" instead of crashing the claim loop."""
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(value, dict):
        return value
    return {}


def _admission_opts(action_args: Mapping[str, Any]) -> dict[str, Any]:
    opts = action_args.get("admission")
    return opts if isinstance(opts, dict) else {}


def declared_max_duration_seconds(action_args: Mapping[str, Any]) -> float | None:
    """The job's declared ``admission.max_duration_seconds``, or ``None`` if
    absent/malformed."""
    value = _admission_opts(action_args).get("max_duration_seconds")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def allows_deferred_over_cap(action_args: Mapping[str, Any]) -> bool:
    """True iff the submission opted into deferred/parked semantics
    (``admission.allow_deferred_over_cap``), which downgrades an
    over-waiter-cap outcome from terminal rejection to an ordinary deferral."""
    return bool(_admission_opts(action_args).get("allow_deferred_over_cap"))


def notify_request(action_args: Mapping[str, Any]) -> dict[str, Any] | None:
    """The job's ``admission.notify`` payload if present and well-formed,
    else ``None``.

    ``deliver_to`` must be a non-empty ``str``; the optional ``kind`` and
    ``dedup_key`` fields, when the key is present at all, must be a
    non-empty ``str`` -- an explicit ``null`` is rejected the same as any
    other wrong type, it is not treated as "absent". A malformed payload
    (e.g. ``deliver_to`` as an ``int``, or an explicit ``kind: null``) is
    treated as no notify request -- it must never surface later as a
    claim-time crash in ``DispatchSignal``/``enqueue_dispatch``."""
    notify = _admission_opts(action_args).get("notify")
    if not isinstance(notify, dict):
        return None
    deliver_to = notify.get("deliver_to")
    if not isinstance(deliver_to, str) or not deliver_to:
        return None
    if "kind" in notify:
        kind = notify["kind"]
        if not isinstance(kind, str) or not kind:
            return None
    if "dedup_key" in notify:
        dedup_key = notify["dedup_key"]
        if not isinstance(dedup_key, str) or not dedup_key:
            return None
    return notify


async def holder_is_running(db: StateDB, concurrency_key: str) -> bool:
    """True iff some row currently ``running`` shares *concurrency_key*."""
    async with db._read() as conn:
        row = (await conn.execute(text(_HOLDER_RUNNING_SQL), {"key": concurrency_key})).first()
    return row is not None


async def waiter_ahead_count(
    db: StateDB,
    concurrency_key: str,
    *,
    before_queued_at: float,
    exclude_id: str | None = None,
) -> int:
    """Count of rows sharing *concurrency_key* in ``queued``/``retry_wait``
    that are "ahead" of a row queued at *before_queued_at*.

    With *exclude_id* (the claim-time case: the row already exists), "ahead"
    means strictly earlier by the same ``(queued_at, id)`` order the claim
    loop pages candidates by -- rows queued later never count against an
    earlier one, so the outcome is independent of processing order.

    Without *exclude_id* (the submit-time case: the row doesn't exist yet),
    every current waiter for the key counts, since a not-yet-inserted row
    would land after all of them.
    """
    if exclude_id is not None:
        async with db._read() as conn:
            result = (
                (
                    await conn.execute(
                        text(_WAITER_AHEAD_WITH_EXCLUDE_SQL),
                        {
                            "key": concurrency_key,
                            "exclude_id": exclude_id,
                            "before_queued_at": before_queued_at,
                        },
                    )
                )
                .mappings()
                .first()
            )
    else:
        async with db._read() as conn:
            result = (
                (await conn.execute(text(_WAITER_COUNT_NO_EXCLUDE_SQL), {"key": concurrency_key}))
                .mappings()
                .first()
            )
    return result["n"] if result else 0


async def admit(
    row: Mapping[str, Any],
    worker: WorkerCaps,
    db: StateDB,
    *,
    now: float | None = None,
) -> AdmissionDecision:
    """The admission decision for one candidate *row*. Pure StateDB reads
    only -- never a write, never a machine-local lock acquisition."""
    now = now if now is not None else time.time()
    action_args = normalize_action_args(row.get("action_args"))

    # 4. Duration guard: unconditional and static (independent of concurrency
    # state), so it is checked first regardless of whether the row is also
    # capability- or concurrency-blocked.
    max_duration = declared_max_duration_seconds(action_args)
    if max_duration is not None and max_duration >= worker.lease_ttl:
        return AdmissionDecision(
            admitted=False,
            terminal=True,
            reason_code=RunReasons.SKIPPED_DURATION_EXCEEDS_LEASE,
            reason_summary=(
                f"declared max_duration_seconds={max_duration} >= lease TTL "
                f"({worker.lease_ttl}s); lease renewal is not yet shipped "
                "(ADR-0071 delta #5)"
            ),
        )

    # 1. Capability match -- unchanged D4 behavior, deferred not terminal.
    required = capabilities.matching_tokens(_as_list(row.get("required_capabilities")))
    if not capabilities.worker_can_serve(required, worker.advertised_capabilities):
        return AdmissionDecision(
            admitted=False,
            terminal=False,
            reason_summary="worker does not advertise required capabilities for this row",
        )

    # 2 & 3. Concurrency-key block + waiter cap.
    concurrency_key = row.get("concurrency_key")
    if concurrency_key is not None:
        holder_running = concurrency_key in worker.claimed_keys or await holder_is_running(
            db, concurrency_key
        )
        if holder_running:
            cap = worker.key_concurrency * worker.waiter_cap_multiplier
            ahead = await waiter_ahead_count(
                db,
                concurrency_key,
                before_queued_at=row["queued_at"],
                exclude_id=row["id"],
            )
            if ahead >= cap:
                if allows_deferred_over_cap(action_args):
                    return AdmissionDecision(
                        admitted=False,
                        terminal=False,
                        reason_summary=(
                            f"waiter cap ({cap}) exceeded for "
                            f"concurrency_key={concurrency_key!r} but submission opted "
                            "into deferred/parked semantics"
                        ),
                    )
                return AdmissionDecision(
                    admitted=False,
                    terminal=True,
                    reason_code=RunReasons.SKIPPED_WAITER_CAP_EXCEEDED,
                    reason_summary=(
                        f"waiter cap ({cap}) exceeded for concurrency_key="
                        f"{concurrency_key!r}: {ahead} job(s) already waiting behind "
                        "the running holder"
                    ),
                )
            return AdmissionDecision(
                admitted=False,
                terminal=False,
                reason_summary=(
                    f"concurrency_key={concurrency_key!r} currently running; deferred to next tick"
                ),
            )

    return AdmissionDecision(admitted=True)


def _as_list(value: Any) -> list[Any]:
    """Normalize a JSON column that is a string on SQLite but a native list
    on Postgres -- mirrors ``worker._normalize_json_list`` (duplicated here,
    not imported, to avoid a circular import with ``worker.py``, which
    imports this module)."""
    if not value:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)
