---
name: tdd
description: >
  Guide test-driven development workflow. Suggest when: "test first", "TDD",
  "write tests before", "red green refactor" mentioned, or when implementing
  features or bug fixes where a test-first approach is beneficial. Enforces
  Red-Green-Refactor cycle.
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
---

# TDD Workflow

Orchestrate test-driven development: Red → Green → Refactor.

## Activation Triggers

- "TDD", "test-driven", "test first", "red green refactor"
- Bug fix where proving the bug first is valuable
- Feature with clear acceptance criteria
- "write the test before implementing"

## The Cycle

| Phase | Action | Gate |
|-------|--------|------|
| **RED** | Write failing test expressing desired behavior | Run it — MUST fail. If it passes, it tests nothing new. |
| **GREEN** | Write minimum code to make the test pass | Run it — MUST pass. No over-engineering. |
| **REFACTOR** | Clean up while staying green | Run ALL tests — MUST pass. Never refactor with failures. |

### Run commands

```bash
# lionagi Python
scripts/ci.sh test-python tests/test_feature.py::test_name  # single test
scripts/ci.sh test-python                                  # full suite
scripts/ci.sh test-python-cov                              # with coverage

# Rust
cargo test test_name -- --nocapture   # single test
cargo test --workspace                 # full workspace
```

## Key Principles

- **Test must fail first**: A test that passes before implementation proves nothing
- **Minimal implementation**: Don't over-engineer in the GREEN phase
- **Refactor only when green**: Never refactor with failing tests
- **Small cycles**: Keep each cycle focused on one behavior
- **One behavior per test**: Each test verifies one specific behavior
- **Use the project runner**: Follow the repository's configured test and CI entry points

## Coverage Gates

Meet the repository's configured coverage threshold. Do not substitute generic percentages
for project policy.

For lionagi, check with `scripts/ci.sh test-python-cov`.

See [methodology.md](methodology.md) for detailed cycle steps, multi-cycle patterns,
parallel agent TDD through the plugin's MCP server (`fanout.submit`), anti-patterns, and
lint/quality commands.

## Relevant Source Files

- `lionagi/mcp/server.py`, `lionagi/mcp/verbs.py` — `fanout.submit` / `agent.submit`, the
  MCP path for parallel and single-agent TDD sessions
- `lionagi/cli/orchestrate/fanout.py` — `li o fanout`, the checkout-local equivalent for
  parallel workers
- `lionagi/cli/agent.py` — `li agent`, the checkout-local equivalent for a single-agent
  implementation session
- `docs/internals/ci.md` — documents the supported Python test, coverage, lint, and format entry point
- `pyproject.toml` — project test and Pyright configuration
