# ADR-0027: Scheduled Runs and Event-Triggered Invocations

**Status**: Proposed
**Date**: 2026-05-23

## Context

Lion Studio is currently a passive monitor — it observes sessions, runs, and projects but cannot
initiate agent work. To transform Studio into an active operator, we need the ability to:

1. **Schedule recurring agent runs** (cron/interval): "every night at midnight, run perf
   optimization playbook", "every 30 minutes, check for stale sessions"
2. **React to external events** (GitHub polling): "when a new PR is opened on ohdearquant/lionagi,
   run codex review"
3. **Chain conditional follow-ups**: "if the codex review rejects, spawn analysts to triage"

### Competitive Landscape (researched 2026-05-23)

| Platform | Trigger Types | Conditional Logic | State Model |
|---|---|---|---|
| Anthropic Routines | cron + API + GitHub events | None (logic in prompt) | Stateless per run |
| OpenAI Codex Automations | cron + webhook + file upload | None | Thread (stateful) or standalone |
| LangGraph Cloud | cron + webhook | None | Thread-bound or stateless |
| ChatGPT Tasks | Natural language time only | None | Opaque |
| n8n / Zapier | cron + 1200–8000 event triggers | Workflow nodes | Visual DAG |

**No platform offers conditional chaining as a first-class scheduling primitive.** All encode
conditional logic either in the prompt (Anthropic, OpenAI) or in workflow graph nodes (n8n).
Lion Studio can differentiate by offering `on_fail` / `on_success` action DAGs directly on the
schedule definition — composable, recursive, declarative. Everything in Lion is a graph; schedule
chains are no exception.

Anthropic's CronCreate is session-scoped and ephemeral (dies with the terminal session, 7-day
auto-expire). Claude Code Routines are cloud-hosted with a daily cap. OpenAI Codex Automations
and LangGraph Cloud are similarly paywalled hosted services. Lion Studio's scheduler is open
source, persistent, local, uncapped, provider-agnostic, and integrated with the existing
session/invocation model.

### Design Constraints

- Studio server is a single uvicorn process. Scheduler runs **in-process**.
- No public URL assumed — GitHub integration uses **polling**, not webhooks.
- Execution is subprocess-based (`asyncio.create_subprocess_exec`), not in-process import.
- Each fire creates an `invocations` row, linking to child sessions via existing FK model.

## Decision

Add an in-process asyncio scheduler to the Studio server with two new SQLite tables, a service
layer, REST API, and frontend page. The scheduler ticks every 30 seconds and evaluates due
schedules.

### 1. Trigger Types

Three trigger types, matching the patterns that emerged from competitive analysis:

| Type | Config Fields | Evaluation |
|---|---|---|
| `cron` | `cron_expr` (5-field standard) | `croniter.get_next()` from `last_fired_at` |
| `interval` | `interval_sec` (integer seconds) | `last_fired_at + interval_sec <= now` |
| `github_poll` | `github_repo`, `github_filter`, `poll_interval_sec` | HTTP poll → cursor-based new-event detection |

### 2. Action Execution

Each schedule defines an action that maps to a CLI command:

| `action_kind` | CLI Command |
|---|---|
| `agent` | `uv run li agent <model> <prompt> [--agent <name>]` |
| `flow` | `uv run li o flow <model> <prompt>` |
| `fanout` | `uv run li o fanout <model> <prompt>` |
| `play` | `uv run li play <playbook>` |

Subprocesses are spawned via `asyncio.create_subprocess_exec` with `stdout=DEVNULL` and
`stderr=PIPE` (2 KB tail captured on failure). The subprocess writes its own run directory
(`~/.lionagi/runs/`). The `LIONAGI_INVOCATION_ID` environment variable is passed so child
sessions link to the schedule's invocation row.

### 3. Conditional Chains (DAG)

Everything in Lion is a graph. Schedule actions form a DAG with conditional edges — each node
can define `on_fail` and/or `on_success` as action definitions that are themselves graph nodes:

```json
{
  "on_fail": {
    "kind": "flow",
    "model": "claude/sonnet",
    "prompt": "Triage rejected PR #{{pr_number}}: {{pr_title}}",
    "on_fail": {
      "kind": "agent",
      "model": "ollama/qwen3",
      "prompt": "Escalate: both review and triage failed for PR #{{pr_number}}"
    }
  }
}
```

When an action completes, exit code determines which edge to follow:
- `exit_code == 0` → `on_success` (if defined)
- `exit_code != 0` → `on_fail` (if defined)

Chain runs record `chain_parent_id` and `chain_depth`. Safety cap at depth 10 to prevent
runaway recursion from misconfigured chains. This is a DAG of DAGs — a schedule fires a flow,
the flow is a DAG of operations, and the schedule chain itself is a DAG of actions.

### 4. Overlap and Missed-Fire Policies

Borrowed from LangGraph's `multitask_strategy` pattern:

