# ADR-0021: Skill Artifacts, Structured Output, and Reactive Chaining

**Status**: Proposed
**Date**: 2026-05-21
**Extends**: ADR-0020 (skill invocations), ADR-0009 (SQLite state layer), ADR-0011 (shows)

## Context

ADR-0020 introduced the `invocations` table to track skill-level
orchestration. But three deeper questions remain:

### 1. Where does show data live?

Shows currently have dual persistence:
- **DB**: `shows` + `plays` tables (ADR-0011) — status, metadata, FKs.
- **Filesystem**: `$HOME/khive-work/shows/<topic>/` — prompts, intents,
  verdicts, logs, agent artifacts, worktrees.

The filesystem holds the bulk: `_show.md` (plan), `_intent.md` (per-play),
`_prompt.md` (per-play), `_verdict.json` (per-play gate), `.log` (stdout),
and agent-produced files nested under `<agent_id>/`. These are large,
unstructured, and numerous.

But the **structured parts** — verdicts, play metadata, gate results — are
queryable and displayable. They should be in the DB. The filesystem should
hold only what's too large or too unstructured for a column.

### 2. How do artifacts get into the DB?

Today, `li play` writes `run.json` + `branches/*.json` to
`~/.lionagi/runs/<id>/`. `li state import` backfills them into sessions.
But skill-level artifacts (verdicts, reports, analysis results) have no
ingestion path — they're files that Studio can't see.

The question isn't just "does it need to" — it's "what's the contract
between a skill producing output and Studio displaying it?"

### 3. Where do structured output models live?

Skills produce typed results: codex review produces a verdict
(`gate_passed`, `feedback`, `severity`). Flow control produces
`FlowControlVerdict` (`should_continue`, `reason`, `next_steps`). Research
produces a landscape analysis. But these models are scattered:

- `FlowControlVerdict` → `lionagi/cli/orchestrate/flow.py`
- Gate verdict → inline JSON in show skill markdown
- Review verdict → untyped markdown file

There's no shared location for "the Pydantic models that skills produce
and Studio displays."

### 4. How do skills chain reactively?

The compelling use case: PR opens → codex review runs in a worktree →
produces verdict artifact → if APPROVE, merge; if REQUEST_CHANGES, file
issues. Today this requires a human (Ocean) reading the verdict and
typing the next command. The pieces exist but don't connect.

## Decision

### Part A: Structured output models — `lionagi/outcomes/`

Create `lionagi/outcomes/` as the shared location for skill result types.
These are the **contract** between skill producers and Studio consumers.

```
lionagi/outcomes/
  __init__.py
  _base.py          # SkillOutcome base
  verdict.py        # ReviewVerdict, GateVerdict
  analysis.py       # ResearchAnalysis, ImpactReport
  plan.py           # ShowPlan, FlowPlan (move from cli/orchestrate)
  ci.py             # CIResult, LintResult, TestResult
```

Base type:

```python
from lionagi.models.hashable_model import HashableModel

class SkillOutcome(HashableModel):
    """Base for all structured skill outputs.

    Persisted as an artifacts row with kind=outcome_kind (e.g.,
    'review_verdict', 'gate_verdict', 'ci_result'). The primary
    outcome for an invocation is resolved by querying the latest
    artifact whose kind is a registered outcome type. Studio
    renderers dispatch on artifact.kind directly.
    """
    outcome_kind: str              # "review_verdict", "gate_verdict", "ci_result", ...
    summary: str                   # one-line human-readable result
    passed: bool | None = None     # tri-state: True/False/None(not applicable)
```

Concrete types:

```python
class ReviewVerdict(SkillOutcome):
    outcome_kind: Literal["review_verdict"] = "review_verdict"
    verdict: Literal["APPROVE", "APPROVE_WITH_SUGGESTIONS",
                     "REQUEST_CHANGES", "REJECT"]
    findings: list[Finding]
    round: int = 1

class Finding(HashableModel):
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: str           # "security", "correctness", "style", ...
    file: str | None
    line: int | None
    description: str
    suggestion: str | None

class GateVerdict(SkillOutcome):
    outcome_kind: Literal["gate_verdict"] = "gate_verdict"
    gate_passed: bool
    feedback: str | None
    notes: str | None

class CIResult(SkillOutcome):
    outcome_kind: Literal["ci_result"] = "ci_result"
    lint_passed: bool | None
    tests_passed: bool | None
    build_passed: bool | None
    test_count: int | None
    failure_summary: str | None
```

