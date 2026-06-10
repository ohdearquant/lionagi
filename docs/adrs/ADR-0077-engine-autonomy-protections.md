# ADR-0077: Engine Autonomy Protections and the Hypothesis Engine

**Status**: Accepted — implemented
**Date**: 2026-06-09

## Context

ADR-0075 shipped the engine layer: standing reaction machines (research, review,
planning) where casts agents emit typed events and reaction rules spawn more
work. Two pressures now change the requirements:

1. **Workers will be small, cheap, or local models — and agent processes.**
   The economic target is running engines on open-weight models, subscription
   CLI agents (`claude_code`, `codex`, `pi`), or local ones — not frontier API
   calls. Weak workers fail differently: they emit malformed JSON, hallucinate
   fields, forget to emit entirely, and wander off-topic under recursion. The
   engine layer, not the model, must supply quality, direction, and safety.

2. **Autonomous recursion needs hard resource protection.** The only bounds
   were semantic (`max_depth`, topic dedup) and a concurrency semaphore.
   Nothing capped total agent count or wall-clock; a model emitting 50
   plausible sub-topics per node recurses within `max_depth` yet spends an
   unbounded budget. Prompt-level caps ("at most 8") are suggestions weak
   models ignore.

A live-path audit also found a real defect: agent emissions arrive on the bus
as `StructuredOutput` *bundles* (one dynamic model with a field per
capability), and `EngineRun.by_type` returned the envelopes rather than the
typed events — so research/review synthesis crashed in any real run. Unit
tests emitted raw events and never exercised the bundle path.

## Decision

### 1. Bundle-aware queries

`EngineRun.by_type` unwraps both `Signal` envelopes and capability bundles via
the observer's `TypeFilter` matching, returning typed event instances. A
scripted-provider e2e suite (`tests/engines/test_engines_scripted_e2e.py`)
drives every engine through the real `fenced JSON → attempt_extract → bundle →
reaction` path so this class of defect cannot reland.

### 2. Hard resource budget (per run)

- `max_agents` (default 50): the primary recursion bound. `EngineRun.spawn`
  declines new work once exhausted (single `budget_exhausted` notify);
  `make_agent` raises `EngineBudgetError`.
- `deadline_s`: optional wall-clock cap checked at the same points.
- **Graceful degradation**: terminal stages (synthesis, verdict) pass
  `exempt=True` — a budget-capped run still reports everything it gathered
  instead of dying empty.

Layering: budget (hard) > judge gate (advisory) > `max_depth`/dedup (semantic)
> semaphore (concurrency). Prompts are never load-bearing for safety.

### 3. Emission repair loop

In-grant capability blocks that fail schema validation were silently dropped
(`logger.debug`). They now surface as `EmissionRejected` bus events carrying
the validation error and the emitting branch's name. On top of that,
`EngineRun.operate_with_repair(branch, instruction, arrived=..., emits=...)`
re-prompts up to `retries` times when the stage's expected emission did not
arrive, naming the expected top-level keys. This is the difference between a
weak model recovering and its work vanishing — the single highest-leverage
mechanism for the small-model thesis.

### 4. Hierarchical judge gate

Setting `judge_model` arms `Engine.judge(run, eid, subject)` at expansion
points (hypothesis question fan-out, research depth spawn): a cheap judge
agent sees the run's **root objective** plus the candidate item and emits a
`JudgeVerdict` (`PASS`/`REJECT` text fallback for judges too weak to emit).
Reject stops that branch. Errors fail open with a `judge_error` notify —
quality gating is advisory; the budget remains the hard backstop. This is the
direction-control mechanism: off-topic, duplicative, trivial, or unsafe
expansions die at the gate instead of spending budget.

### 5. Per-stage model routing; workers are agent processes

