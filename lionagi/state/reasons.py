# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0057: controlled vocabulary for status reason codes on Studio entities."""

from __future__ import annotations

import json
from typing import Any, Final

# Canonical singular entity types (ADR-0057); validated at write time in
# StateDB.update_status().
VALID_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session",
        "show",
        "play",
        "invocation",
        "team",
        "schedule_run",
    }
)

# Frontend route alias: the /runs/<id> route renders the `session` entity.
ENTITY_ROUTE_ALIASES: Final[dict[str, str]] = {
    "run": "session",
}

# Plural table names, accepted in update_status() with a deprecation warning
# for older call sites; remove once all call sites use the canonical form.
ENTITY_TABLE_ALIASES: Final[dict[str, str]] = {
    "sessions": "session",
    "shows": "show",
    "plays": "play",
    "invocations": "invocation",
    "teams": "team",
    "schedule_runs": "schedule_run",
}


# The one allowed two-segment reason code; the three-segment linter skips it.
LEGACY_IMPORTED: Final[str] = "legacy.imported"


# Reason code format: <domain>.<status_or_outcome>.<cause>, three segments,
# lowercase/snake_case. Compound conditions go in reason_summary, not the code.


class RunReasons:
    """Outcomes of session execution (the CLI teardown's view)."""

    STARTED_OK = "run.started.ok"
    COMPLETED_OK = "run.completed.ok"
    FAILED_EXIT_NONZERO = "run.failed.exit_nonzero"
    FAILED_EXCEPTION = "run.failed.exception"
    # split by classify_provider_error's retryable classification -- see docs/internals/runtime.md
    FAILED_PROVIDER_RETRYABLE = "run.failed.provider_retryable"
    FAILED_PROVIDER_NONRETRYABLE = "run.failed.provider_nonretryable"
    FAILED_MISSING_ARTIFACT = "run.failed.missing_artifact"  # ADR-0029
    FAILED_MISSING_CWD = (
        "run.failed.missing_cwd"  # legacy; superseded by FAILED_CWD_INHERIT_REFUSED
    )
    # fail-closed: an execution root that can't be resolved at fire time must
    # not silently fall back to the daemon's own cwd -- see docs/internals/runtime.md
    FAILED_CWD_INHERIT_REFUSED = "run.failed.cwd_inherit_refused"
    FAILED_ESCALATED = "run.failed.escalated"  # undeclared-artifact backstop
    # loop exited clean but no commits/artifacts were produced (completion-trust gate)
    COMPLETED_EMPTY_NO_EVIDENCE = "run.completed_empty.no_evidence"
    # DAG's own result stands even when a post-completion finalize step raised
    COMPLETED_FINALIZE_ERROR = "run.completed.finalize_error"
    # a gate node rejected mid-DAG and its dependent subtree was
    # short-circuited to skipped rather than run against the rejected
    # baseline -- the run genuinely completed, so status stays "completed",
    # but the reason distinguishes it from a clean pass
    COMPLETED_GATE_REJECTED = "run.completed.gate_rejected"
    # the planned DAG completed, but at least one reactive SpawnRequest was
    # refused because the run had no remaining spawn capacity
    COMPLETED_SPAWN_REFUSED = "run.completed.spawn_refused"
    # unlike COMPLETED_FINALIZE_ERROR, a raised write failure is a real failure
    # (status -> failed); distinct from FAILED_MISSING_ARTIFACT's post-hoc gap
    FAILED_ARTIFACT_WRITE = "run.failed.artifact_write"
    # live-message persistence gave up at teardown: the run's own work stands,
    # but its transcript is missing messages it produced, so every later read
    # of that transcript is reading an incomplete record
    COMPLETED_MESSAGE_LOSS = "run.completed.message_loss"
    TIMED_OUT_DEADLINE = "run.timed_out.deadline"
    ABORTED_USER = "run.aborted.user"
    CANCELLED_SIGINT = "run.cancelled.sigint"
    CANCELLED_SIGTERM = "run.cancelled.sigterm"  # externally delivered SIGTERM (exit 143)
    CANCELLED_SYSTEM = "run.cancelled.system"
    CANCELLED_ORCHESTRATOR = "run.cancelled.orchestrator"
    # `li kill` reason codes
    CANCELLED_MANUAL_KILL = "run.cancelled.manual_kill"
    CANCELLED_FORCE_KILL = "run.cancelled.force_kill"
    CANCELLED_STALE_AUTO = "run.cancelled.stale_auto"
    PAUSED_OPERATOR = "run.paused.operator"
    # ADR-0071 D4: task-application worker lease outcomes
    QUEUED_LEASE_EXPIRED = "run.queued.lease_expired"
    FAILED_LEASE_ATTEMPTS_EXHAUSTED = "run.failed.lease_attempts_exhausted"
    # committed but the scheduler crashed before confirming launch; startup
    # recovery tombstones the orphaned row and re-fires a fresh occurrence
    FAILED_NEVER_DISPATCHED = "run.failed.never_dispatched"
    # ADR-0071 D3: admit() seam claim-time terminal rejections
    SKIPPED_WAITER_CAP_EXCEEDED = "run.skipped.waiter_cap_exceeded"
    SKIPPED_DURATION_EXCEEDS_LEASE = "run.skipped.duration_exceeds_lease"


