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
  agent_hash      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated
  ON sessions(updated_at DESC);
-- ADR-0019: lets the staleness query (running sessions sorted by oldest
-- activity) skip the full table scan.
CREATE INDEX IF NOT EXISTS idx_sessions_status_last_msg
  ON sessions(status, last_message_at) WHERE status = 'running';
-- ADR-0020: grouped runs view fetches all sessions for an invocation.
CREATE INDEX IF NOT EXISTS idx_sessions_invocation
  ON sessions(invocation_id) WHERE invocation_id IS NOT NULL;

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
  agent_name      TEXT
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
  updated_at          REAL    NOT NULL
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
  updated_at      REAL    NOT NULL
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
                  )
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
                    status IN ('running', 'completed', 'failed',
                               'timed_out', 'aborted', 'cancelled')
                  ),
  session_count   INTEGER NOT NULL DEFAULT 0,
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  node_metadata   JSON
);

CREATE INDEX IF NOT EXISTS idx_invocations_skill ON invocations(skill);
CREATE INDEX IF NOT EXISTS idx_invocations_status ON invocations(status);
CREATE INDEX IF NOT EXISTS idx_invocations_updated ON invocations(updated_at DESC);

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
-- holds the SkillOutcome.model_dump() JSON; `file_path` optionally
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
