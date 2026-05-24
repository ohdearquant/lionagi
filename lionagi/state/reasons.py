# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Status reason code namespace (ADR-0028).

Every status transition on a Studio entity carries a structured reason:
a code (machine-readable, stable), a summary (human-readable, mutable),
and optional evidence references (typed pointers to related entities).

This module is the single source of truth for the controlled vocabulary.
"""

from __future__ import annotations

from typing import Final

# ── Entity taxonomy ──────────────────────────────────────────────────
# Canonical singular entity types consumed by ADR-0028 (status reasons),
# ADR-0030 (attention queue), and ADR-0031 (entity headers). Validated
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

    COMPLETED_OK = "run.completed.ok"
    FAILED_EXIT_NONZERO = "run.failed.exit_nonzero"
    FAILED_EXCEPTION = "run.failed.exception"
    FAILED_MISSING_ARTIFACT = "run.failed.missing_artifact"  # ADR-0029
    TIMED_OUT_DEADLINE = "run.timed_out.deadline"
    ABORTED_USER = "run.aborted.user"
    CANCELLED_SYSTEM = "run.cancelled.system"
    CANCELLED_ORCHESTRATOR = "run.cancelled.orchestrator"


class SessionReasons:
    """Health-derived reasons that may be written by admin transitions.

    Per ADR-0024, the doctor classifier surfaces phantom sessions but
    does NOT auto-transition. The operator initiates the transition;
    these codes record *why* the operator chose to transition.
    """

    HEALTH_STALE_NO_HEARTBEAT = "session.stale.no_heartbeat"
    HEALTH_ORPHANED_NO_PROCESS = "session.orphaned.no_process"
    HEALTH_ZOMBIE_STALE_LOCKS = "session.zombie.stale_locks"
    HEALTH_PHANTOM_PROCESS_DEAD = "session.phantom.process_dead"
    HEALTH_PHANTOM_MISSING_ARTIFACTS = "session.phantom.missing_artifacts"


class PlayReasons:
    """Show-play lifecycle reasons (ADR-0011 play vocabulary)."""

    PENDING_WAITING_DEPS = "play.pending.waiting_on_deps"
    PENDING_READY = "play.pending.ready"
    BLOCKED_INVALID_DEPS = "play.blocked.invalid_deps"
    BLOCKED_DEP_FAILED = "play.blocked.dep_failed"
    GATE_FAILED_VERDICT = "play.gate_failed.verdict"
    ESCALATED_GATE_TWICE = "play.escalated.gate_twice"
    MERGED_OK = "play.merged.ok"


class ShowReasons:
    """Show-level orchestration reasons."""

    BLOCKED_NO_READY_PLAYS = "show.blocked.no_ready_plays"
    COMPLETED_FINAL_GATE = "show.completed.final_gate"
    ABORTED_OPERATOR = "show.aborted.operator"


class ScheduleReasons:
    """ADR-0027 schedule-fire outcomes."""

    FIRED_DUE = "schedule.fired.due"
    SKIPPED_OVERLAP = "schedule.skipped.overlap"
    SKIPPED_MISSED_FIRE = "schedule.skipped.missed_fire"


# ── Validator ────────────────────────────────────────────────────────


def _collect(*classes: type) -> frozenset[str]:
    """Pull str-valued public class attributes off each reason class.

    Filters out dunders, descriptors, and any non-string values so the
    resulting frozenset is exactly the controlled vocabulary, not
    whatever Python happens to put in ``__dict__``.
    """
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
) | {LEGACY_IMPORTED}


# ── Validation helpers ───────────────────────────────────────────────


def validate_reason_code(code: str) -> str:
    """Return ``code`` if it is a registered reason code, else raise.

    Raises:
        ValueError: code not in :data:`VALID_REASON_CODES`.
    """
    if code not in VALID_REASON_CODES:
        raise ValueError(
            f"invalid reason_code: {code!r}. Must be one of "
            f"{sorted(VALID_REASON_CODES)} (defined in "
            "lionagi/state/reasons.py)"
        )
    return code


def validate_entity_type(entity_type: str) -> str:
    """Return canonical entity_type (resolves aliases) or raise.

    Accepts:
      - canonical names (`session`, `show`, ...)
      - frontend route aliases (`run` → `session`)
      - plural table names (`sessions` → `session`)

    Raises:
        ValueError: entity_type not recognized.
    """
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
    """Resolve a canonical entity_type to its SQLite table name.

    Pre-validates via :func:`validate_entity_type` so aliases also work.
    """
    canonical = validate_entity_type(entity_type)
    return ENTITY_TYPE_TO_TABLE[canonical]
