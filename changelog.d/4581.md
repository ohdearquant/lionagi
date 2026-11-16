### Security

- A Codex child process, and every command it runs, no longer inherits credential variables from the process that launched it. It keeps `OPENAI_API_KEY`, `CODEX_*` variables and the `env_key` of each model provider defined in the user's codex config or the run's `-c` overrides.
