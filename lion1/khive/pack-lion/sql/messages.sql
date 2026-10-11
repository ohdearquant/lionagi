SELECT * FROM lion_messages WHERE session_id = ?1 AND namespace = ?2 AND seq > ?3 ORDER BY seq LIMIT ?4
