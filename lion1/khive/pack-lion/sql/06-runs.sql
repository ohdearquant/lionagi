-- One job of an actor under a profile, with its session and its outcome. An agent's wake is a run
-- whose trigger is the mail batch.
CREATE TABLE IF NOT EXISTS lion_runs (
    id          TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    actor       TEXT,
    profile     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    bounds      TEXT,                      -- the job's bounds, as JSON
    trigger     TEXT,                      -- the inbound ids that started it, as JSON
    continues   TEXT,                      -- the run this one resumes by replay
    outcome     TEXT,                      -- success, continue, exhausted, refused or stopped
    cost        REAL,
    turns       INTEGER,
    output      TEXT,                      -- the output fields, as JSON
    started     INTEGER NOT NULL,
    ended       INTEGER
)
