-- One request to a model and its answer or refusal. No content: seq_from, seq_to and folded name
-- the messages rendered into it.
CREATE TABLE IF NOT EXISTS lion_calls (
    id          TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    model       TEXT NOT NULL,
    seq_from    INTEGER NOT NULL,
    seq_to      INTEGER NOT NULL,
    folded      TEXT,                      -- seqs in the range folded to a placeholder, as JSON
    reply_seq   INTEGER,                   -- the message the answer became
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    cost        REAL,
    duration_ms INTEGER,
    status      TEXT NOT NULL,             -- open, ok, refused, limit, cut or failed
    error       TEXT,
    opened      INTEGER NOT NULL,
    closed      INTEGER
)
