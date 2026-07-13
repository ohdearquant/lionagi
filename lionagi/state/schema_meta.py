# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy MetaData for all 28 StateDB tables — single source of truth for schema DDL."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)

metadata = MetaData()

# ── schema_meta ───────────────────────────────────────────────────────────────

schema_meta = Table(
    "schema_meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

# ── message_types ─────────────────────────────────────────────────────────────

message_types = Table(
    "message_types",
    metadata,
    Column("type_id", Integer, primary_key=True),
    Column("lion_class", Text, nullable=False, unique=True),
)

# ── messages ──────────────────────────────────────────────────────────────────

messages = Table(
    "messages",
    metadata,
    Column("id", Text, primary_key=True),
    Column("created_at", Float, nullable=False),
    Column("node_metadata", JSON),
    Column("content", JSON, nullable=False),
    Column("embedding", LargeBinary),
    Column("sender", Text),
    Column("recipient", Text),
    Column("channel", Text),
    Column("role", Text, nullable=False),
    Column(
        "lion_class",
        Integer,
        ForeignKey("message_types.type_id"),
        nullable=False,
    ),
)

Index("idx_messages_role", messages.c.role)
Index("idx_messages_lion_class", messages.c.lion_class)
Index(
    "idx_messages_sender",
    messages.c.sender,
    sqlite_where=text("sender IS NOT NULL"),
    postgresql_where=text("sender IS NOT NULL"),
)
Index(
    "idx_messages_recipient",
    messages.c.recipient,
    sqlite_where=text("recipient IS NOT NULL"),
    postgresql_where=text("recipient IS NOT NULL"),
)
Index("idx_messages_created", messages.c.created_at)

# ── progressions ─────────────────────────────────────────────────────────────

progressions = Table(
    "progressions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("created_at", Float, nullable=False),
    Column("collection", Text, nullable=False, server_default="[]"),
)

# ── projects ──────────────────────────────────────────────────────────────────

projects = Table(
    "projects",
    metadata,
    Column("name", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("path", Text),
    Column("github", Text),
    Column("description", Text),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("last_seen_at", Float),
)

Index("idx_projects_source", projects.c.source)
Index("idx_projects_updated", projects.c.updated_at)

# ── invocations ───────────────────────────────────────────────────────────────
# Defined before sessions because sessions FK -> invocations.

invocations = Table(
    "invocations",
    metadata,
    Column("id", Text, primary_key=True),
    Column("skill", Text, nullable=False),
    Column("plugin", Text),
    Column("prompt", Text),
    Column("started_at", Float, nullable=False),
    Column("ended_at", Float),
    Column(
        "status",
        Text,
        CheckConstraint(
            "status IN ('running','completed','completed_empty','failed',"
            "'timed_out','aborted','cancelled')",
            name="ck_invocations_status",
        ),
        nullable=False,
        server_default="running",
    ),
    Column("session_count", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("node_metadata", JSON),
    # ADR-0057 denormalized reason columns.
    Column("status_reason_code", Text),
    Column("status_reason_summary", Text),
    Column("status_evidence_refs", JSON),
)

Index("idx_invocations_skill", invocations.c.skill)
Index("idx_invocations_status", invocations.c.status)
Index("idx_invocations_updated", invocations.c.updated_at)

# ── sessions ──────────────────────────────────────────────────────────────────

sessions = Table(
    "sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("created_at", Float, nullable=False),
    Column("node_metadata", JSON),
    Column("name", Text),
    Column("user", Text),
    Column("progression_id", Text, ForeignKey("progressions.id"), nullable=False),
    Column("first_msg_id", Text, ForeignKey("messages.id")),
    Column("last_msg_id", Text, ForeignKey("messages.id")),
    Column("updated_at", Float, nullable=False),
    # Provenance.
    Column("playbook_name", Text),
    Column("agent_name", Text),
    Column(
        "invocation_kind",
        Text,
        CheckConstraint(
            "invocation_kind IS NULL OR invocation_kind IN ('agent','play','flow','fanout','show-play')",
            name="ck_sessions_invocation_kind",
        ),
    ),
    Column("show_topic", Text),
    Column("show_play_name", Text),
    Column("artifacts_path", Text),
    Column(
        "source_kind",
        Text,
        CheckConstraint(
            "source_kind IS NULL OR source_kind IN ('live','imported_fs')",
            name="ck_sessions_source_kind",
        ),
        server_default="live",
    ),
    # Lifecycle — no CHECK (ADR-0057: Python is source of truth).
    Column("status", Text),
    Column("started_at", Float),
    Column("ended_at", Float),
    # Activity.
    Column("last_message_at", Float),
    # Phase.
    Column("current_phase", Text),
    # Skill invocation FK.
    Column("invocation_id", Text, ForeignKey("invocations.id")),
    # Provenance disclosure.
    Column("model", Text),
    Column("provider", Text),
    Column("effort", Text),
    Column("agent_hash", Text),
    # Project detection.
    Column("project", Text),
    Column("project_source", Text),
    # ADR-0057 denormalized reason columns.
    Column("status_reason_code", Text),
    Column("status_reason_summary", Text),
    Column("status_evidence_refs", JSON),
    # ADR-0064 artifact contract.
    Column("artifact_contract_json", JSON),
    Column("artifact_verification_json", JSON),
    # Run usage.
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("total_cost_usd", Float),
    Column("num_turns", Integer),
    Column("duration_ms", Float),
)

Index("idx_sessions_updated", sessions.c.updated_at)
Index("idx_sessions_status_updated", sessions.c.status, sessions.c.updated_at)
Index(
    "idx_sessions_status_last_msg",
    sessions.c.status,
    sessions.c.last_message_at,
    sqlite_where=text("status = 'running'"),
    postgresql_where=text("status = 'running'"),
)
Index(
    "idx_sessions_invocation",
    sessions.c.invocation_id,
    sqlite_where=text("invocation_id IS NOT NULL"),
    postgresql_where=text("invocation_id IS NOT NULL"),
)
Index(
    "idx_sessions_project",
    sessions.c.project,
    sqlite_where=text("project IS NOT NULL"),
    postgresql_where=text("project IS NOT NULL"),
)

# ── branches ──────────────────────────────────────────────────────────────────

branches = Table(
    "branches",
    metadata,
    Column("id", Text, primary_key=True),
    Column("created_at", Float, nullable=False),
    Column("node_metadata", JSON),
    Column("user", Text),
    Column("name", Text),
    Column(
        "session_id",
        Text,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("progression_id", Text, ForeignKey("progressions.id"), nullable=False),
    Column("system_msg_id", Text, ForeignKey("messages.id")),
    # Provenance disclosure.
    Column("model", Text),
    Column("provider", Text),
    Column("agent_name", Text),
    Column("status", Text),
    Column("started_at", Float),
    Column("ended_at", Float),
)

Index("idx_branches_session", branches.c.session_id)

# ── definitions ───────────────────────────────────────────────────────────────

definitions = Table(
    "definitions",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "kind",
        Text,
        CheckConstraint("kind IN ('agent','playbook')", name="ck_definitions_kind"),
        nullable=False,
    ),
    Column("name", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("message", Text),
)

Index("idx_def_kind_name", definitions.c.kind, definitions.c.name, definitions.c.version)
UniqueConstraint(
    definitions.c.kind, definitions.c.name, definitions.c.version, name="idx_def_unique_version"
)

# ── shows ─────────────────────────────────────────────────────────────────────

shows = Table(
    "shows",
    metadata,
    Column("id", Text, primary_key=True),
    Column("topic", Text, nullable=False, unique=True),
    Column("goal", Text),
    Column("repo", Text),
    Column("base_branch", Text),
    Column("integration_branch", Text),
    Column(
        "status",
        Text,
        CheckConstraint(
            "status IN ('active','completed','aborted','imported')",
            name="ck_shows_status",
        ),
        nullable=False,
        server_default="active",
    ),
    Column("show_dir", Text, nullable=False),
    Column("status_source", Text, nullable=False, server_default="unknown"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    # ADR-0057 denormalized reason columns.
    Column("status_reason_code", Text),
    Column("status_reason_summary", Text),
    Column("status_evidence_refs", JSON),
)

Index("idx_shows_topic", shows.c.topic)
Index("idx_shows_status", shows.c.status)
Index("idx_shows_updated", shows.c.updated_at)

# ── plays ─────────────────────────────────────────────────────────────────────

plays = Table(
    "plays",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "show_id",
        Text,
        ForeignKey("shows.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", Text, nullable=False),
    Column("playbook", Text),
    Column("effort", Text),
    Column(
        "status",
        Text,
        CheckConstraint(
            "status IN ('pending','prepared','running','running_complete',"
            "'gated','gate_failed','redoing','merged','escalated','blocked','aborted_after_finish')",
            name="ck_plays_status",
        ),
        nullable=False,
        server_default="pending",
    ),
    Column("attempt", Integer, nullable=False, server_default="1"),
    Column("session_id", Text, ForeignKey("sessions.id")),
    Column("started_at", Float),
    Column("ended_at", Float),
    Column("exit_code", Integer),
    Column("worktree", Text),
    Column("branch", Text),
    Column("merge_sha", Text),
    Column("merged_at", Float),
    Column("gate_passed", Integer),
    Column("gate_feedback", Text),
    Column("depends_on", JSON, server_default="[]"),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    # ADR-0057 denormalized reason columns.
    Column("status_reason_code", Text),
    Column("status_reason_summary", Text),
    Column("status_evidence_refs", JSON),
)

Index("idx_plays_show", plays.c.show_id)
Index("idx_plays_status", plays.c.status)
Index("idx_plays_session", plays.c.session_id)
UniqueConstraint(plays.c.show_id, plays.c.name, name="idx_plays_show_name")

# ── teams ─────────────────────────────────────────────────────────────────────

teams = Table(
    "teams",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("member_count", Integer, nullable=False, server_default="0"),
    Column("members", JSON, nullable=False, server_default="[]"),
    Column("node_metadata", JSON),
    Column(
        "status",
        Text,
        CheckConstraint("status IN ('active','archived')", name="ck_teams_status"),
        nullable=False,
        server_default="active",
    ),
    # ADR-0057 denormalized reason columns.
    Column("status_reason_code", Text),
    Column("status_reason_summary", Text),
    Column("status_evidence_refs", JSON),
)

Index("idx_teams_name", teams.c.name)
Index("idx_teams_updated", teams.c.updated_at)
Index("idx_teams_status", teams.c.status)

# ── team_messages ─────────────────────────────────────────────────────────────

team_messages = Table(
    "team_messages",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "team_id",
        Text,
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", Float, nullable=False),
    Column("sender", Text, nullable=False),
    Column("recipient", Text, nullable=False, server_default="all"),
    Column("content", Text, nullable=False),
    Column("summary", Text),
    Column("read_by", JSON, nullable=False, server_default="[]"),
    Column("session_id", Text, ForeignKey("sessions.id")),
)

Index("idx_team_msgs_team", team_messages.c.team_id)
Index("idx_team_msgs_created", team_messages.c.created_at)
Index(
    "idx_team_msgs_session",
    team_messages.c.session_id,
    sqlite_where=text("session_id IS NOT NULL"),
    postgresql_where=text("session_id IS NOT NULL"),
)

# ── schedules ─────────────────────────────────────────────────────────────────

schedules = Table(
    "schedules",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("description", Text),
    Column(
        "enabled",
        Integer,
        CheckConstraint("enabled IN (0,1)", name="ck_schedules_enabled"),
        nullable=False,
        server_default="1",
    ),
    Column(
        "trigger_type",
        Text,
        CheckConstraint(
            "trigger_type IN ('cron','interval','github_poll')",
            name="ck_schedules_trigger_type",
        ),
        nullable=False,
    ),
    Column("cron_expr", Text),
    Column("interval_sec", Integer),
    Column("github_repo", Text),
    Column("github_filter", JSON),
    Column("github_cursor", Text),
    Column("poll_interval_sec", Integer),
    Column(
        "action_kind",
        Text,
        CheckConstraint(
            "action_kind IN ('agent','flow','fanout','play','flow_yaml','command')",
            name="ck_schedules_action_kind",
        ),
        nullable=False,
    ),
    Column("action_model", Text),
    Column("action_prompt", Text),
    Column("action_agent", Text),
    Column("action_playbook", Text),
    Column("action_flow_yaml", Text),
    Column("action_project", Text),
    # ADR-0070 delta 1: persisted per-schedule execution root, captured once
    # at creation (see schema.sql for the fuller comment).
    Column("action_cwd", Text),
    Column("action_extra_args", JSON, server_default="[]"),
    # Allow-listed executable + templated argv for the
    # 'command' action kind (see schema.sql for the fuller comment).
    Column("action_command", Text),
    Column("action_command_args", JSON, server_default="[]"),
    Column("on_success", JSON),
    Column("on_fail", JSON),
    Column("last_fired_at", Float),
    Column("next_fire_at", Float),
    Column(
        "missed_fire_policy",
        Text,
        CheckConstraint(
            "missed_fire_policy IN ('skip','run_once')",
            name="ck_schedules_missed_fire_policy",
        ),
        nullable=False,
        server_default="skip",
    ),
    Column(
        "overlap_policy",
        Text,
        CheckConstraint(
            "overlap_policy IN ('skip','allow')",
            name="ck_schedules_overlap_policy",
        ),
        nullable=False,
        server_default="skip",
    ),
    # One-shot / bounded-run semantics: NULL means unlimited (see
    # schema.sql for the fuller comment on how the engine counts runs).
    Column("max_runs", Integer),
    # Cumulative spend budget: NULL means unlimited (see schema.sql).
    Column("budget_usd", Float),
    Column("budget_tokens", Integer),
    Column("project", Text),
    # Metric threshold alerts: {metric, op, value, window_minutes} config
    # blob + the timestamp of the last breach fire (doubles as the cooldown
    # anchor -- see schema.sql for the fuller comment).
    Column("threshold_config", JSON),
    Column("last_alert_at", Float),
    # Observer self-health (github_poll poller): last_healthy_poll_at is
    # stamped on any 2xx/304 github_poll() read (including a healthy-empty
    # one); poller_consecutive_401 counts consecutive 401s and resets only
    # on a healthy read (a transient error/non-200 between 401s does not
    # reset the run). Read by StateDB.metric_value's
    # github_poll_healthy_age_minutes / github_poll_consecutive_401
    # threshold metrics -- see SchedulerEngine._tick_github for the stamp.
    Column("last_healthy_poll_at", Float),
    Column("poller_consecutive_401", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

Index(
    "idx_schedules_enabled",
    schedules.c.enabled,
    schedules.c.next_fire_at,
    sqlite_where=text("enabled = 1"),
    postgresql_where=text("enabled = 1"),
)
Index("idx_schedules_name", schedules.c.name)
Index(
    "idx_schedules_project",
    schedules.c.project,
    sqlite_where=text("project IS NOT NULL"),
    postgresql_where=text("project IS NOT NULL"),
)

# ── schedule_runs ─────────────────────────────────────────────────────────────
# ADR-0071 D2: generalized into the durable task-application entity. schedule_id
# is nullable (an ad-hoc task application has schedule_id IS NULL); the status
# CHECK carries the full ADR-0072 lifecycle; queued_at/leased_by/
# lease_expires_at/concurrency_key are ADR-0071's queue columns.

schedule_runs = Table(
    "schedule_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "schedule_id",
        Text,
        ForeignKey("schedules.id", ondelete="CASCADE"),
    ),
    Column("invocation_id", Text, ForeignKey("invocations.id")),
    Column("trigger_context", JSON, nullable=False),
    Column("action_kind", Text, nullable=False),
    Column("action_args", JSON, nullable=False),
    Column(
        "status",
        Text,
        CheckConstraint(
            "status IN ('queued','waiting_dependency','running','retry_wait',"
            "'completed','failed','timed_out','skipped','cancelled')",
            name="ck_schedule_runs_status",
        ),
        nullable=False,
        server_default="running",
    ),
    Column("exit_code", Integer),
    Column("chain_parent_id", Text, ForeignKey("schedule_runs.id")),
    Column("chain_depth", Integer, nullable=False, server_default="0"),
    Column("fired_at", Float, nullable=False),
    Column("ended_at", Float),
    Column("error_detail", Text),
    Column("created_at", Float, nullable=False),
    # ADR-0057.
    Column("updated_at", Float),
    Column("status_reason_code", Text),
    Column("status_reason_summary", Text),
    Column("status_evidence_refs", JSON),
    # ADR-0071 D2 / ADR-0071: durable queue columns.
    Column("queued_at", Float),
    Column("leased_by", Text),
    Column("lease_expires_at", Float),
    Column("concurrency_key", Text),
    # ADR-0071 D4: bounds the lease-expiry recovery loop (worker.py's reaper).
    Column("lease_attempts", Integer, nullable=False, server_default="0"),
    # ADR-0071 D2: task-application provenance (seam into ADR-0073).
    Column("required_capabilities", JSON),
    Column("execution_target", Text),
    Column("library_ref", Text),
    Column("library_content_hash", Text),
    # Delivery-contract marker: stamped once the scheduler engine confirms
    # the external process for this occurrence was actually launched. See
    # the schema.sql CREATE TABLE comment for the full rationale.
    Column("dispatched_at", Float),
    # Nullable sidecar metadata blob for resuming a run, shaped like an
    # Element.to_dict(mode="db") payload. NULL means no resume state has
    # been captured for this run.
    Column("resume_packet", JSON),
)

Index("idx_sched_runs_schedule", schedule_runs.c.schedule_id, schedule_runs.c.fired_at)
Index(
    "idx_sched_runs_status",
    schedule_runs.c.status,
    sqlite_where=text("status = 'running'"),
    postgresql_where=text("status = 'running'"),
)
Index(
    "idx_sched_runs_invocation",
    schedule_runs.c.invocation_id,
    sqlite_where=text("invocation_id IS NOT NULL"),
    postgresql_where=text("invocation_id IS NOT NULL"),
)
Index(
    "idx_schedule_runs_queue",
    schedule_runs.c.status,
    schedule_runs.c.queued_at,
    sqlite_where=text("status IN ('queued', 'retry_wait')"),
    postgresql_where=text("status IN ('queued', 'retry_wait')"),
)
Index(
    "idx_schedule_runs_concurrency",
    schedule_runs.c.concurrency_key,
    schedule_runs.c.status,
    sqlite_where=text("status IN ('queued', 'running', 'retry_wait')"),
    postgresql_where=text("status IN ('queued', 'running', 'retry_wait')"),
)

# ── workers ─────────────────────────────────────────────────────────────────
# ADR-0071 D5: capability-matching worker registry -- the only genuinely new
# table this ADR pair adds.

workers = Table(
    "workers",
    metadata,
    Column("worker_id", Text, primary_key=True),
    Column("advertised_capabilities", JSON, nullable=False, server_default="[]"),
    Column("execution_targets", JSON, nullable=False, server_default="[]"),
    Column("last_heartbeat_at", Float, nullable=False),
    Column("leased_run_id", Text, ForeignKey("schedule_runs.id")),
)

Index("idx_workers_heartbeat", workers.c.last_heartbeat_at)

# ── admin_events ──────────────────────────────────────────────────────────────

admin_events = Table(
    "admin_events",
    metadata,
    Column("id", Text, primary_key=True),
    Column("created_at", Float, nullable=False),
    Column("action", Text, nullable=False),
    Column("target_id", Text),
    Column("details", JSON, nullable=False),
    Column("actor", Text, nullable=False, server_default="admin"),
)

Index("idx_admin_events_created", admin_events.c.created_at)
Index("idx_admin_events_action", admin_events.c.action)
Index(
    "idx_admin_events_target",
    admin_events.c.target_id,
    sqlite_where=text("target_id IS NOT NULL"),
    postgresql_where=text("target_id IS NOT NULL"),
)

# ── artifacts ─────────────────────────────────────────────────────────────────

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("invocation_id", Text, ForeignKey("invocations.id", ondelete="CASCADE")),
    Column("session_id", Text, ForeignKey("sessions.id")),
    Column("created_at", Float, nullable=False),
    # updated_at has a SQLite server_default in schema.sql but the MIGRATION_COLUMNS
    # comment notes it must be nullable in ALTER TABLE because expressions are not
    # valid column defaults there; the insert path always sets it explicitly.
    Column("updated_at", Float, nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("content", JSON, nullable=False),
    Column("file_path", Text),
)

# Natural-key partial unique indexes (four shapes — see schema.sql comment).
Index(
    "idx_artifacts_natural_key_inv_only",
    artifacts.c.invocation_id,
    artifacts.c.kind,
    artifacts.c.name,
    unique=True,
    sqlite_where=text("invocation_id IS NOT NULL AND session_id IS NULL"),
    postgresql_where=text("invocation_id IS NOT NULL AND session_id IS NULL"),
)
Index(
    "idx_artifacts_natural_key_ses_only",
    artifacts.c.session_id,
    artifacts.c.kind,
    artifacts.c.name,
    unique=True,
    sqlite_where=text("session_id IS NOT NULL AND invocation_id IS NULL"),
    postgresql_where=text("session_id IS NOT NULL AND invocation_id IS NULL"),
)
Index(
    "idx_artifacts_natural_key_both",
    artifacts.c.invocation_id,
    artifacts.c.session_id,
    artifacts.c.kind,
    artifacts.c.name,
    unique=True,
    sqlite_where=text("invocation_id IS NOT NULL AND session_id IS NOT NULL"),
    postgresql_where=text("invocation_id IS NOT NULL AND session_id IS NOT NULL"),
)
Index(
    "idx_artifacts_natural_key_unattached",
    artifacts.c.kind,
    artifacts.c.name,
    unique=True,
    sqlite_where=text("invocation_id IS NULL AND session_id IS NULL"),
    postgresql_where=text("invocation_id IS NULL AND session_id IS NULL"),
)
Index(
    "idx_artifacts_invocation",
    artifacts.c.invocation_id,
    sqlite_where=text("invocation_id IS NOT NULL"),
    postgresql_where=text("invocation_id IS NOT NULL"),
)
Index(
    "idx_artifacts_session",
    artifacts.c.session_id,
    sqlite_where=text("session_id IS NOT NULL"),
    postgresql_where=text("session_id IS NOT NULL"),
)
Index("idx_artifacts_kind", artifacts.c.kind)
Index("idx_artifacts_created", artifacts.c.created_at)
Index(
    "idx_artifacts_invocation_time",
    artifacts.c.invocation_id,
    artifacts.c.created_at,
    sqlite_where=text("invocation_id IS NOT NULL"),
    postgresql_where=text("invocation_id IS NOT NULL"),
)
Index(
    "idx_artifacts_session_time",
    artifacts.c.session_id,
    artifacts.c.created_at,
    sqlite_where=text("session_id IS NOT NULL"),
    postgresql_where=text("session_id IS NOT NULL"),
)

# ── status_transitions ────────────────────────────────────────────────────────

status_transitions = Table(
    "status_transitions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("previous_status", Text),
    Column("status", Text, nullable=False),
    Column("reason_code", Text, nullable=False),
    Column("reason_summary", Text),
    Column("evidence_refs", JSON),
    Column("source", Text, nullable=False),
    Column("actor", Text),
    Column("created_at", Float, nullable=False),
    Column("metadata", JSON),
)

Index(
    "idx_status_transitions_entity",
    status_transitions.c.entity_type,
    status_transitions.c.entity_id,
    status_transitions.c.created_at,
)
Index(
    "idx_status_transitions_reason",
    status_transitions.c.reason_code,
    status_transitions.c.created_at,
)
Index("idx_status_transitions_created", status_transitions.c.created_at)

# ── terminal_deliveries ─────────────────────────────────────────────────────────
# Durable reconciliation-consumer acknowledgment ledger for post-commit
# terminal-event callbacks (never written by the in-process push path itself —
# only a registered reconciliation consumer inserts a row, once it has
# durably processed a terminal event). The composite primary key makes
# concurrent/repeated acks of the same event by the same consumer a
# single-row no-op.

terminal_deliveries = Table(
    "terminal_deliveries",
    metadata,
    Column("transition_id", Text, ForeignKey("status_transitions.id"), primary_key=True),
    Column("consumer", Text, primary_key=True),
    Column("acked_at", Float, nullable=False),
)

Index(
    "idx_terminal_deliveries_consumer",
    terminal_deliveries.c.consumer,
    terminal_deliveries.c.acked_at,
)

# ── session_signals ───────────────────────────────────────────────────────────

session_signals = Table(
    "session_signals",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "session_id",
        Text,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("op_id", Text, nullable=False, server_default=""),
    Column("ts", Float, nullable=False),
    Column("payload", JSON, nullable=False, server_default="{}"),
)

UniqueConstraint(
    session_signals.c.session_id, session_signals.c.seq, name="idx_session_signals_seq"
)
Index("idx_session_signals_session_ts", session_signals.c.session_id, session_signals.c.ts)

# ── engine_runs ───────────────────────────────────────────────────────────────

engine_runs = Table(
    "engine_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("spec_json", JSON, nullable=False),
    Column(
        "status",
        Text,
        CheckConstraint(
            "status IN ('running','completed','failed','cancelled')",
            name="ck_engine_runs_status",
        ),
        nullable=False,
        server_default="running",
    ),
    Column("started_at", Float, nullable=False),
    Column("ended_at", Float),
    Column("session_id", Text, ForeignKey("sessions.id", ondelete="SET NULL")),
    Column("export_dir", Text),
    Column("error", Text),
)

Index("idx_engine_runs_kind", engine_runs.c.kind)
Index("idx_engine_runs_status", engine_runs.c.status)
Index("idx_engine_runs_started", engine_runs.c.started_at)
Index(
    "idx_engine_runs_session",
    engine_runs.c.session_id,
    sqlite_where=text("session_id IS NOT NULL"),
    postgresql_where=text("session_id IS NOT NULL"),
)

# ── engine_defs ───────────────────────────────────────────────────────────────

engine_defs = Table(
    "engine_defs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("kind", Text, nullable=False),
    Column("model", Text),
    Column("max_depth", Integer),
    Column("max_agents", Integer),
    Column("options", JSON),
    Column("description", Text),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

Index("idx_engine_defs_name", engine_defs.c.name)
Index("idx_engine_defs_kind", engine_defs.c.kind)
Index("idx_engine_defs_updated", engine_defs.c.updated_at)

# ── workflow_defs ─────────────────────────────────────────────────────────────
# Named workflow definitions authored in the Studio Designer. spec_json holds
# the versioned node/edge graph; validation lives in the studio service layer.

workflow_defs = Table(
    "workflow_defs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("description", Text),
    Column("spec_json", JSON),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

Index("idx_workflow_defs_name", workflow_defs.c.name)
Index("idx_workflow_defs_updated", workflow_defs.c.updated_at)

# ── session_controls (ADR-0069 D1–D3: live-control transport) ─────────────────
# One row per operator control verb queued against a live session. A poller task
# in `cli/orchestrate/flow.py`'s `_execute_dag` reads unapplied rows and applies
# them against the running executor. Apply/stamp ordering is verb-classed:
# pause/resume are
# idempotent (apply, then stamp — safe to re-apply on a poller crash); message
# is not (stamp 'applying', then apply, then finalize — a crash surfaces as an
# unapplied 'applying' row rather than risking a double injection). 'stop' is
# schema-reserved and rejected by the current poller as unsupported; no CLI
# verb emits it yet.

session_controls = Table(
    "session_controls",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "session_id",
        Text,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "verb",
        Text,
        CheckConstraint(
            "verb IN ('pause','resume','message','stop')",
            name="ck_session_controls_verb",
        ),
        nullable=False,
    ),
    Column("payload", JSON),
    Column("created_at", Float, nullable=False),
    # NULL until the poller consumes the row.
    Column("applied_at", Float),
    # 'applying' (message verb, mid-apply) | 'applied' | 'rejected:<reason>'.
    Column("result", Text),
)

Index(
    "idx_session_controls_pending",
    session_controls.c.session_id,
    session_controls.c.applied_at,
    sqlite_where=text("applied_at IS NULL"),
    postgresql_where=text("applied_at IS NULL"),
)

# ── dispatch_outbox (ADR-0059: durable dispatch outbox) ─────────────────────
# Producer-driven at-least-once outbound delivery. A row survives independent
# of any consumer's liveness; the scheduler tick re-attempts the configured
# notify template until it succeeds, backs off, or exhausts max_attempts.

dispatch_outbox = Table(
    "dispatch_outbox",
    metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("deliver_to", Text, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("dedup_key", Text),
    Column(
        "status",
        Text,
        CheckConstraint(
            "status IN ('pending','delivering','delivered','acked','dead_letter','expired')",
            name="ck_dispatch_outbox_status",
        ),
        nullable=False,
        server_default="pending",
    ),
    Column("attempt", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="8"),
    Column("next_attempt_at", Float, nullable=False),
    Column("ack_required", Integer, nullable=False, server_default="0"),
    Column("ack_token", Text),
    Column("session_id", Text, ForeignKey("sessions.id")),
    Column("schedule_run_id", Text, ForeignKey("schedule_runs.id")),
    Column("last_error", Text),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float),
    Column("updated_at", Float),
)

Index(
    "idx_dispatch_outbox_dedup",
    dispatch_outbox.c.dedup_key,
    unique=True,
    sqlite_where=text("dedup_key IS NOT NULL"),
    postgresql_where=text("dedup_key IS NOT NULL"),
)
Index(
    "idx_dispatch_outbox_due",
    dispatch_outbox.c.status,
    dispatch_outbox.c.next_attempt_at,
    sqlite_where=text("status IN ('pending', 'delivering')"),
    postgresql_where=text("status IN ('pending', 'delivering')"),
)

# ── run_tags ──────────────────────────────────────────────────────────────────
# Free-form review labels attached to a run (session). Kept in the canonical
# metadata (not only schema.sql) so StateDB.open()/create_all builds it on every
# backend, keeping the SQLite, Postgres, and schema-parity paths consistent.

run_tags = Table(
    "run_tags",
    metadata,
    Column(
        "session_id",
        Text,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tag", Text, primary_key=True),
    Column("created_at", Float, nullable=False),
)

# ── approvals (studio operator permission ledger) ──────────────────────────
# Server-side confirm-flow: a mutating action is proposed, a human grants or
# denies it, and the real endpoint consumes the granted approval exactly once.

approvals = Table(
    "approvals",
    metadata,
    Column("id", Text, primary_key=True),
    Column("action_kind", Text, nullable=False),
    Column("params_hash", Text, nullable=False),
    Column("session_id", Text, ForeignKey("sessions.id")),
    Column(
        "status",
        Text,
        CheckConstraint(
            "status IN ('pending','granted','consumed','expired','denied')",
            name="ck_approvals_status",
        ),
        nullable=False,
        server_default="pending",
    ),
    Column("proposed_at", Float, nullable=False),
    Column("granted_at", Float),
    Column("consumed_at", Float),
    Column("expires_at", Float, nullable=False),
)

Index(
    "idx_approvals_status",
    approvals.c.status,
    sqlite_where=text("status IN ('pending', 'granted')"),
    postgresql_where=text("status IN ('pending', 'granted')"),
)
Index(
    "idx_approvals_session",
    approvals.c.session_id,
    sqlite_where=text("session_id IS NOT NULL"),
    postgresql_where=text("session_id IS NOT NULL"),
)

# ── approval_evidence (hash-chained audit trail on the approval ledger) ────
# Append-only: every approval lifecycle event writes one row in the same
# transaction as the approvals status change. See schema.sql for the full
# chain-hash design note.

approval_evidence = Table(
    "approval_evidence",
    metadata,
    Column("id", Text, primary_key=True),
    Column("sequence", Integer, nullable=False),
    Column(
        "event_type",
        Text,
        CheckConstraint(
            "event_type IN ('proposed','granted','denied','consumed','expired')",
            name="ck_approval_evidence_event_type",
        ),
        nullable=False,
    ),
    Column("approval_id", Text, ForeignKey("approvals.id"), nullable=False),
    Column("action_kind", Text, nullable=False),
    Column("status_from", Text),
    Column("status_to", Text, nullable=False),
    Column("params_hash", Text, nullable=False),
    Column("justification_class", Text),
    Column("justification_reason", Text),
    Column("created_at", Float, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("previous_hash", Text, nullable=False),
    Column("chain_hash", Text, nullable=False),
    Column("hmac_sig", Text),
)

Index(
    "idx_approval_evidence_sequence",
    approval_evidence.c.sequence,
    unique=True,
)
Index(
    "idx_approval_evidence_approval",
    approval_evidence.c.approval_id,
)

Index("idx_run_tags_tag", run_tags.c.tag)
