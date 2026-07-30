# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0057: controlled vocabulary for status reason codes on Studio entities."""

from __future__ import annotations

from typing import Final

# ── Entity taxonomy ──────────────────────────────────────────────────
# Canonical singular entity types consumed by ADR-0057 (status reasons),
# the attention queue and entity headers. Validated
# at write time in StateDB.update_status().

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

# Frontend route aliases. The /runs/<id> route renders the `session`
# entity; the entity_type stored in status_transitions and the
# attention queue is always the canonical singular name.
ENTITY_ROUTE_ALIASES: Final[dict[str, str]] = {
    "run": "session",
}

# Plural-to-singular form used by older code paths that accidentally
# pass table names. Permitted in update_status() with a deprecation
# warning; remove once all call sites use the canonical form.
ENTITY_TABLE_ALIASES: Final[dict[str, str]] = {
    "sessions": "session",
    "shows": "show",
    "plays": "play",
    "invocations": "invocation",
    "teams": "team",
    "schedule_runs": "schedule_run",
}


# ── Sentinel ─────────────────────────────────────────────────────────
# The one allowed two-segment reason code. All other codes follow the
# <domain>.<status_or_outcome>.<cause> three-segment format. The
# linter step that enforces three segments skips this single value.

LEGACY_IMPORTED: Final[str] = "legacy.imported"


# ── Reason code classes ──────────────────────────────────────────────
# Format: <domain>.<status_or_outcome>.<cause>
# Three segments. Lowercase. snake_case for multi-word causes.
# Compound conditions go in reason_summary, not in the code.


class RunReasons:
    """Outcomes of session execution (the CLI teardown's view)."""

    STARTED_OK = "run.started.ok"
    COMPLETED_OK = "run.completed.ok"
    FAILED_EXIT_NONZERO = "run.failed.exit_nonzero"
    FAILED_EXCEPTION = "run.failed.exception"
    # A terminal ProviderError (provider CLI subprocess failure) classified
    # by lionagi.providers._provider_errors.classify_provider_error, split by
    # its retryable classification (rate limit / capacity / dropped stream vs
    # bad credentials / unsupported model / context overflow / safety block).
    FAILED_PROVIDER_RETRYABLE = "run.failed.provider_retryable"
    FAILED_PROVIDER_NONRETRYABLE = "run.failed.provider_nonretryable"
    FAILED_MISSING_ARTIFACT = "run.failed.missing_artifact"  # ADR-0029
    # Historical vocabulary: prior to the fail-closed cwd resolver, a schedule
    # whose persisted execution root (action_cwd) or action_project path no
    # longer existed at fire time fell back to inheriting the daemon's own cwd,
    # and the spawned process then exited non-zero. The resolver now refuses
    # that fallback up front for any schedule that carries an execution root
    # (see FAILED_CWD_INHERIT_REFUSED), so new runs no longer receive this
    # code; it stays defined so runs persisted before that change remain
    # readable.
    FAILED_MISSING_CWD = "run.failed.missing_cwd"
    # A schedule carrying an explicit execution root (action_cwd or
    # action_project) could not resolve any of its configured directories at
    # fire time, and the only remaining option was inheriting the daemon's own
    # working directory. Running the action there would execute it in the
    # daemon's directory instead of the schedule's configured root -- silently
    # substituting the working directory (and whatever a tool derives from it)
    # for the one the schedule asked for -- so the resolver fails closed. The
    # error names the configured-but-unavailable root and the daemon directory
    # that would have been substituted.
    FAILED_CWD_INHERIT_REFUSED = "run.failed.cwd_inherit_refused"
    FAILED_ESCALATED = "run.failed.escalated"  # undeclared-artifact backstop
    # Loop exited clean but no commits/artifacts were produced (completion-trust gate).
    COMPLETED_EMPTY_NO_EVIDENCE = "run.completed_empty.no_evidence"
    # DAG produced a genuine result but a post-completion finalize step
    # (persistence/team-teardown) raised. Status stays "completed" — the
    # DAG's own outcome, not the finalize step's — this reason code is what
    # distinguishes it from a clean COMPLETED_OK run.
    COMPLETED_FINALIZE_ERROR = "run.completed.finalize_error"
    # The DAG itself completed, but writing its own output (the synthesis
    # artifact) raised. Unlike COMPLETED_FINALIZE_ERROR, this is a real
    # failure: a run that cannot deliver its output has not succeeded, so
    # status flips to "failed" rather than staying "completed". Distinct
    # from FAILED_MISSING_ARTIFACT (ADR-0029), which is a post-hoc contract
    # verification gap rather than a raised exception during the write.
    FAILED_ARTIFACT_WRITE = "run.failed.artifact_write"
    TIMED_OUT_DEADLINE = "run.timed_out.deadline"
    ABORTED_USER = "run.aborted.user"
    CANCELLED_SIGINT = "run.cancelled.sigint"  # issue #1055
    CANCELLED_SIGTERM = "run.cancelled.sigterm"  # externally delivered SIGTERM (exit 143)
    CANCELLED_SYSTEM = "run.cancelled.system"
    CANCELLED_ORCHESTRATOR = "run.cancelled.orchestrator"
    # `li kill` — Phase 2 reason codes (issue #1094)
    CANCELLED_MANUAL_KILL = "run.cancelled.manual_kill"
    CANCELLED_FORCE_KILL = "run.cancelled.force_kill"
    CANCELLED_STALE_AUTO = "run.cancelled.stale_auto"
    PAUSED_OPERATOR = "run.paused.operator"
    # ADR-0071 D4: task-application worker lease outcomes.
    QUEUED_LEASE_EXPIRED = "run.queued.lease_expired"
    FAILED_LEASE_ATTEMPTS_EXHAUSTED = "run.failed.lease_attempts_exhausted"
    # The occurrence's transaction committed but the scheduler crashed
    # before confirming the external process launched; a startup recovery
    # scan tombstones the orphaned row with this code and re-fires a fresh
    # occurrence in its place.
    FAILED_NEVER_DISPATCHED = "run.failed.never_dispatched"
    # ADR-0071 D3: admit() seam claim-time terminal rejections, surfaced
    # observably on the row itself.
    SKIPPED_WAITER_CAP_EXCEEDED = "run.skipped.waiter_cap_exceeded"
    SKIPPED_DURATION_EXCEEDS_LEASE = "run.skipped.duration_exceeds_lease"


