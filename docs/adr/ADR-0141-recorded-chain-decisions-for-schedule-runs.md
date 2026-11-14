# ADR-0141: Recorded chain decisions for schedule runs

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: scheduling-control-plane
- **Date**: 2026-11-04
- **Relations**: extends ADR-0070; amends ADR-0070 D4's selection input

## Context

ADR-0070 D4 (`docs/adr/ADR-0070-studio-scheduling-and-dispatch-delivery.md:442-476@45fd503ecf2af299d48b762cd895a2b8e650ced5`) lets a schedule declare `on_success` and `on_fail`. After a run's child process exits, the scheduler fires at most one follow-up. Two components decide which branch applies, and they read different inputs. Only the scheduler launches the follow-up. The monitor (`li monitor run`) decides whether to keep waiting for it. Nothing records the scheduler's choice, so the monitor derives it again.

The source and reader census in this record use commit `45fd503ecf2af299d48b762cd895a2b8e650ced5`.

**P1. The scheduler chooses from invocation status. The monitor chooses from exit code.** On the normal path the scheduler sets `success = inv_status == "completed"` after resolving the invocation's terminal status (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). A clean exit whose child never reached a terminal status resolves to `completed_empty`, which is not success (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`), and that status maps to a `failed` run (`lionhub/src/lionhub/orchestration/scheduler/engine.py::_SCHEDULE_RUN_STATUS_FROM_INVOCATION`). The monitor tests for a branch using `exit_code == 0` alone (`lionhub/src/lionhub/serve/cli/monitor.py::_schedule_declares_chain_action`). For an exit-0 child whose invocation is `completed_empty`, the scheduler selects `on_fail` while the monitor looks for `on_success`. If no `on_success` is stored, the monitor does not wait, and `li monitor run` returns the parent's result while the follow-up runs.

**P2. Edits after the run change the monitor's answer, not the scheduler's.** The monitor reads the stored schedule when it checks (`lionhub/src/lionhub/serve/cli/monitor.py::_schedule_declares_chain_action`, through `db.get_schedule`). The scheduler selects from the dict the fire received on the normal path (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`), and from a definition read during recovery (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`). An edit between the two makes them disagree.

**P3. The scheduler chains after losing its terminal write.** The schedule_run terminal write is a compare-and-set on `status = running` (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._guarded_terminal_status`). On the normal path its result gates only the signal (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). The follow-up block (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`) does not read it, so a fire whose row another writer already finalized still launches its `on_fail` follow-up. One such writer is the lifecycle reaper, which writes `timed_out` for stale runs (`lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_stale_schedule_runs`). Dispatched recovery does stop on a lost compare-and-set (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`).

**P4. The decision is not recorded.** The schedule_run row keeps `exit_code`, `status` and `status_evidence_refs` (`lionagi/state/schema.sql:622-647@45fd503ecf2af299d48b762cd895a2b8e650ced5`). The refs written by the terminal write name the invocation or the schedule (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`, `lionhub/src/lionhub/orchestration/scheduler/scheduler_state.py::create_skipped_run`), not the decision. The monitor therefore has to derive it again.

Decision table:

| Concern | Decision |
|---------|----------|
| Which branch a run selected (P1, P2, P4) | D1: the scheduler records the selection as one `chain_decision` ref in its terminal write, computed before the compare-and-set |
| Whether the follow-up fires (P3) | D2: the follow-up fires only when that compare-and-set won |
| Runs that are terminal at creation | D3: they record the `none` selection in the write that first carries their refs |
| What the monitor waits for (P1, P2) | D4: the monitor follows a usable v1 ref and keeps the existing rule when there is none |
| Storage | D5: no schema change; one additive kind in an existing JSON list |

Out of scope, with the reason for each:

- **Admission acknowledgement and waiting beyond the grace.** Acknowledging that a child was admitted needs a second write or a receipt, which this ADR does not add (see Consequences, and the follow-up named there).
- **`li wait`.** It returns when the waited row is terminal and has no chain handling (`lionhub/src/lionhub/serve/cli/wait.py::wait_for_terminal`). It is unchanged.
- **The queued-task worker lane.** Exhausted-lease and execution terminal transitions use legacy `transition()`, whose reason-column flag is off (`lionhub/src/lionhub/orchestration/scheduler/worker.py::reap_expired_leases`, `lionhub/src/lionhub/orchestration/scheduler/worker.py::_execute_claimed`; `lionagi/state/lifecycle/adapters.py::run_legacy_transition`). Terminal admission rejection instead writes `skipped` and reason columns through `update_status`, without supplying a chain-decision ref (`lionhub/src/lionhub/orchestration/scheduler/worker.py::claim_and_execute`, `lionhub/src/lionhub/orchestration/scheduler/worker.py::_reject_claim`). These rows keep the existing monitor rule (Consequence 9).
- **The rest of ADR-0070 D4.** D4's selection rule is amended on one input only: the scheduler selects from the invocation's terminal status, and D4's `exit_code == 0` line is superseded by it (D1). The cap, the branch keys `on_success`/`on_fail`, and the inline recursion stay as written.
- **Recovery's current-definition lookup.** It stays. Consequence 4 records what that costs.
- **Rows written before this change.** They keep the existing monitor rule. History is not rewritten.

