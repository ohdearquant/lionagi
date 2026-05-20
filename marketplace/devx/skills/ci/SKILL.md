---
name: ci
description: >
  Run local CI pipeline (fmt, lint, test, build) before pushing. Suggest when:
  "ci", "run checks", "ensure it works", "before PR", or after large changes
  to verify nothing is broken.
allowed-tools: [Bash, Read, Glob, Grep]
---

# khive ci

Run local CI pipeline. Catch failures before they hit remote CI.

## When to Use

- User says "ci", "check", "ensure it works", "run checks"
- Before creating PRs
- After large changes to verify nothing is broken

## Workflow

### 1. Read Config

Check `.khive/ci.toml`:

```toml
# .khive/ci.toml (optional — auto-detect if missing)

# Pipeline stages run in order. Fail-fast by default.
fail_fast = true

[[stages]]
name = "fmt"
cmd = "cargo fmt --check"
stack = "rust"

[[stages]]
name = "lint"
cmd = "cargo clippy --workspace -- -D warnings"
stack = "rust"

[[stages]]
name = "test"
cmd = "cargo test --workspace"
stack = "rust"
timeout = 300  # seconds

[[stages]]
name = "build"
cmd = "cargo build --release"
stack = "rust"
optional = true  # Don't fail pipeline if this fails

# Python stages
[[stages]]
name = "py-fmt"
cmd = "uv run ruff format --check ."
stack = "python"

[[stages]]
name = "py-lint"
cmd = "uv run ruff check ."
stack = "python"

[[stages]]
name = "py-test"
cmd = "uv run pytest"
stack = "python"
```

### 2. Auto-Detect Pipeline

If no config, build pipeline from project structure:

**Rust project** (Cargo.toml):
1. `cargo fmt --check`
2. `cargo clippy --workspace -- -D warnings`
3. `cargo test --workspace`

**Python project** (pyproject.toml):
1. `uv run ruff format --check .`
2. `uv run ruff check .`
3. `uv run pytest`

**Mixed project**: Run all detected stacks.

### 3. Execute Pipeline

Run each stage sequentially. For each stage:

```
[1/4] fmt ............ ✓ (0.3s)
[2/4] lint ........... ✓ (12.1s)
[3/4] test ........... ✓ (45.2s)
[4/4] build .......... ✓ (120.5s)
```

- If `fail_fast = true` (default): stop on first failure
- If `fail_fast = false`: run all stages, report all failures
- If stage has `optional = true`: report failure but continue

### 4. Report

```
CI Pipeline: 4/4 passed ✓
  fmt:   ✓ (0.3s)
  lint:  ✓ (12.1s)
  test:  ✓ (45.2s)
  build: ✓ (120.5s)
Total: 178.1s
```

On failure:
```
CI Pipeline: FAILED at stage 2/4
  fmt:   ✓ (0.3s)
  lint:  ✗ FAILED (12.1s)
    error: unused variable `x` in src/main.rs:42
  test:  — skipped (fail_fast)
  build: — skipped (fail_fast)
```

## Important Rules

- NEVER use naked `python` or `pip` — always `uv run`
- Auto-detect stacks when no config present
- Respect timeouts on long-running stages
- Report clear pass/fail with timing for each stage
- On failure, show the actual error output (not just "failed")
