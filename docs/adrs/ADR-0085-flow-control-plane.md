# ADR-0085: Flow Control Plane — Pause/Resume, Message Injection, Checkpoint Resume, Status, Usage, Fallback

**Status**: Proposed
**Date**: 2026-07-02
**Builds on**: ADR-0083 (lifecycle signal contract) · ADR-0072 (reactive capability bus) · ADR-0075 (session bus observers) · ADR-0009 (sqlite state layer)

## Context

A `li o flow` / `li play` run is fire-and-forget today. Once the DAG is
executing, the operator has exactly two levers: wait, or kill the process.
This produces three concrete failure classes, all observed in production use:

1. **A kill loses the whole run.** There is no way to stop a flow at a safe
   boundary and continue it later. A 60-90 minute multi-op run that needs a
   correction (or must yield the machine) is a total loss.
2. **No mid-run steering.** When an operator sees the flow heading the wrong
   direction, there is no way to inject a correction, extra context, or a new
   task into the running DAG — even though the engine already supports graph
   growth (`ReactiveExecutor.inject()`, SpawnRequests).
3. **Completion is poll-only.** Launchers discover a flow's terminal state by
   polling StateDB or the process table. Nothing pushes "I'm done" outward.

The engine already has every internal seam this needs; what is missing is a
**control plane**: a way for an external process to reach a running flow, and
for a finished flow to reach outward.

### Existing seams (verified against source)

| Seam | Location | What it gives us |
|------|----------|------------------|
| Pre-completed-node skip | `operations/flow.py` `DependencyAwareExecutor.__init__` — nodes with `EventStatus.COMPLETED` get their completion event set and response restored | Cross-process resume: rebuild the graph, mark done nodes done, re-execute |
| Terminal-status gate | `operations/flow.py` `_execute_operation` first branch (`Event._TERMINAL_STATUSES`) | Resumed nodes are skipped without re-invocation |
| Limiter acquire point | `operations/flow.py` `_execute_operation` — `async with limiter:` | The one choke point every op passes before starting: the pause gate goes immediately before it |
| Live graph injection | `operations/flow.py` `ReactiveExecutor.inject(operation, after=..., independent=...)` | Message/op injection into a running flow needs no new engine mechanics |
| Flow workspace context | `DependencyAwareExecutor.context` (a `Note`), merged into every not-yet-started op's parameters by `_prepare_operation` | Queued guidance text becomes visible to all subsequent ops by deep-merging here |
| Live persistence loop | `cli/orchestrate/flow.py` `_execute_dag` — heartbeat task, `_op_segments`, `env._live_persist` StateDB ctx | The natural host for a control-poller task; already owns the session row |
| Terminal finalize | `cli/orchestrate/flow.py` `_run_flow` finally-block — `stop_live_persist` + invocation status resolution | The single place terminal status is known: notify hook fires here |
| Lifecycle lanes | ADR-0083 — `queued/running/awaiting_approval/succeeded/failed/escalated` | `paused` is added as a seventh lane (additive, same pattern) |

## Decision

Add a **run control plane** in eight parts. Parts 1-3 share one transport
(a StateDB control table + an in-process poller); parts 4-5 are the outbound
direction; parts 6-8 are the observability and adaptive-routing surfaces
(status, usage, fallback) that ride on the same StateDB substrate.

### 1. Control transport: `session_controls` table + poller

