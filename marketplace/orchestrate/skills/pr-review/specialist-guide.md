# PR Review Specialist Guide

Full reference for running multi-perspective PR reviews.

## Phase 0 — Fetch context (once, upfront)

```bash
mkdir -p _context
gh auth status                                                     # fail fast if unauthenticated
gh pr view <pr_ref> --repo <owner/repo> > _context/pr.txt          # metadata + body
gh pr diff <pr_ref> --repo <owner/repo> > _context/diff.txt        # unified diff
```

Specialists read these files from disk, which avoids running `gh pr diff`
five times in parallel.

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

### Running with `fanout.submit`

For a quick parallel fan-out where each specialist is independent, call the plugin's
MCP tool, `mcp__plugin_orchestrate_lion__request`, with one `fanout.submit` op per
dimension. Ask for the catalog first, then ask for the verb schema in a separate call:

```json
{"help": true}
```

```json
{"help": "fanout.submit"}
```

Help and operations cannot share one call. Copy the returned `schema_fingerprint` into
each op as a sibling of `args`; a nested or omitted fingerprint is not read, so the op is
refused with `stale_schema` and starts nothing. The schema is authoritative for every field:

```json
{
  "ops": [
    {
      "op": "fanout.submit",
      "args": {
        "query": ["<model-spec>"],
        "prompt": "Review PR #<pr_ref> for correctness only. Diff is at _context/diff.txt. Write findings to correctness_review/correctness_findings.md.",
        "cwd": "/absolute/path/to/your/checkout",
        "num_workers": 1
      },
      "schema_fingerprint": "<from help='fanout.submit'>"
    },
    {
      "op": "fanout.submit",
      "args": {
        "query": ["<model-spec>"],
        "prompt": "Review PR #<pr_ref> for security only. Diff is at _context/diff.txt. Write findings to security_review/security_findings.md.",
        "cwd": "/absolute/path/to/your/checkout",
        "num_workers": 1
      },
      "schema_fingerprint": "<from help='fanout.submit'>"
    },
    {
      "op": "fanout.submit",
      "args": {
        "query": ["<model-spec>"],
        "prompt": "Review PR #<pr_ref> for test coverage only. Diff is at _context/diff.txt. Write findings to tests_review/tests_findings.md.",
        "cwd": "/absolute/path/to/your/checkout",
        "num_workers": 1
      },
      "schema_fingerprint": "<from help='fanout.submit'>"
    }
  ]
}
```

Inside a lionagi checkout, the CLI equivalent is one `li o fanout` per dimension. The command
takes `[MODEL] PROMPT`, so three prompts on one invocation is not a shorter way of writing
this — it fails argument parsing before any review starts:

```bash
li o fanout <model-spec> -n 1 "Review PR #<pr_ref> for correctness only. Diff is at _context/diff.txt. Write findings to correctness_review/correctness_findings.md."
li o fanout <model-spec> -n 1 "Review PR #<pr_ref> for security only. Diff is at _context/diff.txt. Write findings to security_review/security_findings.md."
li o fanout <model-spec> -n 1 "Review PR #<pr_ref> for test coverage only. Diff is at _context/diff.txt. Write findings to tests_review/tests_findings.md."
```

These CLI commands run one after another. The MCP request submits its three ops in order;
each successful run then continues independently in the background. Check every op's `ok`,
retain each `result.run_id`, and observe each run separately. A run id is a handle, not review
output. Ask for `help='job.status'` and `help='job.output'` in separate calls before filling
their arguments:

```json
{"ops": [{"op": "job.status", "args": {"run_id": "<run_id>"}}]}
```

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

### Running with `flow.submit` (DAG with synthesis)

For a structured plan with critic synthesis, describe the whole task in one prompt
and let the planner build the DAG. Read this verb's schema in a separate call first:

```json
{"help": "flow.submit"}
```

```json
{
  "ops": [
    {
      "op": "flow.submit",
      "args": {
        "query": ["<model-spec>"],
        "prompt": "Review PR #<pr_ref> from _context/pr.txt and _context/diff.txt. Use independent correctness, security, and test-coverage specialists, then a critic that consumes their findings and writes critic_final/final_synthesis.md.",
        "cwd": "/absolute/path/to/your/checkout"
      },
      "schema_fingerprint": "<from help='flow.submit'>"
    }
  ]
}
```

`flow.submit` asks the planner to declare real data dependencies; prompt formatting does not
guarantee a particular edge. When exact ordering matters, first submit the same task with
`dry_run: true`, inspect the plan with `job.output`, then submit it without `dry_run`. For an
executing run, `job.wait` observes a bounded window and may need to be called again before
`job.output` contains the final result:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
```

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

Check each op's `ok`; one failed op does not stop its siblings.

Inside a lionagi checkout, the CLI equivalent is `li o flow`:

```bash
li o flow <model-spec> "Review PR #<pr_ref> from _context/pr.txt and _context/diff.txt. Use independent correctness, security, and test-coverage specialists, then a critic that consumes their findings and writes critic_final/final_synthesis.md."
```

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
gh pr comment <pr_ref> --repo <owner/repo> \
  --body-file critic_final/final_synthesis.md
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
| `lionagi/cli/orchestrate/flow.py` | `li o flow` — `TaskAssignment` planning, dependency-graph execution, optional synthesis |
| `lionagi/cli/orchestrate/_common.py` | Worker prompt and team helpers, plus the shared worker `operate` node builder |
| `lionagi/cli/orchestrate/_orchestration.py` | Shared setup/finalize, project detection |
| `lionagi/agent/spec.py` | AgentSpec presets for specialist agents |
| `lionagi/agent/factory.py` | create_agent() — wires Branch + tools + hooks |
| `lionagi/session/branch.py` | Branch facade — each specialist runs in its own Branch |
| `lionagi/cli/_runs.py` | Run manifest layout under `$LIONAGI_HOME/runs/{run_id}/` (default `~/.lionagi/runs/{run_id}/`) |
