# Studio Internals Reference

Non-obvious invariants, protocol contracts, and design rationale for
`lionagi/studio/` that don't belong inline as long-form comments. Terse
reference — not a narrative. Organized by module path.

## lionagi/studio/app.py

**Host-header validation (`_HOST_AUTHORITY_RE`/`validate_host_header`)** —
strict authority grammar (`host` or `host:port`, or bracketed IPv6 `[addr]`
with optional `:port`), deliberately stricter than `request.url.hostname`,
which normalizes/mis-parses authorities like `"127.0.0.1:8765.evil.com"` or
`"[::1]evil.com"` into an accepted hostname. `validate_host_header` defends
against DNS rebinding, where a malicious page points a browser at
`http://127.0.0.1:<port>` with an attacker-controlled Host and, once past
CORS/auth, reaches the daemon as if same-origin — registered outermost
(added after CORSMiddleware, so it runs first under Starlette's LIFO
middleware wrapping) so every request, including preflight OPTIONS, has its
Host checked before CORS can answer.

**Middleware order** in `create_app()`: Host validation -> CORS ->
Content-Type/CSRF check -> bearer-token gate -> route. A real preflight
never reaches the bearer-token/Content-Type middlewares because CORS
answers it first.

**`require_json_content_type`** — rejects state-changing `/api` requests
that don't declare a JSON body. FastAPI parses request bodies as JSON
regardless of declared Content-Type, so a cross-site "simple request"
(`text/plain`, no CORS preflight) carrying a JSON-shaped body would
otherwise reach route handlers unchecked — the classic form-based JSON CSRF
vector. The SPA always sends `application/json` on requests carrying a body
(`apps/studio/frontend/src/lib/api.ts` `fetchJson`) and no body at all for
routes that need none, so this only rejects traffic the frontend itself
never produces.

**`_mount_spa`** — uses a 404 exception handler, not a catch-all route, for
the SPA fallback: a catch-all `/{full_path:path}` route would intercept
`/api/shows` before FastAPI's trailing-slash redirect fires, whereas an
exception handler runs only after every route has been tried and missed.

**Startup and the 501 store guard** — `StoreNotAddressableError` becomes a 501
at the route, which only helps if startup gets far enough for routes to be
reachable at all. A subsystem that can read only a local SQLite file must
therefore skip itself during `lifespan` rather than raise: `operator_startup`
checks `require_file_store()` and returns empty against a server-backed or
in-memory store, because raising there aborts the whole lifespan and the
daemon serves nothing, including the routes whose whole job is to say this
condition cannot be served. `OperatorStore.path()` raises the same
`StoreNotAddressableError` as the rest of the SQLite-direct layer rather than
an `OperatorStoreError`, so the routes that open the store answer 501
(permanent) instead of 503 (retryable), and the definition of which stores this
layer can open stays in one place. The qualifier is doing work: a route that
never opens the store is unaffected and answers normally in every mode.
`GET /operator/models` returns its catalog from `operator/catalog.py` and so
returns 200 against a server-backed store like any other, which is worth
knowing before reading a 200 there as evidence that the store is reachable.

## lionagi/studio/cli.py

**`_validate_chain_action_node`** — Validates one `chain_action` node, recursing
into nested `on_success`/`on_fail` the way the engine's chain-fire would reach
them. `chain_depth` mirrors the engine's own gate (scheduler/engine.py,
`chain_depth < _MAX_CHAIN_DEPTH`); recursion stops there because a node beyond
that depth never has its own `on_success`/`on_fail` read by the engine.
`self_field` tracks which chain field a node was reached through, for the
re-fire warning (does the node set its own copy, or inherit the parent's via
shallow merge?).

**`_base_url`** — Tolerates a base URL that already carries an `/api` suffix
(older documented workaround) so requests don't double up to `/api/api/...`
and 404. Warns once instead of stripping silently, since a reverse proxy whose
public prefix genuinely ends in `/api` needs visibility into the rewrite.

**`_await_vite_ready_url`** — A background thread drains Vite's stdout for
the life of the dev-server process so Vite never blocks on a full pipe
buffer; once the function itself returns (ready URL found, or timed out) that
thread stops parsing lines and only keeps draining. It's a daemon thread with
no explicit join, so it exits with the process rather than needing shutdown
wiring.

## lionagi/studio/scheduler/engine.py

