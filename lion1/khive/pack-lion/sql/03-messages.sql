-- One entry of a session: for a run of this runtime, one Entry of its record, by its seq.
CREATE TABLE IF NOT EXISTS lion_messages (
    session_id   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    namespace    TEXT NOT NULL,
    role         TEXT NOT NULL,            -- the provider's: system, user, assistant, tool
    kind         TEXT NOT NULL,            -- the runtime's (input, system, text, result) or the CLI's own
    turn         INTEGER NOT NULL DEFAULT 0,
    alias        TEXT,                     -- the model's own name for the entry
    text         TEXT NOT NULL,
    raw          TEXT,                     -- the entry as stored, when it differs from text
    content_hash TEXT NOT NULL,
    parent_seq   INTEGER,                  -- for a result, the command it answers
    directive    TEXT,                     -- the view directive the entry carries, as JSON
    created      INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
)
