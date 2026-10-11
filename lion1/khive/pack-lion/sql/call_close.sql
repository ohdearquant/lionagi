UPDATE lion_calls
SET status = ?3, reply_seq = ?4, tokens_in = ?5, tokens_out = ?6, cost = ?7, duration_ms = ?8,
    error = ?9, closed = ?10
WHERE id = ?1 AND namespace = ?2
