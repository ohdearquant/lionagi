# Codebase Audit

Fan three workers across your repo in parallel — dead code, API drift, and missing
tests — and get independent findings without sequential bottlenecks.

## Setup

```bash
pip install lionagi          # or: uv add lionagi
# codex — requires ChatGPT Plus/Pro subscription (not an API key):
npm install -g @openai/codex
codex login
```

## Command

```bash
# serialize with --max-concurrent 1 if rate-limited
li o fanout codex "Audit this repo for dead functions, API drift, and untested paths" \
  -n 3 --save ./audit-out
```

```text
# output:
Phase 1: Orchestrator decomposing task into 3 agent requests...
Phase 1 done (2.1s): 3 requests generated.
Phase 2: Fanning out to 3 workers: [codex/gpt-5.3-codex-spark, codex/gpt-5.3-codex-spark, codex/gpt-5.3-codex-spark]
Phase 2 done (9.4s).
Saved 3 worker results to /home/you/project/audit-out
════════════════════════════════════════════════════════════
  Worker 1/3  [codex/gpt-5.3-codex-spark]
[...trimmed...]
```

## How it works

- Workers run concurrently; results write to `./audit-out/worker_1.md` … `worker_3.md`
- Branch snapshots land in `~/.lionagi/runs/<run_id>/branches/`
- Resume any worker after the run with the branch ID printed in the hints:

```bash
li agent -r <branch_id> "List the top 3 dead functions by call-site count."
```

## Next

- [Research synthesis](research-synthesis.md) — add `--with-synthesis` to consolidate findings
- [CLI reference: `li o fanout`](../cli-reference.md#li-o-fanout) — all flags and defaults