## Readers of stored evidence refs

At the source commit, `git grep` finds `status_evidence_refs` or `evidence_refs` on 111 lines in 21 non-test files under `lionagi/` and `apps/`. The `lionagi/` subtotal is 103 lines in 17 files. Including the moved modules under `lionhub/src/` brings the non-test total to 201 lines in 31 files.

Each reader below is listed with what it does with the list and what it does with an entry whose `kind` it does not know. Writers are skipped; the list of skipped writers follows the table.

| # | Reader | What it does with the list | Unknown kind | Reached by schedule-run refs |
|---|--------|----------------------------|--------------|------------------------------|
| R1 | `lionhub/src/lionhub/serve/cli/monitor.py::_detail_session` | Prints each entry as `label`, else `kind`, for completed session rows | Printed by kind name | No: session rows only |
| R2 | `lionhub/src/lionhub/serve/cli/status.py::_build_view`, `lionhub/src/lionhub/serve/cli/status.py::_render_human` | Prints `label` or `kind`, then `path`, `id` or `ref` | Printed as `kind: ref`; the new element prints as `chain_decision: on_fail` | No: `resolve_any_target` (`lionhub/src/lionhub/orchestration/inspection.py::resolve_any_target`) has no schedule-run lookup; only `lionhub/src/lionhub/serve/cli/wait.py::_resolve_wait_target` adds one, and `li wait` does not print refs. |
| R3 | `lionhub/src/lionhub/serve/cli/monitor.py` wait path | None today | n/a | Yes, by design (D4) |
| R4 | `lionagi/state/db.py::StateDB._reaper_evidence_exclusion_sql`, `lionagi/state/db.py::StateDB.metric_value` | Tests for the reaper's `kind == "invocation"` entry | Ignored (the predicate tests one kind) | No: reads the `invocations` table only |
| R5 | `lionagi/state/db.py::StateDB._row_to_dict` | Decodes JSON text into a list. No interpretation | Passed through as a dict | Yes, pass-through |
| R6 | `lionagi/state/db.py::StateDB.get_schedule_history` | Returns `status_transitions.evidence_refs` verbatim for `entity_type = 'schedule'` only | Passed through | No: schedule-run history is not returned |
| R7 | `lionagi/studio/services/run_view.py::get_run_view`, `lionagi/studio/services/run_view.py::list_run_views` | Returns the schedule-run row merged with the run view (`{**run, ...}`) | Passed through to the HTTP API | Yes. The key reaches API clients. The Studio schedule-run types (`apps/studio/frontend/src/lib/types.ts:462-495@45fd503ecf2af299d48b762cd895a2b8e650ced5`) declare no such field, so the Studio client ignores it |
| R8 | `lionagi/studio/services/run_view.py::_with_skip_reasons` | Reads `reason_code` and `reason_summary` of skipped runs from `status_transitions`, not the refs | n/a | Unaffected |
| R9 | `lionagi/studio/operator/run_progress.py::_evidence_failed_op_ids` | Keeps entries with `kind == "failed_operation"` and a string `id` | Ignored | No: session rows (called at `lionagi/studio/operator/run_progress.py::_dag_progress`, `lionagi/studio/operator/run_progress.py::run_progress`) |
| R10 | `lionagi/studio/operator/application_mcp.py` `"status_evidence_refs",`, `lionagi/studio/operator/application_mcp.py::_INVOCATION_DROP` | Drops the field from session and invocation output | Never inspected | No |
| R11 | `lionagi/studio/services/invocations.py::_serialize_invocations`, `lionagi/studio/services/invocations.py::get_invocation`; `lionagi/studio/services/runs.py::get_run`; `lionagi/studio/services/sessions.py::get_session` | Parse and return the field for invocation and session rows | Passed through | No |
| R12 | `apps/studio/frontend/src/components/history/RunDetail.tsx:452-461@45fd503ecf2af299d48b762cd895a2b8e650ced5` | `evidenceFailedNodeIds`: keeps entries with `kind === "failed_operation"` and a string `id` | Ignored | No: session detail |
| R13 | `apps/studio/frontend/src/lib/api.ts:1511@45fd503ecf2af299d48b762cd895a2b8e650ced5`, `apps/studio/frontend/src/lib/api.ts:1553-1555@45fd503ecf2af299d48b762cd895a2b8e650ced5` | `SessionDetail` type: `Array<{ kind?, id?, label? }>` | Compiles. Extra keys are tolerated at runtime | No: session detail |
| R14 | `apps/vscode/src/api/types.ts:29@45fd503ecf2af299d48b762cd895a2b8e650ced5`, `apps/vscode/src/api/types.ts:78@45fd503ecf2af299d48b762cd895a2b8e650ced5`; `apps/vscode/src/runs/runDetailPanel.ts:313@45fd503ecf2af299d48b762cd895a2b8e650ced5`, `apps/vscode/src/runs/runDetailPanel.ts:329-345@45fd503ecf2af299d48b762cd895a2b8e650ced5`, `apps/vscode/src/runs/runDetailPanel.ts:357-372@45fd503ecf2af299d48b762cd895a2b8e650ced5` | Typed as records. The run and invocation banner payloads copy the list | Passed through | No: run and invocation detail |
| R15 | `apps/vscode/media/runDetail.js:328-336@45fd503ecf2af299d48b762cd895a2b8e650ced5` | Shows the count of entries as "N evidence ref(s)" | Counted as evidence. Misleading | No: run and invocation banners |

