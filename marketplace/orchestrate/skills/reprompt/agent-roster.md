# Agent Selection

## How to Spawn Agents

Use the `Task` tool with `subagent_type` matching the role name. Agents default to **Sonnet** (set in `.claude/agents/` frontmatter). Opus reserved for orchestrators only.

```python
# Example: spawn an implementer
Task(
    description="Fix auth middleware",
    prompt="...",
    subagent_type="implementer",   # matches α[implementer]
    model="sonnet"
)
```

## Full Agent Roster

All 13 roled agents map directly to `subagent_type` values in the Task tool:

| Role / subagent_type | Identity                          | Spawn Condition                                | Never Spawn When       |
| -------------------- | --------------------------------- | ---------------------------------------------- | ---------------------- |
| **implementer**      | Production-quality code builder   | Always (if code changes needed)                | N/A                    |
| **tester**           | Test-driven validation specialist | Code changes exist                             | Docs/config only       |
| **reviewer**         | Artifact reviewer (PRs, reports, docs against standards) | C ≥ 0.6, artifact review                  | Ad-hoc/experimental    |
| **critic**           | Adversarial quality gate (runs AFTER main agents) | C ≥ 0.7, P_MULT/P_FLOW, P0 risk | Every PR, routine      |
| **researcher**       | Exhaustive information gatherer with provenance    | Novel domain, foundational gaps  | Standard features      |
| **architect**        | System design specialist, clean interfaces         | Topology change, new boundaries, κ > 0.3 | CRUD, single-file |
| **analyst**          | Experiment-driven validator with statistical rigor | Perf SLO, algorithm choice, data | Premature optimization |
| **theorist**         | Formal proof specialist, mathematical rigor        | Lean proofs, safety-critical, type theory | Application code  |
| **auditor**          | Security/compliance enforcer, vulnerability ID     | Auth, crypto, PII, compliance    | Internal tools         |
| **strategist**       | Strategic orchestration planner, decision algebra  | Complex pattern selection, resource alloc | Simple tasks      |
| **innovator**        | Breakthrough concept generator, cross-domain       | Novel solutions, paradigm shifts | Incremental changes    |
| **commentator**      | Constructive feedback (吐槽+鼓励+给想法)             | Work needs honest reactions, ideas | No feedback needed     |
| **suggester**        | Pre-orchestration verbose sampling (3×∥ for λ) | Big task decomposition, orchestration planning | Execution tasks        |

### Agent Capabilities

Each agent type has different tool access:

| Agent Type | Can Edit/Write | Can Run Bash | Can Spawn Sub-agents | Best For |
|------------|---------------|--------------|---------------------|----------|
| implementer | Yes | Yes | No | Code changes, fixes, features |
| tester | Yes | Yes | No | Test writing, validation, verification |
| reviewer | Yes (read-heavy) | Yes | No | Code review, quality gates |
| critic | Yes (read-heavy) | Yes | No | Adversarial review, missed-risk scan |
| researcher | No (read-only) | Yes (read) | No | Information gathering, web search |
| architect | Yes | Yes | No | System design, module structure |
| analyst | Yes | Yes | No | Benchmarks, experiments, data analysis |
| theorist | Yes | Yes | No | Formal proofs, type theory |
| auditor | Yes | Yes | No | Security review, compliance check |
| strategist | Yes | Yes | No | Orchestration planning, resource allocation |
| innovator | Yes | Yes | No | Novel solutions, cross-domain synthesis |
| commentator | Yes (read-heavy) | Yes | No | Honest feedback (吐槽), encouragement, ideas |
| suggester | Read-only | Read-only | No | Pre-orchestration space exploration (3×∥) |

### Built-in Agent Types (USE SPARINGLY)

These are Claude Code built-ins. Prefer roled agents above, but these are acceptable for specific narrow uses:

| subagent_type | When Acceptable | When NOT to Use |
|---------------|-----------------|-----------------|
| **Explore** | Quick codebase search when you need fast file/pattern discovery | Deep research, implementation |
| **general-purpose** | Multi-step tasks needing full tool access when no specific role fits | When a roled agent clearly fits |
| **Bash** | Simple command execution | Anything requiring judgment |
| **haiku** model | Quick, trivial lookups (via `model: "haiku"`) | Anything requiring quality/judgment |

**Note**: `Plan` subagent_type is effectively dead — plan mode is forbidden system-wide.

## Default Crew by Complexity

```
C < 0.3:   λ alone or 1 implementer (Expert pattern)
C 0.3-0.5: implementer + tester
C 0.5-0.7: 3× suggester (recommended) → implementer + tester + critic (after each phase)
C > 0.7:   3× suggester (MANDATORY) → λ decides plan → implementer + tester + critic (between ALL phases) + cross-challenge round
```

## Orchestration Rules (MANDATORY)

