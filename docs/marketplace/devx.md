# devx Plugin

Developer-experience skill bundle for lionagi — conventional commits, CI, code formatting, GitHub PR creation, mid-session summarization, and project bootstrapping.

**Source**: `marketplace/devx/`  
**Install**: `claude /plugin install devx@lionagi`  
**Version**: 0.1.0 (Apache-2.0)

!!! note "Not shipped: session-start / session-summarize"
    These skills depend on project-specific memory context and identity files and are intentionally omitted. `/summarize` covers mid-session checkpointing. See `marketplace/devx/skills/TODO.md` for details.

## Skills

### `/commit`

> **Source**: `marketplace/devx/skills/commit/SKILL.md`

Conventional commit workflow with pre-commit checks, auto-staging, and optional push.

**When to use**: any time a commit is needed; when the user says "commit", "save changes", or "push"; after completing code changes.

**Workflow**:

1. Read `.lionagi/commit.toml` (optional — auto-detects defaults if missing)
2. Run `git status` and `git diff --stat` to assess staged/unstaged changes
3. Run pre-commit checks (stack-detected: Rust → `cargo fmt --check && cargo clippy`; Python → `uv run ruff check . && uv run ruff format --check .`)
4. Compose a conventional commit message: `<type>(<scope>): <subject>`
5. Commit using a heredoc (never shell-escaping issues)
6. Push if `default_push = true` in config or user says `--push`

**Config** (`.lionagi/commit.toml`):

```toml
default_push = true
allow_empty_commits = false
conventional_commit_types = ["feat", "fix", "build", "chore", "ci", "docs",
    "perf", "refactor", "revert", "style", "test"]
default_stage_mode = "all"   # "all" or "patch"

[pre_commit]
enabled = true
steps = [
    { cmd = "cargo fmt --check", stack = "rust" },
    { cmd = "uv run ruff check .", stack = "python" },
]
```

**Conventions enforced**:

