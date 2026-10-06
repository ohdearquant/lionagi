-- a substring over key and value; the case folding is the handler's, so the pattern arrives folded
-- and the columns are folded here the same way
SELECT key, value, run, seq, version, written FROM lion_notes
WHERE namespace = ?1 AND profile = ?2
  AND (instr(lower(key), ?3) > 0 OR instr(lower(value), ?3) > 0)
ORDER BY key
