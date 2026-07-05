# Reliable Review Runs

A review harness launches an agent, waits for it to finish, and acts on the verdict. The
failure mode to design out: treating "the process ended" as "the review exists and is
trustworthy". A run can end `completed` with no evidence written, or die to an external signal
after the verdict file was half-written. The run record knows the difference; consume it.

## Launch

```bash
li agent -a reviewer --bypass --effort high \
  --prompt-file review_prompt.md --save ./review-out
```

Capture the run/session id from the launch output — it is the handle everything below keys on.

## Wait on the record, not the process

The completion contract (ADR-0094) defines `li wait <id>` as the machine surface: one
tab-delimited line per run with `status`, `reason`, `artifact_dir`, and `exit_code`, where
`reason` distinguishes `run.completed.ok` from `run.completed_empty.no_evidence` and
`run.failed.missing_artifact`.

Until `li wait` ships, the honest interim is explicit about being a workaround (this is
tracked debt, not the recommended end state):

- Poll `li agent status <id>` for a terminal status, then
- read the run directory's manifest (`run.json`) for artifact paths, then
- verify the artifact you need actually exists and is non-empty before consuming it.

Never gate on the output file appearing alone: a file can exist before the run is terminal,
and a run can be terminal without the file being complete.

## Act on the reason code

- `run.completed.ok` — consume the artifact from the run directory.
- `run.completed_empty.no_evidence` — the run finished but produced nothing verifiable; treat
  as a failed review, not a clean pass.
- `run.failed.*` / `run.cancelled.*` / `run.timed_out.*` — inspect
  `status_reason_summary` on the record before retrying; an external cancellation
  (`run.cancelled.sigterm`) usually means re-run as-is, while `run.failed.exception` means
  the prompt or environment needs a change first.

## Verify before trusting

A reviewer's "APPROVE" is data, not a decision. Read the review artifact itself; check that it
addresses the diff you asked about (a stale artifact from a previous round is the classic
trap — compare file mtime against the round you fired).

Next: [Reliable multi-leg runs](reliable-multi-leg-runs.md) — the same discipline on a DAG.
