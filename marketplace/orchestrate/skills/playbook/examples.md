# Playbook Examples

## Repository Examples Directory

The `examples/playbooks/` directory in the lionagi repository contains reference
implementations:

| File | What it demonstrates |
|---|---|
| `minimal.playbook.yaml` | Simplest possible: `model` + `prompt`, no args |
| `audit.playbook.yaml` | Typed `args:` with `{input}` and `{arg_name}` interpolation |
| `feature.playbook.yaml` | Phased TDD workflow with `max-ops`, `show-graph`, and `{scope}` arg |
| `pr-review.playbook.yaml` | Multi-arg, multi-dimension review with `argument-hint` and `show-graph` |
| `research.playbook.yaml` | Technical research pipeline |
| `test-coverage.playbook.yaml` | Iterative coverage loop |
| `resolve-issues.playbook.yaml` | GitHub issue resolution |
| `doc-alignment.playbook.yaml` | Documentation generation and alignment |
| `chatgpt-orchestrate.playbook.yaml` | ChatGPT-backed orchestration |
| `persistent-chat.playbook.yaml` | Persistent team-attached conversation |

---

## Example 1: Simple single-agent task

```yaml
name: minimal
description: Simplest possible playbook — prompt only, positional text appended.
model: claude-code/sonnet-4-6

prompt: |
  You are a patient teacher. Explain the following topic in plain language,
  with one concrete example.
```

Run with `play.submit`, whose op carries the fingerprint that
`help={"verb": "play.submit", "playbook": "minimal"}` returns, as a sibling of `args`:

```json
{"ops": [{"op": "play.submit", "args": {"playbook": "minimal", "prompt": "what is a monad?"}, "schema_fingerprint": "<from that help call>"}]}
```

Inside a lionagi checkout: `li play minimal "what is a monad?"`

The positional text is appended after a blank line because `{input}` is not
declared in the template. This is equivalent to a `{input}` placeholder at the
end. No `args:` block is needed.

---

## Example 2: Parametric audit with typed args

```yaml
name: audit
description: Parametric audit — typed args with template interpolation.
argument-hint: '[--mode MODE] [--worker-count N] [--strict]'

model: claude-code/sonnet-4-6
agent: orchestrator
effort: high

args:
  mode:
    type: str
    default: dry
    help: "audit mode: dry | security | dead-code | api-surface"
  worker_count:
    type: int
    default: 8
    help: "number of parallel codex workers"
  strict:
    type: bool
    default: false
    help: "fail on any finding above MEDIUM severity"

prompt: |
  Run a {mode} audit with {worker_count} parallel workers. Strict mode: {strict}.

  Target scope: {input}

  Each worker audits ONE module independently. Consolidation happens after
  all workers return.
```

Run with `play.submit`, the `playbook` and `prompt` (`{input}`) args:

```json
{"ops": [{"op": "play.submit", "args": {"playbook": "audit", "prompt": "src/auth/", "cwd": "/absolute/path/to/your/checkout"}, "schema_fingerprint": "<from help={\"verb\": \"play.submit\", \"playbook\": \"audit\"}>"}]}
```

`src/auth/` is relative, so the run has to be told what it is relative *to*. An omitted
`cwd` resolves to the server's own directory, not yours, and the audit would then read a
different tree or find nothing at all. The CLI form below needs no such argument because it
already starts in your shell's directory.

To set the custom `mode`/`worker_count`/`strict` args away from their YAML defaults, read the
schema that same help call returns: naming the playbook is what resolves *its own* declared
arguments into the reply, which a bare `help=true` catalog request does not do. That call is
required anyway, since its fingerprint is what the op above carries. Inside a lionagi
checkout, the same run with those flags set is:
`li play audit --mode security --worker-count 4 --strict "src/auth/"`

Key points:
- `argument-hint` populates the CLI `--help` display but does not affect parsing.
- `args:` keys must use underscores (not dashes). `strict` is a `bool` arg, so
  passing `--strict` on the CLI sets it to `true` without a value.
- `{input}` receives the positional text (`"src/auth/"`).
- `{mode}`, `{worker_count}`, and `{strict}` are filled from args or their defaults.

---

## Example 3: Multi-perspective PR review with synthesis

