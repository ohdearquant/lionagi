-- lionagi state schema v1 — PostgreSQL dialect
-- Converted from schema.sql (SQLite dialect).
--
-- Differences from the SQLite schema:
--   * SERIAL / BIGSERIAL instead of INTEGER PRIMARY KEY (auto-increment)
--   * JSONB instead of TEXT / JSON for JSON columns (indexable, type-safe)
--   * TIMESTAMPTZ instead of REAL / TEXT for timestamps (stored as epoch
--     seconds in SQLite; PostgreSQL uses native timestamp type)
--   * now() instead of datetime('now') / strftime('%s','now')
--   * ON CONFLICT DO NOTHING instead of INSERT OR IGNORE
--   * Partial indexes use the same WHERE predicates (both dialects support them)
--   * No PRAGMA directives (PostgreSQL has no equivalent at DDL time)
--   * No CHECK constraints on status columns that list all values — the
--     Python layer (lionagi/state/db.py) is the source of truth; constraints
--     are kept only where they are cheap and stable.
--
-- Table and column names are IDENTICAL to the SQLite schema so that shared
-- application code can target either backend without column mapping.
--
-- NOTE: This schema is SINGLE-TENANT. Do NOT add tenant_id columns or
-- row-level security policies. That is a separate commercial concern.

