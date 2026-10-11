-- One history a model is shown: a run of this runtime, or a CLI session it drives (ADR-0025 D1).
CREATE TABLE IF NOT EXISTS lion_sessions (
    id                  TEXT PRIMARY KEY,
    namespace           TEXT NOT NULL,
    source              TEXT NOT NULL,      -- lion1, claude_code or codex
    provider_session_id TEXT,               -- the CLI's own id, for a harness session
    actor               TEXT,
    profile             TEXT,
    transcript          TEXT,               -- the CLI's transcript path, for a harness session
    message_count       INTEGER NOT NULL DEFAULT 0,
    started             INTEGER NOT NULL,   -- microseconds since the epoch, as the store's tables
    ended               INTEGER,
    branch_of           TEXT,               -- the session a branch continues; null for any other (D12)
    branch_seq          INTEGER             -- the seq it continues from: the parent's view before it, its own rows after
)
