-- a harness session's row, admitted only while the take that writes it is current (D12)
INSERT INTO lion_messages
    (session_id, seq, namespace, role, kind, turn, alias, text, raw, content_hash, parent_seq, directive, created, carried)
SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14
WHERE EXISTS (
    SELECT 1 FROM lion_sessions s
    JOIN lion_takes t ON t.namespace = s.namespace AND t.provider_session_id = s.provider_session_id
    WHERE s.id = ?1 AND s.namespace = ?3 AND t.generation = ?15
)
