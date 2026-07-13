# Studio Internals Reference

Non-obvious invariants, protocol contracts, and design rationale for
`lionagi/studio/` that don't belong inline as long-form comments. Terse
reference — not a narrative. Organized by module path.

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