**Why `lionagi/outcomes/` and not `lionagi/models/`?** The `models/`
package holds infrastructure types (HashableModel, FieldModel, Note).
Outcomes are domain types — the result vocabulary of the skill system.
Separate package, clear boundary.

**Why not in `apps/studio/`?** Outcomes are produced by the CLI and
consumed by Studio. They belong in the shared `lionagi` package, not
in either consumer.

### Part B: Artifact persistence — DB for structured, filesystem for blobs

Add an `artifacts` table for structured skill outputs:

```sql
CREATE TABLE IF NOT EXISTS artifacts (
  id              TEXT    PRIMARY KEY,
  invocation_id   TEXT    REFERENCES invocations(id) ON DELETE CASCADE,
  session_id      TEXT    REFERENCES sessions(id),
  created_at      REAL    NOT NULL,
  kind            TEXT    NOT NULL,         -- "review_verdict", "gate_verdict", "ci_result", ...
  name            TEXT    NOT NULL,         -- human label: "Round 1 verdict", "CI lint"
  content         JSON    NOT NULL,         -- SkillOutcome.model_dump()
  file_path       TEXT                      -- optional: path to large blob on disk
);

CREATE INDEX IF NOT EXISTS idx_artifacts_invocation ON artifacts(invocation_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
```

The split:
- **DB (`content` JSON)**: structured outcomes — verdicts, results,
  analyses. Queryable, displayable, chainable.
- **Filesystem (`file_path`)**: large blobs — full logs, generated code,
  worktree diffs, screenshots. Referenced by path, not stored in DB.

Ingestion path:

```python
# In skill runner (CLI side), after producing a verdict:
from lionagi.state.db import StateDB
from lionagi.outcomes.verdict import ReviewVerdict

verdict = ReviewVerdict(
    verdict="REQUEST_CHANGES",
    findings=[...],
    round=1,
    summary="3 high-severity issues found",
    passed=False,
)

async with StateDB.open() as db:
    await db.insert_artifact(
        invocation_id=inv_id,
        session_id=session_id,
        kind=verdict.outcome_kind,
        name="Round 1 verdict",
        content=verdict.model_dump(),
    )
```

No batch import needed — artifacts are written at production time, not
backfilled. Legacy filesystem artifacts stay where they are; new skills
write to both DB and filesystem.

### Part C: Show data — split structured from bulk

Shows already have DB tables (ADR-0011). The missing piece is persisting
play-level structured outcomes. With the artifacts table, this becomes
natural:

| Show artifact | Where | Why |
|---------------|-------|-----|
| Play gate verdict | `artifacts` table | Structured, queryable, displayable |
| Play metadata (`_meta.json`) | `plays` table columns | Already there (ADR-0011) |
| Show plan (`_show.md`) | `invocations.node_metadata` | The plan is invocation-level state |
| Play intent (`_intent.md`) | filesystem | Large text, rarely queried |
| Play prompt (`_prompt.md`) | filesystem | Large text, skill-internal |
| Agent logs (`.log`) | filesystem | Large, streaming, append-only |
| Agent artifacts (`<agent_id>/`) | filesystem | Unstructured, varied |
| Worktrees | filesystem | Ephemeral, disposable |

The show skill writes gate verdicts as `GateVerdict` artifacts linked to
both the invocation and the play's session. Studio renders them in the
play detail view.

### Part D: Reactive chaining — triggers and hooks

A declarative way to chain skills based on outcomes and events:

```yaml
# ~/.lionagi/chains/pr-review.yaml
name: pr-review-chain
trigger:
  event: pr.opened          # GitHub webhook or poll
  filter:
    base: main
    draft: false

steps:
  - skill: codex-pr-review
    with:
      pr: "{{ trigger.pr_number }}"
      effort: high
    timeout: 30m

  - skill: ci
    condition: "{{ steps[0].outcome.verdict == 'APPROVE' }}"
    with:
      scope: full

  - action: gh-merge
    condition: "{{ steps[0].outcome.passed and steps[1].outcome.passed }}"
    with:
      pr: "{{ trigger.pr_number }}"
      method: squash
    requires_approval: true    # pause and ask Ocean

  - action: gh-comment
    condition: "{{ steps[0].outcome.verdict == 'REQUEST_CHANGES' }}"
    with:
      pr: "{{ trigger.pr_number }}"
      body: "{{ steps[0].outcome.summary }}"
```

#### Chain execution model

Chains are **not** a workflow engine. They are a thin reactive layer:

1. **Triggers** fire from events (webhook, schedule, file watch, manual).
2. **Steps** execute sequentially. Each step is a skill invocation or a
   shell action.
3. **Conditions** are Jinja2 expressions over prior step outcomes.
4. **`requires_approval`** pauses the chain and notifies Ocean. The chain
   resumes when Ocean approves (via Studio UI or CLI).
5. **Timeouts** abort a step if it exceeds the limit.

Chains produce invocations — each step creates an invocation with
`chain_run_id` and `step_index` linking it back to the chain run.
The chain itself is an invocation with `skill='chain'` and
`node_metadata` holding the chain definition + execution state.

Step-to-invocation linkage columns on `invocations`:

```sql
ALTER TABLE invocations ADD COLUMN chain_run_id TEXT
  REFERENCES chain_runs(id);
ALTER TABLE invocations ADD COLUMN step_index INTEGER;

CREATE INDEX IF NOT EXISTS idx_invocations_chain_run
  ON invocations(chain_run_id) WHERE chain_run_id IS NOT NULL;
```

#### Chain persistence

```sql
CREATE TABLE IF NOT EXISTS chains (
  id              TEXT    PRIMARY KEY,
  name            TEXT    NOT NULL,
  definition      JSON    NOT NULL,         -- the YAML parsed to JSON
  status          TEXT    NOT NULL DEFAULT 'active' CHECK(
                    status IN ('active', 'paused', 'disabled')
                  ),
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_runs (
  id              TEXT    PRIMARY KEY,
  chain_id        TEXT    NOT NULL REFERENCES chains(id),
  trigger_event   JSON,                     -- the event that fired this run
  invocation_id   TEXT    REFERENCES invocations(id),  -- root invocation
  status          TEXT    NOT NULL DEFAULT 'running' CHECK(
                    status IN ('running', 'completed', 'failed',
                               'timed_out', 'aborted', 'cancelled',
                               'waiting_approval')
                  ),
  current_step    INTEGER NOT NULL DEFAULT 0,
  step_outcomes   JSON    NOT NULL DEFAULT '[]',  -- array of SkillOutcome dicts
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chain_runs_chain ON chain_runs(chain_id);
CREATE INDEX IF NOT EXISTS idx_chain_runs_status ON chain_runs(status);
CREATE INDEX IF NOT EXISTS idx_chain_runs_invocation
  ON chain_runs(invocation_id) WHERE invocation_id IS NOT NULL;
```

#### Scheduling

Chains with `trigger.schedule` use cron syntax:

```yaml
trigger:
  schedule: "0 2 * * *"     # 2 AM daily
  # or
  schedule: "every 6h"      # simplified syntax
```

The scheduler is a lightweight daemon (`li chain daemon`) or a system
cron entry that runs `li chain check` periodically. It does NOT need to
be always-on — `li chain check` evaluates all active chains' triggers
and fires any that match.

#### Event sources (incremental)

Start with two event sources, add more as needed:

| Source | Mechanism | Events |
|--------|-----------|--------|
| **Schedule** | `li chain check` on cron | `schedule.fired` |
| **Manual** | `li chain fire <name>` | `manual.fired` |

Future (not in this ADR):
- GitHub webhooks (`pr.opened`, `pr.merged`, `issue.created`)
- File watch (`file.changed` in a watched directory)
- Invocation completion (`invocation.completed` with outcome filter)

### Part E: Studio rendering — outcome-aware display

