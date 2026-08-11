# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Additive schema definitions consumed by StateDB's runtime migrations."""

from __future__ import annotations

MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "sessions": [
        ("updated_at", "REAL"),
        ("cc_session_id", "TEXT"),
        # The CLI run this session belongs to; NULL on rows created before
        # this migration and on sessions that were not started by a run.
        ("run_id", "TEXT"),
        ("playbook_name", "TEXT"),
        ("agent_name", "TEXT"),
        ("invocation_kind", "TEXT"),
        ("show_topic", "TEXT"),
        ("show_play_name", "TEXT"),
        ("artifacts_path", "TEXT"),
        ("source_kind", "TEXT"),
        ("status", "TEXT"),
        ("started_at", "REAL"),
        ("ended_at", "REAL"),
        # Activity marker for staleness detection (read by ADR-0057 D6).
        ("last_message_at", "REAL"),
        # Live flow phase for the `li monitor` PHASE column.
        ("current_phase", "TEXT"),
        # Optional FK to invocations table.
        ("invocation_id", "TEXT"),
        # Provenance disclosure columns.
        ("model", "TEXT"),
        ("provider", "TEXT"),
        ("effort", "TEXT"),
        ("agent_hash", "TEXT"),
        # ADR-0063: project detection for session organization.
        ("project", "TEXT"),
        ("project_source", "TEXT"),
        # ADR-0057: denormalized current status reason (hot read path).
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
        # ADR-0064: resolved artifact contract and teardown result.
        ("artifact_contract_json", "JSON"),
        ("artifact_verification_json", "JSON"),
        # Run usage populated at RunEnd.
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("total_cost_usd", "REAL"),
        ("num_turns", "INTEGER"),
        ("duration_ms", "REAL"),
        # Reconciled here so the indexes below have columns to be built on;
        # a store old enough to predate them must still open.
        ("first_msg_id", "TEXT"),
        ("last_msg_id", "TEXT"),
        ("progression_id", "TEXT"),
    ],
    "branches": [
        ("system_msg_id", "TEXT"),
        # Reconciled here so the index below it has a column to be built on;
        # a store old enough to predate the column must still open.
        ("progression_id", "TEXT"),
        # Per-branch provenance.
        ("model", "TEXT"),
        ("provider", "TEXT"),
        ("agent_name", "TEXT"),
        ("status", "TEXT"),
        ("started_at", "REAL"),
        ("ended_at", "REAL"),
    ],
    "shows": [
        ("status_source", "TEXT NOT NULL DEFAULT 'unknown'"),
        # ADR-0057.
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
    ],
    "plays": [
        # ADR-0057.
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
    ],
    "invocations": [
        # ADR-0057.
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
    ],
    "teams": [
        # ADR-0057.
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
    ],
    "session_controls": [
        # When a consumer claimed the row. NULL on rows created before this
        # migration and on rows no consumer has claimed.
        ("claimed_at", "REAL"),
    ],
    "artifacts": [
        # Nullable in ALTER TABLE because expressions aren't valid
        # column defaults there; insert_artifact() always sets this.
        ("updated_at", "REAL"),
    ],
    "schedules": [
        # YAML flow spec column added by sched-yaml feature.
        ("action_flow_yaml", "TEXT"),
        # One-shot / bounded-run semantics: NULL means unlimited.
        ("max_runs", "INTEGER"),
        # Cumulative spend budget: NULL means unlimited.
        ("budget_usd", "REAL"),
        ("budget_tokens", "INTEGER"),
        # Rolling-window fire cap: {max_fires, window_sec}; NULL is unlimited.
        ("rate_limit", "JSON"),
        # Metric threshold alerts: {metric, op, value, window_minutes}
        # config blob + the cooldown timestamp of the last breach fire.
        ("threshold_config", "JSON"),
        ("last_alert_at", "REAL"),
        # Observer self-health: last healthy (2xx/304) github_poll() read,
        # and the consecutive-401 counter (resets only on a healthy read).
        ("last_healthy_poll_at", "REAL"),
        ("poller_consecutive_401", "INTEGER NOT NULL DEFAULT 0"),
        # Bounded retry for a fire that refuses before dispatching: which
        # event the streak applies to, and how many times it has refused.
        ("predispatch_refusal_event", "TEXT"),
        ("predispatch_refusal_count", "INTEGER NOT NULL DEFAULT 0"),
        # ADR-0070 delta 1: persisted per-schedule execution root, captured
        # once at creation. NULL on rows created before this migration.
        ("action_cwd", "TEXT"),
        # Allow-listed executable + templated argv for the
        # 'command' action kind.
        ("action_command", "TEXT"),
        ("action_command_args", "JSON"),
        # Declarative ScheduleSet layer: versioned identity, resolved
        # snapshot + digest, and set ownership. NULL on legacy/quick-create rows.
        ("spec_version", "TEXT"),
        ("managed_by", "TEXT"),
        ("owner_key", "TEXT"),
        ("authored_spec", "JSON"),
        ("resolved_target", "JSON"),
        ("resolved_digest", "TEXT"),
        ("resolved_timezone", "TEXT"),
        # The zone a cron schedule was last actually resolved in, plus how
        # that zone was arrived at. NULL on rows not resolved since this
        # migration; the scheduler stamps them at startup and at each fire.
        ("effective_timezone", "TEXT"),
        ("effective_timezone_source", "TEXT"),
        # Terminal notification: filtered callback on the spawned invocation.
        ("notify_on", "JSON"),
        ("notify_command", "TEXT"),
    ],
    "schedule_runs": [
        # ADR-0057: schedule_runs originally had no updated_at.
        # update_status() writes it, so it must exist.
        ("updated_at", "REAL"),
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
        # ADR-0071 D2 / ADR-0071: durable queue columns.
        ("queued_at", "REAL"),
        ("leased_by", "TEXT"),
        ("lease_expires_at", "REAL"),
        ("concurrency_key", "TEXT"),
        # ADR-0071 D2: task-application provenance columns.
        ("required_capabilities", "JSON"),
        ("execution_target", "TEXT"),
        ("library_ref", "TEXT"),
        ("library_content_hash", "TEXT"),
        # ADR-0071 D4: bounds the lease-expiry recovery loop (worker.py's reaper).
        ("lease_attempts", "INTEGER NOT NULL DEFAULT 0"),
        # Delivery-contract marker: stamped once the scheduler engine
        # confirms the external process was actually launched. NULL on a
        # row whose occurrence-insert transaction committed but launch was
        # never confirmed -- see the CREATE TABLE comment in schema.sql.
        ("dispatched_at", "REAL"),
        # Nullable sidecar metadata blob for resuming a run, shaped like an
        # Element.to_dict(mode="db") payload. NULL means no resume state
        # has been captured for this run.
        ("resume_packet", "JSON"),
    ],
    # engine run persistence; created via schema.sql, these columns allow
    # ALTER TABLE on existing databases that pre-date this table
    "engine_runs": [
        ("id", "TEXT NOT NULL"),
        ("kind", "TEXT NOT NULL"),
        ("spec_json", "JSON NOT NULL"),
        ("status", "TEXT NOT NULL DEFAULT 'running'"),
        ("started_at", "REAL NOT NULL"),
        ("ended_at", "REAL"),
        ("session_id", "TEXT"),
        ("export_dir", "TEXT"),
        ("error", "TEXT"),
    ],
    # Fencing revision added after attention_dispositions/history already
    # shipped (schema_version was never bumped for their original add, so a
    # store carrying them predates this column too). DEFAULT 1 only
    # satisfies ALTER TABLE's NOT NULL requirement -- StateDB's one-time
    # attention-dispositions backfill raises pre-existing rows to their true
    # history-derived count.
    "attention_dispositions": [
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
    ],
    # Global append-order counter, added alongside attention_dispositions
    # .revision above. DEFAULT 0 is a placeholder for the ALTER TABLE NOT
    # NULL requirement only -- 0 never survives the backfill, which assigns
    # every pre-existing row a real (created_at, id)-ordered value.
    "attention_disposition_history": [
        ("sequence", "INTEGER NOT NULL DEFAULT 0"),
    ],
    # ADR-0059: durable dispatch outbox; see engine_runs above re ALTER TABLE
    "dispatch_outbox": [
        ("id", "TEXT NOT NULL"),
        ("kind", "TEXT NOT NULL"),
        ("deliver_to", "TEXT NOT NULL"),
        ("payload", "JSON NOT NULL"),
        ("dedup_key", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("attempt", "INTEGER NOT NULL DEFAULT 0"),
        ("max_attempts", "INTEGER NOT NULL DEFAULT 8"),
        ("next_attempt_at", "REAL NOT NULL"),
        ("ack_required", "INTEGER NOT NULL DEFAULT 0"),
        ("ack_token", "TEXT"),
        ("session_id", "TEXT"),
        ("schedule_run_id", "TEXT"),
        ("last_error", "TEXT"),
        ("created_at", "REAL NOT NULL"),
        ("expires_at", "REAL"),
        ("updated_at", "REAL"),
    ],
}

