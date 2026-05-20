---
name: init
description: >
  Bootstrap development environment. Suggest when: "init", "setup", "bootstrap",
  starting on a new project clone, or switching machines. Detects Rust/Python/Node,
  installs deps, sets up git hooks.
allowed-tools: [Bash, Read, Glob, Grep, Write]
---

# khive init

Bootstrap a project's dev environment from scratch.

## When to Use

- User says "init", "setup", "bootstrap"
- Starting work on a new project clone
- After switching machines or environments

## Workflow

### 1. Read Config

Check `.khive/init.toml`:

```toml
# .khive/init.toml (optional — auto-detect if missing)
ignore_missing_optional_tools = false
disable_auto_stacks = []
force_enable_steps = []

[custom_steps.pre_commit_setup]
cmd = "uv run pre-commit install"
run_if = "file_exists:.pre-commit-config.yaml"
```

### 2. Verify Tools

Check required tools are installed:

| Tool | Required For | Check Command |
|------|-------------|---------------|
| `git` | All | `git --version` |
| `cargo` | Rust | `cargo --version` |
| `rustc` | Rust | `rustc --version` |
| `uv` | Python | `uv --version` |
| `gh` | PRs/Issues | `gh --version` |
| `deno` | Docs/TS | `deno --version` |
| `khived` | Khive daemon | `khived status` |

Report missing tools with install instructions.

### 3. Auto-Detect and Run Steps

**Rust** (if `Cargo.toml` exists):
```bash
cargo check --workspace
```

**Python** (if `pyproject.toml` exists):
```bash
uv sync
```

**Node** (if `package.json` exists):
```bash
pnpm install --frozen-lockfile
```

**Pre-commit** (if `.pre-commit-config.yaml` exists):
```bash
uv run pre-commit install
```

**Lambda setup** (if `.khive/lambda.yaml` exists):
```bash
~/.khive/bin/generate-claude "$(pwd)"
```

### 4. Verify

Run a quick check to ensure everything works:
```bash
# Rust
cargo check --workspace 2>&1 | tail -5

# Python
uv run python -c "print('Python OK')"
```

### 5. Report

```
init: tools ✓ (git, cargo, uv, gh, khived)
init: rust ✓ (cargo check)
init: python ✓ (uv sync)
init: pre-commit ✓ (hooks installed)
init: lambda ✓ (.claude/ generated)
```

## Important Rules

- NEVER use naked `python` or `pip`
- If a step fails, continue with other steps but report the failure
- Always verify tools before running stack-specific steps
- Generate `.claude/` if `.khive/lambda.yaml` exists
