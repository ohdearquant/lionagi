-- lionagi state schema v1
-- Core tables: messages, progressions, sessions, branches,
-- shows, plays, definitions.
--
-- Field names match model_dump() output from the runtime objects.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA cache_size = -64000;

-- ── Schema version ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_meta (
  key     TEXT PRIMARY KEY,
  value   TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', '1');
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('created_at', strftime('%s', 'now'));

-- ── Message types (int enum for lion_class) ───────────────────────────────

CREATE TABLE IF NOT EXISTS message_types (
  type_id       INTEGER PRIMARY KEY,
  lion_class    TEXT    NOT NULL UNIQUE        -- full qualified class path
);

INSERT OR IGNORE INTO message_types (type_id, lion_class) VALUES
  (0, '__unknown__'),
  (1, 'lionagi.protocols.messages.system.System'),
  (2, 'lionagi.protocols.messages.instruction.Instruction'),
  (3, 'lionagi.protocols.messages.assistant_response.AssistantResponse'),
  (4, 'lionagi.protocols.messages.action_request.ActionRequest'),
  (5, 'lionagi.protocols.messages.action_response.ActionResponse');

-- ── Messages ──────────────────────────────────────────────────────────────
-- Atomic content.  Referenced by progressions, not owned by branch/session.

CREATE TABLE IF NOT EXISTS messages (
  id            TEXT    PRIMARY KEY,
  created_at    REAL    NOT NULL,
  node_metadata JSON,
  content       JSON    NOT NULL,
  embedding     BLOB,                         -- packed float32 vec or NULL; sqlite-vec indexes these
  sender        TEXT,
  recipient     TEXT,
  channel       TEXT,
  role          TEXT    NOT NULL,             -- 'user' | 'assistant' | 'system' | 'tool' | ...
  lion_class    INTEGER NOT NULL REFERENCES message_types(type_id)
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

-- ── Progressions ──────────────────────────────────────────────────────────
-- Progression[Message] — ordered sequence of message IDs.
-- collection is a JSON array of message id strings.

CREATE TABLE IF NOT EXISTS progressions (
  id            TEXT    PRIMARY KEY,
  created_at    REAL    NOT NULL,
  collection    TEXT    NOT NULL DEFAULT '[]' -- JSON array of message id strings
);

-- ── Projects (ADR-0026) ───────────────────────────────────────────────────
-- Auto-registered from session detection; also created explicitly via Studio.
-- Uses name as primary key (project names are unique + used as FK in sessions.project).

CREATE TABLE IF NOT EXISTS projects (
    name         TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT,
    github       TEXT,
    description  TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    last_seen_at REAL
);

CREATE INDEX IF NOT EXISTS idx_projects_source ON projects(source);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);

-- ── Run tags ──────────────────────────────────────────────────────────────
-- User-defined m2m labels over runs (a run == a session). Free-form strings.
CREATE TABLE IF NOT EXISTS run_tags (
    session_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_run_tags_tag ON run_tags(tag);

-- ── Sessions ──────────────────────────────────────────────────────────────
-- Scope boundary.  Owns a progression (the session-level message pool)
-- and zero or more branches.

CREATE TABLE IF NOT EXISTS sessions (
  id              TEXT    PRIMARY KEY,
  created_at      REAL    NOT NULL,
  node_metadata   JSON,
  name            TEXT,
  user            TEXT,
  progression_id  TEXT    NOT NULL REFERENCES progressions(id),
  first_msg_id    TEXT    REFERENCES messages(id),
  last_msg_id     TEXT    REFERENCES messages(id),
  updated_at      REAL    NOT NULL,
  -- ── Provenance (ADR-0012) ──────────────────────────────────────────────
  playbook_name   TEXT,
  agent_name     TEXT,
  invocation_kind TEXT CHECK(
                    invocation_kind IS NULL
                    OR invocation_kind IN
                      ('agent', 'play', 'flow', 'fanout', 'show-play')
                  ),
  show_topic      TEXT,
  show_play_name  TEXT,
  artifacts_path  TEXT,
  source_kind     TEXT    DEFAULT 'live' CHECK(
                    source_kind IS NULL
                    OR source_kind IN ('live', 'imported_fs')
                  ),
  -- ── Lifecycle (ADR-0025, supersedes ADR-0017) ─────────────────────
  -- No CHECK constraint: ADR-0025 makes Python the source of truth for
  -- session.status (VALID_SESSION_STATUSES in lionagi/state/db.py). The
  -- six-value vocabulary (running, completed, failed, timed_out, aborted,
  -- cancelled) can evolve without a SQLite table rebuild.
  status          TEXT,
  started_at      REAL,
  ended_at        REAL,
  -- ── Activity (ADR-0019) ────────────────────────────────────────────
  -- Bumped on every message INSERT so staleness_check() can answer
  -- "is this running session still active?" without scanning messages.
  last_message_at REAL,
  -- ── Live execution phase (#1235) ───────────────────────────────────
  -- Coarse flow lifecycle marker (planning → executing → synthesizing)
  -- surfaced as the PHASE column in `li monitor`. NULL for non-flow
  -- sessions, which fall back to agent_name/playbook_name in the reader.
  current_phase   TEXT,
  -- ── Skill invocation (ADR-0020) ────────────────────────────────────
  -- Optional FK to the higher-order skill orchestration (e.g. /show or
  -- /codex-pr-review) that spawned this session. NULL when the CLI
  -- ran standalone. Orthogonal to invocation_kind, which describes the
  -- CLI primitive (agent / play / flow / fanout / show-play).
  invocation_id   TEXT    REFERENCES invocations(id),
  -- ── Provenance disclosure (ADR-0022) ────────────────────────────────
  -- Resolved values — what the runtime actually used after defaults,
  -- overrides, and fallbacks. ``model`` is the canonical spec ("claude/
  -- claude-sonnet-4-6"), not the user input ("sonnet"). ``agent_hash``
  -- is a 16-char SHA-256 fingerprint of the agent profile content at
  -- invocation time for drift detection.
  model           TEXT,
  provider        TEXT,
  effort          TEXT,
  agent_hash      TEXT,
  -- ── Project detection (ADR-0026) ────────────────────────────────────
  project         TEXT,
  project_source  TEXT,
  -- ── Status reason (ADR-0028) ────────────────────────────────────────
  -- Denormalized "current reason" for the hot read path. The full
  -- history of transitions lives in ``status_transitions``; these
  -- three columns let a status pill render its tooltip without a JOIN.
  -- All writes go through StateDB.update_status() in the same SQLite
  -- transaction as the status update, so the columns and history table
  -- never drift.
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSON,
  -- ── Artifact contract (ADR-0029) ──────────────────────────────────────
  -- Resolved contract snapshot written at session creation and verifier
  -- result written at teardown. NULL contract means verification skipped.
  artifact_contract_json      JSON,
  artifact_verification_json  JSON,
  -- ── Run usage (populated at RunEnd) ───────────────────────────────────
  input_tokens    INTEGER,   -- prompt tokens (uncached)
  output_tokens   INTEGER,   -- completion tokens
  total_cost_usd  REAL,      -- 0 for subscription runs
  num_turns       INTEGER,   -- LLM turns in the run
  duration_ms     REAL       -- wall-clock run duration in milliseconds
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated
  ON sessions(updated_at DESC);
-- ADR-0028: failed/timed_out queries in the attention queue (ADR-0030)
-- need an index that covers terminal states, not just running. The
-- existing idx_sessions_status_last_msg is a partial index for
-- status='running' only.
CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
  ON sessions(status, updated_at DESC);
-- ADR-0019: lets the staleness query (running sessions sorted by oldest
-- activity) skip the full table scan.
CREATE INDEX IF NOT EXISTS idx_sessions_status_last_msg
  ON sessions(status, last_message_at) WHERE status = 'running';
-- ADR-0020: grouped runs view fetches all sessions for an invocation.
CREATE INDEX IF NOT EXISTS idx_sessions_invocation
  ON sessions(invocation_id) WHERE invocation_id IS NOT NULL;
-- ADR-0026: project-scoped session listing in Studio.
CREATE INDEX IF NOT EXISTS idx_sessions_project
  ON sessions(project) WHERE project IS NOT NULL;

-- ── Branches ──────────────────────────────────────────────────────────────
-- A progression with identity.  Branch config (provider, model,
-- system_prompt, tools, effort, etc.) lives in metadata.

CREATE TABLE IF NOT EXISTS branches (
  id              TEXT    PRIMARY KEY,
  created_at      REAL    NOT NULL,
  node_metadata   JSON,                       -- agent config: provider, model, tools, effort, ...
  user            TEXT,
  name            TEXT,
  session_id      TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  progression_id  TEXT    NOT NULL REFERENCES progressions(id),
  system_msg_id   TEXT    REFERENCES messages(id),  -- system prompt; just a reference to the message
  -- ── Provenance disclosure (ADR-0022) ────────────────────────────────
  -- Per-branch (per-agent) resolved model + provider + agent role name.
  -- For multi-agent flows the session-level model is the "default" and
  -- per-branch model is the actual model that produced messages on this
  -- branch. agent_name here is the *role* within the flow (e.g., "r1"
  -- or "critic"), not the agent_profile name on sessions.
  model           TEXT,
  provider        TEXT,
  agent_name      TEXT,
  status          TEXT,
  started_at      REAL,
  ended_at        REAL
);

CREATE INDEX IF NOT EXISTS idx_branches_session
  ON branches(session_id);

-- ── Definitions (versioned agent + playbook files) ───────────────────────────
-- Disk files remain source of truth; this table tracks edit history.
-- Current version = MAX(version) per (kind, name).

CREATE TABLE IF NOT EXISTS definitions (
  id          TEXT    PRIMARY KEY,
  kind        TEXT    NOT NULL
              CHECK(kind IN ('agent', 'playbook')),  -- ADR-0016 editable set
  name        TEXT    NOT NULL,           -- e.g. 'analyst', 'review-flow'
  path        TEXT    NOT NULL,           -- disk path relative to .lionagi/
  content     TEXT    NOT NULL,           -- full file content at this version
  version     INTEGER NOT NULL,           -- monotonic per (kind, name)
  created_at  REAL    NOT NULL,
  message     TEXT                        -- optional edit note
);

CREATE INDEX IF NOT EXISTS idx_def_kind_name
  ON definitions(kind, name, version DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_def_unique_version
  ON definitions(kind, name, version);

-- ── Shows (multi-play DAGs) ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shows (
  id                  TEXT    PRIMARY KEY,
  topic               TEXT    NOT NULL UNIQUE,
  goal                TEXT,
  repo                TEXT,
  base_branch         TEXT,
  integration_branch  TEXT,
  status              TEXT    NOT NULL DEFAULT 'active' CHECK(
                        status IN ('active', 'completed', 'aborted', 'imported')
                      ),
  show_dir            TEXT    NOT NULL,
  status_source       TEXT    NOT NULL DEFAULT 'unknown',
  created_at          REAL    NOT NULL,
  updated_at          REAL    NOT NULL,
  -- ADR-0028: see sessions table for the denormalization rationale.
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSON
);

CREATE INDEX IF NOT EXISTS idx_shows_topic ON shows(topic);
CREATE INDEX IF NOT EXISTS idx_shows_status ON shows(status);
CREATE INDEX IF NOT EXISTS idx_shows_updated ON shows(updated_at DESC);

-- ── Plays (within a show) ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS plays (
  id              TEXT    PRIMARY KEY,
  show_id         TEXT    NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
  name            TEXT    NOT NULL,
  playbook        TEXT,
  effort          TEXT,
  status          TEXT    NOT NULL DEFAULT 'pending' CHECK(
                    status IN (
                      'pending', 'prepared', 'running', 'running_complete',
                      'gated', 'gate_failed', 'redoing', 'merged',
                      'escalated', 'blocked', 'aborted_after_finish'
                    )
                  ),
  attempt         INTEGER NOT NULL DEFAULT 1,
  session_id      TEXT    REFERENCES sessions(id),
  started_at      REAL,
  ended_at        REAL,
  exit_code       INTEGER,
  worktree        TEXT,
  branch          TEXT,
  merge_sha       TEXT,
  merged_at       REAL,
  gate_passed     INTEGER,
  gate_feedback   TEXT,
  depends_on      JSON    DEFAULT '[]',
  sort_order      INTEGER NOT NULL DEFAULT 0,
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  -- ADR-0028: see sessions table for the denormalization rationale.
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSON
);

CREATE INDEX IF NOT EXISTS idx_plays_show ON plays(show_id);
CREATE INDEX IF NOT EXISTS idx_plays_status ON plays(status);
CREATE INDEX IF NOT EXISTS idx_plays_session ON plays(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plays_show_name ON plays(show_id, name);

-- ── Teams (ADR-0019) ─────────────────────────────────────────────────────
-- Mirrors the JSON files at ~/.lionagi/teams/{id}.json (still primary
-- write path; populated via dual-write or `li state import-teams`).
-- Storing teams in the DB unlocks queries, cross-session linkage, and
-- replaces the file-only model that doesn't compose with async DB code.

CREATE TABLE IF NOT EXISTS teams (
  id              TEXT    PRIMARY KEY,
  name            TEXT    NOT NULL,
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  member_count    INTEGER NOT NULL DEFAULT 0,
  members         JSON    NOT NULL DEFAULT '[]',
  node_metadata   JSON,
  status          TEXT    NOT NULL DEFAULT 'active' CHECK(
                    status IN ('active', 'archived')
                  ),
  -- ADR-0028: see sessions table for the denormalization rationale.
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSON
);

CREATE INDEX IF NOT EXISTS idx_teams_name ON teams(name);
CREATE INDEX IF NOT EXISTS idx_teams_updated ON teams(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_teams_status ON teams(status);

CREATE TABLE IF NOT EXISTS team_messages (
  id              TEXT    PRIMARY KEY,
  team_id         TEXT    NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  created_at      REAL    NOT NULL,
  sender          TEXT    NOT NULL,
  recipient       TEXT    NOT NULL DEFAULT 'all',
  content         TEXT    NOT NULL,
  summary         TEXT,
  read_by         JSON    NOT NULL DEFAULT '[]',
  session_id      TEXT    REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_team_msgs_team ON team_messages(team_id);
CREATE INDEX IF NOT EXISTS idx_team_msgs_created ON team_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_team_msgs_session ON team_messages(session_id)
  WHERE session_id IS NOT NULL;

-- ── Invocations (ADR-0020) ───────────────────────────────────────────────
-- Skill-level orchestration records. One invocation row per /show,
-- /codex-pr-review, etc., aggregating the N sessions that the skill
-- spawned. invocation_id is FK'd from sessions; invocation_kind on
-- sessions remains the CLI primitive (agent/play/flow/...).

CREATE TABLE IF NOT EXISTS invocations (
  id              TEXT    PRIMARY KEY,
  skill           TEXT    NOT NULL,
  plugin          TEXT,
  prompt          TEXT,
  started_at      REAL    NOT NULL,
  ended_at        REAL,
  status          TEXT    NOT NULL DEFAULT 'running' CHECK(
                    status IN ('running', 'completed', 'completed_empty',
                               'failed', 'timed_out', 'aborted', 'cancelled')
                  ),
  session_count   INTEGER NOT NULL DEFAULT 0,
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  node_metadata   JSON,
  -- ADR-0028: see sessions table for the denormalization rationale.
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSON
);

CREATE INDEX IF NOT EXISTS idx_invocations_skill ON invocations(skill);
CREATE INDEX IF NOT EXISTS idx_invocations_status ON invocations(status);
CREATE INDEX IF NOT EXISTS idx_invocations_updated ON invocations(updated_at DESC);

-- ── Schedules (ADR-0027) ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schedules (
  id                  TEXT    PRIMARY KEY,
  name                TEXT    NOT NULL UNIQUE,
  description         TEXT,
  enabled             INTEGER NOT NULL DEFAULT 1
                      CHECK(enabled IN (0, 1)),
  trigger_type        TEXT    NOT NULL
                      CHECK(trigger_type IN ('cron', 'interval', 'github_poll')),
  cron_expr           TEXT,
  interval_sec        INTEGER,
  github_repo         TEXT,
  github_filter       JSON,
  github_cursor       TEXT,
  poll_interval_sec   INTEGER,
  action_kind         TEXT    NOT NULL
                      CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play', 'flow_yaml')),
  action_model        TEXT,
  action_prompt       TEXT,
  action_agent        TEXT,
  action_playbook     TEXT,
  action_flow_yaml    TEXT,
  action_project      TEXT,
  action_extra_args   JSON    DEFAULT '[]',
  on_success          JSON,
  on_fail             JSON,
  last_fired_at       REAL,
  next_fire_at        REAL,
  missed_fire_policy  TEXT    NOT NULL DEFAULT 'skip'
                      CHECK(missed_fire_policy IN ('skip', 'run_once')),
  overlap_policy      TEXT    NOT NULL DEFAULT 'skip'
                      CHECK(overlap_policy IN ('skip', 'allow')),
  -- One-shot / bounded-run semantics: NULL means unlimited. Once the number
  -- of fired top-level runs (chain children excluded) reaches max_runs, the
  -- engine auto-disables the schedule via the existing enabled flag.
  max_runs            INTEGER,
  project             TEXT,
  created_at          REAL    NOT NULL,
  updated_at          REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_enabled
  ON schedules(enabled, next_fire_at) WHERE enabled = 1;
CREATE INDEX IF NOT EXISTS idx_schedules_name
  ON schedules(name);
CREATE INDEX IF NOT EXISTS idx_schedules_project
  ON schedules(project) WHERE project IS NOT NULL;

-- ── Schedule Runs (ADR-0027) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schedule_runs (
  id                  TEXT    PRIMARY KEY,
  schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  invocation_id       TEXT    REFERENCES invocations(id),
  trigger_context     JSON    NOT NULL,
  action_kind         TEXT    NOT NULL,
  action_args         JSON    NOT NULL,
  status              TEXT    NOT NULL DEFAULT 'running'
                      CHECK(status IN ('running', 'completed', 'failed',
                                       'skipped', 'cancelled')),
  exit_code           INTEGER,
  chain_parent_id     TEXT    REFERENCES schedule_runs(id),
  chain_depth         INTEGER NOT NULL DEFAULT 0,
  fired_at            REAL    NOT NULL,
  ended_at            REAL,
  error_detail        TEXT,
  created_at          REAL    NOT NULL,
  -- ADR-0028: schedule_runs needs updated_at so StateDB.update_status()
  -- can write it consistently (the only entity table that originally
  -- lacked one).
  updated_at          REAL,
  -- ADR-0028: see sessions table for the denormalization rationale.
  status_reason_code     TEXT,
  status_reason_summary  TEXT,
  status_evidence_refs   JSON
);

CREATE INDEX IF NOT EXISTS idx_sched_runs_schedule
  ON schedule_runs(schedule_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_sched_runs_status
  ON schedule_runs(status) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_sched_runs_invocation
  ON schedule_runs(invocation_id) WHERE invocation_id IS NOT NULL;

-- ── Admin event log (ADR-0024) ───────────────────────────────────────────
-- Append-only audit log following NIST SP 800-92 pattern. Every admin
-- mutation (transition, prune, checkpoint, vacuum, classify) inserts
-- one row; no UPDATE / DELETE except the bounded cleanup job.

CREATE TABLE IF NOT EXISTS admin_events (
  id          TEXT    PRIMARY KEY,
  created_at  REAL    NOT NULL,
  action      TEXT    NOT NULL,    -- transition|prune|checkpoint|vacuum|classify
  target_id   TEXT,                -- session_id, or NULL for DB-wide actions
  details     JSON    NOT NULL,
  actor       TEXT    NOT NULL DEFAULT 'admin'  -- admin|doctor_auto|chain
);

CREATE INDEX IF NOT EXISTS idx_admin_events_created
  ON admin_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_events_action
  ON admin_events(action);
CREATE INDEX IF NOT EXISTS idx_admin_events_target
  ON admin_events(target_id) WHERE target_id IS NOT NULL;

-- ── Artifacts (ADR-0021) ─────────────────────────────────────────────────
-- Structured skill outputs (review verdicts, gate verdicts, CI results,
-- ...). The split is DB-for-structured, filesystem-for-blobs: `content`
-- holds the outcome's JSON payload; `file_path` optionally
-- points to a large blob (full log, generated artifact, worktree diff).
-- `kind` is the discriminator the frontend renderer dispatches on.

CREATE TABLE IF NOT EXISTS artifacts (
  id              TEXT    PRIMARY KEY,
  invocation_id   TEXT    REFERENCES invocations(id) ON DELETE CASCADE,
  session_id      TEXT    REFERENCES sessions(id),
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL DEFAULT (strftime('%s','now')),
  kind            TEXT    NOT NULL,
  name            TEXT    NOT NULL,
  content         JSON    NOT NULL,
  file_path       TEXT
);

-- Natural uniqueness keys for idempotent upserts. INSERT OR REPLACE must
-- NOT be used — it deletes then re-inserts, generating a new id and
-- breaking external references. Use ON CONFLICT DO UPDATE instead.
-- SQLite treats NULLs as distinct in UNIQUE indexes, so a single
-- 4-column index on (invocation_id, session_id, kind, name) fails
-- when either FK is NULL. Four partial indexes cover every reachable
-- artifact shape without the NULL-distinctness pitfall.
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
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_created
  ON artifacts(created_at DESC);
-- Composite indexes that match the ORDER BY shape of the two list
-- queries — avoids a temp B-tree for the sort step.
CREATE INDEX IF NOT EXISTS idx_artifacts_invocation_time
  ON artifacts(invocation_id, created_at) WHERE invocation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_session_time
  ON artifacts(session_id, created_at) WHERE session_id IS NOT NULL;

-- ── Status transitions (ADR-0028) ────────────────────────────────────
-- Append-only history of every status change across all entity types.
-- Hot reads use the denormalized status_reason_* columns on each
-- entity table; this table is the cold path for audit, "show me all
-- failures with reason X", and the run-detail status-history tab.
-- Writes are paired with the entity status UPDATE in a single SQLite
-- transaction via StateDB.update_status(), so the two views never
-- drift.

CREATE TABLE IF NOT EXISTS status_transitions (
  id              TEXT    PRIMARY KEY,
  entity_type     TEXT    NOT NULL,    -- canonical singular: 'session' | 'show' | ...
                                       -- (see lionagi/state/reasons.py VALID_ENTITY_TYPES)
  entity_id       TEXT    NOT NULL,
  previous_status TEXT,                -- NULL for the first transition
  status          TEXT    NOT NULL,
  reason_code     TEXT    NOT NULL,    -- see lionagi/state/reasons.py VALID_REASON_CODES
  reason_summary  TEXT,
  evidence_refs   JSON,                -- list[{kind, id|path|ref, label?}]
  source          TEXT    NOT NULL,    -- 'executor' | 'agent' | 'admin' | 'system'
  actor           TEXT,                -- session_id, user, 'doctor_auto', ...
  created_at      REAL    NOT NULL,
  metadata        JSON                 -- optional: timing, exit code, exc class
);

CREATE INDEX IF NOT EXISTS idx_status_transitions_entity
  ON status_transitions(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_status_transitions_reason
  ON status_transitions(reason_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_status_transitions_created
  ON status_transitions(created_at DESC);

-- ── Session signals (Phase C Move 1) ─────────────────────────────────────────
-- Append-only lifecycle signal log emitted by SessionObserver.emit().
-- seq is a monotonic per-session counter (assigned at INSERT via MAX+1).
-- payload holds the JSON-serialised signal fields (kind, op_id, name, …).
-- The SSE endpoint polls rows WHERE session_id = ? AND seq > ? ORDER BY seq.

CREATE TABLE IF NOT EXISTS session_signals (
  id          TEXT    PRIMARY KEY,         -- uuid4 hex
  session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,            -- per-session monotone, 1-based
  kind        TEXT    NOT NULL,            -- signal class name (NodeStarted, …)
  op_id       TEXT    NOT NULL DEFAULT '', -- op/node id when applicable
  ts          REAL    NOT NULL,            -- Unix epoch seconds (float)
  payload     JSON    NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_signals_seq
  ON session_signals(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_session_signals_session_ts
  ON session_signals(session_id, ts);

-- ── Engine runs (Phase C Move 2) ─────────────────────────────────────────────
-- One row per `li engine run` invocation.  Tracks the kind, spec, lifecycle
-- status, and optional link to the Session that ran inside the engine.
-- session_id is a nullable FK: populated after the engine creates its Session
-- so the row exists from the moment the CLI is invoked.

CREATE TABLE IF NOT EXISTS engine_runs (
  id          TEXT    PRIMARY KEY,         -- uuid4 hex
  kind        TEXT    NOT NULL,            -- 'research' | 'review' | 'coding' | 'hypothesis' | 'planning'
  spec_json   JSON    NOT NULL,            -- serialised CLI spec (prompt / artifact / findings …)
  status      TEXT    NOT NULL DEFAULT 'running'
              CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
  started_at  REAL    NOT NULL,            -- Unix epoch seconds
  ended_at    REAL,                        -- NULL while running
  session_id  TEXT    REFERENCES sessions(id) ON DELETE SET NULL,
  export_dir  TEXT,                        -- filesystem path when --save used
  error       TEXT                         -- last exception message on failure
);

CREATE INDEX IF NOT EXISTS idx_engine_runs_kind
  ON engine_runs(kind);
CREATE INDEX IF NOT EXISTS idx_engine_runs_status
  ON engine_runs(status);
CREATE INDEX IF NOT EXISTS idx_engine_runs_started
  ON engine_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_engine_runs_session
  ON engine_runs(session_id) WHERE session_id IS NOT NULL;

-- ── Engine definitions ────────────────────────────────────────────────────────
-- Named, persisted engine configurations created via Studio.  A definition
-- captures the engine kind + tunable parameters so operators can launch
-- a specific pipeline on demand without repeating its configuration.

CREATE TABLE IF NOT EXISTS engine_defs (
  id          TEXT    PRIMARY KEY,
  name        TEXT    NOT NULL UNIQUE,
  kind        TEXT    NOT NULL,    -- one of the five engine kinds
  model       TEXT,               -- optional model override
  max_depth   INTEGER,            -- max pipeline depth [1, 100]
  max_agents  INTEGER,            -- max concurrent agents [1, 100]
  options     JSON,               -- {test_cmd?, export_dir?} only
  description TEXT,
  created_at  REAL    NOT NULL,
  updated_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_engine_defs_name
  ON engine_defs(name);
CREATE INDEX IF NOT EXISTS idx_engine_defs_kind
  ON engine_defs(kind);
CREATE INDEX IF NOT EXISTS idx_engine_defs_updated
  ON engine_defs(updated_at DESC);

-- ── Workflow definitions ──────────────────────────────────────────────────────
-- Named, persisted workflow graphs authored in the Studio Designer.  The
-- spec_json holds the canvas graph (nodes, edges, inputs, outputs) that the
-- frontend renders and serializes to YAML.

CREATE TABLE IF NOT EXISTS workflow_defs (
  id          TEXT    PRIMARY KEY,
  name        TEXT    NOT NULL UNIQUE,
  description TEXT,
  spec_json   JSON,
  created_at  REAL    NOT NULL,
  updated_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_defs_name
  ON workflow_defs(name);
CREATE INDEX IF NOT EXISTS idx_workflow_defs_updated
  ON workflow_defs(updated_at);

-- ── Session controls (ADR-0085 part 1: run control plane transport) ───────────
-- One row per operator control verb queued against a live session.  A poller
-- task in cli/orchestrate/flow.py's _execute_dag (same lifecycle as the
-- heartbeat loop) reads unapplied rows (applied_at IS NULL) and applies them
-- against the running executor.  Apply/stamp ordering is verb-classed:
-- pause/resume/stop are idempotent (apply, then stamp), message is not
-- (stamp 'applying', then apply, then finalize).  'stop' is schema-reserved
-- for a later slice (the checkpoint writer); no CLI verb emits it yet.

CREATE TABLE IF NOT EXISTS session_controls (
  id          TEXT    PRIMARY KEY,         -- uuid4 hex
  session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  verb        TEXT    NOT NULL
              CHECK(verb IN ('pause', 'resume', 'message', 'stop')),
  payload     JSON,                        -- verb-specific; NULL for pause/resume
  created_at  REAL    NOT NULL,
  applied_at  REAL,                        -- NULL until the poller consumes it
  result      TEXT                         -- 'applying' | 'applied' | 'rejected:<reason>'
);

CREATE INDEX IF NOT EXISTS idx_session_controls_pending
  ON session_controls(session_id, applied_at) WHERE applied_at IS NULL;

-- ── Dispatch outbox (ADR-0092: durable dispatch outbox) ────────────────────────
-- Producer-driven at-least-once outbound delivery. A row survives independent
-- of any consumer's liveness; the scheduler tick re-attempts the configured
-- notify template until it succeeds, backs off, or exhausts max_attempts.

CREATE TABLE IF NOT EXISTS dispatch_outbox (
  id                TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,              -- 'revival_ping' | 'terminal_notify' | ...
  deliver_to        TEXT NOT NULL,              -- opaque routing key for the transport template
  payload           JSON NOT NULL,              -- DispatchSignal contract
  dedup_key         TEXT,                       -- cross-submission idempotency
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'delivering', 'delivered', 'acked', 'dead_letter', 'expired')),
  attempt           INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 8,
  next_attempt_at   REAL NOT NULL,              -- backoff schedule; drives the tick scan
  ack_required      INTEGER NOT NULL DEFAULT 0, -- opt-in retry-until-ack tier
  ack_token         TEXT,                       -- consumer presents this to `li dispatch ack`
  session_id        TEXT REFERENCES sessions(id),        -- denormalized, nullable
  schedule_run_id   TEXT REFERENCES schedule_runs(id),   -- denormalized, nullable
  last_error        TEXT,
  created_at        REAL NOT NULL,
  expires_at        REAL,
  updated_at        REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_outbox_dedup
  ON dispatch_outbox(dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dispatch_outbox_due
  ON dispatch_outbox(status, next_attempt_at)
  WHERE status IN ('pending', 'delivering');