# metadata.create_all() skips indexes when their table already exists, so
# idempotent DDL for indexes introduced after deployment lives here instead.
# These index the child keys of messages(id) for prune's delete-scan cost,
# not for any query -- see schema.sql for the measurement.
_MESSAGE_POINTER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_first_msg_id ON sessions(first_msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_last_msg_id ON sessions(last_msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_branches_system_msg_id ON branches(system_msg_id)",
    # Same shape one level up: progressions(id) is the parent of these two.
    "CREATE INDEX IF NOT EXISTS idx_sessions_progression_id ON sessions(progression_id)",
    "CREATE INDEX IF NOT EXISTS idx_branches_progression_id ON branches(progression_id)",
)

# attention_disposition_history predates its `sequence` column on a store
# already carrying the table (see attention_disposition_history above), so
# metadata.create_all() never issues this CREATE INDEX for it either -- same
# reasoning as _MESSAGE_POINTER_INDEXES. Must run after StateDB's
# attention-dispositions backfill assigns unique sequence values, or the
# uniqueness check fails against the ALTER TABLE ... DEFAULT 0 placeholder.
_ATTENTION_HISTORY_SEQUENCE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_attention_disposition_history_sequence "
    "ON attention_disposition_history(sequence)",
)

MIGRATION_INDEXES: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "CREATE INDEX IF NOT EXISTS idx_sessions_cc_session "
        "ON sessions(cc_session_id) WHERE cc_session_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_schedules_owner_key "
        "ON schedules(owner_key) WHERE owner_key IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_sessions_run_id "
        "ON sessions(run_id) WHERE run_id IS NOT NULL",
        *_MESSAGE_POINTER_INDEXES,
        "CREATE INDEX IF NOT EXISTS idx_branches_session_created "
        "ON branches(session_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_updated_id ON sessions(updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_status_updated_id "
        "ON sessions(status, updated_at DESC, id DESC)",
        *_ATTENTION_HISTORY_SEQUENCE_INDEX,
    ),
    "postgresql": (
        "CREATE INDEX IF NOT EXISTS idx_sessions_cc_session "
        "ON sessions(cc_session_id) WHERE cc_session_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_schedules_owner_key "
        "ON schedules(owner_key) WHERE owner_key IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_sessions_run_id "
        "ON sessions(run_id) WHERE run_id IS NOT NULL",
        *_MESSAGE_POINTER_INDEXES,
        "CREATE INDEX IF NOT EXISTS idx_branches_session_created "
        "ON branches(session_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_updated_id ON sessions(updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_status_updated_id "
        "ON sessions(status, updated_at DESC, id DESC)",
        *_ATTENTION_HISTORY_SEQUENCE_INDEX,
    ),
}