- **`overlap_policy`**: `skip` (default, do not fire if previous run still active) or `allow`
- **`missed_fire_policy`**: `skip` (default, drop missed fires) or `run_once` (fire once on
  startup if overdue). `skip` is correct for a local dev tool — running last night's playbook
  8 hours late is harmful.

### 5. GitHub Polling

- Poll `GET /repos/{owner}/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=20`
- New-event detection via `github_cursor` (ISO-8601 `updated_at` timestamp)
- ETag caching via `If-None-Match` header (304 = no changes, still costs 1 rate-limit point)
- Auth: `gh auth token` subprocess → `GITHUB_TOKEN` env fallback
- Rate-limit awareness: back off when `X-RateLimit-Remaining < 10`
- Prompt template variables: `{{pr_number}}`, `{{pr_title}}`, `{{pr_url}}`, `{{pr_author}}`

### 6. Schema

Two new tables in `state.db`:

**`schedules`** — one row per schedule definition:

```sql
CREATE TABLE IF NOT EXISTS schedules (
  id                  TEXT    PRIMARY KEY,
  name                TEXT    NOT NULL UNIQUE,
  description         TEXT,
  enabled             INTEGER NOT NULL DEFAULT 1,
  trigger_type        TEXT    NOT NULL,     -- 'cron' | 'interval' | 'github_poll'
  cron_expr           TEXT,
  interval_sec        INTEGER,
  github_repo         TEXT,
  github_filter       JSON,
  github_cursor       TEXT,
  poll_interval_sec   INTEGER,
  action_kind         TEXT    NOT NULL,     -- 'agent' | 'flow' | 'fanout' | 'play'
  action_model        TEXT,
  action_prompt       TEXT,
  action_agent        TEXT,
  action_playbook     TEXT,
  action_project      TEXT,
  action_extra_args   JSON    DEFAULT '[]',
  on_success          JSON,
  on_fail             JSON,
  last_fired_at       REAL,
  next_fire_at        REAL,
  missed_fire_policy  TEXT    NOT NULL DEFAULT 'skip',
  overlap_policy      TEXT    NOT NULL DEFAULT 'skip',
  project             TEXT,
  created_at          REAL    NOT NULL,
  updated_at          REAL    NOT NULL
);
```

**`schedule_runs`** — one row per schedule firing:

```sql
CREATE TABLE IF NOT EXISTS schedule_runs (
  id                  TEXT    PRIMARY KEY,
  schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  invocation_id       TEXT    REFERENCES invocations(id),
  trigger_context     JSON    NOT NULL,
  action_kind         TEXT    NOT NULL,
  action_args         JSON    NOT NULL,
  status              TEXT    NOT NULL DEFAULT 'running',
  exit_code           INTEGER,
  chain_parent_id     TEXT    REFERENCES schedule_runs(id),
  chain_depth         INTEGER NOT NULL DEFAULT 0,
  fired_at            REAL    NOT NULL,
  ended_at            REAL,
  error_detail        TEXT,
  created_at          REAL    NOT NULL
);
```

### 7. Scheduler Engine Lifecycle

Attaches to FastAPI's `lifespan` context manager:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await scheduler.start()
    yield
    await scheduler.stop()
```

The engine is a single `asyncio.Task` that:
1. Queries `schedules WHERE enabled=1 AND next_fire_at <= now` every 30 seconds
2. For GitHub schedules: polls the API, fires only when new events are found
3. Fires due schedules (subject to overlap check)
4. Tracks running processes in `dict[schedule_id, run_id]` for overlap detection
5. On subprocess completion: records exit code, evaluates conditional chain

### 8. API Endpoints

```
GET    /api/schedules/                  List schedules
POST   /api/schedules/                  Create schedule
GET    /api/schedules/{id}              Get schedule detail
PATCH  /api/schedules/{id}              Update schedule
DELETE /api/schedules/{id}              Delete schedule
POST   /api/schedules/{id}/enable       Enable schedule
POST   /api/schedules/{id}/disable      Disable schedule
POST   /api/schedules/{id}/trigger      Manual trigger (fire immediately)
GET    /api/schedules/{id}/runs         List runs for this schedule
GET    /api/schedule-runs/{run_id}      Get single run with chain children
```

### 9. Invocation Linkage

Each fire creates an `invocations` row with `skill="scheduled:{schedule.name}"` and
`plugin=schedule.trigger_type`. The subprocess receives `LIONAGI_INVOCATION_ID` as an
environment variable, causing child sessions to set `invocation_id` on creation. This means
the existing invocation detail page in Studio already shows all sessions spawned by a
scheduled run — zero new UI work for session grouping.

### 10. CLI (`li schedule`)

Schedule management from the terminal, without requiring Studio UI:

```
li schedule list                          # List all schedules (enabled/disabled)
li schedule create <name> --trigger cron --cron "0 0 * * *" \
    --action play --playbook perf-opt     # Create a cron schedule
