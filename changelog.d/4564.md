### Security

- A Claude Code, Gemini or Pi child process, and every command it runs, no longer inherits credential variables from the process that launched it. It keeps its own provider's login variables and the names the declared secret lookup fills for CLI children; `LIONAGI_STUDIO_AUTH_TOKEN` and `LIONAGI_REMOTE_*` are removed from every CLI child, Codex included.

### Fixed

- The Studio CLI clients build every request through one factory that sends `LIONAGI_STUDIO_AUTH_TOKEN` only to the configured Studio origin, and warn once when that origin is plain HTTP to a non-loopback host.
