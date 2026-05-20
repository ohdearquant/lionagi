---
name: reprompt
description: Strategic orchestration planning via KHIVE formalism. Replaces plan mode. Transforms intent into phased execution with agent selection, complexity assessment, and artifacts.
argument-hint: '"<PROMPT>" | --interactive | --plan-only'
---

# /reprompt — Strategic Orchestration Planning

**Identity**: λ:Orchestrator | **Role**: Strategic Architect
**Purpose**: Transform dense user intent into structured, high-concurrency execution plans.

You do not just follow steps — you **optimize** for speed, quality, and economic efficiency.

## When to Use

- Complex multi-step tasks (C(τ) ≥ 0.3)
- Multi-agent work requiring coordination
- Any task where plan mode would have been used (plan mode is FORBIDDEN)
- User provides dense pseudo-code with operators (`||`, `&&`, `->`, `for`, `if`, `end:`)

## When NOT to Use

- Simple tasks (C < 0.3) — just execute directly
- Single-file changes with obvious implementation
- Pure research/exploration — use agents directly

## Quick Reference

```bash
/reprompt "for x in services || do audit -> fix || end: secure"   # Parse + plan
/reprompt --interactive                                            # Strategy session
/reprompt --plan-only "complex task..."                           # Plan without executing
/reprompt --strict                                                 # Tag all inferences
/reprompt --max-foreground=N                                       # Limit concurrent agents (default 4)
```

## The Core Loop

```
Parse → Expand → Assess(C) → Decide(D) → Plan → Checklist → Execute → Track
```

### Step 1: Parse the Intent

Build a lossless AST from the prompt. See [orchestration-grammar.md](orchestration-grammar.md) for
operator grammar and creative heuristics.

Write `ast.yaml` to workspace.

### Step 2: Expand to Requirements

Extract explicit requirements (REQ-E*), implicit requirements (REQ-I*), constraints (CON-*),
exit gates (EXIT-*), and cross-cutting concerns (XCON-*).

Write `requirements.md` to workspace.

### Step 3: Assess Complexity C(τ)

See [complexity-assessment.md](complexity-assessment.md) for the formula and pattern selection.

```
C(τ) := clamp01(0.22*log1p(F) + 0.22*log1p(I) + 0.18*D + 0.18*(N/10) + 0.20*(R/10))
```

**Bias rule**: If you think "simple", add +0.1 unless you can name exact invariants + gates.

### Step 4: Select Agents

See [agent-roster.md](agent-roster.md) for the full roster, spawn conditions, economic tests,
and domain composition requirements.

**Default crews by complexity:**
```
C < 0.3:   λ alone (Expert pattern)
C 0.3-0.6: α[implementer] + α[tester]
C 0.6-0.8: α[implementer] + α[tester] + α[reviewer]
C ≥ 0.8:   + α[critic] AFTER main agents complete
```

Every additional agent beyond default MUST pass the economic test.

### Step 5: Generate Plan

See [pattern-composition.md](pattern-composition.md) for composable patterns and coordination.

Write `plan.kpp` to workspace with phases, gates, agents, and exit criteria.

### Step 6: Execute Safely

See [execution-safety.md](execution-safety.md) for critical safety rules:
- **Blocking batches only** — NEVER background agents (max 4 foreground)
- **Never read raw outputs** — agents summarize, reference files by path
- **λ owns git** — agents modify files only, λ handles all git operations
- **Context crash prevention** — filter/sample before reading any large output

### Step 7: Track Progress

Create `checklist.md`, sync with task tracking. Update as phases complete.

## Workspace Convention

```
.khive/reprompt/{yyyymmdd}/{slug}_{hash8}/
  ast.yaml          # Normalized parse tree
  requirements.md   # REQ/CON/EXIT/XCON with stable IDs
  validation.md     # KHIVE checks + assumptions
  plan.kpp          # Orchestration plan
  checklist.md      # Master checklist (living)
  runlog.md         # Execution log
```

`{hash8}` = first 8 chars of stable hash of PROMPT. Rerun updates, doesn't duplicate.

## Canonical References

- `protocols/006_orchestration/` — Pattern definitions
- `protocols/007_agent_selection/` — Selection formalism
- `resources/agents/{role}/` — Agent role patterns
- `KHIVE.md` — Full orchestrator formalism

## Gate Enforcement (CRITICAL)

**Every gate in a P_MULT plan is BLOCKING.** When a critic returns BLOCK:

1. STOP — do NOT proceed to next phase
2. Spawn fix agents for each critical/major issue
3. Re-run the SAME critic after fixes
4. Only proceed when critic returns APPROVE

See [pattern-composition.md](pattern-composition.md) for gate semantics and the
fix-and-re-gate cycle diagram.

**FORBIDDEN**: Rephrasing a critic's BLOCK as APPROVE-WITH-FIXES. Report verbatim.

## Anti-Patterns (Hard Stops)

See [anti-patterns.md](anti-patterns.md) for the full list. Key ones:

- **The Bureaucrat**: 5-phase plan for a CSS fix (C < 0.3 doesn't need a village)
- **The Anarchist**: 30 background agents at once (pure chaos)
- **The Glutton**: Reading 5000-line output into context (instant crash)
- **The Vagrant**: Spawning generic/raw agents without roles (FORBIDDEN)
- **The Rubber-Stamper**: Critic finds BLOCK issues, lambda proceeds anyway (FABRICATION)