**`_reserve_max_runs_budget` / `_release_max_runs_claim`** — The single-process
analogue of a DB-backed compare-and-set for `schedule['max_runs']`; only one
scheduler process runs today, so no DB-backed reservation exists. Guarded by
an engine-wide lock so the tick loop, `fire_now()`, and GitHub polling can't
both read the same count and both claim it before either claim is visible.
Reads `inflight` (this process's outstanding claims) *before* the awaited
`count_schedule_runs()` call, not after — release() is deliberately lock-free
(a claim must still release from a cancelled/failing `_fire()`'s `finally`
without depending on this lock), so a concurrent fire's claim can vanish
mid-await. Reading `inflight` first means the sum can only ever over-count
(a spurious refusal, self-correcting on the next tick), never under-count
into an actual overshoot — reading it after the await would let a fire that
both persists its row and releases its claim inside this call's await window
disappear from both the persisted count (read too early) and the in-flight
snapshot (read too late). The claim is released from `_fire()`'s own
`finally`, not from inside `_check_max_runs()`: a fire failing before ever
reaching `_check_max_runs()` (e.g. `create_invocation` raising) would
otherwise leak the claim for the life of the process. Chain children never
call this — only top-level fires consume budget.

**`_fire_inner` delivery contract** — At-least-once up to confirmed process
launch, at-most-once past it, across three windows: (1) before the occurrence
transaction commits, a crash leaves nothing durable, so a restart fires
fresh, never a duplicate. (2) Between commit and `spawn_and_wait()`
confirming launch (`on_launched` stamping `dispatched_at`), the row is
durable but undispatched; `_recover_undispatched_fires()` finds it on startup
and re-fires via `supersedes_run_id`, which routes the occurrence insert
through `tombstone_and_replace_schedule_run()` to tombstone the orphan and
insert the replacement atomically (its CAS also requires `dispatched_at IS
NULL`, so a launch confirmed in the race against recovery wins and the
tombstone is a no-op). (3) Once `dispatched_at` is confirmed, the process
genuinely exists and is never re-fired — resolved by the stale-run reaper or
its own terminal write. A duplicate real-world side effect is worse than one
unretried outcome, hence the asymmetry. The occurrence-insert and any
`extra_schedule_fields` cursor advance land in the same transaction as this
delivery boundary — see `_write_occurrence()` for the atomic-with-tombstone
recovery path.

**`_reconcile_dispatched_orphans`** — Startup-only; handles schedule_runs rows
that were confirmed dispatched but never reached a terminal status, which
`_recover_undispatched_fires()` (safe to blindly re-fire) can't touch since a
row here may have a genuinely live child. Resolves only from positive
completion evidence: when every session linked to the invocation has
independently reached a terminal status (each session's own teardown writes
its own terminal status, independent of whether the dispatching scheduler
survived to see the exit code), the schedule_run is finalized via
`resolve_invocation_terminal()`. Unknown liveness is never treated as death —
a row with no sessions yet, or any non-terminal session, falls through
unchanged to the wall-clock stale reaper (`reap_stale_schedule_runs`).

**`_recompute_armed_cron_schedules`** — Startup-only re-resolution of every
enabled cron schedule's `next_fire_at` under the current timezone
interpretation, guarding against stale fire times after
`LIONAGI_SCHEDULER_TZ` (or the host timezone) changed since the schedule was
last armed. A schedule already due (`next_fire_at <= now`) is left alone here
so it flows through `_check_missed_fires()` first — `missed_fire_policy`
("run_once"/"skip") must get a chance to run before anything advances the
timestamp, and `_check_missed_fires()` runs immediately after this method
returns in `_tick_loop`, synchronously advancing `next_fire_at` via the
recovery path before the following `_tick()` call. Only schedules still ahead
of now — the timezone-migration correction case this hook exists for — are
recomputed here; the method never fires anything itself.

**`_check_budget`** — Pre-fire cumulative spend gate, not a mid-run
interrupt: a run already in flight is never killed when it crosses the
budget line, since its cost is unknown until it terminates, so a schedule may
overshoot by up to one run's cost before the next fire is refused. Pair with
`LIONAGI_STUDIO_INVOCATION_DEADLINE_SECONDS` to bound a single run's
worst-case spend. `spend["cost_usd"]` sums only *reported* cost — a session
whose engine never priced itself contributes nothing, not a confirmed $0 —
so this deliberately does not force exhaustion over unreported sessions
alone (that would turn a data gap into an outage); it only surfaces the gap
via a log line and `unreported_sessions` in the schedule's spend rollup, so a
near-zero reading with many unreported sessions reads as "unknown," not
"cheap."

**`_threshold_alert_update_fields`** — Stamps `last_alert_at` into the same
`update_schedule()` call that already writes `last_fired_at`/`next_fire_at`
inside `_fire_inner()`, deliberately placed after `create_schedule_run()` has
durably persisted the run row. Stamping the cooldown any earlier (e.g. before
`_fire()` starts) risks consuming it on a pre-persistence failure (e.g.
`create_invocation()` raising) that leaves zero durable record an alert was
ever attempted — the exact silent-loss shape the feature exists to prevent.
Only top-level fires (`chain_depth == 0`) of a threshold-configured schedule
stamp the cooldown; `on_success`/`on_fail` chain children are follow-on
actions of the same alert cycle, not a new one.

## lionagi/studio/scheduler/subprocess.py

**`spawn_and_wait`** — Both stdout/stderr streams are captured and drained
concurrently, bounded rather than buffered whole: a single `communicate()`
call on both pipes would hold an entire streaming leg's output in memory,
and draining one stream at a time deadlocks once the other fills its buffer.
When `action_kind="command"`, the command allow-list check re-runs here,
immediately before spawn, closing the window where an awaited DB call
between `build_argv()` and this function gives a revoked
`LIONAGI_SCHEDULER_COMMAND_ALLOWLIST` env var a scheduling point to land in.

## lionagi/studio/services/schedules.py

- **`_svc_validate_action_command`** — Delegates to the subprocess validators
  so charset/allow-list rules live in one place. `build_argv` re-checks the
  allow-list again at spawn time since `LIONAGI_SCHEDULER_COMMAND_ALLOWLIST`
  can change between schedule creation and fire.
- **`_svc_recompute_next_fire_guarded`** — The caller's DB write has already
  committed, so a recompute failure must not surface as a 500. One retry
  covers transient contention; if both fail, the row keeps its stale
  `next_fire_at` — healed only by the daemon-startup recompute, or by firing
  once on the stale timestamp and recomputing from there.
- **`_svc_validate_github_repo`** — Delegates to `github._validate_github_repo`
  (owner/name regex, CWE-918 guard) so the pattern lives in one place. `None`
  = field not supplied (no-op); `""` = explicit invalid value, forwarded for
  rejection.
- **`github_filter` allowed keys** — `event` narrows *which* PR lifecycle
  moment fires the trigger; the rest narrow *which PRs* are considered. Only
  `pr_merged` has real dispatch semantics in `github_poll()` today —
  `pr_opened`/`pr_updated`/`pr_closed` are accepted because the frontend's
  create-schedule form ships all four, but are currently inert server-side.
  `same_repo_only` excludes fork-origin PRs (head repo ≠ polled repo), whose
  diffs are attacker-controlled input.
- **`_svc_validate_prompt`** — Rejects `action_prompt == '--'`: that literal
  end-of-options token is silently consumed by argparse and never reaches the
  runner as prompt text. All other content, including leading `-`, is safe
  because the structural argv fix places a `--` sentinel before positionals.
- **ADR-0070 delta 1 (execution root snapshot)** — An explicit `action_cwd`
  always wins. Otherwise a registered `action_project`'s path is captured
  once at write time, not re-resolved at fire time, so a later project-registry
  change or daemon restart can't move the schedule's spawn cwd. If neither
  resolves, `action_cwd` stays `None` and the engine falls back to
  `LIONAGI_SCHEDULER_CWD` / the daemon's cwd, same as a pre-migration row.
- **`update_schedule` PATCH semantics** — Uses `exclude_unset`, not
  `exclude_none`: a field the client never mentioned is untouched, while an
  explicit `null` passes through so the field can be cleared or rejected.
  `exclude_none` would make an all-null PATCH indistinguishable from an empty one.

## lionagi/studio/services/runs.py

**`_open_regular_file_no_follow`** — `resolve_workspace_path` only validates
at check time; nothing stops the target being replaced with a symlink before
a later path-based `open()` follows it out of the artifact root (CWE-367/59).
Walks each path component via `os.open(..., dir_fd=parent_fd)` off a
descriptor obtained *before* that component, never by re-walking a path
string, with `O_NOFOLLOW` refusing a symlink at any position.

**`_build_steps_from_db`** — `message_count`/`roles` come from
`message_stats`, falling back to `message_total` then windowed page length for
legacy payloads. The `message_stats.message_count` check is key-presence
based, not truthy: a legitimate `0` (stale progression referencing pruned
message ids) must not fall through to `0 or fallback`.

## lionagi/studio/services/sessions.py

- **`_fetch_action_messages`** — The `+m.lion_class` query hint disqualifies
  the `lion_class` index so the planner uses the id primary key for the IN
  list; without it SQLite drives the query off `idx_messages_lion_class` and
  rescans every action-class row table-wide per chunk.
- **`get_session_messages_after`** — Joins each branch's progression via
  `json_each` rather than binding every message id into an `IN (...)` clause,
  since a branch with thousands of messages would exceed SQLite's 999
  bound-variable limit.

## lionagi/studio/services/admin.py

- **`process_liveness`** — Tri-state: `True` = observed alive; `False` =
  confirmed dead (recorded pid gone, start-time verified when recorded);
  `None` = unknown (no recorded pid/match — normal for externally-driven
  sessions). A bare recycled pid with no recorded start time reads alive
  (fails toward live, not falsely dead).
- **`transition_sessions` CAS block** — `WHERE status='running'` can only move
  running→target (legal forward transition, never overwrites terminal
  status) — do not widen or drop that predicate. The `last_message_at`/
  `updated_at` equality guards stop this reconcile from clobbering a session
  that went active again between classification and write (the oscillation
  fix); `update_status()`'s `expected_statuses` guard doesn't compare on
  these columns, so routing through it would regress the protection. Intentional
  specialized CAS, not a bypass of the shared chokepoint.

## lionagi/studio/services/workflow_run.py

- **Module purpose** — Runs a compiled `WorkflowDef` through lionagi's
  `Session.flow`, persisted like any other run. Deliberately does not reuse
  `_orchestration.setup_orchestration_persist`/`teardown_persist` verbatim:
  those close a process-wide shared StateDB singleton meant for one-shot CLI
  processes, but the Studio server is long-lived with several runs in flight
  — this module opens/closes its own request-scoped connection instead.
- **Engine-operation registration ordering** — `ctx` must exist before the
  `"engine"` operation is registered: engine sub-agent branches
  (`Engine.make_agent`) are born mid-run, like flow-cloned branches, so they
  need the same `on_branch_created` seam used for `session.flow()`.
- **`flow_progress_signals`** — `run_workflow_def` drives `session.flow`
  directly, bypassing the engine (the usual signal source), so without this
  emit the run persists structure+results but no node-progress rows.
- **Clone-branch persistence** — Flow-created clone branches (predecessor,
  no explicit `branch_id`; see `FlowExecutor._preallocate_all_branches`) are
  born after `_setup_run_persist` already registered setup-time branches.
  Without `on_branch_created`, a clone's transcript never persists even
  though run-DAG signals still render (those persist via the session-level
  observer, not per-branch hooks).
- **`CancelledError` handling** — A cancelled request/task aborts
  `session.flow` with `CancelledError` (a `BaseException`, bypasses `except
  Exception`); the run must be recorded cancelled, not the optimistic
  "completed" default, before re-propagating.
- **`run_workflow_def` error contract** — Raises `WorkflowNotFoundError`
  (404) or `WorkflowCompileError` (422, carries node_id/edge_id) on compile
  failure, never a bare 500. `base_dir` is a run-level containment root for
  node `config.cwd`, never a spec field. `_session` is a private testability
  seam; real callers never pass it.

## lionagi/studio/services/workflow_compile.py

- **Security surface** — The only new surface is `StudioExprCondition`, a
  restricted-grammar expression evaluator for designer-authored edge
  conditions. Never calls eval/exec/compile/`__import__`; the AST is walked
  against a closed node-type allowlist before evaluation.
- **Safe expression grammar** — Allowed: comparisons, boolean and/or/not,
  literals (str/int/float/bool/None), list/tuple literals of those, names,
  attribute access, subscript/key access, in/not in. Everything else (calls,
  lambdas, comprehensions, f-strings, walrus, imports, dunders) is rejected
  before evaluation.
- **`_resolve_node_cwd`** — Containment order matters: raw-string traversal
  check before path resolution, then symlink resolution before the
  containment check, then existence check.
- **`StudioExprCondition.__init__`** — Parses before pydantic's validation
  machinery runs; a `model_validator` would wrap `UnsafeExpressionError` into
  `pydantic_core.ValidationError`, breaking callers matching on the former.
- **`compile_workflow_def`** — Returns `(graph, id_map)` mapping authored
  node ids to internal Operation ids. Raises `WorkflowCompileError`
  (node_id/edge_id set) on any problem. `base_dir` is a run-level containment
  root, never read from the spec itself — a spec carrying its own `base_dir`
  is rejected so a shared/contributed def can't pin its own containment root.
- **Engine node re-validation** — Node-level config overrides never went
  through `engine_defs`' creation-time checks (allowed keys, no CLI-flag/
  shell-metachar injection, budgets in [1, 100]); re-validated here so a
  saved workflow can't smuggle a hostile `test_cmd` or unbounded budget.