Skipped writers: `lionhub/src/lionhub/orchestration/scheduler/engine.py` (the terminal, selection and setup-failure writes), `lionhub/src/lionhub/orchestration/scheduler/scheduler_state.py::create_skipped_run`, `lionhub/src/lionhub/orchestration/scheduler/scheduler_state.py::resolve_invocation_terminal`, `lionagi/studio/services/admin.py::TransitionBody`, `lionagi/studio/services/admin.py::transition_sessions` (it reads only the list its caller passes and appends its own kinds), `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_stale_invocations`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_null_status_sessions`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_phantom_sessions`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_phantom_sessions_detailed`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_stale_plays`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_stale_schedule_runs`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::_REAPABLE_SHOW_STATUSES`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::_recompute_show_status_from_disk`, `lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_stale_shows`, `lionhub/src/lionhub/orchestration/runs.py::_teardown_common`, `lionhub/src/lionhub/orchestration/runs.py::_write_terminal_session_status`, `lionhub/src/lionhub/serve/cli/orchestrate/flow.py::_resolve_invocation_terminal_flow`, `lionhub/src/lionhub/serve/cli/orchestrate/flow.py::_run_flow`, `lionhub/src/lionhub/orchestration/kill.py::_persist_cancel`, `lionagi/studio/services/shows.py::import_shows`, `lionagi/studio/services/launches.py::_spawn_detached`, `lionagi/state/_mirror_common.py::reconcile_status`, `lionagi/state/transitions.py::StateReason`, `lionagi/state/transitions.py::transition`, `lionagi/state/lifecycle/models.py::ReasonRecord`, `lionagi/state/lifecycle/service.py::SQLAlchemyLifecycleService.append_history_in_transaction`, `lionagi/state/lifecycle/service.py::SQLAlchemyLifecycleService._write`, the writes in `lionagi/state/db.py`, the schema declarations in `lionagi/state/schema.sql`, `lionagi/state/schema_meta.py` and `lionagi/state/schema_migrations.py`, and `lionhub/src/lionhub/agent/casts/emission.py::ComplianceVerdict`, whose `evidence_refs` is an unrelated `list[str]` field.

No reader breaks on an unknown kind. R15 would count the element as evidence if it ever received a schedule-run list; it is not reached by schedule-run rows today, and D1 does not change that. R7 is reached: the list, element included, passes through to HTTP API clients, and the Studio client declares no field for it.

## Decision

### D1: The scheduler records its selection in the terminal write

The terminal write of a schedule_run adds one element to its `evidence_refs` list:

```text
{"kind": "chain_decision", "v": 1, "ref": "on_success" | "on_fail" | "none"}
```

`ref` names the schedule's branch key that the run selected, or `none`. The element follows the documented entry shape (`lionagi/state/schema.sql:796@45fd503ecf2af299d48b762cd895a2b8e650ced5`, `{kind, id|path|ref, label?}`), so readers that print `path`, `id` or `ref` show the selection (R2). `v` is the element's version.

The selection is computed before the compare-and-set:

```python
chosen = _selected_chain_action(schedule, success)     # lionhub/src/lionhub/orchestration/scheduler/engine.py::_selected_chain_action
selection = (
    "none"
    if chosen is None or chain_depth >= _MAX_CHAIN_DEPTH  # lionhub/src/lionhub/orchestration/schedule/parsing.py::_MAX_CHAIN_DEPTH
    else ("on_success" if success else "on_fail")
)
```

Normal path: the terminal write at `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner` becomes `evidence_refs=[{"kind": "invocation", "id": inv_id}, chain_decision]`.

Dispatched recovery: the terminal write at `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans` gets the same element.

Exact semantics:

- The input is the `schedule` dict that `_fire_inner` received (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`) on the normal path. It is the same object the follow-up overlay is built from (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`).
- On recovery the input is `get_schedule(sid)`, read before the compare-and-set. Today that read follows the compare-and-set (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`). D1 moves it above `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`.
- If that read returns None, the selection is `none`. Today the chain is skipped there (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`).
- `success` is the value the scheduler already computes: `inv_status == "completed"` on the normal path (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`), and `run_status == "completed"` on recovery (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`). For the scheduler, the selection input is therefore the invocation's terminal status, and this supersedes ADR-0070 D4's `exit_code == 0 and on_success present -> on_success` line: an exit-0 child whose invocation is `completed_empty` selects `on_fail`.
- A stored branch that the launch predicate rejects, such as one with an unusable kind, gives `none`. The predicate is the one that launches the child (`lionhub/src/lionhub/orchestration/schedule/parsing.py::_chain_action_with_usable_selected_kind`). The warning that `_selected_chain_action` logs today (`lionhub/src/lionhub/orchestration/scheduler/engine.py::_selected_chain_action`) is still logged. It is now logged before the write.
- A depth at or above the cap gives `none`. Today the cap is checked before selection (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). D1 moves it into the selection, so a recorded branch always has a child to launch.
- No stored branch gives `none`.

Gate: the element is written in the same `UPDATE` as `status`, under the same compare-and-set.

- Path: `_guarded_terminal_status` (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._guarded_terminal_status`), then `SchedulerStateService.update_status` (`lionhub/src/lionhub/orchestration/scheduler/scheduler_state.py::_DBSchedulerStateService.update_status`), then `StateDB.update_status` (`lionagi/state/db.py::StateDB.update_status`, delegating at `lionagi/state/db.py` `return await _lifecycle_adapters.run_update_status(`), then `run_update_status` (`lionagi/state/lifecycle/adapters.py::run_update_status`). That function calls `service._transition(...)` without the reason-column flag (`lionagi/state/lifecycle/adapters.py::run_update_status`).
- `_transition` defaults `write_reason_columns` to `True` (`lionagi/state/lifecycle/service.py::SQLAlchemyLifecycleService._transition`). With the flag set, `_write` puts `status_evidence_refs = :evidence_refs` in the same `UPDATE` as `status` (`lionagi/state/lifecycle/service.py::SQLAlchemyLifecycleService._write`). That statement is guarded by `status = :previous_status` (`lionagi/state/lifecycle/service.py::SQLAlchemyLifecycleService._write`). The status change and the ref are one statement.
- The policy's `reason_columns` defaults to `True` (`lionagi/state/lifecycle/models.py::LifecyclePolicy`). The schedule_run registration does not override it (`lionagi/state/lifecycle/policy.py::build_default_registry`). That flag is read only by `LifecycleService.transition` (`lionagi/state/lifecycle/service.py::SQLAlchemyLifecycleService.transition`), which the scheduler does not call.
- Result: the flag is true for schedule_run terminal writes on the normal path and on dispatched recovery. Two other writers differ. The lifecycle reaper also goes through `db.update_status` and keeps the flag (`lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_stale_schedule_runs`), but it is not one of the two paths named here, and it records no selection. The queued-task worker's exhausted-lease and execution terminal writes call `transition()`, whose legacy route forces `write_reason_columns=False` (`lionhub/src/lionhub/orchestration/scheduler/worker.py::reap_expired_leases`, `lionhub/src/lionhub/orchestration/scheduler/worker.py::_execute_claimed`; `lionagi/state/transitions.py::transition`; `lionagi/state/lifecycle/adapters.py::run_legacy_transition`). Refs written through that route are not stored. Terminal admission rejection instead writes `skipped` and reason columns through `StateDB.update_status`, without supplying a chain-decision ref (`lionhub/src/lionhub/orchestration/scheduler/worker.py::claim_and_execute`, `lionhub/src/lionhub/orchestration/scheduler/worker.py::_reject_claim`). Consequence 9 covers this.

