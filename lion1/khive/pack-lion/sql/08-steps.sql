-- One step of a flow's run; the step's own work is the run it names.
CREATE TABLE IF NOT EXISTS lion_steps (
    flow_run_id TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    profile     TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    inputs_from TEXT,                      -- the steps it reads, as JSON
    outcome     TEXT,
    PRIMARY KEY (flow_run_id, idx)
)