- **`make_engine_operation`** — Runs the resolved engine class in-process
  against the same session, so sub-agent branches wire into this run.
  `on_branch_created`, when given, threads into `engine.run()` so spawned
  sub-agent branches register for persistence like flow-cloned branches.

## lionagi/studio/services/approvals.py

- **Lifecycle** — An action is proposed (pending) → a human grants or denies
  → the consuming endpoint must consume the granted approval exactly once.
  Expiry and single-use are enforced here, not by caller convention: a granted approval
  that's expired, already consumed, or whose params don't hash-match the
  action being executed is rejected.
- **Principal separation** — A request carrying the operator/service
  principal marker header is rejected for grant/deny before the row is
  touched (the browser frontend never sends it); additive to the bearer-token
  gate, not a replacement.
- **Evidence chain** — Every lifecycle event appends a hash-chained row to
  `approval_evidence` in the same transaction as the status change
  (`chain_hash = sha256(content_hash + previous_hash)`, genesis = `"0"*64`).
  Evidence rows never store raw params. Optional HMAC-SHA256 signing is off
  by default (`LIONAGI_STUDIO_EVIDENCE_HMAC_KEY`).
- **Service-principal header** — Presence alone (any non-empty value)
  disqualifies grant/deny; there is no "correct" value, so a caller can't
  guess past the check.