Why this way:

- The choice and the status are one row write under one compare-and-set. A recorded selection exists exactly when the scheduler's terminal write won. D2 and D4 depend on that property.
- The choice is computed before the write. A crash before the write leaves no record, and the row stays `running` for startup reconciliation (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`, `lionagi/state/db.py::StateDB.list_dispatched_running_schedule_runs`).
- The decision is recorded, not its inputs. The monitor never derives the branch again for a row that has a record, so edits and exit-code/status differences do not matter for that row.

### D2: A lost terminal compare-and-set never chains

The follow-up block runs only when the schedule_run compare-and-set returned true.

Current structure on the normal path: `written = await self._guarded_terminal_status(...)` (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). `if written:` gates only the signal (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). The invocation write (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`), `_check_max_runs` (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`) and the chain (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`) run outside that gate.

D2 moves the chain under `if written:`. `_check_max_runs` stays where it is, because it does not depend on the chain.

Exact semantics:

- Compare-and-set won: the selection is written (D1), and the child is fired inline as today (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`).
- Compare-and-set lost: the `UPDATE` matches no row, so no selection is written. There is no signal and no child. The row keeps the other writer's status and evidence.
- Dispatched recovery: unchanged. `if not written: continue` already applies (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`).
- Setup failure: unchanged. It returns before any chain (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`).
- Cancellation: unchanged. The cancellation handler never launches a follow-up.
- The invocation write does not gate the chain. An invocation-side loss with a schedule_run win still chains. The schedule_run row is the one the monitor reads (`lionhub/src/lionhub/serve/cli/monitor.py::_poll_pending_once`).

Why this way: a lost compare-and-set means another finalizer owns the row's follow-on effects. Recovery already encodes that rule (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`). The normal path applies it too, which removes P3.

