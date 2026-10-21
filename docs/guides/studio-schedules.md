# Automate work with schedules

Schedules are durable definitions evaluated by the Studio daemon. Start it
with [the Studio getting-started guide](studio-getting-started.md) before
creating or triggering work.

## Create a schedule

Create a daily CLI-agent action:

```bash
li schedule create daily-review \
  --cron "0 9 * * *" \
  --action-kind agent \
  --model codex \
  --cwd . \
  --prompt "Review the repository and summarize the highest-risk change."
```

The command prints `Created:` followed by the schedule ID and name. Save the ID:

```bash
SCHEDULE_ID=<id-from-create>
```

Inspect and trigger it without waiting for the next cron tick:

```bash
li schedule get "$SCHEDULE_ID"
li schedule trigger "$SCHEDULE_ID"
li schedule runs "$SCHEDULE_ID"
```

`trigger` prints a schedule-run ID when the daemon accepts the fire. Wait for
that run in a script with:

```bash
li monitor run <schedule-run-id> --max-wait 900
```

The wait command exits when the schedule run reaches a terminal status and
maps that status to its process exit code. By default it follows
`on_success`/`on_fail` chain children; add `--no-chain` to wait only for the
literal run ID.

## Use the Schedules page

Open **Schedules** in the left rail. Switch between cards, a table, and a
month calendar to inspect the same schedule definitions. Cards and rows show
whether a schedule is enabled and summarize its trigger and recent activity.
Select a schedule to open its detail dialog.

- Use the enabled toggle to pause or re-enable future scheduled fires without
  deleting the definition or its history.
- Choose **New schedule** to configure cron, interval, or GitHub-poll work.
  The form includes the action, prompt, project attribution, and missed-fire
  and overlap policies. Use the CLI options below for threshold conditions
  and cumulative budget limits.
- In detail, edit the definition and save your changes. **Run now** requests
  a fire without waiting for its next scheduled time. Admission limits still
  apply; a request is not a guarantee that work starts.
- The detail history shows recent fires, their status and duration, and
  expandable error details when available. **Open run** follows a linked
  invocation into the execution view.
- Deletion requires confirmation and removes the schedule definition.

The page and dialogs are implemented by `routes/schedules/index.tsx` and
`components/schedules/` under `apps/studio/frontend/src/`. The CLI and UI use
the daemon's schedules service, `lionagi/studio/services/schedules.py`; CLI
flags and output are defined in `lionagi/studio/cli.py`. Keep the daemon
running for automatic fires.

Run lists, recent-run summaries and single-run records include `scheduled` and
`missed_fire_at`. `scheduled` is true only for a fire of the schedule's own timer
(cron, interval or `at`), including that fire dispatched again after a daemon
restart; manual fires, repository-event fires (`github_poll`), the late run of a
missed fire under `missed_fire_policy=run_once`, chain children, and the rows a
timer tick records without firing (a missed-fire skip, an overlap skip, a deferral
at the concurrent-fire cap, a budget refusal) are false. The
run a restart replaces is marked `failed` and still reads true, so that one timer
fire shows as two runs. For a missed-fire skip, `missed_fire_at` is the stored
intended fire time as epoch seconds, including its fractional part. A stored integer
past 2**53 is null, and a stored decimal with more digits than a float holds is
served as the nearest float. The underlying trigger payload remains private as a
field; the single-run record still serves a run's stored failure text
(`error_detail`, `reason_summary` and `outcome`) as before, and that text can quote
part of the payload when the error or command output that produced it did.

## Other trigger and safety options

The live schedule API supports:

- `--interval SECONDS` for fixed intervals.
- `--trigger-type github --github-repo OWNER/NAME --github-filter JSON` for
  polled GitHub events.
- `--threshold-config JSON` for metric-threshold alerts evaluated on the
  schedule's cadence.
- `--once` or `--max-runs N` to bound the number of fires.
- `--max-cost-usd` and `--max-tokens` to stop future fires after a cumulative
  budget is reached.
- `li schedule limits` to inspect the daemon-wide concurrent-fire cap.

## Expected state

- The schedule definition appears in `li schedule list` and Studio.
- Each fire creates a schedule-run row visible through
  `li schedule runs ID`.
- The action creates its normal session, run state, and artifacts.
- Disabling a schedule preserves its history; deleting removes the definition.

If a schedule command reports a connection failure, start `li studio` or set
`LIONAGI_STUDIO_URL` to the daemon you intend to use. If a trigger is accepted
but no work completes, inspect `li schedule runs ID`, then use
`li monitor run RUN_ID` and `li doctor`.

Next, return to [durable operations](durable-operations.md) for live control and
checkpoint recovery.
