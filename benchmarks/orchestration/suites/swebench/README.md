# SWE-bench Verified Mini — real bugs, deterministic oracle

The synthetic mutation suite was a toy: one hand-planted defect, judged by an
LLM that was handed the answer. This suite replaces it with the field standard.

## Why this is the proper benchmark

| Axis | Synthetic mutation | SWE-bench Verified Mini |
|------|--------------------|--------------------------|
| Data | 1 planted defect in our own file | 50 real GitHub bugs (25 django, 25 sphinx) |
| Oracle | LLM judge (leadable, circular) | **test suite in Docker — deterministic, no judge** |
| Ground truth | we wrote it | developer gold patch + held-out tests |
| Discrimination | everything scored 1.0 | cheap models ~20-40%, strong ~70%+ |
| Market-legible | no | yes — comparable to Devin / SWE-agent / HAL |
| Contamination | n/a | post-2019 code, mostly past cheap-model cutoffs |

Source: `MariusHobbhahn/swe-bench-verified-mini` (same difficulty distribution as
the full Verified-500, ~5 GB vs ~130 GB of images).

## Pipeline

```
load.py     50 real instances → benchmark-agnostic Task (cached locally, no Docker)
   ↓
runner      clone repo @ base_commit into an ISOLATED sandbox → lionagi's OWN
            coding agent (AgentConfig.coding: reader/editor/bash/search) resolves
            the issue → git diff = model_patch.  Orchestration patterns
            (single | fanout | flow) coordinate the coder(s).
   ↓
oracle.py   write predictions.jsonl → official `swebench.harness.run_evaluation`
            applies model_patch + test_patch, runs FAIL_TO_PASS / PASS_TO_PASS.
            resolved == all FAIL_TO_PASS pass AND all PASS_TO_PASS still pass.
```

We run **our own coding agent on our own models** (gpt-5.4-mini default, or
deepseek-4.7-flash — cheaper for experimentation), NOT external CLI agents. API
models also report reasoning tokens in `usage`, so cost accounting is exact
(unlike the codex CLI, which hides reasoning from its JSON stream).

## Execution sandbox — Daytona

Per-task isolation is mandatory: agents edit a real repo and run shell commands,
and the local `bash` tool's cwd is per-call (concurrent local tasks would race on
process cwd). Daytona cloud sandboxes give one isolated environment per task,
parallelism, and a clean place to run the oracle. A Daytona-backed execution
backend for lionagi's coding tools is the integration (extends
`lionagi/tools/sandbox.py`, which today only does local git worktrees).

## Status

- [x] `load.py` — 50 real instances → Task, verified, cached
- [x] `oracle.py` — official harness invocation + report parsing, verified
- [x] measurement layer (cost/stats/blind-judge/CIs) — shared `harness/`, reused here
- [ ] Daytona execution backend (pending SDK research)
- [ ] coding-agent runner (single → fanout → flow) on the sandbox
- [ ] first real end-to-end resolved-rate on N instances

## Cost note

Oracle is deterministic (no judge tokens). Agent cost is priced per `harness/cost.py`
(tokens × published price; gpt-5.4-mini = $0.75/$4.50/$0.075 per 1M in/out/cached).
The win condition is reported as **resolved-rate per dollar**, not raw resolved-rate,
so a multi-agent "win" that just spends more tokens is not counted (arxiv 2604.02460).