Behavior change: a run that another finalizer ended, such as a reaped `timed_out` run, no longer launches its follow-up when its process later exits. Today it does (P3).

### D3: Rows that are terminal at creation record `none`

A row created in a terminal state carries the `none` element in the first write that carries its evidence refs.

- `create_skipped_run` (`lionhub/src/lionhub/orchestration/scheduler/scheduler_state.py::create_skipped_run`). The INSERT names no evidence column (`lionagi/state/db.py::StateDB._build_schedule_run_insert_stmt`), so the first write with refs is the `update_status` at `lionhub/src/lionhub/orchestration/scheduler/scheduler_state.py::create_skipped_run`. Its list is `[{"kind": "schedule", "id": sid}]`. The element joins that list.
- Setup failure (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). The row is inserted by `_write_occurrence` (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`, `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._write_occurrence`) with no refs. The first write with refs is the `update_status` at `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`. Its list is `[{"kind": "schedule", "id": sid}]`. The element joins that list.
- The callers of `create_skipped_run` (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._record_missed_fire_skip`, `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._maybe_record_deferred`, `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._disable_for_budget_exhausted`, `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._evaluate_due_fire`) never reach selection. Their `none` describes what already happens.

Exact semantics: the element is `{"kind": "chain_decision", "v": 1, "ref": "none"}`. These rows launch no follow-up before or after this change.

Why this way: D4 makes the monitor return at once on `none`. Today these rows already resolve at once, because the monitor skips `cancelled` and `skipped` rows and rows without an exit code (`lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`). That rule is a status list. The element makes the same outcome a property of the row.

### D4: The monitor follows the recorded selection

Proposed helper, in `lionhub/src/lionhub/serve/cli/monitor.py`:

```python
def _recorded_selection(row: dict[str, Any]) -> str | None:
    """'on_success' | 'on_fail' | 'none' for a usable v1 chain_decision ref, else None."""
```

Input: `row["status_evidence_refs"]` as a list, or as JSON text. `get_schedule_run` decodes the column (`lionagi/state/db.py::StateDB.get_schedule_run`, `lionagi/state/db.py::StateDB._row_to_dict`). The monitor already accepts both forms for session rows (`lionhub/src/lionhub/serve/cli/monitor.py::_detail_session`).

A ref is usable only when all of these hold: it is a dict, `kind == "chain_decision"`, `v` is the integer `1` (not a boolean), and `ref` is one of the three values. Any other `v`, or a malformed element, is treated as no ref. The monitor writes one warning line for each affected row per wait, naming the run id and the reason. The CLI logging module re-exports `warn` (`lionhub/src/lionhub/orchestration/console.py` `from lionagi._log_channels import (`, `lionhub/src/lionhub/orchestration/console.py::__all__`; `lionagi/_log_channels.py::warn`). The implementation chooses the channel. The one-line requirement is fixed.

