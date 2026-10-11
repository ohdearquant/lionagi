-- the tip moves only for the current take, and only from the tip that take read
UPDATE lion_takes SET tip = ?4
WHERE namespace = ?1 AND provider_session_id = ?2 AND generation = ?3 AND tip IS ?5
