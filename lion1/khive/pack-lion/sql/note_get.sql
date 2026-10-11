SELECT key, value, run, seq, version, written FROM lion_notes
WHERE namespace = ?1 AND profile = ?2 AND key = ?3
