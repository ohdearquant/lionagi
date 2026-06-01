# Orchestration Quality Benchmark

Measures **how good** a lionagi orchestration is — not whether it runs. Distinct
from `benchmarks/` (runtime micro-benchmarks); this grades the *output quality*
of multi-agent coordination.

## Why

A multi-agent chain that produces the same answer a single agent would — or
worse, amplifies a false positive into a confident headline finding — is
theater. Without a benchmark, every "is this orchestration good?" is vibes.
This harness turns it into numbers with error bars.

## How ground truth is manufactured

You don't hand-label every run. Two sources:

1. **Mutation** (`suites/mutation/`) — plant a *known* defect in a clean file →
   a task whose correct answer is known. Keep a clean copy whose only flag-bait
   is *intended* behavior → any Medium+ finding there is a measurable false
   positive. This is mutation testing, applied to review agents.
2. **Git archaeology** (planned) — mine `fix:` commits: the pre-fix tree is the
   task, the diff is ground truth. Free real-world labeled data.
3. **Public benchmarks** (planned) — SWE-bench / τ-bench adapters behind the
   same `Task` interface, so a lionagi number sits next to the market.

## Metrics

| metric | meaning |
|--------|---------|
| **recall** | P(found the planted defect \| mutant task) |
| **FP-avoid** | P(did NOT flag intended behavior Medium+ \| clean task) |
| **severity error** | \|reported − true\| on none/low/med/high/critical |
| **wall** | wall-clock seconds (cost proxy) |
| **lift** | any metric minus the single-agent baseline |

The headline question is **lift vs a single strong agent**. If N agents cost N×
and don't beat 1, the pattern is theater.

## Layout

```
harness/
  config.py   OrchestrationConfig — the variable space (pattern, roles, modes, model, grounding)
  task.py     Task + Label + RunResult + ScoredResult (benchmark-agnostic)
  runner.py   (config, task) -> RunResult   [drives lionagi: single | fanout | flow]
  judge.py    (RunResult, labels) -> ScoredResult   [constrained LLM-judge, hand-validated]
suites/
  mutation/   mutate.py (plant defects) + tasks/ (generated) + labels
run.py        run a matrix (configs × tasks × trials) -> results/*.json
score.py      judge results + aggregate per-config table
```

## MVP experiment

Tests three hypotheses at once:

- **H1** — does multi-agent beat a single agent? (`single` vs `flow_default`)
- **H2** — does an adversarial-mode critic cut false positives?
- **H3** — does design-intent grounding kill intended-behavior false positives?

```
configs: single · flow_default · flow_adversarial_grounded
tasks:   bus_mutant (real break→return defect) · bus_clean (BaseException bait)
trials:  3 each → 18 runs
```

```bash
uv run python benchmarks/orchestration/run.py --smoke   # 1 run, validate plumbing
uv run python benchmarks/orchestration/run.py           # full 18-run matrix
uv run python benchmarks/orchestration/score.py         # judge + report table
```

## Anti-circularity

The judge is **validated against hand labels** before its numbers are trusted,
and it answers a *constrained* question ("does this output match THIS known
label?") not an open "is this good?". All raw outputs are saved so every judge
call can be spot-checked. Never grade an LLM purely with an unchecked LLM.
