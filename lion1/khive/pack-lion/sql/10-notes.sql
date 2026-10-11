-- A profile's notes (ADR-0025 D6): one row per profile and key, the value as JSON, who wrote it,
-- its version counted up on every write.
CREATE TABLE IF NOT EXISTS lion_notes (
    namespace TEXT NOT NULL,
    profile   TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    run       TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    version   INTEGER NOT NULL DEFAULT 1,
    written   INTEGER NOT NULL,
    PRIMARY KEY (namespace, profile, key)
)
