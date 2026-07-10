# ADR-0070: Studio scheduling and dispatch delivery

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: scheduling-control-plane
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0027, v0-0058

## Context

Studio starts one in-process `SchedulerEngine` with the FastAPI lifespan. The engine owns trigger
evaluation, fire-time admission, immediate child-process launch, run finalization, bounded inline
follow-ups, outbound dispatch scans, and one host task-worker tick. These concerns share a timer,
but they do not share one state machine.

Schedules persist `cron`, `interval`, or `github_poll` triggers. Cron and interval schedules may
also carry a metric threshold; in that form the cadence evaluates a condition and fires only on a
breach. A manual fire is an operation over an existing schedule, not a fourth trigger type.

Scheduled actions are launched immediately and awaited by the scheduler task. They enter
`schedule_runs` as `running`, never as `queued`, and do not acquire worker leases. The generalized
table also holds ad-hoc queued rows, but ADR-0071 records that separate path.

`dispatch_outbox` is another sibling hosted by the same tick. It provides producer-driven durable
outbound delivery with deduplication, guarded claims, bounded retry/backoff, optional
acknowledgement, expiry, and dead letters. It does not execute LionAGI work.

This ADR answers five problems:

- **P1 — Trigger evaluation needs a host lifecycle.** Studio needs one loop with defined startup,
  missed-fire, maintenance, and shutdown behavior.
- **P2 — A due fire needs explicit admission.** Overlap, cumulative budget, run count, threshold
  cooldown, and global concurrency must be decided before launch.
- **P3 — Subprocess construction crosses a trust boundary.** Stored schedule fields become argv,
  cwd, environment, and process-group state and must be validated without a shell.
- **P4 — Follow-up recursion can become unbounded or unobservable.** Success/failure continuations
  need a depth cap and parent linkage even though they are not first-class dependency rows.
- **P5 — Outbound notifications need durability independent of task execution.** A notification
  transport failure must be retryable without reopening or changing the producer's run status.

| Concern | Decision |
|---|---|
| Engine lifecycle and tick order | D1: Run one 30-second in-process scheduler loop under Studio lifespan. |
| Trigger and fire admission | D2: Persist three trigger types and apply missed-fire, overlap, threshold, budget, run-count, and global-slot gates before launch. |
| Invocation and subprocess execution | D3: Record invocation/run facts, construct argv from a closed vocabulary, launch a new process group, and terminalize from exit outcome. |
| Follow-up behavior | D4: Execute inline `on_success`/`on_fail` actions recursively with parent linkage and depth 10. |
| Dispatch delivery | D5: Keep a separate at-least-once outbox with guarded delivery attempts and bounded acknowledgement. |

Out of scope:

- Scheduled fires are not currently routed through the leased queue; ADR-0072 owns that target.
- The scheduler is not a distributed leader-elected service. One Studio process is assumed.
- Schedule budgets are not a general usage ledger and do not select fallback models.
- Follow-ups are not independently addressable dependency entities and have no separate
  cancel/retry command.
- Dispatch is not task admission, a trigger, or a live executor-control channel.
- An external notification protocol is not fixed; `dispatch.notify_template` is configuration.

## Decision

### D1 — Studio owns one in-process 30-second loop

Studio lifespan starts and stops the module singleton:

```python
# lionagi/studio/scheduler/engine.py
class SchedulerEngine:
    def __init__(self, svc: SchedulerStateService | None = None) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def fire_now(self, schedule_id: str) -> str | None: ...

scheduler = SchedulerEngine()

_TICK_INTERVAL = 30
_MAX_CHAIN_DEPTH = 10
_DEFERRED_RECORD_EVERY = 10
```

`lionagi/studio/app.py` calls `scheduler.start()` before serving requests and
`scheduler.stop()` during lifespan teardown. `start()` recomputes enabled cron schedule fire times
under the current configured timezone, then creates the tick task. The loop checks missed fires
once, then repeats `_tick()` followed by a 30-second sleep.

