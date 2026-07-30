# Teams and Tracking

Team coordination patterns, invocation tracking, and scheduling.

**Writing to a team, and opening or closing an invocation record, are CLI-only today.**
Reading is not: the MCP catalog carries `team.list` and `invoke.list`, so you can see what
exists over MCP and only need `li team` / `li invoke` from inside a lionagi checkout to
change it. The write verbs are in the catalog too, named as ones the server declines with
its reason — `invoke.start` and `invoke.end` because the surface cannot tell that whoever
opened a record is the one closing it. Call `help=true` and read what the catalog says
about a verb before assuming it is missing.

---

## Team Coordination (reads over MCP, writes CLI-only)

Teams enable inter-agent messaging during a flow or fanout. Agents can broadcast
findings or ask peers for clarification. This is orthogonal to which interface submitted
the run — a flow's own agents message each other through the team regardless. From the
outside, `team.list` over MCP shows which teams exist; creating one, showing its messages,
and sending to it are CLI operations.

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

## Invocation Tracking (ADR-0020, reads over MCP, writes CLI-only)

Group multiple sessions spawned by a skill into one parent record, visible
in Studio's `/invocations` page. `invoke.list` over MCP reads those records, and `job.list`
reads recent runs newest-first with a status filter. *Opening and closing* a record is the
CLI's job: the catalog names `invoke.start` and `invoke.end` as verbs it declines, because
the surface cannot tell that the caller who opened a record is the one closing it.

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
subprocesses on a schedule. The engine is a background service, but the schedules
themselves are ordinary records you can manage from a session: the MCP catalog carries
`schedule.*` verbs for listing, reading, validating, creating, triggering,
enabling/disabling, deleting and exporting them, and `li schedule <subcommand>` is the same
surface from a terminal. Studio's `/schedules` page and the REST API are a third view of
the same records, not the only way in. Ask `help='schedule.create'` for the argument shape
before writing one; the fields below describe what a schedule contains.

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
