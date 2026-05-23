# Teams and Tracking

Team coordination patterns, invocation tracking, and scheduling.

---

## Team Coordination

Teams enable inter-agent messaging during a flow or fanout. Agents can broadcast
findings or ask peers for clarification.

### Fresh team per invocation (`--team-mode`)

Creates a new team UUID each run. Good for isolated pipelines.

```bash
li o flow claude "Multi-agent code review" \
    --team-mode review-session --save ./out --yolo --bypass
```

### Persistent team across invocations (`--team-attach`)

Loads existing team (preserving message history) or creates it if absent.
Good for long-running iterative workflows.

```bash
# First run: creates the team
li o flow claude "Start the migration plan" \
    --team-attach project-alpha --save ./out --yolo --bypass

# Later runs: reuse the same team, history preserved
li o flow claude "Continue the migration" \
    --team-attach project-alpha --save ./out --yolo --bypass
```

`--team-mode` and `--team-attach` are mutually exclusive.

### Direct team operations

```bash
li team create "my-team" -m "researcher,writer,reviewer"
li team list
li team show my-team
li team send "Found a critical bug" --team my-team --to all --from analyst
li team receive --team my-team --as reviewer
```

### When to use teams

- **Use teams**: agent negotiation, parallel overlapping work, long-running flows
  where agents need to share intermediate findings
- **Skip teams**: purely sequential DAGs, fully independent parallel agents,
  speed-critical work (messaging adds latency)

### Team patterns

- **Negotiation**: parallel agents message each other to resolve conflicting approaches
- **Review loop**: reviewer sends fix requests to implementer via team messages
- **Broadcast**: strategist announces priority changes to all workers

---

## Invocation Tracking (ADR-0020)

Group multiple sessions spawned by a skill into one parent record, visible
in Studio's `/invocations` page.

```bash
# Open an invocation
INV=$(li invoke start --skill orchestrate --prompt "Full security audit")

# Run flows under that invocation
li o flow claude "Audit authentication" --save ./auth-out \
    --invocation "$INV" --yolo --bypass

li o fanout claude "Audit input validation" -n 3 \
    --invocation "$INV" --save ./val-out --yolo --bypass

# Close the invocation
li invoke end "$INV" --status completed

# List recent invocations
li invoke list --skill orchestrate --limit 10
```

Accepted statuses: `completed`, `failed`, `timed_out`, `aborted`, `cancelled`.

`--invocation` is accepted by `li agent`, `li o fanout`, and `li o flow`.

---

## Scheduling (ADR-0027)

The Studio scheduler engine fires `li agent`, `li o flow`, and `li play` as
subprocesses on a schedule. Manage schedules via the Studio UI at `/schedules`
or the REST API.

### Trigger types

- **cron**: standard cron expression (e.g., `0 */6 * * *` for every 6 hours)
- **interval**: fixed interval in seconds (e.g., `3600` for hourly)
- **github_poll**: polls GitHub REST API for new PRs/events, fires on match

### DAG chains

Each schedule can declare `on_success` and `on_fail` to form conditional
follow-up actions. Chains are recursive (DAG of DAGs) with a depth cap at 10.

### Studio integration

- `/schedules` page: list, create, enable/disable, trigger manually
- Schedule runs visible in `/schedules/{id}/runs`
- Each run links to its session in `/runs`

### Source

- Scheduler engine: `apps/studio/server/scheduler/engine.py`
- GitHub poller: `apps/studio/server/scheduler/github.py`
- REST endpoints: `apps/studio/server/routers/schedules.py`
- Schema: `lionagi/state/schema.sql` (schedules + schedule_runs tables)