- **`_require_human_principal`** — With no bearer token configured, granting
  is unavailable entirely (fail closed) rather than open to any local caller.
- **`_write_evidence`** — Caller must already hold the write lock on `db` (a
  preceding `BEGIN IMMEDIATE` in the same transaction as the status change)
  so the tail read is race-free; this function opens no transaction of its own.
- **`require_approval`** — Validates a granted approval for exactly this
  action and consumes it atomically. A mutating route must call it before its
  side effect, passing the same action_kind/params it's about to act on.

## lionagi/studio/services/definitions.py

- **Per-(kind, name) concurrency lock** — Shared across all requests in this
  process; spans the DB write inside `StateDB.save_definition()` and the
  subsequent disk write so both are atomic from the service's perspective —
  a crash between them can't leave disk ahead of history.
- **`save_definition` ordering (ADR-0077 D2)** — DB write must succeed before
  the file is written; the per-(kind, name) lock serializes concurrent saves.
- **`_find_definition_file`** — Candidates are literal-path joins, not glob
  patterns. Symlinks outside `base` are intentionally left unresolved and
  unrestricted — restricting them would break symlinked agent definitions.
- **The mixed-source reads** — `list_definitions` and `get_definition` answer
  from two places at once: current content from disk, version history from the
  store. Both halves now resolve the same way, because history is read through
  `StateDB` exactly as `save_definition` writes it. Reading SQLite directly was
  the failure this replaces: writes went through `StateDB` and landed in the
  configured server while reads fell back to the default local path, so an old
  local database left over from a previous deployment was reported as this
  definition's versions, laid over content read live from disk. Nothing in that
  payload looks wrong; the two halves simply came from different stores.

  When the store cannot be read at all, the disk half is still answered and the
  history half is null rather than empty. The distinction is the point: an
  empty history is a claim about the definition, and a caller told there are no
  versions concludes nothing was ever saved. The true statement is about the
  store, so `get_definition` returns `versions: null` with
  `history_available: false`, and `list_definitions` reports `has_versions:
  null` for the same reason. A client that does not handle it fails on a null
  instead of quietly believing the definition was never versioned.

  Routes whose whole answer *is* history have no disk half to fall back on, so
  they refuse: `get_version` and `rollback_definition` raise
  `HistoryUnavailableError` and the routes map it to 503. 503 rather than the 501
  the Operator routes use, and the two are not interchangeable — every store
  this deployment can be configured for is one `StateDB` reads, so failing to
  read it is an operational condition a retry can outlive, where the Operator's
  SQLite-only store makes the refusal permanent for that deployment. The
  refusal body is a fixed string and carries nothing the driver said.

## lionagi/studio/services/playbooks.py

- **`_check_spec_fields`** — Mirrors
  `lionagi/cli/orchestrate/__init__.py::_validate_spec_fields()` exactly,
  implemented inline to avoid loading the full orchestrate module at import
  time. The two must be kept in sync by hand.
- **`update_playbook`** — Conservative merge writing a playbook YAML back to
  disk: `description` overwrites when present; graph keys
  (`use`/`steps`/`links`) only when non-empty; declarative keys overwrite or
  clear on `None`/`""`; all other disk keys preserved. Writes through
  symlinks to the real source file.

## lionagi/studio/services/task_applications.py

- **ADR-0071 D1 architecture** — `TaskApplication` is the frozen submit shape
  every binding shares. This module wires the in-process binding
  (`submit_task`/`cancel_task`); any other binding calls these same
  functions rather than duplicating the contract. `submit_task` writes a
  durable `queued` row into `schedule_runs` (ADR-0071 D2 generalized task
  entity, `schedule_id` NULL) as a plain INSERT (no prior CAS state to
  guard); every status move after routes through
  `lionagi.state.transitions.transition()`. No worker/lease loop or remote
  execution lives here — `execution_target`/`library_ref` record provenance
  (ADR-0073). `required_capabilities` derives the
  D4 host-scoped `concurrency_key` at submit time only (`capabilities.py`);
  claim-time eligibility/affinity matching lives in `worker.py`.
- **Action-kind vocabulary widen** — ADR pair adds `"workflow"` (ADR-0073
  registry-resolved definitions) to the launcher vocabulary — a CHECK widen,
  reusing the launcher's closed set + `"playbook"` alias rather than a
  second copy.
- **`idempotency_key`** — Part of the ADR-0072 dedup submit contract:
  `submit_task` rejects a non-`None` value rather than silently
  double-enqueueing a retried application.