Per-tick order is:

```text
1. periodic lifecycle reapers, when their interval elapsed
2. periodic state-database checkpoint, when its interval elapsed
3. due dispatch_outbox delivery scan
4. host task-worker heartbeat/reap/claim pass
5. enabled schedule evaluation
```

Relevant settings and defaults are:

```python
# lionagi/studio/config.py
MAX_SCHEDULED_CONCURRENT = int(
    os.environ.get("LIONAGI_STUDIO_MAX_SCHEDULED_CONCURRENT", "4")
)
INVOCATION_DEADLINE_SECONDS = int(
    os.environ.get("LIONAGI_STUDIO_INVOCATION_DEADLINE_SECONDS", "7200")
)
REAPER_INTERVAL_SECONDS = int(
    os.environ.get("LIONAGI_STUDIO_REAPER_INTERVAL_SECONDS", "300")
)
SCHEDULER_TZ = os.environ.get("LIONAGI_SCHEDULER_TZ") or _system_local_tz_name()
CHECKPOINT_INTERVAL_SECONDS = int(
    os.environ.get("LIONAGI_STUDIO_CHECKPOINT_INTERVAL_SECONDS", "3600")
)
```

Exact lifecycle semantics:

- Startup cron recomputation skips already-due rows so missed-fire policy sees them first.
- An invalid configured IANA timezone logs a warning and falls back to UTC rather than stopping the
  scheduler.
- One tick concern failing logs independently; dispatch or worker failure does not prevent schedule
  evaluation in that tick.
- A top-level `_tick()` exception is logged and the loop sleeps before retrying.
- Fires are background tasks tracked in `_fire_tasks`; shutdown cancels the tick, cancels every
  tracked fire, awaits them with `return_exceptions=True`, and clears the set.
- Child processes are terminated by their cancellation path (D3), so scheduler shutdown does not
  intentionally orphan their process groups.
- Schedules do not fire while Studio is down. Startup applies only the stored `missed_fire_policy`.

The 30-second tick is also the minimum normal latency for dispatch and the host task queue because
neither has another sleep loop. Source comments treat it as an accepted local latency/load tradeoff
but record no measurement for the exact value. The 300-second reaper, 3600-second DB checkpoint,
two-hour invocation deadline, and default concurrency of four are configurable inherited defaults;
their exact numeric tuning is not justified by measurements in this source area.

Why this way: one local loop makes Studio deployment simple and gives all hosted maintenance work a
bounded lifecycle. The tradeoff is availability and coupling inside `SchedulerEngine`: a Studio
outage suspends triggers and the large engine class coordinates several policies.

Code anchors: `lionagi/studio/app.py` (`lifespan`); `lionagi/studio/scheduler/engine.py`
(`SchedulerEngine.start`, `_tick_loop`, `_tick`, `stop`); `lionagi/studio/config.py`.

### D2 — Three trigger types pass explicit fire gates

The persisted schedule contract is:

```sql
-- lionagi/state/schema.sql (selected columns)
CREATE TABLE schedules (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL UNIQUE,
  enabled             INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  trigger_type        TEXT NOT NULL
                      CHECK(trigger_type IN ('cron', 'interval', 'github_poll')),
  cron_expr           TEXT,
  interval_sec        INTEGER,
  github_repo         TEXT,
  github_filter       JSON,
  github_cursor       TEXT,
  poll_interval_sec   INTEGER,
  action_kind         TEXT NOT NULL
                      CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play', 'flow_yaml')),
  action_model        TEXT,
  action_prompt       TEXT,
  action_agent        TEXT,
  action_playbook     TEXT,
  action_flow_yaml    TEXT,
  action_project      TEXT,
  action_extra_args   JSON DEFAULT '[]',
  on_success          JSON,
  on_fail             JSON,
  last_fired_at       REAL,
  next_fire_at        REAL,
  missed_fire_policy  TEXT NOT NULL DEFAULT 'skip'
                      CHECK(missed_fire_policy IN ('skip', 'run_once')),
  overlap_policy      TEXT NOT NULL DEFAULT 'skip'
                      CHECK(overlap_policy IN ('skip', 'allow')),
  max_runs            INTEGER,
  budget_usd          REAL,
  budget_tokens       INTEGER,
  project             TEXT,
  threshold_config    JSON,
  last_alert_at       REAL,
  last_healthy_poll_at REAL,
  poller_consecutive_401 INTEGER NOT NULL DEFAULT 0,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);
```

