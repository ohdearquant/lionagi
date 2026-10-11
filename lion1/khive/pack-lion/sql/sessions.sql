SELECT * FROM lion_sessions
WHERE namespace = ?1 AND (?2 IS NULL OR source = ?2) AND (?3 IS NULL OR actor = ?3)
ORDER BY started DESC
LIMIT ?4
