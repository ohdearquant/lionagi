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
# Python (always uv run — never naked pytest)
uv run pytest tests/test_feature.py::test_name -v   # single test
uv run pytest                                         # full suite
uv run pytest --cov=src --cov-report=term-missing    # with coverage

# Rust
cargo test test_name -- --nocapture   # single test
cargo test --workspace                 # full workspace
```

## Key Principles

- **Test must fail first**: A test that passes before implementation proves nothing
- **Minimal implementation**: Don't over-engineer in the GREEN phase
- **Refactor only when green**: Never refactor with failing tests
- **Small cycles**: Keep each cycle short (5-15 minutes)
- **One behavior per test**: Each test verifies one specific behavior
- **Never use naked python/pytest**: Always `uv run` for Python projects

## Coverage Gates (lionagi standard)

- Business logic: ≥ 90%
- API surface: ≥ 85%
- Utilities: ≥ 80%

Check after each cycle: `uv run pytest --cov=src --cov-report=term-missing`

See [methodology.md](methodology.md) for detailed cycle steps, multi-cycle patterns,
parallel agent TDD with `li o fanout`, anti-patterns, and lint/quality commands.

## Relevant Source Files

- `lionagi/cli/orchestrate/fanout.py` — `li o fanout` parallel workers for multi-agent TDD
- `lionagi/cli/agent.py` — `li agent` for single-agent implementation sessions
- `pyproject.toml` — project test configuration and coverage settings