The HTTP create model mirrors these columns:

```python
# lionagi/studio/services/schedules.py
class CreateScheduleRequest(BaseModel):
    name: str
    description: str | None = None
    trigger_type: str
    cron_expr: str | None = None
    interval_sec: int | None = None
    github_repo: str | None = None
    github_filter: dict | None = None
    poll_interval_sec: int | None = None
    action_kind: str
    action_model: str | None = None
    action_prompt: str | None = None
    action_agent: str | None = None
    action_playbook: str | None = None
    action_flow_yaml: str | None = None
    action_project: str | None = None
    action_extra_args: list[str] | None = None
    on_success: dict | None = None
    on_fail: dict | None = None
    missed_fire_policy: str = "skip"
    overlap_policy: str = "skip"
    max_runs: int | None = None
    budget_usd: float | None = None
    budget_tokens: int | None = None
    project: str | None = None
    threshold_config: dict | None = None
```

Creation requires the trigger and action kind. Cron requires a valid `cron_expr`; interval requires
a positive integer `interval_sec`; GitHub polling requires `github_repo`; `flow_yaml` requires
non-empty inline YAML. `max_runs`, `budget_usd`, and `budget_tokens` must be positive when supplied.
Cron expressions are interpreted in `SCHEDULER_TZ`, while `next_fire_at` remains a UTC epoch.

The management CLI currently exposes creation choices `agent`, `playbook`, and `flow_yaml`, while
the table admits `agent`, `flow`, `fanout`, `play`, and `flow_yaml`. Creation stores the literal CLI
value; it does not map `playbook` to `play`. The only alias mapping exists later in
`subprocess.build_argv()`. Consequently `playbook` can fail the table constraint before fire time.
This mismatch is current behavior, not an intended alias contract.

Top-level fire admission order for cron/interval is:

```text
threshold evaluation/cooldown, when configured
  → overlap gate
  → cumulative token/cost gate
  → max_runs reservation
  → daemon-wide concurrency-slot reservation
  → tracked fire task
```

Exact gate semantics:

- **Missed fire `skip`:** startup writes a skipped run with evidence and advances
  `next_fire_at`.
- **Missed fire `run_once`:** startup reserves a future `next_fire_at` before creating one recovery
  task. If reservation fails, it does not also queue recovery; the immediately following normal
  tick may own the due fire.
- **Overlap `skip`:** if the schedule id is already in the engine's in-memory `_running` map, write
  a skipped run and advance cadence. `allow` bypasses this check.
- **Threshold within bounds:** advance cadence without creating a run.
- **Threshold breach in cooldown:** advance cadence without a run. Cooldown equals
  `window_minutes * 60`; an in-memory reservation prevents two ticks from racing before
  `last_alert_at` persists.
- **Budget exhausted:** cumulative prior session cost or tokens disables the schedule before a new
  fire. It does not interrupt a run already in flight and can overshoot by one run.
- **`max_runs` exhausted:** top-level terminal run count plus in-process claims refuses the fire and
  disables the schedule. Inline chain children do not consume the count.
- **Global capacity exhausted:** automatic fires remain due and retry next tick. A skipped/deferred
  record is written on the first deferral and every tenth deferral thereafter to avoid row spam.
