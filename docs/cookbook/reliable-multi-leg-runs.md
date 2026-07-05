# Reliable Multi-Leg Runs

`li o flow` plans a DAG of agent legs and executes it with dependency-aware parallelism. Since
0.26.15 the planner is structured and loud: an unplannable prompt raises a plan error with a
non-zero exit instead of silently exiting clean, and flags may appear anywhere relative to the
positionals. What remains on the harness author is consuming completion truthfully across
many legs.

## Launch

```bash
li o flow claude/sonnet \
  "Harden the draft spec: one leg per section, then a synthesis leg" \
  --save ./spec-hardening --bypass
```

For repeatable multi-leg work, prefer a spec file (`-f spec.yaml`) or a playbook (`-p name`)
over a long inline prompt.

## One run, many legs — wait on the run

The flow run is the unit of completion, not the individual legs. The completion contract
(ADR-0094) applies to the flow run id: `li wait <flow_run_id>` returns the terminal line with
the run-level `status`, `reason`, and the run directory. Per-leg outcomes live inside the run
directory: the manifest lists each branch/leg with its own artifacts.

Until `li wait` ships, the interim is `li play status <id>` / `li agent status <id>` polling
plus manifest reads — the same tracked-debt workaround as single-agent runs.

## Tail legs are where trust dies

A DAG's last legs (gates, synthesis) run when the budget and patience are thinnest. Two rules:

- **Declare artifact contracts up front.** Flow populates artifact contracts at plan time and
  fails loud on undeclared escalation; keep leg prompts explicit about the file each leg must
  produce so the verifier has something to verify.
- **Check the run-level reason, then the tail leg's artifact.** A run whose reason is
  `run.completed.ok` had its contracts verified; `run.completed_empty.no_evidence` or
  `run.failed.missing_artifact` on a tail leg means the DAG "finished" without its deliverable
  — treat as failure regardless of status.

## Partial failure

Legs that fail don't necessarily fail the run — read the manifest to see which legs produced
artifacts and which did not, and re-run only what's missing (`--resume` on the flow, or a
targeted `li agent -r` on the failed branch) instead of re-paying for the whole DAG.

Next: [Reliable recurring runs](reliable-recurring-runs.md) — the same contract under a
scheduler.
