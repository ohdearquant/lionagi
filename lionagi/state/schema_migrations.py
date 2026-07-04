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
        # ADR-0019: activity marker for staleness detection.
        ("last_message_at", "REAL"),
        # #1235: live flow phase for the `li monitor` PHASE column.
        ("current_phase", "TEXT"),
        # ADR-0020: optional FK to invocations table.
        ("invocation_id", "TEXT"),
        # ADR-0022: provenance disclosure columns.
        ("model", "TEXT"),
        ("provider", "TEXT"),
        ("effort", "TEXT"),
        ("agent_hash", "TEXT"),
        # ADR-0026: project detection for session organization.
        ("project", "TEXT"),
        ("project_source", "TEXT"),
        # ADR-0028: denormalized current status reason (hot read path).
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
        # ADR-0029: resolved artifact contract and teardown result.
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
        # ADR-0022: per-branch provenance.
        ("model", "TEXT"),
        ("provider", "TEXT"),
        ("agent_name", "TEXT"),
        ("status", "TEXT"),
        ("started_at", "REAL"),
        ("ended_at", "REAL"),
    ],
    "shows": [
        ("status_source", "TEXT NOT NULL DEFAULT 'unknown'"),
        # ADR-0028.
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
    ],
    "plays": [
        # ADR-0028.
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
    ],
    "invocations": [
        # ADR-0028.
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
    ],
    "teams": [
        # ADR-0028.
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
    ],
    "schedule_runs": [
        # ADR-0028: schedule_runs originally had no updated_at.
        # update_status() writes it, so it must exist.
        ("updated_at", "REAL"),
        ("status_reason_code", "TEXT"),
        ("status_reason_summary", "TEXT"),
        ("status_evidence_refs", "JSON"),
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
}