Cases in `_advance_chains` (`lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`), applied to a row that has become terminal, in this order:

- Chain following is off (`--no-chain`, so `chain` is false): resolved now, as today (`lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`). The recorded selection is not read.
- Status `cancelled` or `skipped`: resolved now, as today (`lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`).
- Selection `on_success` or `on_fail`: the row joins `awaiting_grace` with `_CHAIN_GRACE_TICKS` (`lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`). The child is found by `chain_parent_id` (`lionhub/src/lionhub/serve/cli/monitor.py::_find_chain_children`), as today. The stored schedule is not read.
- Selection `none` (new case): resolved now, whatever `exit_code` is. When nothing else is pending and no grace is open, the wait loop ends before its next sleep (`lionhub/src/lionhub/serve/cli/monitor.py::_dispatch_wait._chain_open`, `lionhub/src/lionhub/serve/cli/monitor.py::_dispatch_wait`).
- No usable ref: the existing rule applies unchanged, using `exit_code` and the stored schedule (`lionhub/src/lionhub/serve/cli/monitor.py::_schedule_declares_chain_action`, `lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`).

The final exit code of the wait is computed as today from the last resolved link (`lionhub/src/lionhub/serve/cli/monitor.py::_dispatch_wait`).

Why this way: the monitor reads the scheduler's decision, so the two agree for every row written after this change, whatever the stored schedule or exit code says. Rows without a usable ref keep the existing rule exactly, so an older monitor and a newer scheduler do not contradict each other in either direction. The two-tick bound (`_CHAIN_GRACE_TICKS`, `lionhub/src/lionhub/serve/cli/monitor.py::_CHAIN_GRACE_TICKS`) is not changed.

### D5: No schema change

- `schedule_runs.status_evidence_refs` is `JSON` (`lionagi/state/schema.sql:647@45fd503ecf2af299d48b762cd895a2b8e650ced5`). The change adds one element to a list that the column already holds.
- The schema version does not change. A store is refused only when its version is above the reader's `SCHEMA_VERSION` (`lionagi/state/db.py::StateDB._refuse_unless_native`, `lionagi/state/db.py::StateDB._refuse_newer_schema`), so this change does not cause refusals.
- Existing readers see the new element. The readers table above lists what each does with it.

## Consequences

