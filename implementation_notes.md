# Khive injection default-path wiring

- Added opt-in `khive_injection` configuration to `AgentSpec`, including composition and YAML round-tripping for boolean and mapping values.
- Registered `KhiveInjectionProvider` during `create_agent` only when configured. Default profile IDs derive from the cast role as `<role>-recall-v1`; explicit policies and profile IDs are preserved.
- Promoted `khive_injection` to a first-class CLI profile field and propagated it through single-agent, orchestrator, and worker construction paths while preserving verbatim profile prompts.
- Added registration, default-off, nested-policy, fail-loud, YAML, profile parsing, and verbatim-profile tests. Registration tests do not execute provider I/O.

## Verification

- Agent and khive injection test suite: passed.
- Affected CLI profile and orchestration test suite: passed.
- Clean `import lionagi`: passed.
- Ruff lint and format checks for all requested source files: passed.
