# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ALTER TABLE column definitions consumed by StateDB._reconcile_columns for schema migrations."""

from __future__ import annotations

MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "sessions": [
        ("updated_at", "REAL"),
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
        # #1235: live flow phase for the `li monitor` PHASE column.
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
    ],
    "branches": [
        ("system_msg_id", "TEXT"),
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
        # Metric threshold alerts: {metric, op, value, window_minutes}
        # config blob + the cooldown timestamp of the last breach fire.
        ("threshold_config", "JSON"),
        ("last_alert_at", "REAL"),
        # Observer self-health: last healthy (2xx/304) github_poll() read,
        # and the consecutive-401 counter (resets only on a healthy read).
        ("last_healthy_poll_at", "REAL"),
        ("poller_consecutive_401", "INTEGER NOT NULL DEFAULT 0"),
        # ADR-0070 delta 1: persisted per-schedule execution root, captured
        # once at creation. NULL on rows created before this migration.
        ("action_cwd", "TEXT"),
        # Allow-listed executable + templated argv for the
        # 'command' action kind.
        ("action_command", "TEXT"),
        ("action_command_args", "JSON"),
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
    # Phase C Move 2: engine run persistence.
    # New table created via schema.sql; these columns allow ALTER TABLE on
    # existing databases that pre-date this table (rare, but handled uniformly).
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
    # ADR-0059: durable dispatch outbox.
    # New table created via schema.sql; these columns allow ALTER TABLE on
    # existing databases that pre-date this table (rare, but handled uniformly).
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