- **`_derive_concurrency_key` (D4 rule)** — Only serialization-class tokens
  (per `capabilities.py`'s token→class map) fold into a host-scoped
  `concurrency_key`; eligibility/affinity-only tasks get none.
- **`cancel_task`** — Only `queued -> cancelled` is permitted
  (`transitions.py`'s ADR-0071 vocab gate rejects any other move, e.g. out
  from a leased/running row).

## lionagi/studio/services/db_maintenance.py

- **`prune_old_data` FK safety** — `branches` CASCADE on `sessions`;
  `artifacts`/`plays`/`team_messages`/`dispatch_outbox` have soft FKs (no
  CASCADE), so `session_id` is nullified before DELETE.
  `schedule_runs.chain_parent_id` and `dispatch_outbox.schedule_run_id` are
  nullified before parent delete.
- **`dispatch_outbox` retention (ADR-0059 delta 3)** — Two separate windows:
  terminal success (delivered/acked) and dead-lettered/expired.
  pending/delivering rows are excluded from both. Unlike the session branch,
  `status_transitions` rows for purged dispatch ids are left in place — no FK
  from `status_transitions` to `dispatch_outbox` (ADR-0057 D2), and the
  dispatch transition trail is the compact audit record this delta exists to
  keep, not the high-volume history the session branch cascades away.
- **Orphan cleanup** — Scoped to pruned lineage only; never touches rows
  outside it, to avoid a newborn-orphan race where `_persist.py` commits a
  progression before the session row exists.
- **Audit event ordering** — Runs after the prune transaction commits;
  `insert_admin_event` opens its own write transaction, and nesting it inside
  the prune transaction would self-deadlock on the sqlite write lock.

## lionagi/studio/services/leo.py

- **Security boundary** — Mutating tools never execute; they return a
  `proposed_action` dict as an SSE payload — confirmation and the actual
  endpoint call belong to the client, not this service. `ui_command` tools
  return a declarative command dict intended for client-side handling
  (navigation, form prefill) — commands never mutate server state. Sessions are in-memory (server restart clears
  history); auth is the studio bearer-token gate at app-level middleware,
  same as every other route.
- **No `from __future__ import annotations`** — Leo tool callables are
  introspected by `function_to_schema`, which requires real (non-string)
  parameter annotations.
- **Session registry bounding** — Capped at `_MAX_SESSIONS` so a long-running
  server doesn't grow the dict forever: capacity eviction drops the
  least-recently-used session, idle eviction sweeps sessions untouched for
  `_IDLE_EXPIRY_SECONDS`. Both run lazily on create/access — no background
  timer.
- **`_run_turn`** — Scans only the messages `Branch.ReAct()` appends during
  this turn for `proposed_action`/`ui_command` outputs, so a proposal
  surfaced on an earlier turn never resurfaces later. Must only be called
  while holding `sess.lock`.

## lionagi/studio/services/stats.py

`_ACTIVITY_WINDOWS` folds the ADR-0057 D1 seven-value session status
vocabulary into four Pulse-sparkline buckets: `timed_out` joins `failed`
(both terminal non-success), `aborted` joins `cancelled` (both deliberate
stops). `get_stats_route` intentionally reads the runs count from SQLite
sessions (not `runs_svc.list_runs()`, which reads filesystem dirs and returns
a different count) so the dashboard matches the Runs list page.

## lionagi/studio/scheduler/admit.py

ADR-0071 D3: `admit(row, worker, db) -> AdmissionDecision` is the worker
claim loop's admission predicate, extracted to one named, StateDB-backed,
unit-testable function. It borrows `Processor.handle_denied`'s
terminal-vs-deferred return *shape* only (`True` = terminal, `False` =
deferred/re-enqueue) — never the `Processor` class itself, which is
`asyncio.Queue`-backed and in-process only, useless for a fleet of
independent CLI processes claiming from a shared `schedule_runs` table.

Conditions evaluated, in this order:

1. Duration guard (D6) — a job declaring `max_duration_seconds` at or above
   the worker's lease TTL is terminal-rejected: lease renewal is not yet
   shipped (ADR-0071 delta #5), so an admitted long-runner would just lose
   its lease mid-flight.
2. Capability match (`capabilities.worker_can_serve`) — a mismatch defers
   (row left `queued`, never faked).
3. Concurrency-key block — a matching key currently `running` (this pass or
   a prior one) defers the row to the next tick.
4. Waiter cap (D-Cap) — per `concurrency_key`, at most `key_concurrency *
   waiter_cap_multiplier` rows may sit `queued`/`retry_wait` behind a
   running holder. Over cap is a terminal rejection unless the submission
   opted into deferred/parked semantics (D-Reject).

GPU/bench-window locks are never consulted here: `admit()` only ever reads
StateDB. Machine-local lock acquisition and arbitration stay a worker-side
execution responsibility (ADR-0071 D5's own stated limit, reaffirmed by D3).

`action_args["admission"]` payload convention (documented shape inside the
existing free-form `args`/`action_args` dict, no schema change):

```text
{
    "max_duration_seconds": <float>,       # duration guard input
    "allow_deferred_over_cap": <bool>,     # opt out of terminal rejection
                                            # when the waiter cap is hit
    "notify": {
        "deliver_to": <str>,                # required, non-empty
        "kind": <str>,                      # optional, default "terminal_notify"
        "dedup_key": <str | None>,          # optional
    },
}
```

A `notify` payload with a field of the wrong type (e.g. `deliver_to` as an
int) is dropped by `notify_request()` rather than surfaced — it must never
crash the claim loop for a row that is already correctly skipped. A
claim-time terminal rejection must still surface observably even though the
submitter is no longer on the wire by then: `worker.py`'s claim loop, on a
terminal `AdmissionDecision`, transitions the row `queued -> skipped`
carrying the reason and — whenever `notify_request()` finds a notify
payload — emits a `dispatch_outbox` row via
`lionagi.dispatch.outbox.enqueue_dispatch`.

## lionagi/studio/scheduler/engine.py

**`_reserve_max_runs_budget`** — reserves one top-level fire against a
schedule's `max_runs` cap. A fire consumes budget the instant it fires, not
when it resolves, so the count it checks is `fired + inflight`: `fired` is
the persisted count of `running`-or-terminal `schedule_run` rows, and
`inflight` is an in-process counter of fires that have claimed budget but
whose occurrence row has not yet committed. The two are disjoint views of
the same fire — a claim is released the moment the occurrence row lands —
so summing them counts each fire exactly once, except for a brief instant
during the handoff where a fire can appear in both; that only ever
over-counts, which just causes a spurious refusal that self-corrects on the
next tick. Only one scheduler process runs today, so this reservation is
in-memory (an `asyncio.Lock`-guarded dict), not a database compare-and-set.

The order of the two reads inside that lock is the load-bearing part.
`inflight` is read *before* the `await` on `count_schedule_runs()`, not
after. `release()` (called from `_fire()`'s `finally` block on every exit
path, so a claim always gets freed even from a cancelled or failing fire)
does not take the lock — it must work even while a fire is mid-cancellation,
where acquiring a lock from a `finally` block risks a deadlock. That makes
it possible for a concurrent fire to release its claim *while this call is
suspended awaiting the database*. If `inflight` were read after that await,
a fire that both writes its occurrence row and releases its claim entirely
inside the suspended window would vanish from both counts at once: too late
for the in-flight snapshot (already released) and too early for the
persisted count (the read started before the write landed) — letting a
bounded schedule fire one more time than `max_runs` allows. Reading
`inflight` first means it still captures that other fire's claim before it
can disappear, so the sum can only ever over-count, never under-count.

`_tick()`'s ad-hoc task-worker pass runs single-flight: a second `_tick()`
firing while the first pass is still in progress must not start a second
pass, and must not await the first pass either — a slow or hung worker pass
would otherwise stall every schedule's due-time evaluation for the whole
tick. `_tick()` starts the pass as a background task and returns promptly
regardless of whether it is still running.

`resolve_terminal` (child-session outcome inference) does not trust a
leader process's exit code as evidence that a still-running child session's
own work has finished — the terminal stamp comes from the leader's stderr
pipe closing, not from the child's work actually ending. A child session
that has not reached any terminal status of its own is reported as
`completed_empty` (no positive evidence) rather than being inferred as
`completed`.

## lionagi/studio/scheduler/worker.py

`claim_and_execute`'s D4 match rule: row R is claimable iff its capability
tokens are a subset of `advertised_capabilities` AND its `execution_target`
is in `execution_targets` (NULL/empty target = claimable by anyone).
Candidates are paged oldest-first through a `(queued_at, id)` keyset cursor
until `limit` eligible candidates are found or the queue is exhausted,
bounded by `_MAX_CLAIM_SCAN_ROWS` (a fairness/latency cap, not a correctness
cap — a later pass resumes the same order); a long prefix of unservable rows
never permanently hides an eligible row behind it, unlike a fixed-size
prefetch window.

If `worker_id`'s heartbeat is older than `heartbeat_ttl`, the pass claims
nothing (in-flight leases still recover via `reap_expired_leases`); a worker
with no heartbeat history yet is not treated as stale.

Returns the number of rows claimed, regardless of execution outcome. Each
claim is one guarded CAS (`queued -> running`); a lost race or a row another
caller already moved is skipped, not retried within this pass. A terminal
admission rejection never counts toward the returned total.

## lionagi/studio/scheduler/signals.py

Mint site: `SchedulerEngine._fire_inner()`, immediately after each of the
three `_guarded_terminal_status("schedule_run", ...)` calls returns `True` —
the one choke point every scheduled run's terminal write already passes
through (in-process, synchronous with the commit, no polling latency). The
module stays agnostic about *where* that mint happens: the signal classes
and `SchedulerSignalBus` only need the same status/reason_code/entity_id
fields a generic post-commit hook on `LifecycleService.transition()` would
eventually carry, so promoting the mint site later is a call-site move, not
a redesign of this module. `LifecycleService` and `StateDB` schema are
untouched by this module — it imitates the shape of the existing in-run DAG
signal bus (`lionagi.session.signal`/`observer`) without reusing its
Flow/route/stream machinery, none of which a scheduler daemon process needs
(`schedule_runs` is already the durable record).

Failure semantics: `SchedulerSignalBus.emit` never swallows a handler
exception. Handlers run concurrently with `return_exceptions=True`; any
failures are raised together as an `ExceptionGroup` after every handler has
had a chance to run. A handler-raised `CancelledError` cannot be nested in
`ExceptionGroup`, so it is surfaced as the distinct
`SchedulerHandlerCancelled` marker instead — the mint call site (`engine.py`)
records either form. Cancellation of the emitter task remains a plain
`CancelledError` and propagates, so a broken handler is visible without
stopping unrelated schedules or swallowing scheduler shutdown.

## lionagi/studio/operator/

The Operator (ADR-0083) is a durable, human-gated chat surface over Studio:
one long-lived conversation per project view, backed by SQLite
(`store.py`'s `OperatorStore`), that can read run state and propose
mutating actions but never executes one without an explicit human decision.
The package splits into the durable store, a Branch-backed engine that
drives one provider turn (`engine.py`), a coordinator that wires turns to
proposals (`coordinator.py`), a set of MCP-exposed read/write tools
(`application_mcp.py` plus the per-action adapters), and shared
redaction/catalog helpers.

### Turn identity and the propose/poll/execute pattern

Every mutating tool in this package — `cancel_run`, `resume_run`,
`rename_session`, `launch_playbook` (`application_mcp.py`), and the
stdio permission bridge (`permission_mcp.py`) — follows the same shape.
The tool reads its durable turn identity from three environment variables
(`LIONAGI_OPERATOR_DB_PATH`, `LIONAGI_OPERATOR_CONVERSATION_ID`,
`LIONAGI_OPERATOR_REQUEST_ID`) set by the daemon around the provider
subprocess; a real MCP call always has them, and their absence is treated
as a hard configuration error, not a soft fallback. The tool then resolves
its target, builds a command payload, and calls `OperatorStore.create_proposal`
with a `risk` tier (`mutate`/`execute`/`admin`) and an idempotency key
derived from `canonical_hash` — the same command submitted twice (e.g. a
retried HTTP request) reuses the first proposal rather than creating a
second one. It polls `get_proposal` until the status leaves `pending`
(expiring it past its deadline if needed) and returns a redacted result.
The proposal only carries the *decision*; the actual state-changing act
lives in a paired `execute_*_command` function
(`execute_cancel_command`, `execute_resume_command`,
`execute_rename_session_command`), invoked by the coordinator only once a
human allows the proposal. This split matters for ownership: a command
captures the project it resolved against at proposal time, but every
`execute_*` function re-fetches the target row and re-checks that project
match immediately before writing, because a human's approval window is a
gap the target's project (or status) can change across. Re-checking only at
resolution time would let a race during that gap either write to a run that
changed ownership or silently no-op in a way the caller couldn't
distinguish from success.

`cancel_run` additionally reuses `lionagi.cli.kill`'s exact primitives
(`_kill_one`, `_list_running_children`) so a cancellation from the Operator
is indistinguishable from one issued through `li kill --recursive`: running
children are signalled deepest-first so none are orphaned, and
`_persist_cancel`'s guard against a non-`running` row means a race that lets
the process finish naturally during the approval window degrades to
`already_terminal` rather than a double- or wrong-run cancel. `resume_run`
is a distinct operation from un-pausing: the session lifecycle policy
(`lionagi/state/lifecycle/policy.py`) has no edge back out of a terminal
status such as `cancelled`, so resuming never reopens the old run's status —
it launches a new invocation that either continues an `agent` run's branch
with a new instruction (`li agent -r`) or replays a `play`/`flow`/`show-play`
run's persisted checkpoint (`li o flow --resume`), and the two argument
shapes are mutually exclusive because the checkpoint owns the plan for the
second kind. `rename_session` renames a run's own record; it is
deliberately separate from renaming the Operator conversation itself
(`OperatorStore.update_conversation`), since one conversation can discuss
many runs across its life.

### Resolving a run reference

`run_progress.py::resolve_run` (reused by `run_findings.py`,
`resume_run.py`, and `rename_session.py`) and `cancel_run.py`'s own
`_resolve_run` both accept the same reference vocabulary: an exact run/session
id, an 8+ hex id prefix, a name or playbook substring (minimum 3
characters), or the literal `"current"`. Id/prefix resolution reuses
`lionagi.cli._util.fetch_unique_row`, the same primitive `li kill` uses, so
a reference this tool accepts resolves identically to one typed at the CLI.
Every arm is scoped to the calling turn's own project — read from the
turn's stored context, never accepted as an argument — and a foreign
project's run is reported exactly like a nonexistent one (`{"found": False}`)
rather than surfaced as an ambiguity candidate or a resolved id, either of
which would itself disclose that the id exists. A turn with no owner
mapping at all raises `MissingOwnerContextError` rather than falling back
to matching every project's runs (both modules keep their own small copy
of this check and error rather than sharing one, so each stays
self-contained). Nothing is ever guessed: an ambiguous prefix or substring
match returns candidates instead of picking one, capped at `MAX_CANDIDATES`
(10) with a `truncated` flag when more existed.

`"current"` resolves against the turn's own frozen context, not a
later-reported live view — deliberately, because a cancellation or rename
acting on a view reported *after* the instruction was sent could target a
run the human was never looking at when they issued it. (Contrast this
with `get_current_view`, below, which does prefer a later live report — a
read has no such hazard.) The frontend's selection payload for this key is
carried under `"s"` for mission/history views (`OperatorPanel.tsx`'s
`selection` writer) and under `"sel"` for the library space; there is never
a `runId`/`run_id`/`sessionId` key on the wire for the former, which is why
`cancel_run._current_run_id` reads `"s"` specifically while
`run_progress._resolve_current` also tries the longer names for the paths
that do use them.

### View freshness: observation count, not wall clock

The Operator tracks "where is the human right now" (`space`, `route`,
`selection`, `filters`) two ways: as a snapshot frozen onto each turn's
context, and as an out-of-band report a page can send any time via
`OperatorStore.record_view`. Ordering between a turn's frozen snapshot and
a later view report — and between two view reports from the same page — is
by `observation_seq`, an integer each page increments for the views it
sends, never by server arrival time or a wall clock. Arrival order is
wrong because a page can queue a report before an instruction is sent and
have it land after; a wall clock is wrong because it can step backwards and
leave a stale view holding the higher timestamp. The `observer_id` that
travels with every count identifies which page produced it, because a
count means nothing compared across pages — two browser tabs on one
conversation are two independent counters, and a reload starts a new one.
`record_view` gives each observer its own row for the same reason: a
shared row would let a delayed report from an older navigation overwrite a
newer page's high-water mark. A report that does not count higher than
that observer's own stored count is discarded outright.

`application_mcp.py::get_current_view` is the read side: it starts from the
turn's frozen context and only swaps in the stored view when that view's
`observation_seq` is both present and strictly greater than the turn's own
— otherwise the turn's snapshot is the most defensible answer available.
The response reports whether the returned context is `"turn"` (nothing
newer confirmed) or `"live"` (a fresher report exists), but never echoes
the raw sequence number itself, since a bare count invites exactly the
cross-page comparison this mechanism exists to prevent. `OperatorViewReport`
(`types.py`) requires both `observation_seq` and `observer_id` for this
reason — a report that cannot say who saw it or where it fell in their
sequence can never be ordered against anything, so it is rejected outright
rather than stored with a misleading freshness label.

### Model catalog, provider selection, and effort clamping

`catalog.py` is the single source of truth `GET /operator/models` renders
from and the coordinator validates every turn's selection against before
it reaches a provider CLI; model ids and effort ceilings are grounded in
the provider request models themselves (`ClaudeEffort` for `claude_code`,
the codex effort-ceiling tables in `service/providers.py` for `codex`, and
`resolve_agy_model` — which folds effort into the model name rather than
taking a separate parameter — for `gemini_code`). `model_effort_choices`
derives the efforts it offers for a given model from the same clamp
functions the request-build path applies (`_clamp_claude_effort`,
`_clamp_codex_effort`, `_clamp_gemini_effort`), rather than restating a
second copy of the ceilings: an effort is offered only when clamping it
for that model is a no-op. This matters because the request path clamps
silently — a non-Opus Claude model turns `xhigh` into `high`, most Codex
models turn `max`/`ultra` into `xhigh`, Gemini Pro has no Medium tier — and
offering a value the request would silently change is worse than not
offering it: the operator picks one level and a different one runs with
nothing said. `resolve_selection` validates a client's requested
`(provider, model, effort)` the same way: when a model is named, effort is
checked against that specific model's ceiling rather than its provider's
whole vocabulary, so a stale client can't pin an effort the request path
would then quietly reduce.

### Provider session identity vs. durable branch identity

`OperatorStore` tracks two different kinds of continuity for a
conversation, and conflating them was a real bug each fixes. A **provider
session id** (`claim_resolved_pair`, `set_provider_session_id`) is a
resumability token that belongs to the exact `(provider, model)` pair that
created it — resuming a Claude session against Codex is meaningless.
Before `claim_resolved_pair` existed, only an explicit pin change could
invalidate a stored session; an *unpinned* conversation runs on whatever
the environment resolves to, re-read every turn, so moving the
process-wide default provider silently tried to resume a session that
belonged to the old pair. `claim_resolved_pair` compares the pair about to
run against the pair that last ran and drops the stored session (returning
`None` to resume with) exactly when they differ. A `NULL` stored pair — no
turn has recorded a resolution yet, true for every conversation alive when
this shipped — is not treated as a mismatch, which would otherwise drop
every live session once at upgrade. `clear_provider_model` removes an
explicit pin (an omitted `model` on a turn means "keep the current pin",
so there has to be a separate way to ask for "no pin"), and drops the
provider session as a consequence of the pair changing, not as its own
separate effect — clearing an already-unpinned conversation is a no-op.

A **branch id** (`claim_branch_id`) is a different, longer-lived identity:
the id every turn of a conversation builds its in-process `Branch` with.
Before this existed, every turn constructed a brand-new `Branch()` with a
fresh random id, so the CLI's own persistence path
(`setup_agent_persist` in `lionagi/cli/_runs.py`) saw a never-before-seen
id each time and created a new `sessions` row per turn instead of one row
per conversation. Feeding back the same claimed id lets that existing
"resume" logic run instead — it looks up the id in the `branches` table
and appends to the existing session when found. `claim_branch_id` mints
the id once (a fresh UUID) and persists it; every call after that returns
the same value. It is race-safe the same way `claim_resolved_pair` is —
wrapped in the store's `BEGIN IMMEDIATE` transaction, so two turns racing
to claim the first id block against each other rather than minting two.
Note that this only ever stores an identity, never a live `Branch` object:
turns arrive as separate HTTP requests, the daemon restarts between them,
and two browser tabs can drive one conversation at once, so no Python
object can be assumed shared across turns.

`fork_conversation` copies a source conversation's turns into a new,
independent one, but only turns that reached a terminal status
(`completed`/`failed`/`cancelled`) — a turn still streaming is left out, so
forking mid-turn ends the fork at the last completed turn rather than
copying a half-written one, and the source conversation keeps streaming
untouched. `up_to_sequence`, when given, additionally caps the fork at an
earlier point in history so a user can branch from any prior turn, not
only the tip. The fork starts with no provider session of its own, so it
never silently resumes the source's provider-side session.

### Redaction

`redact.py` is shared by every read tool (`run_detail`, `run_progress`,
`run_findings`) and by the tool-argument/artifact payloads those tools and
the write adapters project. `scrub_text` catches secrets and paths by
*shape*: known token prefixes (`sk-`, `ghp_`, JWT-shaped strings), header
and `Bearer` forms, `KEY=value`/`key: value` assignments under a
secret-marker name, and absolute POSIX/Windows paths (collapsed to their
leaf filename, so a path containing spaces, such as
`/home/someone/My Project/notes/secret.txt`, is matched and redacted as a
whole rather than only its first space-free segment). Shape-based matching alone misses a secret that doesn't look
like one — an arbitrary passphrase or short internal token echoed back
verbatim from a run's own environment, which a Studio-launched run
inherits from this server process. `known_secret_values` reads that
environment directly and returns every value stored under a
secret-marker-named key (4+ characters, to exclude incidental short
matches), and `scrub_text` strips any of those literal values in addition
to the shape-based patterns.

Whether a *field name* itself means a credential is decided once, by
`is_secret_field_name`, and every redaction path shares it — two divergent
copies of this rule used to exist, and a value under an `auth` key was
withheld on some projections and served on others. `fold_field_name`
normalizes separators first (`X-API-Key`, `api.key`, and `api_key` all fold
to the same spelling) so the marker list only needs one spelling per
concept. A short, closed set of names (`auth`, `authentication`, `bearer`)
is matched by exact equality rather than substring, because those words
also occur inside unrelated field names (`author`, `authorized_keys_count`)
that must not be redacted. `redact_arguments` applies the same field-name
judgment recursively, carrying the parent key down into nested containers
— without that, `{"auth": "..."}` was withheld while `{"auth": {"value":
"..."}}` was served, the same field-name gap recurring one level down.
Two independent byte caps (`cap_by_bytes` for a list of items,
`cap_payload_by_bytes` for one payload) bound aggregate response size after
redaction; `cap_by_bytes` fails closed on a single oversized item — an
item that alone exceeds the limit is elided rather than admitted whole,
so one huge newest item can't blank out every older, smaller one that
would otherwise fit.

### Bounded read projections: run_detail, run_progress, run_findings

All three read tools report **why** a run couldn't be read, not just that
it couldn't: `run_detail` returns `{"known": False, "source": "unavailable"}`
when the configured store cannot honestly be opened read-only (checked
both before and after the underlying carrier call, so a store that
disappears mid-call is reported as unavailable rather than as an empty
result), versus `{"known": False, "source": "store"}` when the store is
fine but no run matches — `get_run` collapses both situations into a bare
`None`, so `run_detail` runs its own preflight to tell them apart. Every
free-text field goes through `scrub_text` and `project` through
`public_project`; `manifest` (an unbounded mapping) goes through the
recursive redactor plus a byte cap. These fields are redacted even though
today's StateDB-backed carrier already fills most of them with safe
placeholders, because the projection has to be safe for what the field
names promise across every backing carrier (a manifest-backed builder
elsewhere fills the same names from raw, untrusted manifest text), not
just for the values one code path happens to supply right now.

`run_progress` reports operation counts two ways depending on what the run
has: for an ordinary run it counts branches by status; for a DAG run (one
with a `graph`) it instead derives per-node state from the session's
recorded lifecycle signals, because a planned node with no branch
materialized yet would otherwise be invisible to a branch count. Node
state is read from the same `NodeQueued`/`NodeStarted`/... signal kinds the
frontend's live SSE view and the in-run `Signal` bus both use, replayed
over persisted `session_signals` rows since a bounded read tool can't
subscribe to a live stream. `skipped` folds into the `completed` bucket
(the node will never run, but the edge condition that skipped it did what
it was written to do) while still being counted separately in
`skippedCount`, and `escalated` folds into `pending` while being counted in
`escalatedCount` — both counts are always present, including as zero, so a
caller can tell "nothing happened" from "the field doesn't exist yet."

`run_findings` derives tool-call outcomes (`success`/`error`/`pending`)
from message content via the same `_detect_status` heuristic
`lionagi.studio.services.runs` already uses for the run-detail step list,
since plain session messages carry no structured `ok: bool`. Every section
here is bounded by a message *window* (the carrier is called with a fixed
`message_limit`) before any byte cap ever applies — the byte cap almost
never fires in practice, so `truncated` reports both, and the response
carries `total` alongside `returned`/`items` so a caller can tell "the last
50 of 50" from "the last 50 of 48,000." The `errors` section's `partial`
flag propagates from the same window: branch and session status rows are
read in full, but the tool-call-derived half of the errors list was built
from a possibly-incomplete message window, and failing to say so would let
"no errors found" silently mean "no errors observed in what we happened to
load."
