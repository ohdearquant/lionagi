# Scheduling Workflows

Run agents, commands, playbooks, and flows on a schedule — no chat session, no
babysitting. This guide is the shortest path from nothing to a working scheduled
workflow, using only commands that exist today.

## Prerequisites

Start the Studio daemon once (it hosts the scheduler engine):

```bash
li studio start          # backend on 127.0.0.1:8765
curl -s 127.0.0.1:8765/api/admin/health   # verify it is up
```

## One-off schedules: typed quick-create

The fastest way to schedule a single thing. Each action kind is a subcommand with
only the flags that kind actually needs:

```bash
# An agent run every morning at 06:00 New York time
li schedule create agent nightly-review \
  --profile reviewer --prompt-file review-prompt.txt \
  --cron "0 6 * * *" --timezone America/New_York

# A shell command every 15 minutes (the executable goes after a trailing --)
li schedule create command disk-check --every 15m -- df -h

# A flow document fired once at an absolute instant
li schedule create flow release-pipeline \
  --file pipelines/release.flow.yaml \
  --at 2026-08-01T09:00:00-04:00

# A playbook fired when PRs change on a repository
li schedule create playbook pr-triage \
  --playbook triage --github myorg/myrepo --github-filter '{"state": "open"}'
```

Notes that save debugging time:

- `--cron` requires `--timezone` (an IANA name). Expressions resolve in that
  timezone, DST-aware. There is no silent UTC surprise.
- `--at` demands a full RFC 3339 instant with a UTC offset; it implies max-runs 1.
- The working directory is captured at creation time (`--cwd`, defaulting to where
  you ran the command) and persists with the schedule. Where the daemon was started
  no longer matters for new schedules.
- Guardrails are flags, not afterthoughts: `--max-runs`, `--budget-usd`,
  `--budget-tokens`, `--overlap skip|allow`, `--missed-fire skip|run_once`.

## Recurring automation as one file: ScheduleSet

For anything beyond a couple of ad-hoc entries, declare all schedules in one YAML
document and reconcile it. The file is the source of truth; applying it creates,
updates, and (for rows the file owns) removes schedules atomically.

```yaml
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  nightly-review:
    trigger:
      cron:
        expression: "0 6 * * *"
        timezone: America/New_York
    target:
      kind: agent
      profile: reviewer
      prompt: "Review yesterday's changes and write a summary."
    policies:
      overlap: skip
      budget:
        usd: 2.0
  disk-check:
    trigger:
      every: 15m
    target:
      kind: command
      executable: df
      args: ["-h"]
    notify:
      "on": [failed]
      command: "notify-send 'disk-check failed'"
```

Validate without touching the database, preview the reconciliation, then apply:

```bash
li schedule validate schedules.yaml
li schedule apply schedules.yaml --dry-run
li schedule apply schedules.yaml
```

Notes:

- Quote the notify `"on"` key. Unquoted `on:` is YAML 1.1 boolean `true` and the
  parser will reject the document with a pointed error.
- `notify.on` takes terminal statuses (`completed`, `failed`, ...); the command
  runs once per matching terminal event, and a notify failure never affects the
  run's own outcome.
- Target kinds: `agent`, `command`, `playbook`, `flow`. A schedule this set created
  that later disappears from the file is disabled on the next apply (not deleted —
  its run history stays queryable).

## Did it work?

```bash
li schedule list                 # everything, with enabled state and trigger kind
li schedule status <id>          # "did it work" summary for one schedule
li schedule runs <id> --limit 10 # recent runs
li schedule run <run-id>         # one run in detail
li schedule trigger <id>         # fire now, without waiting for the trigger
li monitor run <run-id>          # block until a run reaches a terminal state
```

For runs nobody watches live, make artifact presence part of your triage: a
`completed` status with a missing artifact is the classic silent failure of any
recurring system.

## Stopping things

```bash
li schedule disable <id>   # keep the row, stop firing
li schedule delete <id>    # remove it
li kill <run-or-session-id>  # stop an in-flight run, reaping detached workers
```

## When something misbehaves

- `li schedule get <id>` shows the resolved trigger and next fire time — check
  `next_fire_at` first; a one-shot whose instant already passed will not fire.
- Schedule spawns run `li` from the schedule's stored working directory. After
  reinstalling lionagi into the environment a schedule uses, run `li --version`
  there once — an import-broken install fails at spawn, which otherwise surfaces
  only as failed runs.
- `li schedule limits` shows the global concurrent-fire cap if runs seem queued.
