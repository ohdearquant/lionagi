SELECT * FROM lion_runs
WHERE namespace = ?1 AND (?2 IS NULL OR actor = ?2)
ORDER BY started DESC
LIMIT ?3
