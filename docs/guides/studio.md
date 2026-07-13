# Studio and Schedules

Lion Studio is an operational UI backed by a local daemon. Schedules use that
same daemon, so it must be running when triggers are created or fired.

## Install the Studio dependencies

In a project environment:

```bash
uv add "lionagi[studio]"
```

Or with an activated `pip` environment:

```bash
python -m pip install "lionagi[studio]"
```

## Start Studio

The default starts the local API daemon on port 8765 and opens the hosted web
client at `https://lion-studio.khive.ai`:

```bash
li studio
```

The hosted page is the frontend; it connects to the local daemon at
`http://127.0.0.1:8765`. Keep the command running. Use `--no-open` when you do
not want LionAGI to open a browser.

Other shipped modes are:

```bash
li studio --docker       # bundled frontend and backend through Docker
li studio --no-frontend  # local API only
li studio --dev          # source checkout with frontend hot reload
```

In a second terminal, confirm daemon reachability:

```bash
li doctor
```

The `studio_daemon` check should now be healthy.

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