`Engine(models={"extract": "ollama/qwen3", "conclude": "claude_code/sonnet"})`
routes each stage through `model_for(stage)`. Any process lionagi can drive is
a worker: API chat models, CLI agents (`claude_code/...`, `codex/...`,
`pi/...`), or the scripted test provider. Volume stages go to cheap workers;
judgement stages to capable ones.

### 6. Sandboxed tool grants

`make_agent` threads `permissions` and `cwd` into `AgentSpec.compose` and
auto-applies `guard_destructive` (bash) plus path guards (reader/editor) to
any tool-bearing agent (`secure=True` default). The hypothesis validator
defaults to `permissions="safe"` — experiments can measure for real without
an unguarded shell.

### 7. The hypothesis engine (Chain shape)

A fourth engine encoding hypothesis-driven development: every architectural
decision should be a hypothesis with evidence, not taste.

```text
FindingPosted → QuestionRaised → EvidenceCollected → HypothesisFormed
  → ExperimentDesigned → ResultRecorded → ConclusionDrawn → ApplicationMapped
```

- Events carry engine-stamped ids (`F-1`, `Q-3`, …) assigned by a
  first-registered collector observer; agents fill upstream refs from their
  instructions. The reference graph **is** the audit trail; `trace_chains()`
  reconstructs root→terminal chains.
- Conclusions are typed by basis: `empirical | quantitative | theoretical |
  taste` — taste is legitimate but must be labeled, never disguised.
- Applications map conclusions onto a caller-supplied decision register as
  `supports | challenges | qualifies` — the ADR-support link.
- Back-edges are first-class (results post findings; conclusions raise
  follow-up questions), bounded by cycle generation (`max_depth`) + dedup.
- Experiments whose `method` is not in `executable_methods` (default:
  benchmark) queue on `run.pending` with their full spec — a ready-to-run
  queue for CI/humans — instead of being faked.
- `run.export(dir)` writes `chains.json` (typed event graph, for downstream
  knowledge-graph ingestion) and `report.md` (synthesis + evidence trail).

Shapes now: research = Tree, review = Dimensional, planning = Planned-DAG,
hypothesis = Chain.

## Consequences

**Positive**

- Engines are safe to run autonomously on weak workers: hard budgets bound
  spend, the judge bounds direction, repair bounds emission fragility, guards
  bound tool blast radius.
- The live path is tested end-to-end without a model (scripted provider), so
  transport-level regressions surface in CI.
- Evidence chains give architectural decisions a durable, machine-readable
  provenance trail — accumulating experiment-backed support for every choice.

**Negative**

- Judge gates add one cheap call per expansion point (~1:3–1:10 of worker
  calls depending on fan-out).
- Repair adds up to `retries` extra calls per silent stage; bounded and
  observable (`emission_repair` / `emission_missing`).
- The event store still retains everything; the retention/compaction policy
  flagged in ADR-0075 remains open.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Prompt-level caps only ("at most N") | Weak models ignore them; nothing bounds total spend. |
| Fail-closed judge | A flaky cheap judge would kill healthy runs; budget already hard-bounds the damage of failing open. |
| N-vote redundancy instead of a judge | 2–3× worker cost on every stage; weak against correlated errors; no direction control. |
| Constrained decoding for emissions | Only available on some local backends; repair works transport-universally, including CLI agents. |
| Sub-question relevance via embedding similarity | Cheaper than a judge but cannot assess "worth the budget" or safety; may complement the judge later. |

## References

- ADR-0072 — Reactive Capability Bus (the substrate).
- ADR-0075 — Domain-Specific Agent Engines (the layer this hardens).
- `lionagi/engines/engine.py` — budget, judge, repair, routing, sandbox.
- `lionagi/engines/hypothesis.py` — the Chain-shape engine.
- `lionagi/operations/_observe.py`, `lionagi/session/capabilities.py` —
  `EmissionRejected` surfacing.
- `tests/engines/test_engine_protection.py`,
  `tests/engines/test_engines_scripted_e2e.py`.