**RULE 1: C ≥ 0.5 → 3× suggesters for complex/high-stakes/large-scope/innovative work**
Deploy 3× suggester in parallel BEFORE orchestrating. **Run in background** (`run_in_background: true`) — don't block the user waiting. Each explores solution space independently. Lambda reads all 3 when notified, decides phases/sequencing/fallbacks, THEN orchestrates. 3 Sonnet suggesters save Opus context and collectively produce better plans than Opus alone. **C > 0.7 = MANDATORY. C 0.5-0.7 = strongly recommended.** Don't wait for the user to ask — deploy proactively when the task qualifies.

**RULE 1a: Ground suggesters in source code, not summaries.**
When dispatching suggesters, include 2-3 key source files (handlers, data models, relevant modules) alongside the task description. Suggesters reading only a summary produce vague strategies ("wire semantic search") instead of grounded proposals ("add MATCH clause to channel_service.rs line 142").

**RULE 1b: C ≥ 0.7 → cross-challenge round.**
After 3× initial suggestions, run a quick second round: each suggester reads the other two proposals and writes a 1-paragraph rebuttal identifying the weakest assumption in each. This catches risks no individual suggester found alone. Costs ~10K tokens extra, prevents consensus blindness.

**RULE 2: Multi-phase → critic between ALL phases**
Critic reviews after EACH phase, not just at the end. Feedback must be deliberately addressed (APPROVE → proceed, APPROVE-WITH-FIXES → fix first, REJECT → rework). Ignored findings = orchestration failure.

**RULE 3: Critic is common, not rare**
Include critic for any C ≥ 0.5. It catches what implementers miss — use early, use often.

## Conditional Specialists (Justify Each)

| Specialist  | Trigger                                  | Economic Test              |
| ----------- | ---------------------------------------- | -------------------------- |
| architect   | `arch_decision ∨ κ>0.3 ∨ new_boundaries` | Would design fail without? |
| theorist    | `formal_proofs ∨ safety_critical`        | Are proofs mandatory?      |
| auditor     | `security ∨ auth ∨ crypto ∨ PII`         | Is audit required?         |
| researcher  | `novel_domain ∨ foundational`            | Is prior art missing?      |
| analyst     | `perf_slo ∨ algorithm_choice`            | Is measurement needed?     |
| innovator   | `breakthrough_needed ∨ cross_domain`     | Is novelty required?       |
| strategist  | `C ≥ 0.8 ∨ pattern_ambiguity`            | Is orchestration complex?  |
| commentator | `feedback_needed ∨ fresh_perspective`    | Would honest 吐槽 improve this? |
| suggester   | `pattern_ambiguity ∨ approach_selection`  | Need verbose sampling?     |

## Suggester Pattern (Verbose Sampling)

For high-uncertainty decisions, spawn 2-3 suggesters with different framings:

```python
# Same task context + framing tag. Background. That's it.
ctx = "[task description + 2-3 key source files]"
Agent(subagent_type="suggester", prompt=f"FRAMING: Simplify\n\n{ctx}", run_in_background=True)
Agent(subagent_type="suggester", prompt=f"FRAMING: Risk\n\n{ctx}", run_in_background=True)
Agent(subagent_type="suggester", prompt=f"FRAMING: Strategic\n\n{ctx}", run_in_background=True)
# λ gets notified when each completes, reads all 3, synthesizes plan

# Cross-challenge round (C ≥ 0.7):
Agent(subagent_type="suggester", prompt=f"FRAMING: Cross-Challenge\n\nProposal A: ...\nProposal B: ...\nProposal C: ...\n\nFor each: what is its weakest assumption?", run_in_background=True)
```

Use suggesters BEFORE spawning implementers when the path is unclear.

**Default triple**: Simplify + Risk + Strategic (empirically validated — good coverage with genuine adversarial tension between Simplify/Strategic and Risk).

## Economic Test (For Every Agent Beyond Default)

```
spawn_agent(role) iff:
  1. unique_value: What does this agent uniquely provide?
  2. failure_mode: What fails if we omit this agent?
  3. cost_justified: Is marginal contribution > coordination cost?
```

**If any answer unclear → DON'T SPAWN**

## Domain Composition Requirement

Every roled agent MUST compose domains before executing:

```python
mcp__khive__lore(action="suggest", query="detailed task description 10+ words", role="${role}", limit=8)
mcp__khive__lore(action="compose", ids=["${selected_ids}"], rerank_query="refined focus")
```

This is mandatory, not optional. Without domain composition, agents lack the specialized knowledge atoms for their role.

## Handoff Format (λ → α)

```kpp
from: λ
to: α[implementer]
task: reprompt_{hash8}_scope
ws: .khive/reprompt/{slug}_{hash8}/

ctx:
  C: 0.72
  domains_composed: ["{domain_ids}"]
  focus: [alignment, minimal churn]
in: [checklist.md, requirements.md]
success: [tests_pass, lint_ok, gates_pass]
t_est: 30m
```

## Status Report (α → λ)

```kpp
from: α[tester]
to: λ
task: reprompt_{hash8}_scope
sts: ok|part|fail

quality: {test: 92, docs: part, types: strict, lint: ok}
modified: [src/lib.rs, tests/integration.rs]
out:
  - {file: runlog.txt, desc: "test output", summary: "47 pass, 0 fail"}
t_act: 25m
```
