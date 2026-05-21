---
name: flow-it
description: >
  Orchestrate complex multi-phase tasks via li o flow YAML specs. Use when
  task has multiple independent subtasks (C(τ) ≥ 0.5), would benefit from
  parallel agents, or when the user says "flow it", "write a flow for X",
  "let's flow this", "fan out", "let agents do it", "empaco", "codex sweep",
  "parallel audit", "scan all crates". Writes spec, validates, fires,
  monitors. Covers both multi-agent DAG orchestration AND embarrassingly
  parallel per-module sweeps.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# flow-it — Multi-Agent Orchestration via li o flow

Package a complex task as a lionagi flow YAML spec, validate it, fire it, and
monitor execution. Covers two DAG shapes:

- **Feature flows**: multi-agent DAG for one complex task (architect → implementers → tester → critic)
- **Sweep flows**: embarrassingly parallel per-module audit (N agents, one module each, then consolidate)

Both use the same lionagi flow engine. The orchestrator decides which DAG shape
to use based on your intent. Your job: describe WHAT exhaustively, not HOW.

## When to Use

- "flow it", "write a flow", "fan out", "let agents do it", "多agent搞"
- "empaco", "codex sweep", "parallel audit", "scan all crates"
- Task is large enough that sequential execution would take >30 min
- Task decomposes into independent subtasks
- Quality-critical work that benefits from multiple perspectives
- Monorepo-wide audit (one module per agent, then cross-module consolidation)

## When NOT to Use

- Simple single-file edits (use direct Read/Edit)
- Debugging sessions (you need to stay in the loop)
- Anything under ~10 min of expected work

## Workflow

### 1. Assess Fit

- C < 0.3: just do it directly
- C ∈ [0.3, 0.5): consider flow; usually direct is faster
- C ∈ [0.5, 0.7): flow is a good fit, 4–8 agents
- C ≥ 0.7: flow is strongly preferred, 6–10 agents with critic
- **Sweep**: N modules × 1 agent each, regardless of C per module

### 2. Read Context Before Writing Spec

NEVER write a flow spec blind. Read the relevant files first so the spec
references accurate paths, existing patterns, and real constraints.

### 2b. Explore Lore for Prompt Design (C ≥ 0.5)

For complex flows, deploy 3–6 parallel suggesters exploring lore from
**deliberately unrelated domains** before writing the spec. Each suggester
gets a different angle (biology, governance, rhetoric, control theory, etc.)
and searches lore for cross-domain patterns applicable to the task.

```
suggesters (diverge, 3-6 parallel) → synthesis (converge) → inform prompt
```

This step produces insights that make the "What I want" section dramatically
richer. A flow prompt informed by stigmergic coordination patterns, judicial
review structures, or hermeneutic interpretation theory will produce better
agent output than one written from software engineering intuition alone.

Skip for C < 0.5 or routine flows where the pattern is already established.

### 3. Write the Spec

Place it at `{project}/tools/flows/{name}.yaml`.

```yaml
meta:
  name: {task-name}
  version: "1.0"
  description: >
    One-paragraph explanation.

flow:
  agent: orchestrator
  max_agents: 8          # feature: 6-10; sweep: N modules + consolidator
  max_concurrent: 4
  timeout: 4800
  save_dir_pattern: ".khive/flows/{name}"
  engine: sdk
  # Model routing handled by global lionagi profiles — do NOT set here.
  # Policy: orchestrator=sonnet-4.6-high, critic=opus-4.7-high,
  # implementer/tester=sonnet-4.6-high. Never use *-max variants.

gates:
  # Artifact checks between DAG phases. Agents MUST NOT start until gates pass.
  - phase: post-architect
    require:
      - path: artifacts/design.md
        min_chars: 500
  # Add per-flow gates, e.g.:
  # - phase: post-implementer
  #   require:
  #     - path: artifacts/impl_summary.md

critic_scope: |
  # Independent review mandate — set by spec author, not orchestrator.
  # The orchestrator may NOT narrow or override this scope.
  Review ALL implementation artifacts for: correctness, spec compliance,
  security, error handling, and missing edge cases. Verify against the
  original "Why this matters" and "Acceptance" sections, not just the
  intermediate architecture.

lore:
  auto: true
  role: orchestrator
  query: >
    Keywords for domain context (60+ chars, keyword-rich).
  limit: 6
  max_tokens: 4000
  # Parallel agents (especially suggesters) should get DELIBERATELY DIFFERENT
  # compose queries. Varied lore injection produces diverse perspectives.
  # Orchestrator: assign each parallel agent a distinct angle.

env:
  RUSTC_WRAPPER: ""

prompt: |
  ## Why this matters
  [Ocean's intent. What daily-driver pain this solves.]

  ## What I want
  [EXHAUSTIVE requirements. Every behavior, edge case, acceptance
   criterion. 70% of the prompt lives here. Be LONG.]

  ## Codebase pointers
  [Starting files — where to look, not what to do with them.]

  ## Constraints
  [Hard rules: worktree path, READ before WRITE, no new deps,
   no stubs, scope boundary.]

  ## Acceptance
  [Concrete commands + expected outcomes.]
```

