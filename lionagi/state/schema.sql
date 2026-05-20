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
  invocation_kind TEXT,                        -- agent|play|flow|fanout|show-play
  show_topic      TEXT,
  show_play_name  TEXT,
  artifacts_path  TEXT,
  source_kind     TEXT    DEFAULT 'live',       -- live|imported_fs
  -- ── Lifecycle (ADR-0017) ───────────────────────────────────────────
  status          TEXT CHECK(
                    status IS NULL
                    OR status IN ('running', 'completed', 'failed', 'aborted')
                  ),
  started_at      REAL,
  ended_at        REAL
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated
  ON sessions(updated_at DESC);

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
  system_msg_id   TEXT    REFERENCES messages(id)   -- system prompt; just a reference to the message
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
  status              TEXT    NOT NULL DEFAULT 'active',
  show_dir            TEXT    NOT NULL,
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
  status          TEXT    NOT NULL DEFAULT 'pending',
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
