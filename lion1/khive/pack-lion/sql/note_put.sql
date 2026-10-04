INSERT INTO lion_notes (namespace, profile, key, value, run, seq, version, written)
VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, ?7)
ON CONFLICT (namespace, profile, key) DO UPDATE SET
    value = excluded.value, run = excluded.run, seq = excluded.seq,
    version = lion_notes.version + 1, written = excluded.written
