### Fixed

- Help for `flow.submit`, `fanout.submit` and `play.submit` states the schema-fingerprint retry rule, as `agent.submit` does.
- Both `stale_schema` refusals (missing and outdated fingerprint) state the same retry rule as help, including the condition that the arguments match the returned schema. The MCP server reference now places that schema at `error.schema`, with `error.schema_ref` on later identical refusals in one response.