1. **No admission receipt; the wait is bounded.** The monitor waits at most two ticks for a child. A child whose row is inserted after the grace is not followed, and `li monitor run` returns the parent's result. A follow-up issue tracks admission acknowledgement and waiting beyond the grace. It is #4207.
2. **A crash in the window looks like grace expiry.** A crash after the terminal write (with `on_success` or `on_fail` recorded) and before the child row is inserted leaves a terminal parent and no child. The monitor cannot tell this from a refusal or a grace expiry, and returns the parent's result. Startup reconciliation selects only rows still `running` (`lionagi/state/db.py::StateDB.list_dispatched_running_schedule_runs`; `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`), so the terminal row's follow-up is not re-fired.
3. **A child refused before its row exists looks the same.** `_fire_inner` can return False before the occurrence write (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). For a child, that leaves the parent's selection recorded and no child row. The monitor takes the grace path.
4. **Recovery keeps the current-definition lookup.** Recovery selects from the definition read at recovery time (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._reconcile_dispatched_orphans`). A chain child's overlay (`lionhub/src/lionhub/orchestration/schedule/parsing.py::_merge_chain_action`) is not persisted, so after a restart a recovered child cannot recover the overlay the original fire used. Edits made after a selection is recorded do not change that selection, because D1 writes it once.
5. **Behavior change for lost compare-and-sets.** A run that another finalizer ended, such as the reaper's `timed_out` write (`lionhub/src/lionhub/orchestration/scheduler/lifecycle.py::reap_stale_schedule_runs`), no longer launches its follow-up when its process later exits. Today it does (P3). A normal scheduler occurrence starts with `exit_code` unset (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). The reaper leaves that value unset and writes no `chain_decision` ref. The late exit supplies its exit code only in the terminal update guarded by `status = running` (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._guarded_terminal_status`, `lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`). That update loses, so the row keeps `timed_out` and no exit code. The monitor resolves the run without chain grace and returns 1 when that run is watched alone (`lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`, `lionhub/src/lionhub/serve/cli/monitor.py::_dispatch_wait`). The reaper preserves a previously populated exit code, if any. In that case the legacy no-ref rule still uses the stored code and schedule.
6. **`li wait` does not follow chains.** It returns when the waited row is terminal (`lionhub/src/lionhub/serve/cli/wait.py::wait_for_terminal`). A caller who needs a follow-up's result must use `li monitor run`. This ADR does not change `li wait`.
7. **Rows written before this change keep the old rule.** That includes rows written by the queued-task worker and by any other finalizer. The old disagreement with the scheduler remains for those rows. The change does not repair history.
8. **Each terminal write and its history copy grow by one element.** The status history row copies the same list (`lionagi/state/lifecycle/service.py::SQLAlchemyLifecycleService.append_history_in_transaction`, `lionagi/state/schema.sql:787-796@45fd503ecf2af299d48b762cd895a2b8e650ced5`). Of the readers of `status_transitions`, only `get_schedule_history` returns its `evidence_refs`, and only for schedule definitions (`lionagi/state/db.py::StateDB.get_schedule_history`). The others select counts, identifiers or reason columns, or delete rows.
9. **The queued-task worker lane keeps the existing rule.** Exhausted-lease and execution terminal writes use legacy `transition()` with reason columns off (`lionhub/src/lionhub/orchestration/scheduler/worker.py::reap_expired_leases`, `lionhub/src/lionhub/orchestration/scheduler/worker.py::_execute_claimed`; `lionagi/state/lifecycle/adapters.py::run_legacy_transition`), so refs written through that route are not stored. Terminal admission rejection instead writes `skipped` and reason columns through `StateDB.update_status`, without supplying a chain-decision ref (`lionhub/src/lionhub/orchestration/scheduler/worker.py::claim_and_execute`, `lionhub/src/lionhub/orchestration/scheduler/worker.py::_reject_claim`). The worker has no chain logic. Its own terminal writes leave an absent stored `exit_code` absent; under the existing no-ref rule such a row resolves without grace (`lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`). Moving the remaining legacy worker writes to the reason-column route is a separate change; this ADR neither requires nor blocks it.
10. **Maintenance.** A contributor who adds a reader of `status_evidence_refs` must ignore unknown kinds, and the table above is the list to keep current. A contributor who routes schedule-run terminal writes through `transition()` silently drops the element (`lionagi/state/lifecycle/adapters.py::run_legacy_transition`). The gate test in the Tests section guards against that.
11. **Reversal cost.** Removing D4 leaves the monitor on the existing rule, which is the behavior before this change. Removing D2 reintroduces P3. Neither needs a migration.

## Tests

New tests (none exist at this checkout):

1. `lionhub/tests/lionhub_tests/orchestration/scheduler/test_scheduler_engine.py::test_normal_path_terminal_write_records_chain_decision`: an exit-0 child with an `on_fail` branch and a `completed_empty` invocation. The row's `status_evidence_refs` contains `{"kind": "chain_decision", "v": 1, "ref": "on_fail"}` and the invocation ref.
2. `lionhub/tests/lionhub_tests/orchestration/scheduler/test_scheduler_engine.py::test_recovery_terminal_write_records_chain_decision`: a dispatched orphan with a stored branch. The row carries the same element, and the definition is read before the compare-and-set.
3. `lionhub/tests/lionhub_tests/orchestration/scheduler/test_scheduler_engine.py::test_selection_is_none_at_depth_cap`: a run at `_MAX_CHAIN_DEPTH` records `none` and launches nothing.
4. `tests/state/lifecycle/test_schedule_adapter.py::test_schedule_run_status_write_keeps_reason_columns`: a `db.update_status` write for a schedule_run persists `status_evidence_refs`. A companion case shows that the `transition()` route drops them. This is the reason-column gate test (D1).
5. `lionhub/tests/lionhub_tests/orchestration/scheduler/test_scheduler_engine.py::test_lost_terminal_cas_does_not_chain_normal_path` and `::test_lost_terminal_cas_does_not_chain_recovery`: a lost compare-and-set on each path launches no child and writes no selection.
6. `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_chain_decision_none_returns_without_sleep`: a `none` row resolves with no sleep, whatever its exit code. `::test_chain_decision_branch_with_no_chain_returns_without_sleep`: with `--no-chain`, an `on_success` row resolves with no sleep and the wait returns the row's own result.
7. `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_chain_decision_branch_follows_child`: an `on_fail` selection on an exit-0 parent waits for and prints the child.
8. `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_chain_decision_survives_schedule_edit`: after the stored schedule's `on_success` is removed, a recorded `on_success` still waits for the child.
9. `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_chain_decision_unknown_version_is_legacy_with_one_warning` and `::test_chain_decision_malformed_is_legacy_with_one_warning`: each treats the row as having no ref and emits one warning line for that row.
10. A new evidence-ref reader contract module under `tests/state/`: each Python reader in R1 to R11 receives a list that includes `{"kind": "chain_decision", "v": 1, "ref": "none"}` and behaves as the table says. The TypeScript and extension readers (R12 to R15) get the same case in their own suites.

