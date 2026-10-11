UPDATE lion_sessions
SET ended = ?3,
    message_count = (SELECT COUNT(*) FROM lion_messages WHERE session_id = ?1 AND namespace = ?2)
WHERE id = ?1 AND namespace = ?2
