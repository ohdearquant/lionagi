-- a harness session's row, admitted only while the take that opens it is current (D12); an id another
-- session holds is no row either, so the open is refused by name and rolls back
INSERT OR IGNORE INTO lion_sessions
    (id, namespace, source, provider_session_id, actor, profile, transcript, started, branch_of, branch_seq)
SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10
WHERE EXISTS (
    SELECT 1 FROM lion_takes WHERE namespace = ?2 AND provider_session_id = ?4 AND generation = ?11
)
