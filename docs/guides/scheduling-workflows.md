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

# A command every 15 minutes (the executable goes after a trailing --).
# Command actions are security-gated: the executable name must appear in
# LIONAGI_SCHEDULER_COMMAND_ALLOWLIST (comma-separated) in the daemon's
# environment, must be a bare PATH-resolvable name (no path separators),
# and arguments must be positional — tokens starting with '-' are rejected.
li schedule create command refresh-index --every 15m -- refresh-index nightly

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
updates, and (for rows the file owns) disables schedules atomically.

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
  refresh-index:
    trigger:
      every: 15m
    target:
      kind: command
      executable: refresh-index   # must be in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST
      args: ["nightly"]           # positional tokens only; '-' prefixes rejected
    notify:
      "on": [failed]
      command: "notify-send 'refresh-index failed'"
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

## Event-driven recipe: review every PR change

A github trigger polls a repository and fires once per pull request that changed
since the last poll — new PRs and new pushes to open PRs both count. The event's
fields are available to the agent prompt as `{{var}}` templates: `pr_number`,
`pr_title`, `pr_url`, `pr_author`, `head_sha`, `draft`, `is_same_repo`.

```bash
li schedule create agent pr-review \
  --profile reviewer \
  --prompt 'Review PR #{{pr_number}} ("{{pr_title}}") at {{pr_url}}, head {{head_sha}}.
Read the full diff, then post one review comment: verdict, the head SHA you
reviewed, and any findings with file:line anchors.' \
  --github myorg/myrepo \
  --github-filter '{"draft": false, "same_repo_only": true}'
```

Two filter notes that are really safety notes:

- `"same_repo_only": true` excludes PRs from forks. Fork PRs carry untrusted content —
  code, PR bodies, and comments can all embed instructions aimed at your reviewing
  agent — so keep automated agents off them and route fork PRs to a human instead.
- `"draft": false` skips drafts, so authors can push work-in-progress without
  burning review runs.

Pinning the reviewed `head_sha` into the review output is what makes automated
reviews trustworthy: a verdict is only meaningful for the exact commit it read,
and the next push fires a fresh run with a fresh SHA.

## One-shots and bounded schedules

Three ways to bound how often a schedule fires, and when each fits:

- `--at <instant>` — a point-in-time trigger. Fires exactly once at that instant
  (it implies max-runs 1). The right tool when you know the wall-clock time.
- `--every ... --once` (sugar for `--max-runs 1`) — "fire once, as soon as the
  scheduler picks it up." The idiom for launching a long job detached from your
  terminal: create it with a short interval and `--once`, and the first tick runs
  it.
- `--max-runs N` — a lifetime cap. The schedule auto-disables once N runs have
  fired; re-enabling it later does not reset the counter.

How the budget is counted, because it matters for long-running actions:

- A run consumes budget when it **fires**, not when it finishes. In-flight
  (`running`) and reaped (`timed_out`) runs count, so a one-shot whose action is
  still executing — or whose action timed out — never fires a second time.
- Overlap `skip` rows do not count: while a long one-shot is still executing, an
  interval trigger keeps ticking and records a `skipped` row per tick (visible in
  `li schedule runs`). That is bookkeeping noise, not extra work — the action ran
  once. If the rows bother you, `--at` a real instant instead of polling with an
  interval.

## Measuring schedules: cost, time, and model fit

Every scheduled spawn records what it used: the spawned session rows in
`~/.lionagi/state.db` carry `model`, `effort`, `input_tokens`, `output_tokens`,
`total_cost_usd`, and start/end timestamps, and each `schedule_runs` row links to
its invocation. That makes a schedule a **measurable recipe**: a pinned
combination of task, prompt, model, and effort whose cost and outcome accumulate
run over run.

Read it back with a read-only query:

```bash
sqlite3 "file:$HOME/.lionagi/state.db?mode=ro" "
  SELECT sc.name,
         COUNT(*)                              AS runs,
         ROUND(SUM(s.total_cost_usd), 2)       AS usd,
         SUM(s.output_tokens)                  AS out_tokens,
         ROUND(AVG(s.ended_at - s.started_at)) AS avg_secs
  FROM schedule_runs r
  JOIN schedules  sc ON sc.id = r.schedule_id
  JOIN sessions   s  ON s.invocation_id = r.invocation_id
  WHERE r.fired_at > strftime('%s','now','-7 days')
  GROUP BY sc.name ORDER BY usd DESC;"
```

What to do with the numbers: for each recurring task, start with the cheapest
model tier you believe could do it, and let the run history argue. If a recipe
succeeds consistently, try the next tier down; if it fails or needs rework,
escalate one tier and re-measure. Model routing decided by accumulated per-recipe
evidence beats a static "always use the big model" rule — most recurring
automation (report generation, triage, polling probes, draft passes) is exactly
where smaller models earn their keep, and the schedule is the natural unit to
prove it per task rather than argue it in general.

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