- Never commits `.env`, credentials, or secrets
- Never amends unless explicitly asked
- Never force-pushes unless explicitly asked
- Always runs pre-commit checks before committing
- Conventional commit types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`, `perf`, `build`, `style`, `revert`

---

### `/fmt`

> **Source**: `marketplace/devx/skills/fmt/SKILL.md`

Multi-stack code formatter. One command formats Rust, Python, Markdown, and TypeScript.

**When to use**: user says "format", "fmt", "lint", "clean up code"; before commits (called automatically by `/commit`); after large refactors.

**Workflow**:

1. Read `.khive/fmt.toml` (optional — auto-detects stacks if missing)
2. Auto-detect stacks from project files: `Cargo.toml` → rust; `pyproject.toml` → python; `*.md` → docs (if deno available); `package.json` → deno
3. Run formatters per stack

**What runs per stack**:

| Stack | Format command | Check-only command |
|---|---|---|
| Rust | `cargo fmt` | `cargo fmt --check` |
| Python | `uv run ruff format .` + `uv run ruff check . --fix` | `uv run ruff format --check .` + `uv run ruff check .` |
| Docs (markdown) | `deno fmt **/*.md` | `deno fmt --check **/*.md` |
| Deno/TS | `deno fmt **/*.{ts,js,tsx,jsx}` | `deno fmt --check ...` |

**Config** (`.khive/fmt.toml`):

```toml
enable = ["rust", "python", "docs"]

[stacks.rust]
cmd = "cargo fmt"
check_cmd = "cargo fmt --check"

[stacks.python]
cmd = "uv run ruff format ."
check_cmd = "uv run ruff format --check ."
lint_cmd = "uv run ruff check . --fix"
```

**Check mode**: pass `--check` or say "check" to report failures without modifying files. Missing formatters are skipped with a warning, not a hard failure.

---

### `/ci`

> **Source**: `marketplace/devx/skills/ci/SKILL.md`

Run the local CI pipeline (format, lint, test, build) before pushing. Catch failures before they hit remote CI.

**When to use**: user says "ci", "check", "ensure it works", "run checks"; before creating PRs; after large changes.

**Workflow**:

1. Read `.khive/ci.toml` (optional — auto-detects pipeline if missing)
2. Auto-detect pipeline from project structure (Rust, Python, or mixed)
3. Execute stages sequentially, reporting `✓` / `✗` with timing per stage
4. On failure: show the actual error output, not just "failed"; respect `fail_fast` setting

**Auto-detected pipelines**:

=== "Rust"

    ```
    1. cargo fmt --check
    2. cargo clippy --workspace -- -D warnings
    3. cargo test --workspace
    ```

=== "Python"

    ```
    1. uv run ruff format --check .
    2. uv run ruff check .
    3. uv run pytest
    ```

=== "Mixed"

    Runs all detected stacks.

**Config** (`.khive/ci.toml`):

```toml
fail_fast = true      # stop on first failure (default)

[[stages]]
name = "fmt"
cmd = "uv run ruff format --check ."
stack = "python"

[[stages]]
name = "test"
cmd = "uv run pytest"
stack = "python"
timeout = 300         # seconds

[[stages]]
name = "build"
cmd = "cargo build --release"
stack = "rust"
optional = true       # failure reported but pipeline continues
```

**Output format**:

```
CI Pipeline: 4/4 passed ✓
  fmt:   ✓ (0.3s)
  lint:  ✓ (12.1s)
  test:  ✓ (45.2s)
  build: ✓ (120.5s)
```

---

### `/pr`

> **Source**: `marketplace/devx/skills/pr/SKILL.md`

Create a GitHub PR with branch push, conventional title, and PR metadata — one step.

**When to use**: user says "create PR", "open PR", "submit PR"; feature branch is ready for review.

**Workflow**:

1. Read `.khive/pr.toml` (optional)
2. Check current branch and commits ahead of base
3. Push branch: `git push -u origin <branch>` (if `auto_push_branch = true`)
4. Check for an existing PR on this branch (`gh pr list --head`)
5. Create PR with title inferred from last conventional commit; body with summary bullet points and test plan checklist
6. Apply reviewers/labels from config if set

**Config** (`.khive/pr.toml`):

```toml
default_base_branch = "main"
default_to_draft = false
default_reviewers = []
default_assignees = []
default_labels = []
auto_push_branch = true
```

**Rules enforced**:

- Never creates PR from `main`/`master`
- Always pushes branch before creating PR
- Checks for an existing PR before creating a new one
- Uses `gh` CLI (must be authenticated)

---

### `/summarize`

> **Source**: `marketplace/devx/skills/summarize/SKILL.md`

Mid-session context capture. Checkpoints progress, decisions, and learnings without ending the session.

**When to use**: significant milestone reached but more work ahead; approaching context limits (>100k tokens); switching topics within the same session; user says "summarize", "capture this", "checkpoint".

**This is NOT a session-ending summary** — use `/session-summarize` (not in this plugin) for that.

**Workflow**:

1. Scan recent work: what was accomplished, key decisions (with rationale), user's guidance verbatim, patterns discovered, files modified, open threads
2. Write checkpoint file to `./notes/checkpoints/checkpoint_YYYYMMDD_HHMMSS_{topic}.md` (or `$LIONAGI_NOTES_DIR` if set)
3. Continue working — the session does NOT end

**Checkpoint file format**:

```markdown
---
timestamp: 2026-05-22T10:00:00Z
topic: {topic}
status: continuing
---

# CHECKPOINT: {topic}

## Accomplished
- ...

## Decisions
| Decision | Chose | Over | Rationale |
|---|---|---|---|

## User's Guidance
- "{quote}" — context: ...

## Key Learnings
- ...

## Files Modified
- /absolute/path/to/file — what changed

## Next Steps
- ...
```

**Proactive capture triggers** (write a checkpoint note without being asked):

| Trigger | Capture |
|---|---|
| Decision made | Decision + rationale + alternatives considered |
| Pattern discovered | Semantic memory with "when to use" |
| Significant work completed | Episodic capture of what was done + outcome |
| Session winding down | Offer to run `/session-summarize` |

**Session wind-down signals**: user says "thanks", "that's it", "done for now"; long pause after significant work; context switches to unrelated topic.

---

### `/init`

> **Source**: `marketplace/devx/skills/init/SKILL.md`

Bootstrap a development environment from a fresh clone.

**When to use**: user says "init", "setup", "bootstrap"; starting on a new clone; switching machines or environments.

**Workflow**:

1. Read `.khive/init.toml` (optional)
2. Verify required tools are installed:

    | Tool | Required For | Check |
    |---|---|---|
    | `git` | All | `git --version` |
    | `cargo` + `rustc` | Rust | `cargo --version` |
    | `uv` | Python | `uv --version` |
    | `gh` | PRs/Issues | `gh --version` |
    | `deno` | Docs/TS | `deno --version` |

3. Auto-detect stacks and run setup:
    - `Cargo.toml` → `cargo check --workspace`
    - `pyproject.toml` → `uv sync`
    - `package.json` → `pnpm install --frozen-lockfile`
    - `.pre-commit-config.yaml` → `uv run pre-commit install`
4. Verify each step succeeded; continue on failures but report them
5. Print summary: `init: tools ✓ | rust ✓ | python ✓ | pre-commit ✓`

**Config** (`.khive/init.toml`):

```toml
ignore_missing_optional_tools = false
disable_auto_stacks = []
force_enable_steps = []

[custom_steps.pre_commit_setup]
cmd = "uv run pre-commit install"
run_if = "file_exists:.pre-commit-config.yaml"
```

---

## Agent: `reviewer`

> **Source**: `marketplace/devx/agents/reviewer.md`

| Field | Value |
|---|---|
| Model | `codex/gpt-5.5` |
| Effort | `medium` |
| Yolo | `true` |

**Role**: Artifact review specialist — checks PRs, reports, documents, and deliverables against standards and quality gates. Produces professional verdicts: `APPROVE`, `APPROVE-WITH-SUGGESTIONS`, `REQUEST CHANGES`, or `REJECT`.

**Distinct from `critic`**: Reviewer asks "does this artifact meet our standards?" Critic asks "what's fundamentally wrong?" Reviewer checks CI, tests, conventions. Critic challenges underlying logic.

**Skills the reviewer loads**:

```bash
li skill review           # standard correctness/quality rubric
li skill security-review  # threat-model rubric (when auth/crypto touched)
li skill pr-review        # multi-perspective methodology (for PR reviews)
```

**Verdicts**:

| Verdict | Condition |
|---|---|
| `APPROVE` | All gates pass, no defects |
| `APPROVE-WITH-SUGGESTIONS` | Gates pass, minor non-blocking suggestions |
| `REQUEST CHANGES` | Major or minor defects found |
| `REJECT` | Critical defect, test coverage < 80%, or critical gate failure |

**Use with `li agent`**:

```bash
li agent -a reviewer "Review the PR at https://github.com/..."
```

**Use in a flow DAG**: the reviewer typically runs after implementer/tester and before the final critic gate.