**CRITICAL: Do NOT prescribe phases, agent assignments, dependency chains,
or model routing.** The orchestrator designs its own DAG. Global lionagi
profiles handle model routing per role.

### 4. Set Up Worktree

```bash
# {base_branch}: usually 'main' or 'master'. Detect with:
# git symbolic-ref refs/remotes/origin/HEAD | sed 's|.*/||'
git worktree add -b {branch} ../{project}-{name} {base_branch}
mkdir -p ../{project}-{name}/tools/flows ../{project}-{name}/tools/rubrics
cp tools/flows/{name}.yaml ../{project}-{name}/tools/flows/
```

### 5. Validate

```bash
li o flow -f {path} --dry-run
```

### 6. Fire

**CRITICAL: Launch from the WORKTREE directory.** Codex sandbox scopes file
writes to the launcher's CWD.

```bash
cd /path/to/{worktree} && \
  li o flow -f tools/flows/{name}.yaml \
    --background \
    --save .khive/flows/{name} \
    --yolo \
    --bypass
echo "Flow saved to: .khive/flows/{name}"
```

### 7. Monitor + Diagnose

```bash
# Preferred: open http://localhost:3000/flows/{id} in the lionagi flow monitor
# for live agent state, branch timelines, and per-agent output.
tail -20 /tmp/flow_{name}.log
# Silent failure diagnosis: check .khive/flows/{name}/ for checkpoint artifacts.
```

#### When Flow Fails

- **Checkpoint-resume**: lionagi persists completed branches to `.khive/flows/{name}/`.
  Check what committed successfully before deciding what to re-fire.
- **Salvage partial results**: Read `.khive/flows/{name}/` artifacts before abandoning.
  Completed agents' work is still usable even if later phases failed.
- **Dead-letter queue**: Agents that hard-crashed → check log for last tool call,
  reproduce manually, patch spec, re-fire only the failed phase.
- **Retry vs rewrite**: Retry on transient failures (rate limit, timeout, sandbox
  error). Rewrite the spec on conceptual failures (agent misunderstood scope, wrong
  decomposition, underspecified acceptance). Rate limit → wait 5 min then retry.
  Wrong output → fix the prompt, don't blindly re-fire.

### 8. Evaluate + Ship

```bash
cd {worktree}
git diff --stat $(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|.*/||' || echo main)
RUSTC_WRAPPER='' cargo check --workspace
RUSTC_WRAPPER='' cargo clippy --workspace -- -D warnings
# Commit → Push → PR → Merge → git worktree remove --force
```

Clean stale worktrees immediately after merge.

---

## Sweep Flows (Monorepo Audit)

For per-module parallel audits, the YAML prompt should describe:
1. What to scan for (the audit focus)
2. The module enumeration strategy (e.g., "every crate with >600 LOC")
3. The output format per module
4. What cross-module consolidation should produce

The orchestrator will fan out one agent per module and consolidate results.

Set `minimum_completion_quorum` in your prompt's Constraints section to control
consolidation behavior. Example: "Consolidate when ≥80% of modules complete —
do not wait for stragglers beyond the timeout." Without a quorum, a single slow
or failed agent blocks the entire consolidation phase.

### Audit Prompt Templates

