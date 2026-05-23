# PR Review Specialist Guide

Full reference for running multi-perspective PR reviews.

## Phase 0 — Fetch context (once, upfront)

```bash
gh auth status                                         # fail fast if unauthenticated
gh pr view <pr_ref> --repo <owner/repo>                # metadata + body
gh pr diff <pr_ref> --repo <owner/repo>                # unified diff
```

Save each output to an artifact dir (e.g. `_context/`) so specialists read
from disk — avoids running `gh pr diff` five times in parallel.

If the PR is in the current repo, omit `--repo <owner/repo>`.

## Phase 1 — Specialist Dimensions (CLOSED set)

Pick dimensions based on what the PR touches. Do NOT invent new dimensions
("deploy", "docs", "style") unless explicitly requested. Drop dimensions the PR doesn't touch.

| Dimension | Looks at |
|-----------|----------|
| correctness | logic bugs, missing error handling, invariant violations |
| security | auth, input validation, data exposure, crypto, supply chain |
| architecture | module boundaries, coupling, abstraction cost, tech debt |
| tests | coverage gaps, missing edge cases, test quality |
| perf | hot paths, allocations, complexity, caching correctness |

Each specialist writes `{role}_review/{role}_findings.md` — a severity x file:line x suggestion
table. No prose, structured data only.

### Running with `li o fanout`

For a quick parallel fan-out where each specialist is independent:

```bash
li o fanout \
  "Review PR #<pr_ref> for correctness only. Diff is at _context/diff.txt. Write findings to correctness_review/findings.md." \
  "Review PR #<pr_ref> for security only. Diff is at _context/diff.txt. Write findings to security_review/findings.md." \
  "Review PR #<pr_ref> for test coverage only. Diff is at _context/diff.txt. Write findings to tests_review/findings.md."
```

### Running with `li o flow` (DAG with synthesis)

For a structured plan with critic synthesis:

```bash
li o flow "
  Phase 0: fetch diff with gh pr diff <pr_ref> and save to _context/diff.txt.
  Phase 1: run correctness, security, and tests specialists in parallel.
  Phase 2: critic synthesises all findings into critic_final/final_synthesis.md.
"
```

The `li o flow` orchestrator plans a DAG, so it will naturally sequence
Phase 0 before Phase 1 and Phase 1 before Phase 2.

## Phase 2 — Discussion (optional)

Only include if dimensions cross-pollinate (security finding affects
architecture, test gap changes severity of a correctness bug). If each
dimension reads cleanly independently, skip straight to critic.

If included: specialists re-read each other's findings; write
`{role}_review/{role}_discussion.md` with corroborations and drops.

## Phase 3 — Critic Synthesis Format

Critic reads ALL Phase 1/2 outputs, writes `critic_final/final_synthesis.md`:

```markdown
# PR Review: <pr_ref>

## Verdict
APPROVE | APPROVE-WITH-FIXES | REJECT · one-line rationale

## MUST-FIX (blocks merge)
- [file:line] description · specialist

## SHOULD-FIX (address before merge or file follow-up)
- ...

## CONSIDER
- ...

## Blind spots (what ALL specialists missed)
- ...

## Coverage
Which dimensions actually ran; which were skipped and why.
```

## Phase 4 — Post Comment

Post with ONE consolidated comment, never one-per-agent:

```bash
tmp=$(mktemp)
printf '%s' "$body" > "$tmp"
gh pr comment <pr_ref> --repo <owner/repo> --body-file "$tmp"
```

Use `--body-file` — inline heredocs escape poorly through `gh`.

On failure (auth, network, rate limit), record both the failure AND the
intended body to `critic_final/post_failure.md` so the reviewer can paste manually.

## Severity Rubric

Uniform across all specialists and critic:

- `CRITICAL` — exploitable now / data loss / production crash / auth bypass
- `HIGH`     — data exposure / sev-2 outage risk / clear security gap not yet exploited
- `MEDIUM`   — correctness bug / meaningful perf regression / spec mismatch
- `LOW`      — style / minor refactor opportunity
- `INFO`     — note for future consideration, no action required

Every finding MUST cite `file:line`. No vague "there are issues."

## Source Code Reference

| File | Purpose |
|---|---|
| `lionagi/cli/orchestrate/fanout.py` | `li o fanout` — flat parallel workers |
| `lionagi/cli/orchestrate/flow.py` | `li o flow` — FlowAgent + FlowOp DAG with critic synthesis |
| `lionagi/cli/orchestrate/_common.py` | AgentRequest schema, worker prompt template |
| `lionagi/cli/orchestrate/_orchestration.py` | Shared setup/finalize, project detection |
| `lionagi/agent/config.py` | AgentConfig presets for specialist agents |
| `lionagi/agent/factory.py` | create_agent() — wires Branch + tools + hooks |
| `lionagi/session/branch.py` | Branch facade — each specialist runs in its own Branch |
| `lionagi/cli/_runs.py` | Run manifest layout: ~/.lionagi/runs/{run_id}/ |
