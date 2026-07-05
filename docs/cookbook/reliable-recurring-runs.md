# Reliable Recurring Runs

`li schedule` fires agents, playbooks, and flows on cron, interval, or repository-event
triggers. Two execution-context rules are non-negotiable and must be stated in every recurring
harness, verbatim, because violating either fails silently or off-by-hours.

## Rule 1: the daemon's working directory is the run's working directory

The scheduler engine spawns each action inheriting the daemon's cwd. Start the Studio daemon
from the directory your scheduled actions expect to run in, or every agent action fails at
spawn. Verify before trusting a new schedule:

```bash
curl -s 127.0.0.1:8765/api/admin/health
```

## Rule 2: cron expressions resolve in UTC

`0 18 * * *` fires at 18:00 UTC, not local time. Write cron in UTC and always check
`next_fire_at` immediately after creating a schedule:

```bash
li schedule create --trigger-type cron --cron "0 18 * * *" \
  --action-kind playbook --action-target kg-polish
li schedule runs <id>   # sanity-check next_fire_at before walking away
```

A date-pinned one-shot created after its UTC moment has already passed silently skips to the
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
