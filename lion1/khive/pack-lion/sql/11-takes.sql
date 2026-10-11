-- A CLI session's takes (ADR-0025 D12): the generation each `lion.take_begin` advances, which every
-- harness write must name while it is current, and the tip, the session its takes compare and write.
CREATE TABLE IF NOT EXISTS lion_takes (
    namespace           TEXT NOT NULL,
    provider_session_id TEXT NOT NULL,
    generation          INTEGER NOT NULL,
    tip                 TEXT,               -- null until the first take opens its root
    PRIMARY KEY (namespace, provider_session_id)
)
