# Playbook examples

Install by copying into `~/.lionagi/playbooks/`:

```bash
cp examples/playbooks/*.playbook.yaml ~/.lionagi/playbooks/
```

Then invoke:

```bash
li play minimal "explain decorators"
li play audit --mode security "the auth service"
li play chatgpt-orchestrate --tabs 5 --poll "research M5 chip"
li play list
```

Or via the long form:

```bash
li o flow -p audit --mode security "the auth service"
```

## What's declarative vs. runtime

A playbook YAML is the **declaration** — what to do, with which defaults.
Invoking it produces a **flow** — the actual DAG execution.

## Template interpolation

Inside `prompt:`, three things happen:

1. `{input}` → the positional prompt text from the CLI.
2. `{arg_name}` → a value from the `args:` schema (CLI override > `default`).
3. If no `{...}` placeholders are present, the positional text is
   **appended** with a blank line — mirroring how CC slash commands
   accept extra arguments.

## args: schema vs. argument-hint: fallback

Two ways to declare CLI args:

- **Explicit `args:`** (preferred): typed schema with defaults, help text,
  auto-generated `--help` output.
- **`argument-hint:` string** (CC-compatible fallback): parsed from display
  strings like `'[--tabs N] [--poll]'`. Every `[--flag VALUE]` becomes a
  string arg; every bare `[--flag]` becomes a bool arg. No type coercion.

If both are present, `args:` wins.

## Examples in this directory

### Starter examples

- `minimal.playbook.yaml` — prompt only, no args, positional appended
- `audit.playbook.yaml` — typed `args:` schema with defaults + template
- `chatgpt-orchestrate.playbook.yaml` — CC-compatible `argument-hint`
- `persistent-chat.playbook.yaml` — uses `team_attach:` for a thread that
  accumulates history across invocations

### Production playbooks

These are fully-documented, multi-phase playbooks ready for real projects.
Copy them to `~/.lionagi/playbooks/` and run them immediately.

- `feature.playbook.yaml` — End-to-end feature implementation.
  Phases: codebase scan → design → tests-first → implement → critic gate.
  Enforces no-stub policy and requires all tests to pass before finishing.
  ```
  li play feature "add rate limiting to the API endpoints"
  li play feature --scope auth "add password reset flow"
  ```

- `pr-review.playbook.yaml` — Multi-perspective PR review.
  Parallel specialists (correctness, security, architecture, tests, perf)
  followed by critic synthesis. Optionally posts findings to GitHub.
  Produces APPROVE / APPROVE-WITH-FIXES / REJECT verdict with severity table.
  ```
  li play pr-review 42
  li play pr-review --repo acme/backend --focus security --comment substantive 42
  li play pr-review --depth deep --focus all 137
  ```

- `test-coverage.playbook.yaml` — Iterative coverage improvement.
  Audits baseline, selects lowest-coverage modules, writes tests, verifies,
  and repeats until the target percentage is reached or gains saturate.
  Language-agnostic (detects pytest, vitest, jest, go test, etc.).
  ```
  li play test-coverage "the payments module"
  li play test-coverage --target 90 --focus src/auth "the auth layer"
  ```

- `research.playbook.yaml` — Technical research, read-only.
  Three parallel researchers (web, landscape, current codebase state)
  followed by an analyst and a critic. Every claim must cite a source.
  ```
  li play research "vector database options for semantic search"
  li play research --depth deep "WebAssembly runtimes for plugin sandboxing"
  li play research --depth quick "OpenTelemetry vs Prometheus for metrics"
  ```

- `resolve-issues.playbook.yaml` — Autonomous issue resolution.
  Fetches GitHub issues by label, runs parallel root-cause analysis,
  implements minimal fixes on a shared branch, adds regression tests,
  and posts update comments. Never closes issues or merges — human review required.
  ```
  li play resolve-issues "my-org/my-repo"
  li play resolve-issues --labels "bug,regression" --limit 3 "my-org/my-repo"
  ```

- `doc-alignment.playbook.yaml` — Documentation generation and alignment.
  Three modes: audit (gap report, read-only), generate (write missing docs
  from code), align (update existing docs after code changes). Writes
  READMEs, CLAUDE.md extensions, ADRs, and config specs as appropriate.
  ```
  li play doc-alignment --mode audit "the entire project"
  li play doc-alignment --mode generate --scope src/payments "new payments module"
  li play doc-alignment --mode align "after the v2 API refactor"
  ```

## Team modes

Playbooks can declare one of two team behaviors:

```yaml
team_mode: fresh-audit      # FRESH team every invocation (new UUID)
# or
team_attach: ongoing-chat   # ATTACH by name — first use creates, subsequent reuse
```

At the CLI, the equivalent flags are `--team-mode` and `--team-attach`
(mutually exclusive). `--team-attach` never requires a pre-existing team —
it creates-on-miss. If you want strict "team must exist" semantics, run
`li team create NAME -m ...` before invoking the playbook.