- **Manual fire:** applies budget, max-run, and global-slot checks; capacity is rejected to the
  caller rather than deferred. It returns a generated 12-hex run id once the task is scheduled.
- **GitHub polling:** uses its stored cursor and polling interval (default fallback 300 seconds) and
  reserves capacity before advancing cursor for an event that will fire.

The max-run and global-slot handles release idempotently from `_fire()`'s `finally`, including
cancellation or failure before a run row is written. This closes in-process reservation leaks; it
does not provide multi-process coordination.

Why this way: the gates make refusal/defer reasons explicit and prevent a due fire from being
silently dropped. The cost is policy concentration in one engine and single-process correctness
for reservations.

Code anchors: `lionagi/studio/services/schedules.py`; `lionagi/studio/scheduler/engine.py`
(`_check_missed_fires`, `_maybe_fire`, `_reserve_max_runs_budget`, `_reserve_global_slot`,
`_check_budget`); `lionagi/state/schema.sql`.

### D3 — A scheduled fire is an immediate isolated child process

The run and invocation rows are created by `SchedulerEngine._fire_inner()`. A valid fire first
creates an invocation in `running`, builds argv, creates a `schedule_runs` row in `running`, stamps
a transition reason, advances schedule cadence, then awaits the child process.

The current schedule-run columns are:

```sql
CREATE TABLE schedule_runs (
  id                    TEXT PRIMARY KEY,
  schedule_id           TEXT REFERENCES schedules(id) ON DELETE CASCADE,
  invocation_id         TEXT REFERENCES invocations(id),
  trigger_context       JSON NOT NULL,
  action_kind           TEXT NOT NULL,
  action_args           JSON NOT NULL,
  status                TEXT NOT NULL DEFAULT 'running'
                        CHECK(status IN ('queued', 'waiting_dependency', 'running',
                                         'retry_wait', 'completed', 'failed',
                                         'timed_out', 'skipped', 'cancelled')),
  exit_code             INTEGER,
  chain_parent_id       TEXT REFERENCES schedule_runs(id),
  chain_depth           INTEGER NOT NULL DEFAULT 0,
  fired_at              REAL NOT NULL,
  ended_at              REAL,
  error_detail          TEXT,
  created_at            REAL NOT NULL,
  updated_at            REAL,
  status_reason_code    TEXT,
  status_reason_summary TEXT,
  status_evidence_refs  JSON,
  queued_at             REAL,
  leased_by             TEXT,
  lease_expires_at      REAL,
  concurrency_key       TEXT,
  lease_attempts        INTEGER NOT NULL DEFAULT 0,
  required_capabilities JSON,
  execution_target      TEXT,
  library_ref           TEXT,
  library_content_hash  TEXT
);
```

Scheduled rows have non-null `schedule_id`, begin `running`, and leave queue/lease fields empty.
They are owned by `_fire_inner()`, not ADR-0071's worker.

The subprocess boundary is:

```python
# lionagi/studio/scheduler/subprocess.py
def resolve_li_executable() -> tuple[list[str] | None, str | None]: ...

def build_argv(
    schedule: dict,
    trigger_context: dict,
    *,
    executable_prefix: list[str] | None = None,
) -> tuple[list[str], str | None]: ...

async def spawn_and_wait(
    argv: list[str],
    invocation_id: str,
    *,
    tmp_path: str | None = None,
    cwd: str | None = None,
) -> tuple[int, str]: ...
```

Launcher vocabulary is the schedule table's five kinds plus `engine` for the shared launcher;
`playbook` maps to `play` only inside `build_argv()`. `build_argv()` validates model/identifier
tokens, forbids flag-like extra arguments, renders prompt templates, inserts `--` before free-form
positionals, and uses `create_subprocess_exec` rather than a shell. `flow_yaml` is written to a
temporary file and removed after execution.

Cwd resolves in order:

1. registered `action_project` path when it exists;
2. valid `LIONAGI_SCHEDULER_CWD`;
3. `None`, inheriting daemon cwd with a warning.