class SessionReasons:
    """Health-derived reason codes written by operator-initiated transitions (ADR-0057)."""

    HEALTH_STALE_NO_HEARTBEAT = "session.stale.no_heartbeat"
    HEALTH_ORPHANED_NO_PROCESS = "session.orphaned.no_process"
    HEALTH_ZOMBIE_STALE_LOCKS = "session.zombie.stale_locks"
    HEALTH_PHANTOM_PROCESS_DEAD = "session.phantom.process_dead"
    HEALTH_PHANTOM_MISSING_ARTIFACTS = "session.phantom.missing_artifacts"

    # the only sanctioned exit from a terminal status; carries an override so
    # the reopening is attributable rather than an ordinary write
    REOPENED_BY_RESUME = "session.reopened.by_resume"


class PlayReasons:
    """Show-play lifecycle reasons (ADR-0057 play vocabulary)."""

    PENDING_CREATED = "play.pending.created"
    PENDING_WAITING_DEPS = "play.pending.waiting_on_deps"
    PENDING_READY = "play.pending.ready"
    BLOCKED_INVALID_DEPS = "play.blocked.invalid_deps"
    BLOCKED_DEP_FAILED = "play.blocked.dep_failed"
    GATE_FAILED_VERDICT = "play.gate_failed.verdict"
    ESCALATED_GATE_TWICE = "play.escalated.gate_twice"
    MERGED_OK = "play.merged.ok"


class ShowReasons:
    """Show-level orchestration reasons."""

    ACTIVE_CREATED = "show.active.created"
    BLOCKED_NO_READY_PLAYS = "show.blocked.no_ready_plays"
    COMPLETED_FINAL_GATE = "show.completed.final_gate"
    ABORTED_OPERATOR = "show.aborted.operator"
    # no `_final_verdict.json` ever landed; derived by the reaper from every child play's status
    COMPLETED_ALL_PLAYS_MERGED = "show.completed.all_plays_merged"


class ScheduleReasons:
    """ADR-0070 schedule-fire outcomes. The ``schedule.skipped.`` prefix is
    NOT the full set of reasons a skipped ``schedule_run`` can carry --
    ``DEFERRED_CAPACITY``, ``BUDGET_EXHAUSTED``, and admission-path rejections
    (``RunReasons.SKIPPED_WAITER_CAP_EXCEEDED``) also land on skipped rows
    without that prefix, so a consumer filtering by prefix silently drops
    them. See docs/internals/runtime.md for the per-code caveats (missed-fire
    detection timing, the precondition-code default, deferral sampling)."""

    QUEUED_CREATED = "schedule.queued.created"
    FIRED_DUE = "schedule.fired.due"
    SKIPPED_PRECONDITION = "schedule.skipped.precondition"
    SKIPPED_OVERLAP = "schedule.skipped.overlap"
    SKIPPED_MISSED_FIRE = "schedule.skipped.missed_fire"
    DEFERRED_CAPACITY = "schedule.deferred.capacity"
    BUDGET_EXHAUSTED = "schedule.budget.exhausted"


class TeamReasons:
    """Team lifecycle outcomes (entity_type='team')."""

    ARCHIVED_OPERATOR = "team.archived.operator"


class DispatchReasons:
    """ADR-0059 dispatch_outbox transition outcomes (entity_type='dispatch')."""

    PENDING_ENQUEUED = "dispatch.pending.enqueued"
    DELIVERING_ATTEMPT = "dispatch.delivering.attempt"
    DELIVERED_TRANSPORT_OK = "dispatch.delivered.transport_ok"
    PENDING_RETRY_BACKOFF = "dispatch.pending.retry_backoff"
    DEAD_LETTER_MAX_ATTEMPTS = "dispatch.dead_letter.max_attempts"
    DEAD_LETTER_ACK_TIMEOUT = "dispatch.dead_letter.ack_timeout"
    EXPIRED_DEADLINE = "dispatch.expired.deadline"
    ACKED_CONSUMER = "dispatch.acked.consumer"


