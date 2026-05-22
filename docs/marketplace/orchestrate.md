# orchestrate Plugin

Multi-agent DAG orchestration via `li o flow` and `li o fanout`. Package a complex task as a lionagi flow YAML spec, validate it, fire parallel agents, and monitor execution.

**Source**: `marketplace/orchestrate/`  
**Install**: `claude /plugin install orchestrate@lionagi`  
**Version**: 0.1.0 (Apache-2.0)

## What's Inside

| Asset | Description |
|---|---|
| `/flow-it` skill | Converts a task into a `li o flow` YAML spec; writes, validates, fires, monitors |
| `orchestrator` agent | Plans the DAG, executes phases, passes artifacts, synthesizes results |
| `coordinator` agent | Handles git operations, branch management, commit discipline, progress tracking |

---

## Skill: `/flow-it`

> **Source**: `marketplace/orchestrate/skills/flow-it/SKILL.md`

Package a complex multi-phase task as a lionagi flow YAML spec. Covers two DAG shapes:

- **Feature flows**: multi-agent pipeline for one complex task (architect → implementers → tester → critic)
- **Sweep flows**: embarrassingly parallel per-module audit (N agents, one module each, then consolidate)

**When to use**:

- "flow it", "write a flow", "fan out", "let agents do it"
- "empaco", "codex sweep", "parallel audit", "scan all crates"
- Task is large enough that sequential execution would take >30 min
- Task decomposes into independent subtasks
- Quality-critical work that benefits from multiple perspectives
- Monorepo-wide audit (one module per agent, then consolidation)

**When NOT to use**:

- Simple single-file edits (use Read/Edit directly)
- Debugging sessions (need to stay in the loop)
- Tasks under ~10 min of expected work

### Complexity Thresholds

The skill uses the `C(τ)` complexity score to decide whether a flow is appropriate:

| C(τ) range | Action |
|---|---|
| < 0.3 | Do it directly — no flow needed |
| 0.3–0.5 | Consider flow; direct is usually faster |
| 0.5–0.7 | Flow is a good fit; 4–8 agents |
| ≥ 0.7 | Flow strongly preferred; 6–10 agents with critic |
| Sweep | Any C — N modules × 1 agent each |

### Workflow

**1. Assess fit** using C(τ) thresholds above.

**2. Read context** before writing the spec. Never write a flow spec blind.

**3. (For C ≥ 0.5) Explore lore** — deploy 3–6 parallel suggesters from deliberately unrelated domains (biology, governance, rhetoric, control theory) to enrich the prompt design.

**4. Write the spec** at `{project}/tools/flows/{name}.yaml`:

```yaml
meta:
  name: {task-name}
  version: "1.0"
  description: >
    One-paragraph explanation.

flow:
  agent: orchestrator
  max_agents: 8        # feature: 6-10; sweep: N modules + consolidator
  max_concurrent: 4    # never higher — rate limits dominate
  timeout: 4800
  save_dir_pattern: ".khive/flows/{name}"
  engine: sdk          # ALWAYS sdk — subprocess skips events

gates:
  - phase: post-architect
    require:
      - path: artifacts/design.md
        min_chars: 500

critic_scope: |
  # Independent review mandate — NOT overridable by orchestrator.
  Review ALL implementation artifacts for: correctness, spec compliance,
  security, error handling, and missing edge cases.

lore:
  auto: true
  role: orchestrator
  query: >
    60+ char keyword-rich query for domain context.
  limit: 6

env:
  RUSTC_WRAPPER: ""    # prevents sccache errors

prompt: |
  ## Why this matters
  [Your intent. What daily-driver pain this solves.]

  ## What I want
  [EXHAUSTIVE requirements — edge cases, acceptance criteria. 70% of the prompt.]

  ## Codebase pointers
  [Starting files — where to look, not what to do.]

  ## Constraints
  [Hard rules: worktree path, READ before WRITE, no new deps, no stubs.]

  ## Acceptance
  [Concrete commands + expected outcomes.]
```

!!! warning "Do NOT prescribe phases or agents"
    Do not specify phases, agent assignments, dependency chains, or model routing in the YAML. The `orchestrator` agent designs its own DAG. Global lionagi profiles handle model routing per role.