Studio gains outcome-aware rendering for artifacts:

```typescript
// components/outcomes/OutcomeRenderer.tsx
// Dispatches on artifact.kind to render structured outcomes

switch (artifact.kind) {
  case "review_verdict":
    return <ReviewVerdictCard verdict={artifact.content} />;
  case "gate_verdict":
    return <GateVerdictCard verdict={artifact.content} />;
  case "ci_result":
    return <CIResultCard result={artifact.content} />;
  default:
    return <JsonViewer data={artifact.content} />;
}
```

The run detail page shows artifacts inline with the message timeline.
The invocations page shows the final outcome of each step.

#### ReviewVerdict component specification

The `ReviewVerdictCard` renders the structured verdict as a severity/category
breakdown with blocking findings, not plain text:

```
┌────────────────────────────────────────────────────────────────────┐
│ REQUEST CHANGES                                  reviewer · 5m 27s │
├────────────────────────────────────────────────────────────────────┤
│ Findings                                                           │
│ Major 3   Minor 4   Info 0                                         │
│                                                                    │
│ Categories                                                         │
│ ADR consistency 2 · Dependency claims 1 · Terminology 2 · Scope 2  │
├────────────────────────────────────────────────────────────────────┤
│ Blocking findings                                                  │
│                                                                    │
│ MAJOR  ADR-060 depends on wrong ADR                                │
│        docs/adrs/ADR-060.md                                        │
│        Evidence: adjacent ADRs define a different dependency chain. │
│        Required: correct reference or explain exception.            │
│                                                                    │
│ MAJOR  Mis-scoped product verbs                                    │
│        codex_r1_consistency.md                                     │
│        Required: align product verbs with ADR vocabulary.           │
├────────────────────────────────────────────────────────────────────┤
│ Suggestions                                                        │
│ MINOR  Rename "label packs" to match ADR terminology                │
│ MINOR  Add missing cross-reference to ADR-059                       │
└────────────────────────────────────────────────────────────────────┘
```

Fields rendered: verdict label, reviewer + duration, finding counts by severity,
category breakdown, blocking findings with file + evidence + required fix, and
minor suggestions in a collapsed section.

#### GateVerdict component specification

The `GateVerdictCard` renders as an acceptance criteria checklist:

```
┌─────────────────────────────────────────────────────────────────────┐
│ GATE VERDICT: REJECT                                                │
├─────────────────────────────────────────────────────────────────────┤
│ Acceptance criteria                                                  │
│ ✓ backend tests pass                                                 │
│ ✓ no forbidden scope touched                                         │
│ ✕ implementation_1012.md missing from artifact path                  │
│ ✓ lint clean                                                         │
│                                                                     │
│ Blocking reason                                                      │
│ Required artifact was not produced.                                  │
│                                                                     │
│ Next action                                                          │
│ Re-run implementer with artifact path constraint.                    │
└─────────────────────────────────────────────────────────────────────┘
```

Each criterion shows pass/fail with checkmark or X. Blocking reason and
next action are surfaced prominently, not buried in a text field.

#### CIResult component specification

The `CIResultCard` renders as a test/build/lint matrix with command timings:

```
┌─────────────────────────────────────────────────────────────────────┐
│ CI RESULT: PASSED                                                   │
├─────────────────────────────────────────────────────────────────────┤
│ Backend tests     119 / 119                                         │
│ Lint              passed                                            │
│ Typecheck         passed                                            │
│ Build             passed                                            │
│                                                                     │
│ Commands                                                            │
│ pytest apps/studio/server         2m 14s                            │
│ npm run build                     1m 02s                            │
└─────────────────────────────────────────────────────────────────────┘
```

Each check type gets a row with a pass/fail indicator and count where
applicable. Commands section shows the actual commands run with durations.

### Summary: the full stack

```
Chain (pr-review-chain.yaml)
  → Trigger (pr.opened / schedule / manual)
    → Invocation (skill="codex-pr-review", ADR-0020)
      → Sessions (invocation_kind="agent", ADR-0017)
        → Branches → Messages
      → Artifacts (ReviewVerdict, GateVerdict — this ADR)
        → Structured outcome in DB (queryable, displayable)
        → Large blobs on filesystem (referenced by path)
    → Condition check (Jinja2 over prior outcomes)
      → Next Invocation or requires_approval pause
```

