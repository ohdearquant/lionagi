# Pattern Composition

Patterns are **composable primitives**, not mutually exclusive boxes.

## Pattern Selection via D(τ,ψ)

| C(τ)    | Class    | Pattern         | Crew                                     |
| ------- | -------- | --------------- | ---------------------------------------- |
| < 0.3   | Trivial  | Expert          | λ alone or 1 α[implementer]              |
| 0.3-0.6 | Standard | P_PAR2 / P_SEQ  | α[implementer] + α[tester]               |
| 0.6-0.8 | High     | P_PAR / P_CHO   | α[implementer] + α[tester] + α[reviewer] |
| ≥ 0.8   | Systemic | P_MULT / P_FLOW | Full crew + α[critic] after              |

## Decision Algebra

```
𝒰(P,ψ) := w₁(ψ)·Speed + w₂(ψ)·Quality − w₃(ψ)·CogLoad
D(τ,ψ) := argmax_{P∈Applicable} [ 𝔼(𝒰(P,ψ)) ]
```

**State adjustments**:
- Under pressure: `w₁↑, w₃↑` (speed, reduce overhead)
- Quality-critical: `w₂↑` (security, proofs, data)
- Low energy: `w₃↑` (simpler patterns)

## Legal Compositions

### 1. P_MULT ⊗ P_PAR — Phases with parallel fan-out

- Phase 1: Parallel discovery (α[researcher] + α[analyst])
- Gate: Consolidate issues
- Phase 2: Parallel fixes (α[implementer] teams)
- Gate: Tests/proofs
- Phase 3: Integration (α[reviewer])

### 2. P_PAR → P_CHO → P_SEQ — Discovery → Decision → Implementation

- Run parallel α[researcher] scouts
- Tournament for architecture choice (α[architect] variants)
- Sequential implementation of winner (α[implementer])

### 3. P_FLOW with P_PAR nodes — DAG with parallel work within each node

- Nodes = crates (dependency order)
- Within node: parallel α[implementer] + α[tester]

### 4. Expert → Escalate — Probe then expand

- 5-min α[researcher] probe to reduce uncertainty
- If uncertainty drops, simpler pattern wins

## Coordination Mapping

| Pattern | Method         | Context  | Sync       | Agent Handoff                  |
| ------- | -------------- | -------- | ---------- | ------------------------------ |
| P_PAR   | `work.assign`  | Shallow  | Barrier    | λ→α[roles] parallel            |
| P_CHO   | `work.assign`  | Isolated | Tournament | λ→α[variants], α[critic] picks |
| P_SEQ   | `work.handoff` | Deep     | Pipeline   | α[A]→α[B] chain                |
| P_MULT  | `work.handoff` | Deep     | Phased     | Gate between phases            |
| P_FLOW  | `work.handoff` | Graph    | DAG        | Topological order              |
| Expert  | `work.assign`  | Minimal  | Await      | λ→single α                     |

## Plan Output Format

Write `plan.kpp`:

```kpp
from: λ
task: reprompt_{hash8}
ws: .khive/reprompt/{slug}_{hash8}/

ctx:
  C: 0.75
  pattern: P_MULT
  max_foreground: 4

phases:
  - {id: P1, name: "Discovery", pattern: P_PAR, agents: [α[researcher], α[analyst]], gate: G1}
  - {id: P2, name: "Fix", pattern: P_PAR, agents: [α[implementer], α[tester]], gate: G2}
  - {id: P3, name: "Integration", pattern: Expert, agents: [α[reviewer]], gate: EXIT}

gates:
  - {id: G1, pass: "issues_listed & evidence_links", on_fail: "fix & re-gate"}
  - {id: G2, pass: "tests_pass & lint_ok", on_fail: "fix & re-gate"}

exit:
  - "All aligned"
  - "Gates pass"
  - "Commits recorded"
```

## Gate Enforcement (NON-NEGOTIABLE)

Gates are BLOCKING checkpoints, not informational logs. When a critic/reviewer
finds issues at a gate, the following cycle is MANDATORY:

```text
critic runs → verdict?
  APPROVE           → proceed to next phase
  APPROVE-WITH-FIXES → fix listed items, proceed (no re-review)
  BLOCK             → STOP → fix ALL critical/major → RE-RUN critic → loop until APPROVE
  REJECT            → phase failed → redesign → redo phase entirely

FORBIDDEN:
  ❌ Critic says BLOCK, lambda says "APPROVE-WITH-FIXES" → FABRICATION
  ❌ Listing critical issues then launching next phase → GATE BYPASS
  ❌ Treating critic output as informational → DEFEATS THE ENTIRE PIPELINE
```

**The fix-and-re-gate cycle**:

```
         ┌─────────────┐
         │ Run Critic   │
         └──────┬───────┘
                │
         ┌──────▼───────┐
         │  APPROVE?    │──yes──→ Proceed to next phase
         └──────┬───────┘
                │ no (BLOCK)
         ┌──────▼───────┐
         │ Spawn fixers │ ← one agent per critical/major issue
         └──────┬───────┘
                │
         ┌──────▼───────┐
         │ RE-RUN Critic│ ← same critic prompt, fresh context
         └──────┬───────┘
                │
                └──→ loop until APPROVE or REJECT
```

**Why this matters**: A critic that finds problems but doesn't block progress is
theater. If the lambda can just note the issues and move on, the critic's effort
is wasted, errors propagate to downstream phases, and the final output is built
on unvalidated foundations. The entire multi-phase pipeline's value comes from
the gates between phases — without enforcement, P_MULT degrades to a single
unreviewed pass.