**5. Set up a worktree**:

```bash
git worktree add -b {branch} ../{project}-{name} main
mkdir -p ../{project}-{name}/tools/flows
cp tools/flows/{name}.yaml ../{project}-{name}/tools/flows/
```

**6. Validate**:

```bash
li o flow -f {path} --dry-run
```

**7. Fire** — **must launch from the worktree directory**:

```bash
cd /path/to/{worktree} && \
  li o flow -f tools/flows/{name}.yaml \
    --background \
    --save .khive/flows/{name} \
    --yolo \
    --bypass
```

**8. Monitor**:

```bash
# Open http://localhost:3000/flows/{id} in Lion Studio for live state
tail -20 /tmp/flow_{name}.log
```

**9. Evaluate + ship**:

```bash
cd {worktree}
git diff --stat main
cargo check --workspace     # or uv run pytest
# commit → push → PR → merge → git worktree remove --force
```

### Sweep Flows (Monorepo Audit)

For per-module parallel audits, describe in the YAML prompt:

1. What to scan for (the audit focus — see tier templates below)
2. The module enumeration strategy
3. The output format per module
4. What cross-module consolidation should produce

Set a `minimum_completion_quorum` in the Constraints section (e.g. "Consolidate when ≥80% of modules complete") to prevent a single slow agent from blocking consolidation.

**Audit tier templates** (embed in the "What I want" section):

=== "Tier 1 — Production Crashes"

    - **panic**: `unwrap()`, `expect()`, `panic!()`, `unreachable!()`, `todo!()` in non-test code — is the invariant truly guaranteed?
    - **dead-path**: code that exists but is never reached
    - **error-swallow**: `let _ = result`, `.ok()` discarding context, `map_err(|_| ...)`

=== "Tier 2 — Silent Corruption"

    - **unit-confusion**: raw numeric types with implicit units (seconds vs millis, bytes vs megabytes)
    - **serde-drift**: `rename_all` inconsistent with DB columns, renamed fields missing `alias`
    - **invariant-drift**: struct fields with implied invariants where mutations don't preserve them

=== "Tier 3 — Product Readiness"

    - **error-ux**: user-visible error paths — do messages help fix the problem?
    - **api-contract**: pub types/traits/fns — provisional signatures, missing docs
    - **observability**: critical ops with no tracing span, error branches that log nothing

=== "Tier 4 — Architecture"

    - **layer-violation**: imports from above, domain logic in wrong layer, circular deps
    - **perf-cliff**: N+1 queries, unbounded growth from user input, blocking I/O in async
    - **cancel-safety**: lock acquired then await before release

**Sweep cost**: ~$0.35–0.50 per module. A 29-crate monorepo runs ~$10–15.

### Important Rules (from SKILL.md)

- Always `engine: sdk` — subprocess engine skips events
- Always `RUSTC_WRAPPER: ""` in env — prevents sccache errors
- Always `max_concurrent ≤ 4` — higher wastes tokens on rate limits
- Always launch from the **worktree CWD** — codex sandbox scopes file writes there
- Do NOT edit the YAML spec while a flow is running
- Do NOT use naked `python` or `pip` — always `uv run`
- Copy YAML specs to worktrees — they branch from `main` before the YAML exists

### When a Flow Fails

| Failure type | Action |
|---|---|
| Transient (rate limit, timeout) | Wait 5 min and retry |
| Agent misunderstood scope | Fix the prompt; re-fire the failed phase from checkpoint |
| Conceptual mismatch | Rewrite the spec entirely |
| Partial completion | Check `.khive/flows/{name}/` — completed agents' work is still usable |

---

## Agent: `orchestrator`

> **Source**: `marketplace/orchestrate/agents/orchestrator.md`

| Field | Value |
|---|---|
| Model | `claude/claude-opus-4-6` |
| Effort | `medium` |
| Yolo | `true` |

**Mission**: Plan DAG → Execute phases → Pass artifacts → Synthesize results.

**Two modes**:

- **Fanout** (`li o fanout`): generates one `AgentRequest` per worker; all workers run in parallel; suitable for simple parallel tasks
- **Flow** (`li o flow`): generates a `FlowPlan` — a DAG of phases for complex staged execution

### DAG Planning

The orchestrator determines how many agents and phases are needed using C(τ):

| C(τ) | Pattern | Agents | Phases |
|---|---|---|---|
| < 0.3 | Expert_α | 1 | 1 |
| 0.3–0.6 | P_SEQ / P_PAR2 | 2–3 | 1–2 |
| 0.6–0.8 | P_PAR / P_CHO | 3–7 | fan-out + critic |
| ≥ 0.8 | P_MULT / P_FLOW | 5+ | multi-phase |

**Model routing by role** (empirically validated 2026-04-19):

| Role | Model | Best For |
|---|---|---|
| explorer / researcher | `codex/gpt-5.3-codex` | Exhaustive scanning, evidence grounding |
| analyst / auditor | `codex/gpt-5.3-codex` | Self-correction, data-heavy cross-referencing |
| architect | `codex/gpt-5.3-codex` high | Scope judgment, design decisions |
| implementer / tester | `claude/claude-sonnet-4-6` | Reliable code edits without fabrication |
| coordinator | `claude/claude-sonnet-4-6` | Git branch/commit/merge |
| critic | `claude/opus` high | Adversarial gate — always LAST |

!!! warning "Implementers must never be the first wave"
    gpt-5.3-codex finds problems with file:line precision; Sonnet executes fix specs reliably. Skipping the analysis phase and going straight to implementers results in fabricated fixes (codex) or incomplete coverage (Sonnet scanning without context). Minimum viable pipeline: `explorer → analyst → implementer → critic`.

### Artifact Handoff Protocol

Every agent instruction must name specific artifacts:

```text
Read the explorer inventory at ../e1/inventory.md.
Cross-reference with ../e2/comparison.md.
Write gap_analysis.md in your current directory.
The implementer in the next phase will use this to write fixes.
```

Rules:
- File paths are relative to `save_dir`: `../e1/inventory.md`
- Critic reads ALL prior artifacts, not just the last phase
- Use descriptive filenames (`gap_analysis.md` not `output.md`)
- Never rely on context passing for large outputs — agents must write files

### Critical Sequencing Rules

- `depends_on` is mandatory for every non-root op
- Critics run AFTER all producers — never in parallel
- Root ops (explorer, researcher from raw source) may have empty `depends_on`
- All other ops must declare ≥ 1 upstream dep

**Forbidden anti-patterns**:

```text
❌ Empty non-root depends_on — will race past upstream agents
❌ Critic in parallel with producers — critic runs LAST
❌ Over-decomposing simple tasks — C < 0.3 doesn't need a DAG
❌ Vague instructions: "help with auth" — must specify what artifact to produce
❌ Invented role names — agent IDs must match declared FlowAgent entries
```

---

## Agent: `coordinator`

> **Source**: `marketplace/orchestrate/agents/coordinator.md`

| Field | Value |
|---|---|
| Model | `claude/claude-sonnet-4-6` |
| Effort | `high` |
| Yolo | `true` |

**Mission**: Git operations, branch management, commit discipline, progress tracking.

Lightweight structural agent — handles git plumbing so implementers can focus on code.

**Capabilities**:

- Create and switch branches
- Stage, commit, push (conventional commit format)
- Check CI status, merge PRs
- Report progress (file counts, test results, build status)
- Coordinate handoffs between implementation phases

**Constraints**:

- No code writing — only git and shell operations
- No code review — delegate to `critic`/`reviewer`
- No architectural decisions — delegate to `orchestrator`/`architect`
- Keeps context minimal — reads paths and status, not full file contents

**When to use in a flow**:

- As the "git backbone" in multi-phase flows where implementers write code and coordinator handles branch logistics
- When a flow needs periodic `cargo check`, `npm run build`, or test runs between phases
- To merge lane PRs in dependency order after review approval

**Use with `li agent`**:

```bash
li agent -a coordinator "Create branch feat/new-api, stage all changes in src/, commit with message 'feat(api): add endpoint'"
```