```yaml
name: pr-review
description: Multi-perspective PR review — correctness, security, architecture, tests, performance.
argument-hint: '[--repo OWNER/REPO] [--focus DIMENSION] [--depth DEPTH] [--comment STYLE]'

model: claude-code/sonnet-4-6
agent: orchestrator
effort: high
max-ops: 25
show-graph: true

args:
  repo:
    type: str
    default: ""
    help: "GitHub repo in OWNER/REPO form (e.g. acme/backend). Leave empty to use the repo in the current directory."
  focus:
    type: str
    default: all
    help: "review dimension: all | correctness | security | architecture | tests | perf"
  depth:
    type: str
    default: normal
    help: "review depth: shallow (summary only) | normal | deep (line-by-line)"
  comment:
    type: str
    default: none
    help: "post comments to GitHub: none | brief (verdict only) | substantive (findings summary) | full (all findings as inline comments)"

prompt: |
  Review the pull request described below. Follow the phased process exactly.

  PR reference: {input}
  Repository:   {repo}
  Focus:        {focus}
  Depth:        {depth}
  Post comment: {comment}

  ─────────────────────────────────────────────
  PHASE 0 — FETCH
  ─────────────────────────────────────────────
  If {repo} is not empty, pass `--repo {repo}` to every `gh` command below.
  Otherwise use the repo detected from the current directory.

  Run in parallel:
    gh pr view {input} --json title,body,author,baseRefName,headRefName,additions,deletions,changedFiles
    gh pr diff {input}

  Read the full diff carefully before spawning reviewers.

  ─────────────────────────────────────────────
  PHASE 1 — PARALLEL SPECIALIST REVIEW
  ─────────────────────────────────────────────
  Spawn one sub-agent per active dimension. If {focus} is not "all",
  spawn only the sub-agent for that dimension. All sub-agents run in
  parallel on the same diff.

  Each sub-agent MUST:
  - Read the full diff before forming any opinion.
  - Rate every finding with a severity: CRITICAL | HIGH | MEDIUM | LOW | INFO.
  - Provide file + line reference for each finding where possible.
  - Propose a concrete fix or mitigation for every CRITICAL and HIGH finding.
  - Distinguish between "must fix before merge" and "nice to have".

  DIMENSION CHARTERS
  ──────────────────
  [correctness]
  - Logic errors, off-by-one mistakes, incorrect conditionals.
  - Race conditions and concurrency hazards.
  - Incorrect error propagation or swallowed errors.
  - Incorrect assumptions about input types or ranges.
  - Behavioral regressions compared to the PR description.

  [security]
  - Injection vulnerabilities (SQL, command, path traversal, template).
  - Authentication or authorisation gaps.
  - Secrets or credentials in code or logs.
  - Insecure deserialization, prototype pollution, SSRF.
  - Dependency additions — flag any with known CVEs.
  - Rate-limiting or DoS surface changes.

  [architecture]
  - Violations of existing module boundaries or layering.
  - Unnecessary coupling introduced between components.
  - API surface changes that break backwards compatibility.
  - Patterns inconsistent with the rest of the codebase.
  - Missed abstractions — repeated logic that should be extracted.

  [tests]
  - Missing tests for new public functions or API endpoints.
  - Tests that do not exercise the stated behaviour change.
  - Flaky test patterns (time-dependent, order-dependent, global state).
  - Inadequate edge case coverage.
  - Missing regression tests for bug fixes.

  [perf]
  - Algorithmic complexity regressions (O(n) → O(n²)).
  - Unnecessary database round-trips or N+1 queries.
  - Blocking I/O in async contexts.
  - Memory allocations in hot paths.
  - Missing caching for expensive repeated computations.

  ─────────────────────────────────────────────
  PHASE 2 — CRITIC SYNTHESIS
  ─────────────────────────────────────────────
  After all sub-agents return, a critic sub-agent synthesises the findings:
  - Deduplicate overlapping findings.
  - Resolve contradictions between sub-agents (state which perspective wins
    and why).
  - Escalate any finding that was rated differently by two reviewers.
  - Produce the final structured report (see format below).

  ─────────────────────────────────────────────
  PHASE 3 — COMMENT (conditional)
  ─────────────────────────────────────────────
  Only execute this phase if {comment} is not "none".

  - "brief"       → post one top-level comment with the verdict and a
                    three-bullet summary of blocking issues.
  - "substantive" → post one top-level comment with the full findings
                    summary (all CRITICAL/HIGH, abbreviated MEDIUM/LOW).
  - "full"        → post one top-level comment with the full findings
                    summary AND one inline review comment per CRITICAL/HIGH
                    finding at the relevant file+line.

  Use `gh pr comment {input} --body "..."` for top-level comments.
  Use `gh pr review {input} --comment --body "..." -F -` for inline reviews.

  ─────────────────────────────────────────────
  FINAL REPORT FORMAT
  ─────────────────────────────────────────────
  Produce this structure in your final response:

  ## PR Review: <title>

  **Verdict**: APPROVE | APPROVE-WITH-FIXES | REJECT

  Verdict criteria:
  - APPROVE          → no CRITICAL/HIGH findings, MEDIUM/LOW are advisory.
  - APPROVE-WITH-FIXES → HIGH findings present but all have clear, small fixes;
                        no CRITICAL findings.
  - REJECT           → any CRITICAL finding, or 3+ HIGH findings without
                        obvious fixes.

  ### Summary
  <2-4 sentence narrative of what the PR does and the overall quality>

  ### Findings

  | Severity | Dimension | File:Line | Description | Fix |
  |----------|-----------|-----------|-------------|-----|
  | CRITICAL | ...       | ...       | ...         | ... |
  | HIGH     | ...       | ...       | ...         | ... |
  | MEDIUM   | ...       | ...       | ...         | ... |
  | LOW      | ...       | ...       | ...         | ... |
  | INFO     | ...       | ...       | ...         | ... |

  ### Positive observations
  <What the PR does well — at least two genuine observations>

  ### Required changes before merge
  <Numbered list of CRITICAL and HIGH items only>
```

Run with `play.submit`. For the custom `focus`/`depth`/`repo`/`comment` args, read the schema
returned by `help={"verb": "play.submit", "playbook": "pr-review"}` — naming the playbook is
what resolves its declared arguments into the reply, and that same call returns the
fingerprint the op has to carry:

```json
{"ops": [{"op": "play.submit", "args": {"playbook": "pr-review", "prompt": "123"}, "schema_fingerprint": "<from that help call>"}]}
```

Inside a lionagi checkout, the same run with those flags set is:
`li play pr-review --focus security --depth deep 123`

Key points:
- `show-graph: true` writes the graph that actually executed after the run finishes. Use a
  separate dry run for a text-only plan preview.
- `max-ops: 25` bounds the initial plan plus reactive follow-ups. If the initial plan uses all
  25 assignments, no follow-up spawn capacity remains.
- `with_synthesis` is not set because the critic sub-agent is written directly
  into the prompt instructions (Phase 2). Use `with_synthesis: true` when you
  want the engine to append a generic synthesis step automatically.
- An empty-string default (`default: ""`) is valid for optional `str` args. The
  template branch `If {repo} is not empty` handles the conditional at runtime.