-- ── Schema version ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_meta (
  key     TEXT PRIMARY KEY,
  value   TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('version', '1')
  ON CONFLICT (key) DO NOTHING;
INSERT INTO schema_meta (key, value) VALUES ('created_at', extract(epoch from now())::TEXT)
  ON CONFLICT (key) DO NOTHING;

-- ── Message types ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS message_types (
  type_id    SERIAL PRIMARY KEY,
  lion_class TEXT   NOT NULL UNIQUE
);

INSERT INTO message_types (type_id, lion_class) VALUES
  (0, '__unknown__'),
  (1, 'lionagi.protocols.messages.system.System'),
  (2, 'lionagi.protocols.messages.instruction.Instruction'),
  (3, 'lionagi.protocols.messages.assistant_response.AssistantResponse'),
  (4, 'lionagi.protocols.messages.action_request.ActionRequest'),
  (5, 'lionagi.protocols.messages.action_response.ActionResponse')
  ON CONFLICT (lion_class) DO NOTHING;

-- ── Messages ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS messages (
  id            TEXT        PRIMARY KEY,
  created_at    DOUBLE PRECISION NOT NULL,
  node_metadata JSONB,
  content       JSONB       NOT NULL,
  embedding     BYTEA,
  sender        TEXT,
  recipient     TEXT,
  channel       TEXT,
  role          TEXT        NOT NULL,
  lion_class    INTEGER     NOT NULL REFERENCES message_types(type_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_role
  ON messages(role);
CREATE INDEX IF NOT EXISTS idx_messages_lion_class
  ON messages(lion_class);
CREATE INDEX IF NOT EXISTS idx_messages_sender
  ON messages(sender) WHERE sender IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_recipient
  ON messages(recipient) WHERE recipient IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_created
  ON messages(created_at);

-- ── Progressions ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS progressions (
  id            TEXT        PRIMARY KEY,
  created_at    DOUBLE PRECISION NOT NULL,
  collection    TEXT        NOT NULL DEFAULT '[]'
);

-- ── Projects ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS projects (
  name         TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  path         TEXT,
  github       TEXT,
  description  TEXT,
  created_at   DOUBLE PRECISION NOT NULL,
  updated_at   DOUBLE PRECISION NOT NULL,
  last_seen_at DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_projects_source  ON projects(source);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);

-- ── Invocations ───────────────────────────────────────────────────────────────
-- Defined before sessions because sessions.invocation_id references it.

CREATE TABLE IF NOT EXISTS invocations (
  id              TEXT        PRIMARY KEY,
  skill           TEXT        NOT NULL,
  plugin          TEXT,
  prompt          TEXT,
  started_at      DOUBLE PRECISION NOT NULL,
  ended_at        DOUBLE PRECISION,
  status          TEXT        NOT NULL DEFAULT 'running',
  session_count   INTEGER     NOT NULL DEFAULT 0,
  created_at      DOUBLE PRECISION NOT NULL,
  updated_at      DOUBLE PRECISION NOT NULL,
  node_metadata   JSONB,
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSONB
);

CREATE INDEX IF NOT EXISTS idx_invocations_skill   ON invocations(skill);
CREATE INDEX IF NOT EXISTS idx_invocations_status  ON invocations(status);
CREATE INDEX IF NOT EXISTS idx_invocations_updated ON invocations(updated_at DESC);

-- ── Sessions ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
  id              TEXT        PRIMARY KEY,
  created_at      DOUBLE PRECISION NOT NULL,
  node_metadata   JSONB,
  name            TEXT,
  "user"          TEXT,
  progression_id  TEXT        NOT NULL REFERENCES progressions(id),
  first_msg_id    TEXT        REFERENCES messages(id),
  last_msg_id     TEXT        REFERENCES messages(id),
  updated_at      DOUBLE PRECISION NOT NULL,
  playbook_name   TEXT,
  agent_name      TEXT,
  invocation_kind TEXT,
  show_topic      TEXT,
  show_play_name  TEXT,
  artifacts_path  TEXT,
  source_kind     TEXT        DEFAULT 'live',
  status          TEXT,
  started_at      DOUBLE PRECISION,
  ended_at        DOUBLE PRECISION,
  last_message_at DOUBLE PRECISION,
  invocation_id   TEXT        REFERENCES invocations(id),
  model           TEXT,
  provider        TEXT,
  effort          TEXT,
  agent_hash      TEXT,
  project         TEXT,
  project_source  TEXT,
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSONB,
  artifact_contract_json      JSONB,
  artifact_verification_json  JSONB
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated
  ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
  ON sessions(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status_last_msg
  ON sessions(status, last_message_at) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_sessions_invocation
  ON sessions(invocation_id) WHERE invocation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_project
  ON sessions(project) WHERE project IS NOT NULL;

-- ── Branches ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS branches (
  id              TEXT        PRIMARY KEY,
  created_at      DOUBLE PRECISION NOT NULL,
  node_metadata   JSONB,
  "user"          TEXT,
  name            TEXT,
  session_id      TEXT        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  progression_id  TEXT        NOT NULL REFERENCES progressions(id),
  system_msg_id   TEXT        REFERENCES messages(id),
  model           TEXT,
  provider        TEXT,
  agent_name      TEXT
);

CREATE INDEX IF NOT EXISTS idx_branches_session ON branches(session_id);

-- ── Definitions ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS definitions (
  id          TEXT    PRIMARY KEY,
  kind        TEXT    NOT NULL,
  name        TEXT    NOT NULL,
  path        TEXT    NOT NULL,
  content     TEXT    NOT NULL,
  version     INTEGER NOT NULL,
  created_at  DOUBLE PRECISION NOT NULL,
  message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_def_kind_name
  ON definitions(kind, name, version DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_def_unique_version
  ON definitions(kind, name, version);

-- ── Shows ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shows (
  id                  TEXT        PRIMARY KEY,
  topic               TEXT        NOT NULL UNIQUE,
  goal                TEXT,
  repo                TEXT,
  base_branch         TEXT,
  integration_branch  TEXT,
  status              TEXT        NOT NULL DEFAULT 'active',
  show_dir            TEXT        NOT NULL,
  status_source       TEXT        NOT NULL DEFAULT 'unknown',
  created_at          DOUBLE PRECISION NOT NULL,
  updated_at          DOUBLE PRECISION NOT NULL,
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSONB
);

CREATE INDEX IF NOT EXISTS idx_shows_topic   ON shows(topic);
CREATE INDEX IF NOT EXISTS idx_shows_status  ON shows(status);
CREATE INDEX IF NOT EXISTS idx_shows_updated ON shows(updated_at DESC);

-- ── Plays ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS plays (
  id              TEXT        PRIMARY KEY,
  show_id         TEXT        NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
  name            TEXT        NOT NULL,
  playbook        TEXT,
  effort          TEXT,
  status          TEXT        NOT NULL DEFAULT 'pending',
  attempt         INTEGER     NOT NULL DEFAULT 1,
  session_id      TEXT        REFERENCES sessions(id),
  started_at      DOUBLE PRECISION,
  ended_at        DOUBLE PRECISION,
  exit_code       INTEGER,
  worktree        TEXT,
  branch          TEXT,
  merge_sha       TEXT,
  merged_at       DOUBLE PRECISION,
  gate_passed     INTEGER,
  gate_feedback   TEXT,
  depends_on      JSONB       DEFAULT '[]',
  sort_order      INTEGER     NOT NULL DEFAULT 0,
  created_at      DOUBLE PRECISION NOT NULL,
  updated_at      DOUBLE PRECISION NOT NULL,
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSONB
);

CREATE INDEX IF NOT EXISTS idx_plays_show    ON plays(show_id);
CREATE INDEX IF NOT EXISTS idx_plays_status  ON plays(status);
CREATE INDEX IF NOT EXISTS idx_plays_session ON plays(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plays_show_name ON plays(show_id, name);

-- ── Teams ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS teams (
  id              TEXT        PRIMARY KEY,
  name            TEXT        NOT NULL,
  created_at      DOUBLE PRECISION NOT NULL,
  updated_at      DOUBLE PRECISION NOT NULL,
  member_count    INTEGER     NOT NULL DEFAULT 0,
  members         JSONB       NOT NULL DEFAULT '[]',
  node_metadata   JSONB,
  status          TEXT        NOT NULL DEFAULT 'active',
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSONB
);

CREATE INDEX IF NOT EXISTS idx_teams_name    ON teams(name);
CREATE INDEX IF NOT EXISTS idx_teams_updated ON teams(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_teams_status  ON teams(status);

CREATE TABLE IF NOT EXISTS team_messages (
  id              TEXT        PRIMARY KEY,
  team_id         TEXT        NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  created_at      DOUBLE PRECISION NOT NULL,
  sender          TEXT        NOT NULL,
  recipient       TEXT        NOT NULL DEFAULT 'all',
  content         TEXT        NOT NULL,
  summary         TEXT,
  read_by         JSONB       NOT NULL DEFAULT '[]',
  session_id      TEXT        REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_team_msgs_team    ON team_messages(team_id);
CREATE INDEX IF NOT EXISTS idx_team_msgs_created ON team_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_team_msgs_session ON team_messages(session_id)
  WHERE session_id IS NOT NULL;

-- ── Schedules ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedules (
  id                  TEXT        PRIMARY KEY,
  name                TEXT        NOT NULL UNIQUE,
  description         TEXT,
  enabled             SMALLINT    NOT NULL DEFAULT 1,
  trigger_type        TEXT        NOT NULL,
  cron_expr           TEXT,
  interval_sec        INTEGER,
  github_repo         TEXT,
  github_filter       JSONB,
  github_cursor       TEXT,
  poll_interval_sec   INTEGER,
  action_kind         TEXT        NOT NULL,
  action_model        TEXT,
  action_prompt       TEXT,
  action_agent        TEXT,
  action_playbook     TEXT,
  action_project      TEXT,
  action_extra_args   JSONB       DEFAULT '[]',
  on_success          JSONB,
  on_fail             JSONB,
  last_fired_at       DOUBLE PRECISION,
  next_fire_at        DOUBLE PRECISION,
  missed_fire_policy  TEXT        NOT NULL DEFAULT 'skip',
  overlap_policy      TEXT        NOT NULL DEFAULT 'skip',
  project             TEXT,
  created_at          DOUBLE PRECISION NOT NULL,
  updated_at          DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_enabled
  ON schedules(enabled, next_fire_at) WHERE enabled = 1;
CREATE INDEX IF NOT EXISTS idx_schedules_name
  ON schedules(name);
CREATE INDEX IF NOT EXISTS idx_schedules_project
  ON schedules(project) WHERE project IS NOT NULL;

-- ── Schedule Runs ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedule_runs (
  id                  TEXT        PRIMARY KEY,
  schedule_id         TEXT        NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  invocation_id       TEXT        REFERENCES invocations(id),
  trigger_context     JSONB       NOT NULL,
  action_kind         TEXT        NOT NULL,
  action_args         JSONB       NOT NULL,
  status              TEXT        NOT NULL DEFAULT 'running',
  exit_code           INTEGER,
  chain_parent_id     TEXT        REFERENCES schedule_runs(id),
  chain_depth         INTEGER     NOT NULL DEFAULT 0,
  fired_at            DOUBLE PRECISION NOT NULL,
  ended_at            DOUBLE PRECISION,
  error_detail        TEXT,
  created_at          DOUBLE PRECISION NOT NULL,
  updated_at          DOUBLE PRECISION,
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSONB
);

CREATE INDEX IF NOT EXISTS idx_sched_runs_schedule
  ON schedule_runs(schedule_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_sched_runs_status
  ON schedule_runs(status) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_sched_runs_invocation
  ON schedule_runs(invocation_id) WHERE invocation_id IS NOT NULL;

-- ── Admin event log ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS admin_events (
  id          TEXT        PRIMARY KEY,
  created_at  DOUBLE PRECISION NOT NULL,
  action      TEXT        NOT NULL,
  target_id   TEXT,
  details     JSONB       NOT NULL,
  actor       TEXT        NOT NULL DEFAULT 'admin'
);

CREATE INDEX IF NOT EXISTS idx_admin_events_created
  ON admin_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_events_action
  ON admin_events(action);
CREATE INDEX IF NOT EXISTS idx_admin_events_target
  ON admin_events(target_id) WHERE target_id IS NOT NULL;

-- ── Artifacts ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS artifacts (
  id              TEXT        PRIMARY KEY,
  invocation_id   TEXT        REFERENCES invocations(id) ON DELETE CASCADE,
  session_id      TEXT        REFERENCES sessions(id),
  created_at      DOUBLE PRECISION NOT NULL,
  updated_at      DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
  kind            TEXT        NOT NULL,
  name            TEXT        NOT NULL,
  content         JSONB       NOT NULL,
  file_path       TEXT
);

-- Partial unique indexes matching the SQLite schema (NULLs are distinct
-- in both SQLite and PostgreSQL unique indexes, so the same four-index
-- strategy is used here).
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_natural_key_inv_only
  ON artifacts(invocation_id, kind, name)
  WHERE invocation_id IS NOT NULL AND session_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_natural_key_ses_only
  ON artifacts(session_id, kind, name)
  WHERE session_id IS NOT NULL AND invocation_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_natural_key_both
  ON artifacts(invocation_id, session_id, kind, name)
  WHERE invocation_id IS NOT NULL AND session_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_natural_key_unattached
  ON artifacts(kind, name)
  WHERE invocation_id IS NULL AND session_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_artifacts_invocation
  ON artifacts(invocation_id) WHERE invocation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_session
  ON artifacts(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_kind    ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_invocation_time
  ON artifacts(invocation_id, created_at) WHERE invocation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_session_time
  ON artifacts(session_id, created_at) WHERE session_id IS NOT NULL;

-- ── Status transitions ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS status_transitions (
  id              TEXT        PRIMARY KEY,
  entity_type     TEXT        NOT NULL,
  entity_id       TEXT        NOT NULL,
  previous_status TEXT,
  status          TEXT        NOT NULL,
  reason_code     TEXT        NOT NULL,
  reason_summary  TEXT,
  evidence_refs   JSONB,
  source          TEXT        NOT NULL,
  actor           TEXT,
  created_at      DOUBLE PRECISION NOT NULL,
  metadata        JSONB
);

CREATE INDEX IF NOT EXISTS idx_status_transitions_entity
  ON status_transitions(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_status_transitions_reason
  ON status_transitions(reason_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_status_transitions_created
  ON status_transitions(created_at DESC);