The `li` executable resolves independently of the eventual child cwd. Failure to resolve an
absolute executable path is a launch error rather than silently using a cwd-dependent prefix.

`spawn_and_wait()` passes `LIONAGI_INVOCATION_ID`, redirects stdout to null, captures stderr, and
uses `start_new_session=True`. On cancellation it terminates the full process group with a
five-second grace, then re-raises. The five-second grace is a concrete shutdown budget inherited
from this launcher; source records no measured rationale for the exact duration. Only the last
2048 stderr bytes are retained; this bounds row payload size, with no recorded tuning evidence for
2048.

Exact outcome semantics:

- **Invalid action before run-row creation:** the invocation already exists. The scheduler writes a
  failed schedule-run row with empty `action_args`, terminalizes both records, advances cadence,
  and checks max runs.
- **Exit 0:** schedule run becomes `completed`; invocation terminal status is resolved from its
  linked session evidence.
- **Non-zero exit:** run becomes `failed`, stores `exit_code` and stderr tail, and records either a
  missing-cwd or generic non-zero reason.
- **Concurrent terminal writer wins:** guarded terminalization checks expected status `running`;
  the scheduler treats a lost race as a checked no-op and continues follow-on bookkeeping.
- **Scheduler cancellation:** child process group is terminated; run and invocation attempt to
  become `cancelled`, then cancellation propagates.
- **Internal exception after run creation:** both records attempt terminal `failed`; error detail is
  bounded to the scheduler's chosen summary fields.
- **Daemon crash:** there is no queue lease to reclaim the scheduled row. Startup lifecycle
  reconciliation may classify stale records, but does not recreate the missed subprocess from that
  row.

Why this way: subprocess isolation keeps a scheduled action out of Studio's Python runtime and lets
the existing CLI remain the action adapter. It gives up durable lease recovery and makes stable cwd
and executable resolution load-bearing.

### D4 — Follow-ups are bounded inline recursion

Schedules may store `on_success` and `on_fail` JSON objects. After a child exits, the scheduler
chooses at most one continuation:

```text
exit_code == 0 and on_success present → on_success
exit_code != 0 and on_fail present    → on_fail
otherwise                             → no continuation
```

The selected object overlays the parent schedule. Its `kind` or `action_kind` replaces
`action_kind`; `model`, `prompt`, `agent`, and `playbook` map to their corresponding action fields.
The child receives a trigger context containing the original context plus `chain_from`,
`parent_exit_code`, and `parent_status`.

Exact semantics:

- The child row sets `chain_parent_id=<parent-run-id>` and increments `chain_depth`.
- Recursion continues only while the current depth is less than `_MAX_CHAIN_DEPTH = 10`; at depth
  10 no next child is launched.
- Chain children do not reserve global slots or max-run claims and do not count against
  `max_runs`; they execute within the already-admitted top-level fire task.
- A follow-up is awaited inline, so the parent fire task and any top-level global slot remain held
  until the chain completes.
- Follow-ups have separate schedule-run and invocation rows but no independently persisted edge
  object, policy, retry counter, or cancel address beyond their ids.
- A follow-up failure can select its own inherited/overlaid `on_fail` and continue until the cap.

Ten is a safety cap preventing unbounded recursive JSON. No recorded workload or measurement
explains why ten rather than another bounded value.

Why this way: inline recursion makes simple success/failure notification chains possible without a
new dependency subsystem. The cost is weak addressability and policy inheritance through dict
overlay.

### D5 — Dispatch is a separate guarded outbox

The payload model and enqueue signature are:

```python
# lionagi/session/signal.py
class DispatchSignal(Signal):
    dispatch_id: str = ""
    kind: str = ""
    deliver_to: str = ""
    attempt: int = 0
    ack_token: str | None = None
    body: dict = {}

# lionagi/dispatch/outbox.py
async def enqueue_dispatch(
    db: Any,
    *,
    kind: str,
    deliver_to: str,
    body: dict | None = None,
    dedup_key: str | None = None,
    ack_required: bool = False,
    max_attempts: int = 8,
    expires_at: float | None = None,
    session_id: str | None = None,
    schedule_run_id: str | None = None,
) -> str: ...
```

