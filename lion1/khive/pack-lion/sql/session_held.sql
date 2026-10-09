-- whoever holds the id, in any namespace: the id is the table's key across them
SELECT namespace, source, provider_session_id FROM lion_sessions WHERE id = ?1