li schedule create pr-review --trigger github \
    --repo ohdearquant/lionagi --poll 300 \
    --action flow --model claude/sonnet \
    --prompt "Review PR #{{pr_number}}"   # Create a GitHub poll schedule
li schedule enable <name>                 # Enable a disabled schedule
li schedule disable <name>               # Disable without deleting
li schedule trigger <name>               # Fire immediately (manual trigger)
li schedule delete <name>                # Remove schedule
li schedule runs <name>                  # Show execution history
```

The CLI writes directly to `state.db` via `StateDB` — it does not require the Studio server
to be running for CRUD operations. However, the scheduler engine (which fires schedules) only
runs inside the Studio server process.

### 11. Agent-Accessible Scheduling

Agents running within sessions can manage schedules via a registered tool. This enables
autonomous scheduling — an agent investigating a codebase can schedule follow-up monitoring,
or a review agent can cancel a scheduled re-review after the fix lands.

The `schedule_tool` is registered like any other lionagi tool:

```python
branch.register_tools([schedule_create, schedule_cancel, schedule_list])
```

This closes the loop: Studio schedules agents → agents schedule more work → those agents
schedule more work. DAG of DAG of DAG.

### 12. Dependencies

Add `croniter>=1.4` to `pyproject.toml`. Zero transitive deps, ~15 KB pure Python. APScheduler
was rejected — it re-implements job store, executor, and event bus infrastructure we already
have in SQLite and asyncio.

### 13. File Map

New files:
```
apps/studio/server/scheduler/__init__.py
apps/studio/server/scheduler/engine.py       # SchedulerEngine, tick loop
apps/studio/server/scheduler/github.py       # GitHub polling, ETag, auth
apps/studio/server/scheduler/subprocess.py   # argv building, spawn, await
apps/studio/server/services/schedules.py     # DB access (CRUD)
apps/studio/server/routers/schedules.py      # REST endpoints
apps/studio/frontend/app/schedules/page.tsx  # Schedule management page
lionagi/cli/schedule.py                      # li schedule CLI subcommands
lionagi/tools/schedule.py                    # Agent-accessible schedule tools
```

Modified files:
```
apps/studio/server/app.py                    # Add lifespan hook, register router
lionagi/state/schema.sql                     # Add schedules + schedule_runs tables
lionagi/state/db.py                          # Add migration columns + schedule CRUD
lionagi/cli/__init__.py                      # Register schedule subcommand
pyproject.toml                               # Add croniter dependency
```

## Consequences

**Positive**
- Studio becomes an active operator, not just a passive monitor
- DAG conditional chains (on_fail/on_success, recursive) are a differentiator vs. every competitor
- Subprocess isolation means schedule failures cannot crash the Studio server
- Full integration with existing invocations/sessions model — no new UI for session grouping
- Open source, provider-agnostic — works with any model (cloud or local), no daily caps
- GitHub polling works without public URL (unlike Anthropic Routines' webhook approach)
- CLI + agent tools enable autonomous scheduling — agents can schedule/cancel their own work
- DAG of DAG composition: schedule → flow → operations, all graphs

**Negative**
- In-process scheduler means Studio server must be running for schedules to fire
- GitHub polling is less responsive than webhooks (min ~60s latency vs instant)
- `croniter` is a new dependency (though minimal)
- Subprocess spawning means each fire has ~2-3s startup overhead (Python + uv)
- Missed fires during Studio downtime are dropped by default (correct for local dev, but
  users may be surprised)

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| APScheduler | Re-implements job store + executor we already have in SQLite + asyncio. Adds ~500 KB dep for a 30-line tick loop. |
| GitHub webhooks | Requires public URL. Lion Studio is a local dev tool — ngrok/tunneling adds friction and security risk. Polling with ETag is 95% as good. |
| Separate scheduler daemon | Extra process management complexity for a local tool. In-process asyncio task is simpler, starts/stops with the server. |
| Flat depth-2 chains | Artificially limits composition. Lion's philosophy is "everything is a graph" — the chain model should be recursive, matching the flow/show DAG pattern. Safety cap at 10 is sufficient. |
| Native imports (no subprocess) | Importing lionagi CLI internals directly would couple scheduler to runtime state, make isolation impossible, and prevent resource cleanup on failure. |

## References

- Anthropic Claude Code Routines: https://code.claude.com/docs/en/routines
- Anthropic CronCreate: https://code.claude.com/docs/en/scheduled-tasks
- OpenAI Codex Automations: https://developers.openai.com/codex/app/automations
- LangGraph Cloud Cron Jobs: https://docs.langchain.com/langsmith/cron-jobs
- ADR-0020 (Skill Invocations): invocation model that schedule_runs integrate with
- ADR-0026 (Project Detection): project scoping for scheduled runs