Persistence contract:

```sql
CREATE TABLE dispatch_outbox (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  deliver_to      TEXT NOT NULL,
  payload         JSON NOT NULL,
  dedup_key       TEXT,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending', 'delivering', 'delivered',
                                   'acked', 'dead_letter', 'expired')),
  attempt         INTEGER NOT NULL DEFAULT 0,
  max_attempts    INTEGER NOT NULL DEFAULT 8,
  next_attempt_at REAL NOT NULL,
  ack_required    INTEGER NOT NULL DEFAULT 0,
  ack_token       TEXT,
  session_id      TEXT REFERENCES sessions(id),
  schedule_run_id TEXT REFERENCES schedule_runs(id),
  last_error      TEXT,
  created_at      REAL NOT NULL,
  expires_at      REAL,
  updated_at      REAL
);

CREATE UNIQUE INDEX idx_dispatch_outbox_dedup
  ON dispatch_outbox(dedup_key) WHERE dedup_key IS NOT NULL;
```

Exact enqueue and delivery semantics:

- A duplicate non-null `dedup_key` returns the existing row id inside the insert transaction.
- Enqueue writes `pending`, attempt zero, `next_attempt_at=now`, an optional generated ack token,
  and one initial `status_transitions` fact.
- Due scans include `pending` and lease-expired `delivering` rows. Expired rows transition to
  `expired` before transport.
- A delivery claim is a guarded transition to `delivering` that increments `attempt` and advances
  `next_attempt_at` by `NOTIFY_TIMEOUT_SECONDS + 5` (15 seconds). The attempt guard ensures only one
  overlapping scan wins.
- The configured `dispatch.notify_template` is an argv list. `{payload}` and `{deliver_to}` are
  substituted only as whole argv elements; no shell executes. If `{payload}` is absent, JSON is sent
  on stdin.
- Transport timeout is 10 seconds. The extra five-second claim lease is a small recovery margin;
  the exact values have no recorded empirical tuning rationale.
- Backoff is `min(30 * 2**attempt, 1800)` seconds with no jitter; 1800 bounds delay at 30 minutes.
  The source records the formula as an inherited ruling but attaches no measurement or rationale
  for the exact values.
- Non-ack delivery stops at `delivered` after first transport success.
- Ack-required success returns to `pending` and resends after backoff until `ack`, expiry, or
  `max_attempts`; exhausting successful but unacked sends becomes `dead_letter` with an ack-timeout
  reason.
- Transport failure stores an error (bounded to 2000 characters), retries with backoff, or becomes
  `dead_letter` at the attempt cap.
- `ack_dispatch` requires `ack_required=1` and exact token match, then transitions to `acked`.
- `retry_dispatch` is only for `dead_letter` or `expired`; it atomically resets attempt,
  `next_attempt_at`, and error while returning to `pending`.
- `purge_dispatch` deletes one row in a guarded DB transaction. CLI commands inspect, acknowledge,
  retry, or purge; they do not enqueue arbitrary LionAGI work.

Eight attempts is the default boundedness budget for both failed delivery and missing ack. Source
does not record a workload-derived reason for exactly eight.

Why this way: the outbox preserves an outbound fact independently of transport liveness and keeps
notification failure out of task control flow. A generic task queue would invert ownership: the
consumer would appear to own work already committed by the producer.

## Consequences

- Local deployment is simple: starting Studio starts triggers, dispatch delivery, and one host
  worker.
- Schedules do not execute while Studio is unavailable; `run_once`/`skip` only govern cadence
  recovery, not interrupted process replay.