Include the relevant template in your YAML prompt's "What I want" section.
The orchestrator will decompose into per-module agents.

#### Tier 1 — Production Crashes

**panic**: Find every `unwrap()`, `expect()`, `panic!()`, `unreachable!()`,
`todo!()` in non-test code. For each: is the invariant truly guaranteed?
Could a plausible input trigger it? Hot path or cold path?

**dead-path**: Code that exists but is never reached — match arms for
unconstructed variants, feature-gated code that's always on/off, handlers
for operations nobody dispatches.

**error-swallow**: `let _ = result;`, `.unwrap_or_default()` on Results,
`map_err(|_| ...)` discarding context, `if let Ok(x)` with no else branch,
`.ok()` converting Result to Option.

#### Tier 2 — Silent Corruption

**unit-confusion**: Raw numeric types carrying implicit units — seconds vs
millis, bytes vs megabytes, cents vs dollars. Flag arithmetic mixing units.

**serde-drift**: `#[serde(rename_all)]` inconsistent with DB columns,
renamed fields missing `#[serde(alias)]`, `#[serde(skip)]` on fields
callers expect in JSON.

**invariant-drift**: Struct fields with implied invariants (sorted, non-empty,
monotonic, normalized) where mutations don't preserve the invariant.

#### Tier 3 — Product Readiness

**error-ux**: User-visible error paths — does the message help fix the
problem? Does it leak internals? Is there a recovery action?

**api-contract**: Pub types/traits/fns — provisional signatures, missing
docs, breaking-change risks (enums without `#[non_exhaustive]`).

**observability**: Critical ops with no tracing span, error branches that
log nothing, ops that could take >100ms with no timing.

#### Tier 4 — Architecture

**layer-violation**: Imports from above (foundation importing platform),
domain logic in wrong layer, circular deps via feature flags.

**perf-cliff**: N+1 queries, unbounded growth from user input, blocking I/O
in async, O(n²) on growable collections.

**cancel-safety**: Lock acquired then await before release, partial writes
before await, MutexGuard across await.

**inference-opt**: Unnecessary allocations per-request, clone where
borrow/Arc suffices, SIMD-unfriendly layouts, missing batch opportunities.

#### Tier 5 — Competitive Moat

**unsafe-audit**: Every unsafe block — has SAFETY comment? Is justification
correct? Could it be eliminated with a safe alternative?

#### Special

**archive-delta**: Compare archived code against live codebase. What's
already ported, partially ported, or genuinely unported and valuable?

### Sweep Cost

~$0.35-0.50 per module. Full khive monorepo (29 crates): ~$10-15.
~3% of weekly rate limit per full sweep. Can run 4-5 modes per day.

---

## Important Rules

- **NEVER use naked `python` or `pip`**. Use `uv run`.
- **ALWAYS launch from the WORKTREE CWD** — codex sandbox scopes writes there.
- **ALWAYS use `engine: sdk`** — subprocess engine skips events.
- **ALWAYS set `RUSTC_WRAPPER: ""`** in env to prevent sccache errors.
- **NEVER prescribe phases/agents/deps** — let the orchestrator plan.
- **Global profiles handle model routing** — do NOT specify models in YAML or prompts.
  Policy: orchestrator=sonnet-4.6-high, critic=opus-4.7-high, implementer/tester=sonnet-4.6-high.
  Never use *-max variants.
- **READ BEFORE WRITE** — state this in constraints.
- **Do NOT edit files while flow is running**. The YAML spec is locked at launch —
  edits mid-run produce undefined behavior. Engine-level spec-hash enforcement is planned.
- **max_concurrent ≤ 4** — higher wastes tokens on rate limits.
- **Copy YAMLs to worktrees** — they branch from `{base_branch}` before YAMLs exist.
- **Symlink carefully**: `ln -s SRC DEST` where DEST doesn't exist. NEVER
  `ln -sf` where DEST is an existing directory — creates self-referential loop.

## Reference Specs

- `tools/flows/` in any lionagi checkout — start here for real-world examples
- Feature flows: multi-agent DAG for complex tasks (architect → implementers → tester → critic)
- Audit flows: per-module sweep patterns for monorepo-wide quality gates