## Consequences

**Positive**
- Skills have a typed output contract. Studio knows how to render a
  `ReviewVerdict` vs a `CIResult` without parsing markdown.
- Artifacts are queryable — "show me all failed reviews this week" is a
  SQL query, not a filesystem walk.
- Reactive chaining turns manual multi-skill workflows into declarative
  pipelines while keeping `requires_approval` for safety.
- `lionagi/outcomes/` is the single location for skill result types —
  shared between CLI, Studio, and chains.
- Show data split is explicit: structured → DB, bulk → filesystem.
- Chains are thin — not a workflow engine, just trigger → condition →
  skill invocation.

**Negative**
- New package (`lionagi/outcomes/`) adds to the import surface. Mitigated
  by lazy imports and clear scope boundary.
- Artifact writes add DB operations to skill execution paths. At our
  scale (< 50 artifacts per invocation) this is negligible.
- Chain YAML is another config format to maintain. Mitigated by keeping
  the schema minimal and using Jinja2 (well-known).
- `requires_approval` chains need a notification mechanism (Studio UI
  poll, push notification, or CLI prompt). Deferred to implementation.
- Scheduling requires either a daemon or system cron — neither is
  zero-config. `li chain check` on cron is the simplest.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Store all artifacts in DB (including logs, diffs) | Large blobs degrade SQLite; filesystem is right for multi-MB files |
| Store all artifacts on filesystem only | Structured outcomes lose queryability; Studio can't render without parsing |
| Put outcome models in `lionagi/models/` | Conflates infrastructure types with domain types; separate package is cleaner |
| Put outcome models in `apps/studio/` | CLI produces them; they must be in the shared package |
| Full workflow engine (Airflow/Temporal-style) | Over-engineered for our scale; chains are sufficient for sequential reactive steps |
| Event-driven architecture (pub/sub) | Adds infrastructure dependency; trigger → check → fire is simpler |
| Python-based chain definitions | YAML is declarative and inspectable; Python chains blur the line between config and code |
| Inline outcomes in `invocations.node_metadata` | Metadata is for invocation state; outcomes are first-class entities that multiple sessions contribute to |

## Implementation order

1. `lionagi/outcomes/` — base type + verdict + ci_result (pure models, no deps)
2. `artifacts` table + `StateDB.insert_artifact()` (schema + write path)
3. Studio artifact rendering (frontend components)
4. Retrofit `/codex-pr-review` and `/show` gate to write typed artifacts
5. `chains` + `chain_runs` tables (schema only)
6. `li chain` CLI subcommand (define, fire, check, list)
7. Studio chains page (view chain definitions and runs)
8. Schedule trigger via `li chain check` on cron

Steps 1-4 are immediate value. Steps 5-8 are the chaining layer, which
can be built incrementally.

## References

- [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite state layer
- [ADR-0011](ADR-0011-shows-data-model.md) — Shows data model (plays table)
- [ADR-0020](ADR-0020-skill-invocations.md) — Skill invocations
- [ChatGPT frontend design review](.khive/workspaces/20260521/chatgpt-frontend-review.md) — Kind-based outcome rendering (section 4): ReviewVerdict card with severity/categories/blocking findings, GateVerdict checklist, CIResult matrix
- `lionagi/models/hashable_model.py` — Base model infrastructure
- `lionagi/cli/orchestrate/flow.py` — FlowControlVerdict (existing pattern)
- `~/.lionagi/skills/codex-pr-review/SKILL.md` — Verdict production pattern
- `~/.lionagi/skills/show/SKILL.md` — Gate verdict production pattern

### Prior art

- **autogen Watch Primitives** (`autogen/beta/watch.py`) — EventWatch,
  CadenceWatch, CronWatch, DelayWatch with AllOf/AnyOf/Sequence composites.
  The reactive chaining in Part D is sequential-only; Watch supports
  conditional composition (AllOf = all triggers must fire). Consider
  composite triggers as a future extension.