# Validator


def _collect(*classes: type) -> frozenset[str]:
    """Collect str-valued public attributes from reason classes into the controlled vocabulary."""
    out: set[str] = set()
    for cls in classes:
        for name, value in vars(cls).items():
            if name.startswith("_"):
                continue
            if isinstance(value, str):
                out.add(value)
    return frozenset(out)


VALID_REASON_CODES: Final[frozenset[str]] = _collect(
    RunReasons,
    SessionReasons,
    PlayReasons,
    ShowReasons,
    ScheduleReasons,
    TeamReasons,
    DispatchReasons,
) | {LEGACY_IMPORTED}


# Validation helpers


def validate_reason_code(code: str) -> str:
    """Return code if registered in VALID_REASON_CODES; raises ValueError otherwise."""
    if code not in VALID_REASON_CODES:
        raise ValueError(
            f"invalid reason_code: {code!r}. Must be one of "
            f"{sorted(VALID_REASON_CODES)} (defined in "
            "lionagi/state/reasons.py)"
        )
    return code


def validate_entity_type(entity_type: str) -> str:
    """Return the canonical entity_type (resolving route and table aliases); raises ValueError if unknown."""
    if entity_type in VALID_ENTITY_TYPES:
        return entity_type
    if entity_type in ENTITY_ROUTE_ALIASES:
        return ENTITY_ROUTE_ALIASES[entity_type]
    if entity_type in ENTITY_TABLE_ALIASES:
        return ENTITY_TABLE_ALIASES[entity_type]
    raise ValueError(
        f"invalid entity_type: {entity_type!r}. Must be one of "
        f"{sorted(VALID_ENTITY_TYPES)} (or a registered alias)"
    )


# Table mapping
# StateDB.update_status() uses this to resolve canonical entity_type
# → physical table name for the UPDATE statement.

ENTITY_TYPE_TO_TABLE: Final[dict[str, str]] = {
    "session": "sessions",
    "show": "shows",
    "play": "plays",
    "invocation": "invocations",
    "team": "teams",
    "schedule_run": "schedule_runs",
}


def entity_table(entity_type: str) -> str:
    """Resolve entity_type (including aliases) to its SQLite table name."""
    canonical = validate_entity_type(entity_type)
    return ENTITY_TYPE_TO_TABLE[canonical]


#: Prefix for the node_metadata keys a teardown writes when it observed live-message
#: loss but does not own the session's terminal row. One key per observing leg, never a
#: shared one: legs tear down concurrently, and the database merges a patch key by key,
#: so a leg that rewrote a shared value would replace a sibling's record instead of
#: adding to it. Keys are left in place once folded into a terminal row -- clearing them
#: would race the next leg's write -- and their number is bounded by how many teardowns
#: one session has.
MESSAGE_LOSS_KEY_PREFIX: Final = "message_persist_loss:"


def carried_message_loss_queues(node_metadata: Any) -> list[Any]:
    """Every queue loss left on a session row, across the legs that recorded them.

    Entries are returned as found. They crossed a persistence boundary and may have been
    written by a leg running different code, so the caller validates before summing.
    """
    if isinstance(node_metadata, str):
        try:
            node_metadata = json.loads(node_metadata)
        except (TypeError, ValueError):
            return []
    if not isinstance(node_metadata, dict):
        return []
    queues: list[Any] = []
    for key, value in node_metadata.items():
        if not (isinstance(key, str) and key.startswith(MESSAGE_LOSS_KEY_PREFIX)):
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                continue
        if isinstance(value, dict) and isinstance(value.get("queues"), list):
            queues.extend(value["queues"])
    return queues


def has_message_loss_evidence(session: Any) -> bool:
    """Whether a session row records that it lost live message events.

    Reads the evidence refs rather than the reason code: a row carries one reason and
    a session that lost messages may have failed for something else entirely.
    """
    if not isinstance(session, dict):
        return False
    refs = session.get("status_evidence_refs")
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except (TypeError, ValueError):
            return False
    if isinstance(refs, list) and any(
        isinstance(ref, dict) and ref.get("kind") == "message_persist_loss" for ref in refs
    ):
        return True
    # A leg whose own terminal write never landed -- it deferred, or lost the race to a
    # concurrent teardown -- leaves the loss here instead, and no evidence ref exists.
    return bool(carried_message_loss_queues(session.get("node_metadata")))
