---
name: commit
description: >
  Conventional commit workflow with pre-commit checks, auto-staging, and push.
  Suggest when: "commit", "save changes", "push", or after completing code
  changes that need committing.
allowed-tools: [Bash, Read, Glob, Grep]
---

# lionagi commit

Smart git commit workflow.

## When to Use

- Any time a commit is needed
- User says "commit", "save changes", "push"
- After completing a task that produced code changes

## Workflow

### 1. Read Config

Check for `.lionagi/commit.toml` in the project root:

```toml
# .lionagi/commit.toml (optional — sensible defaults if missing)
default_push = true
allow_empty_commits = false
conventional_commit_types = [
    "feat", "fix", "build", "chore", "ci", "docs",
    "perf", "refactor", "revert", "style", "test"
]
default_stage_mode = "all"  # "all" or "patch"

# Pre-commit checks to run (ordered)
[pre_commit]
enabled = true
steps = [
    { cmd = "cargo fmt --check", stack = "rust" },
    { cmd = "cargo clippy --workspace", stack = "rust" },
    { cmd = "uv run ruff check .", stack = "python" },
    { cmd = "uv run ruff format --check .", stack = "python" },
]

# Git identity fallback
[identity]
name = "lionagi-bot"
email = "lionagi-bot@example.com"
```

### 2. Assess Changes

```bash
git status
git diff --stat
git diff --cached --stat
```

Determine what needs staging. If nothing is staged, stage relevant files.
NEVER use `git add -A` blindly — review what's being added.

### 3. Run Pre-Commit Checks

If `.lionagi/commit.toml` has `[pre_commit]` enabled, run each step:

- **Auto-detect stacks**: Only run checks for stacks present in the project
  - `rust`: if `Cargo.toml` exists
  - `python`: if `pyproject.toml` exists
  - `docs`: if `.md` files changed
- **If checks fail**: Fix the issues, re-stage, and proceed
- **If no config**: Auto-detect and run sensible defaults:
  - Rust project → `cargo fmt --check && cargo clippy`
  - Python project → `uv run ruff check . && uv run ruff format --check .`

### 4. Compose Conventional Commit Message

Format: `<type>(<scope>): <subject>`

- **type**: feat, fix, docs, refactor, test, chore, ci, perf, build, style, revert
- **scope**: optional, the area of change (e.g., memory, cli, types)
- **subject**: imperative, lowercase, no period

Examples:
```
feat(memory): add semantic search with reranking
fix(cli): handle missing config gracefully
refactor(platform): extract policy engine into separate crate
docs: update lambda architecture README
```

If the user provides a message, validate it against conventional commit format.
If invalid, suggest corrections.

### 5. Commit

```bash
git commit -m "$(cat <<'EOF'
<type>(<scope>): <subject>

<body if needed>

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### 6. Push (if configured)

If `default_push = true` in config (or user says `--push`):

```bash
git push origin <current-branch>
```

If push fails (no upstream), set it:
```bash
git push -u origin <current-branch>
```

## Important Rules

- NEVER commit `.env`, credentials, or secrets
- NEVER amend unless explicitly asked
- NEVER force push unless explicitly asked
- ALWAYS run pre-commit checks before committing
- ALWAYS use conventional commit format
- If user provides args like `--type feat --scope ui`, use them directly
- Report the commit hash and what was pushed