11. In `lionhub/tests/lionhub_tests/orchestration/scheduler/test_scheduler_engine.py` and `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py`, cover a dispatched run finalized during startup with no exit code and a recorded `on_fail`. Assert that recovery launches its child, the monitor opens grace and follows the child, and the child's terminal result determines the exit. As a control, a pre-dispatch failure with no exit code and either `none` or no ref launches no child and the monitor resolves without grace.

Existing tests that must stay unchanged, because their fixtures carry no ref and so exercise the existing rule:

- `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_advance_chains_extends_frontier_and_child_exit_code_decides`
- `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_advance_chains_no_declared_action_resolves_without_grace`
- `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_advance_chains_waits_only_for_usable_selected_action`
- `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_advance_chains_grace_expires_without_child_resolves_on_parent_exit_code`
- `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_dispatch_wait_chain_follow_on_fail_recovery_final_link_wins`
- `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_dispatch_wait_chain_follow_no_declared_action_resolves_immediately`
- `lionhub/tests/lionhub_tests/serve/cli/test_monitor_run.py::test_dispatch_wait_chain_follow_grace_expiry_does_not_hang`
- `lionhub/tests/lionhub_tests/orchestration/scheduler/test_scheduler_engine.py::test_chain_selection_ignores_unselected_invalid_value`

## Alternatives considered

- **A dedicated `schedule_runs.chain_decision` column, with a schema version bump and a mutable admission receipt.** The receipt would move through pending, admitted, refused and unknown, and would be written together with the child insert. It would buy the monitor a way to tell a refused child from a late one, and a crash in the window from a grace expiry. It is rejected for this change, for two reasons. A version bump makes readers below the new version refuse the store (`lionagi/state/db.py::StateDB._refuse_unless_native`, `lionagi/state/db.py::StateDB._refuse_newer_schema`). The receipt also ties the parent row's update to the child's insert, which the scheduler does not do today. The disagreements in P1, P2 and P3 all arise when the parent row becomes terminal, before any grace window opens, so recording the decision in that write removes them; the receipt's extra states address a slow or lost child insert, which Consequences 1 to 3 record as limitations.
- **Keep inferring from `exit_code` and the stored schedule.** Rejected. This is P1 and P2. An exit-0 child whose invocation is `completed_empty` produces a branch disagreement (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`, `lionhub/src/lionhub/serve/cli/monitor.py::_schedule_declares_chain_action`). An edit after the run changes the monitor's answer and nothing else.
- **Gate only the chain on the compare-and-set and keep inferring afterwards.** This is the smallest diff, and it is D2 alone. It removes P3 but leaves P1 and P2. The monitor still derives the branch from status and the stored schedule.
- **Record the choice on the invocation row.** Rejected. The monitor reads the schedule_run (`lionhub/src/lionhub/serve/cli/monitor.py::_poll_pending_once`). The invocation's terminal write is a separate compare-and-set (`lionhub/src/lionhub/orchestration/scheduler/engine.py::SchedulerEngine._fire_inner`) whose outcome is not tied to the schedule_run's. A choice on the invocation row could be absent, or could disagree with the schedule_run the monitor reads.
- **Lengthen the grace window.** Rejected. A longer wait hides the disagreement without removing it. The status-versus-exit-code and edit cases return the wrong answer at any grace length. The bound is part of the wait's contract (`lionhub/src/lionhub/serve/cli/monitor.py::_CHAIN_GRACE_TICKS`).

## Open questions

1. **Warning channel.** The CLI logging module re-exports `warn` (`lionhub/src/lionhub/orchestration/console.py` `from lionagi._log_channels import (`, `lionhub/src/lionhub/orchestration/console.py::__all__`; `lionagi/_log_channels.py::warn`). The channel for the D4 warning remains an implementation choice.

## Notes

- The monitor uses `lionhub/src/lionhub/serve/cli/monitor.py::_CHAIN_GRACE_TICKS` for the two-tick wait. Its legacy rule checks chain depth against `lionhub/src/lionhub/orchestration/schedule/parsing.py::_MAX_CHAIN_DEPTH` in `lionhub/src/lionhub/serve/cli/monitor.py::_advance_chains`.
- The cap in D1 is `_MAX_CHAIN_DEPTH = 10`, which ADR-0070 D4 describes as a safety cap with no measured basis.