- Immediate scheduler-owned launch gives scheduled work different restart semantics from ad-hoc
  leased tasks even though both use `schedule_runs`.
- Subprocess isolation and process-group teardown reduce runtime sharing but make executable and cwd
  resolution critical. The daemon-cwd fallback remains environment-dependent.
- Budget and concurrency refusal are visible, but reservations are single-process.
- Inline follow-ups are bounded and linked, but cannot be independently managed as durable edges.
- Dispatch survives producer return and retries independently; it adds its own statuses, attempt
  counters, ack secrets, and operator recovery paths.
- Reversing D1-D4 toward queued admission is a medium migration because historical rows and
  invocation linkage must remain readable. Reversing D5 would require migrating pending/dead-letter
  delivery state and is therefore costly.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Require a stable execution root for every new schedule and migrate existing schedules off inherited daemon cwd; acceptance: schedule behavior is unchanged when Studio starts from a different directory. | S | (filled at issue-open time) |
| 2 | Align `li schedule create` with the persisted action vocabulary; acceptance: every supported stored action is creatable by its canonical public name or explicitly rejected as internal. | S | (filled at issue-open time) |
| 3 | Split trigger/admission, subprocess execution, and follow-up policy behind characterization tests; acceptance: each concern can be tested without starting the complete scheduler loop. | M | (filled at issue-open time) |
| 4 | Route scheduled fires through the queued admission contract in ADR-0072; acceptance: a due trigger writes `queued` and execution starts only after a worker wins a lease. | M | (filled at issue-open time) |
| 5 | Expose explicit queue/dispatch latency and oldest-due metrics; acceptance: operators can distinguish a healthy 30-second wait from stalled trigger, task, or delivery work. | M | (filled at issue-open time) |

## Alternatives considered

### External scheduler daemon

A separate process could isolate trigger failures, continue without the Studio web server, and
support independent deployment. It lost for the current local deployment model because it adds
service discovery, leader election/duplicate-fire protection, configuration, and lifecycle
coordination that the code does not need at present.

### In-process execution instead of child `li`

Calling lane functions directly would avoid process startup and simplify result capture. It lost
because scheduled actions would share Studio's imports, event loop, working directory, provider
state, and failure domain. The existing CLI already supplies the action adapters and invocation
linkage.

### Queue every scheduled fire today

This would immediately unify restart and lease semantics with ADR-0071. It is the target in
ADR-0072, but it was not the organically shipped path. Presenting it as current would hide that
`_fire_inner()` creates `running` and directly awaits the child.

### One state machine for schedules, tasks, and dispatch

A universal transition service could reduce helper count. It lost because schedule cadence,
execution ownership, and outbound delivery have different states and failure recovery. Shared
transaction mechanics are useful; a shared lifecycle vocabulary is not.

### Durable dependency records for every follow-up

First-class edges would buy independent inspection, retry, and cancellation. It lost for the
current implementation because simple bounded success/failure continuations were satisfied by
inline JSON. If independent management becomes required, the current overlay shape should be
migrated rather than described as already durable.

### Dispatch as task transport

Treating an outbound signal as a queued task would reuse worker leases. It lost because dispatch is
producer-driven delivery after state commits. A notification consumer does not own or execute the
source task and must not change its lifecycle.

### Unbounded acknowledgement retries

Retry-until-ack could maximize eventual receipt. It lost because a dead or non-acking consumer
would resend forever. `max_attempts` and optional expiry make failure terminal and inspectable.

### Shell command string for notify transport

A shell string would be easy to configure and could express pipes/redirection. It lost because
payload and routing values are external data. Whole-argv substitution through
`create_subprocess_exec` keeps metacharacters inert.

## Notes

The broader launcher accepts `engine`, but the `schedules.action_kind` constraint does not. This ADR
lists the persisted schedule vocabulary, not every internal launcher branch. `playbook` is a
fire-time alias only and remains a creation-path mismatch until Delta 2 is implemented.
