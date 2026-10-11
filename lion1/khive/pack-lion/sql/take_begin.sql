INSERT INTO lion_takes (namespace, provider_session_id, generation, tip) VALUES (?1, ?2, 1, NULL)
ON CONFLICT (namespace, provider_session_id) DO UPDATE SET generation = lion_takes.generation + 1
