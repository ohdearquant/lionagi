# Reliable Recurring Runs

`li schedule` fires agents, playbooks, and flows on cron, interval, or repository-event
triggers. For creating schedules, start with [Scheduling workflows](../guides/scheduling-workflows.md);
this page covers the reliability rules for runs nobody watches.

## Rule 1: know your schedule's working directory

Each schedule persists its own execution root, captured at creation time (`--cwd`, or the
directory you created it from). The daemon's own cwd only matters for rows created before
execution roots existed, and those log a loud deprecation warning at fire time. Verify the
daemon is up before trusting a new schedule:

```bash
curl -s 127.0.0.1:8765/api/admin/health
li schedule get <id>    # shows the resolved cwd and next fire time
```

## Rule 2: cron expressions resolve in the timezone you name

The typed create commands require `--timezone` with `--cron`; ScheduleSet documents carry
`timezone` next to the expression. Resolution is DST-aware in that zone. Still check
`next_fire_at` immediately after creating a schedule:

```bash
li schedule create playbook kg-polish --playbook kg-polish \
  --cron "0 18 * * *" --timezone America/New_York
li schedule get <id>    # sanity-check next_fire_at before walking away
```

A date-pinned one-shot created after its moment has already passed silently skips to the
next occurrence — for a daily cron, a full day out.

## Rule 3: agent-kind schedules set the model explicitly

Set `action_model` on agent-kind schedules rather than relying on the agent profile's default.

## Completion for runs nobody is watching

Recurring runs finish while you sleep; the record is the only witness. Scheduled runs are the
one kind with a wait surface today: `li monitor run <schedule_run_id>` blocks until terminal
and follows the schedule's success/failure chains. Its line carries status and exit code only
— the reason code and artifact location come from the completion contract (ADR-0094) once
`li wait` lands, which covers scheduled runs with the same frozen line as everything else.

Until then, a morning triage loop over last night's runs:

```bash
li schedule runs <id> --limit 10
```

Then, for anything not plainly successful, read the run directory manifest and check the
artifact exists and is non-empty before treating the night's work as done. A `completed`
status on a recurring run with no artifact is the classic silent rot: the schedule keeps
firing, the artifacts stop appearing, and nothing pages you. Make artifact presence part of
the triage, not an assumption.

Next: [Reliable artifact production](reliable-artifact-production.md) — making sure the
evidence exists when the deadline hits.