class SessionReasons:
    """Health-derived reason codes written by operator-initiated transitions (ADR-0057)."""

    HEALTH_STALE_NO_HEARTBEAT = "session.stale.no_heartbeat"
    HEALTH_ORPHANED_NO_PROCESS = "session.orphaned.no_process"
    HEALTH_ZOMBIE_STALE_LOCKS = "session.zombie.stale_locks"
    HEALTH_PHANTOM_PROCESS_DEAD = "session.phantom.process_dead"
    HEALTH_PHANTOM_MISSING_ARTIFACTS = "session.phantom.missing_artifacts"

    # A resumed branch puts its session back into execution. This is the only
    # sanctioned exit from a terminal status, and it carries an override so the
    # reopening is attributable rather than an ordinary write.
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
    # A show reached completion without ever landing a `_final_verdict.json`
    # (e.g. the last play merged and nothing else ran a final gate); derived
    # by the lifecycle reaper from every child play's on-disk status.
    COMPLETED_ALL_PLAYS_MERGED = "show.completed.all_plays_merged"


class ScheduleReasons:
    """ADR-0070 schedule-fire outcomes.

    Three names here begin with ``schedule.skipped.``, and those three are
    exactly that prefix -- but **the prefix is not the set of reasons a skipped
    ``schedule_run`` can carry, and reading it as one is the mistake this
    docstring exists to prevent.** Two other codes in this same class land on
    rows whose status is ``skipped``: ``DEFERRED_CAPACITY`` and
    ``BUDGET_EXHAUSTED``, neither of which carries the prefix. A third comes
    from a different class entirely -- the task-admission path stamps a
    ``schedule_run`` to ``skipped`` with the admission decision's own code,
    falling back to ``RunReasons.SKIPPED_WAITER_CAP_EXCEEDED``.

    So no enumeration here can be closed: that admission writer takes its code
    from a decision object rather than from a literal, and the only bound
    anywhere in the system is ``VALID_REASON_CODES``, which is the union across
    every reason class in this module. A consumer filtering skipped rows by the
    ``schedule.skipped.`` prefix will silently drop capacity deferrals, budget
    exhaustion and admission rejections -- and silently is the operative word,
    because the filter returns rows and looks like it worked.

    What follows describes the three prefixed codes and is not a claim about
    what else a skipped row may hold.

    ``SKIPPED_OVERLAP`` is stamped when a fire arrives while the previous run of
    the same schedule is still going.

    ``SKIPPED_MISSED_FIRE`` is stamped for a fire whose due instant passed while
    the scheduler was not running, on a schedule whose missed-fire policy is not
    to run it late. Two properties of it surprise readers, and both are
    properties of *when the check runs* rather than of the code:

    - Detection is **once per process start**, not continuous. The missed-fire
      sweep runs in the tick loop's preamble, before the loop begins, and never
      again for the life of the process. So a scheduler that stops and restarts
      records its missed fires, and a scheduler that stalls while still running
      records nothing at all -- the second case leaves no row here and no
      failing health check either.
    - Consequently the timestamp on such a row is bounded by time-to-restart,
      not by the tick interval. It is closer to a restart timestamp than to a
      detection latency, and reading it as "how long the miss took to notice"
      overstates what it measures. A row's lateness and a fired row's lateness
      are set by different clocks and are not comparable.

    ``SKIPPED_PRECONDITION`` is the odd one and the one worth reading twice: no
    code path evaluates a precondition and stamps it. It is the DEFAULT reason
    attached to a ``schedule_run`` that moves to ``skipped`` without an explicit
    code, so in practice it means "skipped, and the writer gave no reason". A
    consumer that treats it as evidence a precondition was checked and failed is
    reading a fallback as a finding.

    One property of ``DEFERRED_CAPACITY`` belongs with these, since it is the
    same kind of trap: those rows are **sampled, not one-per-event**. The
    scheduler counts every deferral and records only the first, then one every
    N deferrals after that, so sustained saturation does not flood
    ``schedule_runs``.

    N is a constant in the scheduler module and this docstring deliberately does
    not repeat its value. Prose here cannot be kept honest about a number
    defined somewhere else: it would read as current long after the number
    changed, and nothing would fail. What holds for any N above one is the part
    worth relying on -- counting these rows undercounts deferrals, and a row's
    timestamp is the sampled deferral's rather than the first one's in that
    stretch.
    """

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


# ── Validator ────────────────────────────────────────────────────────


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


# ── Validation helpers ───────────────────────────────────────────────


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


# ── Table mapping ────────────────────────────────────────────────────
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
