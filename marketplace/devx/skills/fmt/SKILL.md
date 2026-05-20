---
name: fmt
description: >
  Multi-stack code formatter. Suggest when: "format", "fmt", "lint", code style
  issues found, or before committing. Auto-detects Rust/Python/Markdown/TypeScript.
allowed-tools: [Bash, Read, Glob, Grep]
---

# khive fmt

Unified multi-stack code formatting. One command, all languages.

## When to Use

- User says "format", "fmt", "lint", "clean up code"
- Before commits (called automatically by /commit)
- After large refactors

## Workflow

### 1. Read Config

Check `.khive/fmt.toml`:

```toml
# .khive/fmt.toml (optional — auto-detect if missing)
enable = ["rust", "python", "docs"]

[stacks.rust]
cmd = "cargo fmt"
check_cmd = "cargo fmt --check"

[stacks.python]
cmd = "uv run ruff format ."
check_cmd = "uv run ruff format --check ."
lint_cmd = "uv run ruff check . --fix"
lint_check_cmd = "uv run ruff check ."

[stacks.docs]
cmd = "deno fmt **/*.md"
check_cmd = "deno fmt --check **/*.md"

[stacks.deno]
cmd = "deno fmt **/*.{ts,js,tsx,jsx}"
check_cmd = "deno fmt --check **/*.{ts,js,tsx,jsx}"
```

### 2. Auto-Detect Stacks

If no config, detect from project files:
- `Cargo.toml` → rust stack
- `pyproject.toml` → python stack
- `*.md` files → docs stack (only if deno available)
- `package.json` / `deno.json` → deno stack

### 3. Run Formatters

For each enabled/detected stack:

**Rust**:
```bash
cargo fmt
```

**Python** (ALWAYS use uv run):
```bash
uv run ruff format .
uv run ruff check . --fix
```

**Docs** (if deno available):
```bash
deno fmt **/*.md
```

### 4. Check Mode

If user says "check" or `--check`, run check commands instead (no modifications):
```bash
cargo fmt --check
uv run ruff format --check .
uv run ruff check .
```

Report which stacks pass/fail.

### 5. Report

Summarize what was formatted:
```
fmt: rust ✓ (cargo fmt)
fmt: python ✓ (ruff format + ruff check --fix)
fmt: docs — skipped (deno not found)
```

## Important Rules

- NEVER use naked `python` or `pip` — always `uv run`
- If a formatter is not installed, skip with a warning (don't fail)
- In check mode, report failures but don't modify files
- Respect `.khive/fmt.toml` stack-specific excludes