New StateDB table (schema.sql + migration; compatible with the
backend-pluggable StateDB work in #1572):

```sql
CREATE TABLE IF NOT EXISTS session_controls (
  id           TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES sessions(id),
  verb         TEXT NOT NULL,        -- 'pause' | 'resume' | 'message' | 'stop'
  payload      TEXT,                 -- JSON; verb-specific
  created_at   REAL NOT NULL,
  applied_at   REAL,                 -- NULL until the run consumes it
  result       TEXT                  -- 'applied' | 'rejected:<reason>'
);
CREATE INDEX IF NOT EXISTS idx_session_controls_pending
  ON session_controls(session_id, applied_at) WHERE applied_at IS NULL;
```

A **control poller task** runs alongside the existing heartbeat loop in
`_execute_dag` (same lifecycle: started before `run_dag`, cancelled in its
`finally`). Every `poll_interval` (default 2s) it reads unapplied rows for
its session ordered by `created_at`, applies each against the executor, and
stamps `applied_at` + `result`. Writers never touch the executor; the run
itself is the only consumer. This is the same single-writer discipline the
rest of StateDB uses, and it works identically over SQLite and Postgres.

Apply/stamp ordering is verb-classed, because a poller crash between the
two steps is unavoidable and neither order is safe for every verb:

- **Idempotent verbs** (`pause`, `resume`, `stop`): apply, then stamp.
  A crash re-applies on restart — harmless by idempotency (at-least-once).
- **Non-idempotent verbs** (`message`): stamp `result='applying'` first,
  then apply, then finalize to `applied`. A crash loses at most that one
  message, and the surviving `applying` stamp makes the uncertainty visible
  to `li o ctl status` instead of silently double-injecting (at-most-once).

CLI surface (new `li o ctl` subcommand; `li play` inherits since play is
flow):

```bash
li o ctl pause   <session-or-invocation-id>
li o ctl resume  <session-or-invocation-id>
li o ctl msg     <id> "text"  [--to <role-or-agent-id>] [--as-op]
li o ctl stop    <id>         # graceful: pause, DRAIN in-flight ops, checkpoint, exit 'paused'
li o ctl status  <id>         # show lifecycle lanes + pending controls
```

ID resolution accepts a session id, an invocation id (resolved via
`list_sessions_for_invocation`), or an unambiguous prefix.

### 2. Pause / resume (in-run)

Engine (`DependencyAwareExecutor`, inherited by `ReactiveExecutor`):

```python
self._pause_event: ConcurrencyEvent | None = None   # None = not paused

def pause(self) -> None:
    if self._pause_event is None:
        self._pause_event = ConcurrencyEvent()

def resume(self) -> None:
    if self._pause_event is not None:
        self._pause_event.set()
        self._pause_event = None
```

`ConcurrencyEvent` is a one-shot anyio Event (no `clear()`), so pause
installs a fresh unset event and resume sets-and-drops it. In
`_execute_operation`, immediately before `async with limiter:`:

```python
while (gate := self._pause_event) is not None:
    self._emit_paused(operation)      # NodePaused signal, once per wait
    await gate.wait()
```

Semantics: **soft pause at operation boundaries**. Ops already inside the
limiter run to completion; nothing new starts. This is deliberate — child
agent processes (CLI endpoints) cannot be safely frozen mid-turn, and an op
boundary is exactly the granularity the checkpoint (part 3) can restore.

`stop` must honor the same contract: set the pause event, then **await the
in-flight ops to completion** (drain the limiter), then write the final
checkpoint, then exit with terminal status `paused`. Exiting the process
immediately after setting the event would hard-kill the very ops the soft
pause promised to protect.

Signals: add `NodePaused(op_id, name)` and lane `"paused"` to ADR-0083's
contract (additive; `NodeStarted` resets it, terminal-sticky rules
unchanged). The session row gets `current_phase="paused"` via the existing
`_persist_session_phase` helper, so Studio and `li o ctl status` show it.

### 3. Queue message (in-run steering)

`li o ctl msg <id> "text"` supports two application modes, chosen by flags:

- **Context mode (default)**: the poller deep-merges
  `{"operator_messages": [+= {ts, text}]}` into the executor's flow
  workspace (`self.context`), using the same `deep_update` path op results
  use. Every op that has not yet had `_prepare_operation` run sees it in its
  `context` parameter. Cheap, race-free, and honest about scope: already
  in-flight ops are not interrupted. **Delivery is checked, not assumed**:
  if no un-prepared op remains in the graph at apply time, the injection is
  aborted and stamped `rejected:no-pending-ops` — never a success stamp on
  a message no LLM will ever read.
- **Op mode (`--as-op`, requires a reactive flow)**: the poller builds an
  `operate` node from the message (via the run's `role_node_builder` when
  `--to <role>` is given, else the default builder on the orchestrator
  branch) and calls `executor.inject(op, independent=True)`. The message
  becomes a first-class DAG node: it runs, its result lands in
  `operation_results`, and synthesis sees it. On a non-reactive flow the
  control is stamped `rejected:not-reactive`.

Both modes record the message into `node_metadata` so the run's provenance
shows operator interventions.

### 4. Checkpoint + cross-process resume

**Checkpoint writer**: after every op completion (the `NodeCompleted` /
`NodeFailed` observer in `_execute_dag` already fires there), write
`checkpoint.json` atomically into the run dir. Concurrent op completions
fire the observer concurrently, so the serialize-write-rename block runs
under an `asyncio.Lock` with a per-write unique temp name
(`checkpoint.{seq}.tmp`) — a shared static temp file would interleave
writers and rename a torn file into place. Contention is trivial (ops
complete at LLM cadence); the lock also guarantees renames land in
sequence order:

```jsonc
{
  "version": 1,
  "prompt": "...",                       // original task
  "plan": [ /* TaskAssignment dicts + agent_ids + dep_indices */ ],
  "flow_context": { /* executor.context.content */ },
  "ops": { "<node_id>": {"agent_id": "...", "status": "completed|failed|pending",
                          "response": "..." } },
  "spawned": [ /* reactively spawned node params for re-injection */ ],
  "config": { /* model_spec, workers, reactive_spec, max_ops, ... */ }
}
```

Atomic rename makes the mid-write kill window a non-issue: a reader sees
either the previous complete checkpoint or the new one, never a torn file.
(The same writer discipline fixes timeout-kill artifact loss for flows.)

**Resume**: `li o flow --resume <run-or-session-id>` (and `li play --resume`)
loads the checkpoint, replays `setup_orchestration` + `_build_dag` from the
persisted plan (skipping the planner LLM call entirely), then marks each
node whose checkpoint status is `completed` as `EventStatus.COMPLETED` with
its persisted response before execution. The executor's existing
pre-completed seam does the rest: done nodes are skipped, pending nodes run,
dependents receive predecessor results through `_prepare_operation` exactly
as they would have live.

Scope note (v1): resume restores **results-context, not conversational
context**. Predecessor *results* arrive via parameters exactly as live; the
original message history does not. For pending ops with `inherit_context`
this is not a documentation problem but a correctness trap — they expect
their predecessor's actual conversation and would silently run against an
empty branch. So v1 resume **refuses loudly**: if any pending op has
`inherit_context=True`, `--resume` exits with an error naming those ops,
overridable with an explicit `--allow-degraded-context`. Full
branch-message restoration from the run dir's `branches/*.json` snapshots
is the follow-up that lifts the restriction.

The resumed run gets a fresh session row linked to the original via
`resumed_from` in `node_metadata`; the original's terminal status remains
whatever it was (`paused`, `timed_out`, `failed`).

New terminal status: `paused` (via `li o ctl stop` or a future
`--checkpoint-on-timeout`), resolved through the existing
`update_status` path with a new `RunReasons.PAUSED_OPERATOR` reason code.

### 5. Terminal notify (outbound completion signal)

In `_run_flow`'s finally-block, after the invocation terminal status is
resolved (the one place status is final), fire an optional **notify hook**:

```yaml
# .lionagi/settings.yaml
notify:
  on_terminal: "khive-comm-send --to {launcher} --json '{payload}'"   # any shell template
```

The payload is a stable JSON contract:

```json
{"invocation_id": "...", "kind": "flow|play", "playbook": "...",
 "status": "completed|failed|timed_out|aborted|cancelled|paused",
 "save_dir": "...", "cwd": "...", "exit_class": "...",
 "started_at": 0.0, "ended_at": 0.0}
```

Design constraints: the hook is a **generic shell template** resolved from
settings (project overrides global, per ADR-0060 resolution) plus an
optional `--notify <cmd>` flag; lionagi ships no messaging integration. The
hook runs with a short timeout (10s), failures are logged and never affect
the run's exit code, and `{payload}` / `{status}` / `{invocation_id}` are
the substitution variables. A launcher-side khive `comm.send` template gives
push wake-ups to inbox monitors with zero new dependencies in either
project.

### 6. Status surfaces: `li agent status` / `li play status`

One-shot, machine-checkable status for the two run kinds operators actually
ask about, without knowing an id:

```bash
li agent status [<id>] [--json]    # default: latest agent run for this project
li play  status [<id>] [--json]    # default: latest play/flow run
```

Output: lifecycle lane (ADR-0083 lanes + `paused`), current phase, op
progress (`completed/total` from node signals), model + provider, last
activity timestamp, resume handle (conversation/session id), pending
control verbs, exit class when terminal. `--json` emits one stable object;
exit code 0 = terminal-success, 1 = terminal-failure, 3 = still running —
so shell scripts can gate on status without parsing. (3, not 2: argparse
owns exit 2 for usage errors, and a status gate must never confuse "still
running" with "you typo'd a flag".)

Implementation is pure reads over existing tables (`sessions`,
`invocations`, `session_signals`) plus `session_controls` from part 1.
`li o ctl status` becomes an alias into the same renderer. This directly
replaces the hand-rolled sqlite polls every λ writes today.

### 7. Per-provider usage: `usage_events` + `li usage`

Every CLI-endpoint run already captures provider-reported usage — all three
providers converge on `session.usage` (claude_code, codex `turn.completed`,
agy terminal event), and `run.py` persists it as `model_response` metadata
on the AssistantResponse message. The gap is queryability: it lives in
per-run branch-snapshot JSON, so fleet-level consumption is invisible.

New StateDB table, written by the same live-persist/finalize path that
snapshots branches (idempotent per branch turn):

```sql
CREATE TABLE IF NOT EXISTS usage_events (
  id             TEXT PRIMARY KEY,
  invocation_id  TEXT,
  session_id     TEXT NOT NULL,
  branch_id      TEXT,
  provider       TEXT NOT NULL,       -- 'claude_code' | 'codex' | 'gemini_code' | api provider
  model          TEXT NOT NULL,
  message_id     TEXT,                -- AssistantResponse message carrying the usage
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cached_tokens  INTEGER NOT NULL DEFAULT 0,
  thinking_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL,                -- NULL when provider doesn't report it
  fallback_from  TEXT,                -- model this call was a fallback for (part 8)
  created_at     REAL NOT NULL,
  UNIQUE(branch_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_events_provider_time
  ON usage_events(provider, created_at);
```

Idempotency is structural, not aspirational: the writer keys each row on
`(branch_id, message_id)` — the AssistantResponse message the usage rode in
on — and inserts with `ON CONFLICT DO NOTHING`. Re-sweeping a resumed run's
restored responses is then a no-op instead of a billing double-count.

CLI: `li usage [--by provider|model|day] [--since 7d] [--json]` — rollup
queries over the table. This is the burn dashboard (which engine consumed
what, when) and the evidence base the fallback chain needs before any
predictive routing is attempted.

### 8. Mirror-chain fallback (quota-aware model routing)

Problem: gemini quota is a daily window while codex/claude are weekly, so
gemini-routed lanes (mirror, doc verification) die mid-fleet when the daily
window exhausts, and every λ handles it ad hoc.

v1 is **reactive** fallback, configured as chains in settings
(project overrides global, ADR-0060 resolution):

```yaml
fallback:
  chains:
    gemini-3.1-pro: ["gpt-5.5"]
    gemini-3.5-flash: ["gpt-5.3-codex-spark"]
```

Each CLI provider maps its quota-class error surface (codex `rate_limit` /
usage-limit result classes; agy quota errors; claude limit messages) onto
the existing `ProviderQuotaError` from `classify_provider_error` — the
classification seam already exists and fires in production. The `li agent`
invoke path catches it and re-invokes with the next chain model, recording
the hop in `node_metadata` and stamping `fallback_from` on the usage row so
hops are auditable and visible in `li usage`. A hop emits one warn-level
line — fallback must never be silent, or provider degradation hides inside
green runs.

Two hard guards:

- **Cycle guard**: the invoke path carries a `visited_models` set per turn;
  a chain hop into an already-visited model terminates with the original
  `ProviderQuotaError` instead of looping. Chains are user config; a cycle
  (`A → B`, `B → A`) must burn zero extra quota.
- **Fresh-turn scope**: fallback applies only when starting a fresh
  invocation. Resumed conversations (`-r` / `-c`) never hop — a resume
  handle is provider-bound, and silently continuing "the same conversation"
  on a different engine would fabricate context. Quota death on a resumed
  conversation surfaces to the caller.

Explicitly **not** v1: predictive budgeting (routing away from a provider
before it exhausts). That needs `usage_events` history to exist first and
is staged as a follow-up on the same table.

## Consequences

**Positive**

- A runaway or misdirected flow becomes correctable instead of killable-only;
  a yielded machine costs an op boundary, not a run.
- Message injection reuses `inject()` and the flow workspace — no second
  code path for graph mutation, no new engine invariants.
- Resume skips the planner call, so restarting a 10-op run with 7 done costs
  only the 3 pending ops.
- Checkpoint's atomic-rename writer doubles as the fix for artifact loss on
  timeout kills.
- The control table works unchanged across SQLite and Postgres StateDB
  backends, and gives Studio a visible intervention audit trail.
- `li agent status` / `li play status` retire the hand-rolled sqlite polls;
  the exit-code contract makes runs gateable from shell without JSON parsing.
- Usage becomes a fleet-level query instead of per-run JSON archaeology;
  quota exhaustion downgrades from a lane outage to an audited chain hop.

**Negative / accepted costs**

- Soft pause cannot stop an op already executing; a hung child process still
  requires the (separate) stall watchdog to act.
- Poll latency: a control verb takes up to `poll_interval` (2s) to apply.
  Acceptable for human-speed steering; not a real-time API.
- Context-mode messages reach only not-yet-prepared ops. This is inherent to
  the prepare-once parameter model and is documented CLI behavior.
- v1 resume loses conversational branch history for `inherit_context`
  dependents (results survive). Follow-up restores from branch snapshots.
- One more background task per run (poller) and one checkpoint write per op
  completion (small JSON, atomic rename) — negligible against LLM op cost.
- A fallback hop changes which model produced a result mid-run; the
  `fallback_from` stamp plus warn line keep it auditable, but consumers that
  assume a fixed model per alias must read the usage row, not the config.
- `usage_events` grows with every turn; rows are tiny, and `li state doctor`
  gains a prune-by-age for it (folded into the orphan-prune work).

## Implementation slices

1. **Engine pause gate + `NodePaused`/`paused` lane** (`operations/flow.py`,
   `session/signal.py`) — pure in-process, fully unit-testable.
2. **`session_controls` table + poller + `li o ctl`** (`state/schema.sql`,
   `state/db.py`, `cli/orchestrate/_control.py`, `cli/orchestrate/flow.py`).
3. **Status surfaces** (`cli/agent.py`, `cli/orchestrate/__init__.py`,
   shared renderer) — pure reads; lands right after 2 for the pending-controls
   column, but is useful (minus that column) even before it.
4. **Checkpoint writer + `--resume`** (`cli/orchestrate/flow.py`,
   `cli/_runs.py`).
5. **Terminal notify hook** (`cli/orchestrate/_orchestration.py` finalize
   path, settings resolution).
6. **`usage_events` writer + `li usage`** (`state/schema.sql`, live-persist
   path, `cli/usage.py`).
7. **Quota fallback chains** (`providers/*` error mapping → `QuotaExhausted`,
   `cli/agent.py` re-invoke, settings) — depends on 6 for the audit stamp.
8. **Planner watchdog** (`cli/orchestrate/flow.py`): plan-parse failure today
   is swallowed into a silent exit 0. Make it loud — nonzero exit, one bounded
   retry with a strict emit-only-JSON re-prompt, raw planner text persisted to
   the run dir for diagnosis.

Slices are independently landable in this order; each is behavior-preserving
for runs that use no control verbs. Slices 1-3 unblock the pilot playbooks
(pr-review, mirror); 6-7 unblock fleet usage visibility and the mirror lane's
quota resilience.

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Unix signal-based pause (SIGSTOP/SIGTSTP) | Freezes child agent processes mid-API-call and TLS connections; unsafe, and gives no message-injection channel. |
| Control file (`control.jsonl`) in the run dir instead of StateDB | Works, but StateDB is already the run's live read surface (Studio, `li state`), is the substrate the Postgres port makes multi-host, and gives the audit trail for free. |
| gRPC/HTTP control endpoint in the run process | A server in every flow run is a large attack/complexity surface for a v1 whose consumers are CLI + Studio; the DB poller needs no ports, no auth story. |
| Hard pause (cancel in-flight ops, restart on resume) | Wastes the most expensive resource (in-flight LLM turns) and requires op-level idempotency we don't have. |
| Full graph pickle for resume | Serializing live Operations/Branches is fragile across versions; the plan + statuses + responses is the minimal stable contract and reuses the deterministic `_build_dag`. |
| Baking khive comm into the notify hook | lionagi is a general framework; a shell template keeps the boundary clean and lets any consumer (khive, Slack webhook, systemd) subscribe. |

## References

- `lionagi/operations/flow.py` — executor seams cited above
- `lionagi/cli/orchestrate/flow.py` — `_execute_dag` heartbeat/persist loop, `_run_flow` finalize
- ADR-0083 — lifecycle signal contract (`paused` lane extends it)
- ADR-0060 — settings resolution used by the notify hook
