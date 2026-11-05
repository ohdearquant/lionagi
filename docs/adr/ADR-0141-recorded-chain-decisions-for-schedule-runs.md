# ADR-0141: Recorded chain decisions for schedule runs

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: scheduling-control-plane
- **Date**: 2026-11-04
- **Relations**: extends ADR-0070; amends ADR-0070 D4's selection input

## Context

ADR-0070 D4 (`docs/adr/ADR-0070-studio-scheduling-and-dispatch-delivery.md:442-476`) lets a schedule declare `on_success` and `on_fail`. After a run's child process exits, the scheduler fires at most one follow-up. Two components decide which branch applies, and they read different inputs. Only the scheduler launches the follow-up. The monitor (`li monitor run`) decides whether to keep waiting for it. Nothing records the scheduler's choice, so the monitor derives it again.

The problems below are as they exist at this checkout.

**P1. The scheduler chooses from invocation status. The monitor chooses from exit code.** On the normal path the scheduler sets `success = inv_status == "completed"` after resolving the invocation's terminal status (`lionagi/studio/scheduler/engine.py:3291-3296`). A clean exit whose child never reached a terminal status resolves to `completed_empty`, which is not success (`engine.py:3287-3290`), and that status maps to a `failed` run (`engine.py:103-104`). The monitor tests for a branch using `exit_code == 0` alone (`lionagi/cli/monitor.py:1526-1541`). For an exit-0 child whose invocation is `completed_empty`, the scheduler selects `on_fail` while the monitor looks for `on_success`. If no `on_success` is stored, the monitor does not wait, and `li monitor run` returns the parent's result while the follow-up runs.

**P2. Edits after the run change the monitor's answer, not the scheduler's.** The monitor reads the stored schedule when it checks (`monitor.py:1526-1541`, through `db.get_schedule`). The scheduler selects from the dict the fire received on the normal path (`engine.py:2944-2956`), and from a definition read during recovery (`engine.py:1360`). An edit between the two makes them disagree.

**P3. The scheduler chains after losing its terminal write.** The schedule_run terminal write is a compare-and-set on `status = running` (`engine.py:2690-2716`). On the normal path its result gates only the signal (`engine.py:3320`). The follow-up block (`engine.py:3356-3376`) does not read it, so a fire whose row another writer already finalized still launches its `on_fail` follow-up. One such writer is the lifecycle reaper, which writes `timed_out` for stale runs (`lionagi/studio/services/lifecycle.py:578-584`). Dispatched recovery does stop on a lost compare-and-set (`engine.py:1302-1319`).

**P4. The decision is not recorded.** The schedule_run row keeps `exit_code`, `status` and `status_evidence_refs` (`lionagi/state/schema.sql:622`, `:647`). The refs written by the terminal write name the invocation or the schedule (`engine.py:3310`, `engine.py:3110`, `lionagi/studio/services/scheduler_state.py:428`), not the decision. The monitor therefore has to derive it again.

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
- **`li wait`.** It returns when the waited row is terminal and has no chain handling (`lionagi/cli/wait.py:121-218`). It is unchanged.
- **The queued-task worker lane.** Exhausted-lease and execution terminal transitions use legacy `transition()`, whose reason-column flag is off (`lionagi/studio/scheduler/worker.py:277-295`, `:544-609`; `lionagi/state/lifecycle/adapters.py:110-144`). Terminal admission rejection instead writes `skipped` and reason columns through `update_status`, without supplying a chain-decision ref (`worker.py:426-431`, `:488-511`). These rows keep the existing monitor rule (Consequence 9).
- **The rest of ADR-0070 D4.** D4's selection rule is amended on one input only: the scheduler selects from the invocation's terminal status, and D4's `exit_code == 0` line is superseded by it (D1). The cap, the branch keys `on_success`/`on_fail`, and the inline recursion stay as written.
- **Recovery's current-definition lookup.** It stays. Consequence 4 records what that costs.
- **Rows written before this change.** They keep the existing monitor rule. History is not rewritten.

## Readers of stored evidence refs

At this checkout, `git grep` finds `status_evidence_refs` or `evidence_refs` on 197 lines in 30 files across `lionagi/` and `apps/` (non-test). Under `lionagi/` alone it is 189 lines in 26 files. Test files are excluded, and no test file under `lionagi/` matches.

Each reader below is listed with what it does with the list and what it does with an entry whose `kind` it does not know. Writers are skipped; the list of skipped writers follows the table.

| # | Reader | What it does with the list | Unknown kind | Reached by schedule-run refs |
|---|--------|----------------------------|--------------|------------------------------|
| R1 | `lionagi/cli/monitor.py:662-672` | Prints each entry as `label`, else `kind`, for completed session rows | Printed by kind name | No: session rows only |
| R2 | `lionagi/cli/status.py:219`, `:372-379` | Prints `label` or `kind`, then `path`, `id` or `ref` | Printed as `kind: ref`; the new element prints as `chain_decision: on_fail` | No: `resolve_any_target` (`lionagi/cli/_inspection.py:193-250`) has no schedule-run lookup; only `wait.py:44-52` adds one, and `li wait` does not print refs |
| R3 | `lionagi/cli/monitor.py` wait path | None today | n/a | Yes, by design (D4) |
| R4 | `lionagi/state/db.py:6875-6888`, `:6965-6978` | Tests for the reaper's `kind == "invocation"` entry | Ignored (the predicate tests one kind) | No: reads the `invocations` table only |
| R5 | `lionagi/state/db.py:9024-9059` | Decodes JSON text into a list. No interpretation | Passed through as a dict | Yes, pass-through |
| R6 | `lionagi/state/db.py:5944-5982` | Returns `status_transitions.evidence_refs` verbatim for `entity_type = 'schedule'` only | Passed through | No: schedule-run history is not returned |
| R7 | `lionagi/studio/services/run_view.py:315-336` | Returns the schedule-run row merged with the run view (`{**run, ...}`) | Passed through to the HTTP API | Yes. The key reaches API clients. The Studio schedule-run types (`apps/studio/frontend/src/lib/types.ts:462-495`) declare no such field, so the Studio client ignores it |
| R8 | `lionagi/studio/services/run_view.py:247-270` | Reads `reason_code` and `reason_summary` of skipped runs from `status_transitions`, not the refs | n/a | Unaffected |
| R9 | `lionagi/studio/operator/run_progress.py:487-500` | Keeps entries with `kind == "failed_operation"` and a string `id` | Ignored | No: session rows (called at `run_progress.py:527`, `:674`) |
| R10 | `lionagi/studio/operator/application_mcp.py:445`, `:450` | Drops the field from session and invocation output | Never inspected | No |
| R11 | `lionagi/studio/services/invocations.py:117`, `:296`; `lionagi/studio/services/runs.py:377`; `lionagi/studio/services/sessions.py:1422`, `:1700` | Parse and return the field for invocation and session rows | Passed through | No |
| R12 | `apps/studio/frontend/src/components/history/RunDetail.tsx:452-461` | `evidenceFailedNodeIds`: keeps entries with `kind === "failed_operation"` and a string `id` | Ignored | No: session detail |
| R13 | `apps/studio/frontend/src/lib/api.ts:1511`, `:1553-1555` | `SessionDetail` type: `Array<{ kind?, id?, label? }>` | Compiles. Extra keys are tolerated at runtime | No: session detail |
| R14 | `apps/vscode/src/api/types.ts:29`, `:78`; `apps/vscode/src/runs/runDetailPanel.ts:313`, `:329-345`, `:357-372` | Typed as records. The run and invocation banner payloads copy the list | Passed through | No: run and invocation detail |
| R15 | `apps/vscode/media/runDetail.js:328-336` | Shows the count of entries as "N evidence ref(s)" | Counted as evidence. Misleading | No: run and invocation banners |

Skipped writers: `lionagi/studio/scheduler/engine.py` (the terminal, selection and setup-failure writes), `lionagi/studio/services/scheduler_state.py:398-589`, `lionagi/studio/services/admin.py:93`, `:1162-1400` (it reads only the list its caller passes and appends its own kinds), `lionagi/studio/services/lifecycle.py:150-777`, `lionagi/cli/_runs.py:1079-1373`, `:1438`, `lionagi/cli/orchestrate/flow.py:705-863`, `:3614`, `lionagi/cli/_kill_service.py:530`, `lionagi/studio/services/shows.py:533-723`, `lionagi/studio/services/launches.py:321`, `:362`, `lionagi/state/_mirror_common.py:349`, `lionagi/state/transitions.py:33`, `:111`, `lionagi/state/lifecycle/models.py:59`, `lionagi/state/lifecycle/service.py:343-353`, `:354-367`, `:1111-1160`, the writes in `lionagi/state/db.py`, the schema declarations in `lionagi/state/schema.sql`, `lionagi/state/schema_meta.py` and `lionagi/state/schema_migrations.py`, and `lionagi/casts/emission.py:170`, whose `evidence_refs` is an unrelated `list[str]` field.

No reader breaks on an unknown kind. R15 would count the element as evidence if it ever received a schedule-run list; it is not reached by schedule-run rows today, and D1 does not change that. R7 is reached: the list, element included, passes through to HTTP API clients, and the Studio client declares no field for it.

## Decision

### D1: The scheduler records its selection in the terminal write

The terminal write of a schedule_run adds one element to its `evidence_refs` list:

```text
{"kind": "chain_decision", "v": 1, "ref": "on_success" | "on_fail" | "none"}
```

`ref` names the schedule's branch key that the run selected, or `none`. The element follows the documented entry shape (`lionagi/state/schema.sql:796`, `{kind, id|path|ref, label?}`), so readers that print `path`, `id` or `ref` show the selection (R2). `v` is the element's version.

The selection is computed before the compare-and-set:

```python
chosen = _selected_chain_action(schedule, success)     # engine.py:449-457
selection = (
    "none"
    if chosen is None or chain_depth >= _MAX_CHAIN_DEPTH  # _schedule_parsing.py:83
    else ("on_success" if success else "on_fail")
)
```

Normal path: the terminal write at `engine.py:3304-3313` becomes `evidence_refs=[{"kind": "invocation", "id": inv_id}, chain_decision]`.

Dispatched recovery: the terminal write at `engine.py:1302-1312` gets the same element.

Exact semantics:

- The input is the `schedule` dict that `_fire_inner` received (`engine.py:2944-2956`) on the normal path. It is the same object the follow-up overlay is built from (`engine.py:3360`).
- On recovery the input is `get_schedule(sid)`, read before the compare-and-set. Today that read follows the compare-and-set (`engine.py:1360`). D1 moves it above `engine.py:1302`.
- If that read returns None, the selection is `none`. Today the chain is skipped there (`engine.py:1361-1362`).
- `success` is the value the scheduler already computes: `inv_status == "completed"` on the normal path (`engine.py:3295`), and `run_status == "completed"` on recovery (`engine.py:1365`). For the scheduler, the selection input is therefore the invocation's terminal status, and this supersedes ADR-0070 D4's `exit_code == 0 and on_success present -> on_success` line: an exit-0 child whose invocation is `completed_empty` selects `on_fail`.
- A stored branch that the launch predicate rejects, such as one with an unusable kind, gives `none`. The predicate is the one that launches the child (`lionagi/cli/_schedule_parsing.py:174-182`). The warning that `_selected_chain_action` logs today (`engine.py:454-456`) is still logged. It is now logged before the write.
- A depth at or above the cap gives `none`. Today the cap is checked before selection (`engine.py:3356`). D1 moves it into the selection, so a recorded branch always has a child to launch.
- No stored branch gives `none`.

Gate: the element is written in the same `UPDATE` as `status`, under the same compare-and-set.

- Path: `_guarded_terminal_status` (`engine.py:2705-2716`), then `SchedulerStateService.update_status` (`lionagi/studio/services/scheduler_state.py:358-374`), then `StateDB.update_status` (`lionagi/state/db.py:5402`, delegating at `db.py:5481`), then `run_update_status` (`lionagi/state/lifecycle/adapters.py:51-107`). That function calls `service._transition(...)` without the reason-column flag (`adapters.py:95-99`).
- `_transition` defaults `write_reason_columns` to `True` (`lionagi/state/lifecycle/service.py:611-622`). With the flag set, `_write` puts `status_evidence_refs = :evidence_refs` in the same `UPDATE` as `status` (`service.py:1086-1099`). That statement is guarded by `status = :previous_status` (`service.py:1100-1103`). The status change and the ref are one statement.
- The policy's `reason_columns` defaults to `True` (`lionagi/state/lifecycle/models.py:120`). The schedule_run registration does not override it (`lionagi/state/lifecycle/policy.py:308-329`). That flag is read only by `LifecycleService.transition` (`service.py:415-420`), which the scheduler does not call.
- Result: the flag is true for schedule_run terminal writes on the normal path and on dispatched recovery. Two other writers differ. The lifecycle reaper also goes through `db.update_status` and keeps the flag (`lifecycle.py:578-584`), but it is not one of the two paths named here, and it records no selection. The queued-task worker calls `transition()`, whose legacy route forces `write_reason_columns=False` (`lionagi/studio/scheduler/worker.py:495-498`; `lionagi/state/transitions.py:103`; `lionagi/state/lifecycle/adapters.py:143-144`). Refs written through that route are not stored. Consequence 9 covers this.

Why this way:

- The choice and the status are one row write under one compare-and-set. A recorded selection exists exactly when the scheduler's terminal write won. D2 and D4 depend on that property.
- The choice is computed before the write. A crash before the write leaves no record, and the row stays `running` for startup reconciliation (`engine.py:1258`, `lionagi/state/db.py:7068-7077`).
- The decision is recorded, not its inputs. The monitor never derives the branch again for a row that has a record, so edits and exit-code/status differences do not matter for that row.

### D2: A lost terminal compare-and-set never chains

The follow-up block runs only when the schedule_run compare-and-set returned true.

Current structure on the normal path: `written = await self._guarded_terminal_status(...)` (`engine.py:3304`). `if written:` gates only the signal (`engine.py:3320-3332`). The invocation write (`engine.py:3333-3344`), `_check_max_runs` (`engine.py:3354`) and the chain (`engine.py:3356-3376`) run outside that gate.

D2 moves the chain under `if written:`. `_check_max_runs` stays where it is, because it does not depend on the chain.

Exact semantics:

- Compare-and-set won: the selection is written (D1), and the child is fired inline as today (`engine.py:3369-3376`).
- Compare-and-set lost: the `UPDATE` matches no row, so no selection is written. There is no signal and no child. The row keeps the other writer's status and evidence.
- Dispatched recovery: unchanged. `if not written: continue` already applies (`engine.py:1318-1319`).
- Setup failure: unchanged. It returns before any chain (`engine.py:3155`).
- Cancellation: unchanged. The cancellation handler never launches a follow-up.
- The invocation write does not gate the chain. An invocation-side loss with a schedule_run win still chains. The schedule_run row is the one the monitor reads (`lionagi/cli/monitor.py:1485-1491`).

Why this way: a lost compare-and-set means another finalizer owns the row's follow-on effects. Recovery already encodes that rule (`engine.py:1299-1301`). The normal path applies it too, which removes P3.

Behavior change: a run that another finalizer ended, such as a reaped `timed_out` run, no longer launches its follow-up when its process later exits. Today it does (P3).

### D3: Rows that are terminal at creation record `none`

A row created in a terminal state carries the `none` element in the first write that carries its evidence refs.

- `create_skipped_run` (`lionagi/studio/services/scheduler_state.py:398-431`). The INSERT names no evidence column (`lionagi/state/db.py:6487-6515`), so the first write with refs is the `update_status` at `scheduler_state.py:422-430`. Its list is `[{"kind": "schedule", "id": sid}]` at line 428. The element joins that list.
- Setup failure (`engine.py:3057-3113`). The row is inserted by `_write_occurrence` (`engine.py:3057-3082`, `:2836-2873`) with no refs. The first write with refs is the `update_status` at `engine.py:3104-3113`, whose list is at line 3110. The element joins that list.
- The callers of `create_skipped_run` (`engine.py:1517`, `:2399`, `:2457`, `:2591`) never reach selection. Their `none` describes what already happens.

Exact semantics: the element is `{"kind": "chain_decision", "v": 1, "ref": "none"}`. These rows launch no follow-up before or after this change.

Why this way: D4 makes the monitor return at once on `none`. Today these rows already resolve at once, because the monitor skips `cancelled` and `skipped` rows and rows without an exit code (`lionagi/cli/monitor.py:1580-1593`). That rule is a status list. The element makes the same outcome a property of the row.

### D4: The monitor follows the recorded selection

Proposed helper, in `lionagi/cli/monitor.py`:

```python
def _recorded_selection(row: dict[str, Any]) -> str | None:
    """'on_success' | 'on_fail' | 'none' for a usable v1 chain_decision ref, else None."""
```

Input: `row["status_evidence_refs"]` as a list, or as JSON text. `get_schedule_run` decodes the column (`lionagi/state/db.py:7086-7098`, `:9024-9059`). The monitor already accepts both forms for session rows (`monitor.py:652-665`).

A ref is usable only when all of these hold: it is a dict, `kind == "chain_decision"`, `v` is the integer `1` (not a boolean), and `ref` is one of the three values. Any other `v`, or a malformed element, is treated as no ref. The monitor writes one warning line for each affected row per wait, naming the run id and the reason. The CLI logging module (`lionagi/cli/_logging.py`) has no warn helper today. The implementation chooses the channel. The one-line requirement is fixed.

Cases in `_advance_chains` (`lionagi/cli/monitor.py:1560-1645`), applied to a row that has become terminal, in this order:

- Chain following is off (`--no-chain`, so `chain` is false): resolved now, as today (`monitor.py:1582-1593`). The recorded selection is not read.
- Status `cancelled` or `skipped`: resolved now, as today (`monitor.py:1582-1593`).
- Selection `on_success` or `on_fail`: the row joins `awaiting_grace` with `_CHAIN_GRACE_TICKS` (`monitor.py:1597`). The child is found by `chain_parent_id` (`monitor.py:1511-1516`), as today. The stored schedule is not read.
- Selection `none`: resolved now, whatever `exit_code` is (`monitor.py:1598-1599`). Nothing is pending and no grace is open, so the wait loop ends before its next sleep (`monitor.py:1746-1747`, `:1763-1772`).
- No usable ref: the existing rule applies unchanged, using `exit_code` and the stored schedule (`monitor.py:1526-1541`, `:1582-1593`).

The final exit code of the wait is computed as today from the last resolved link (`monitor.py:1775-1789`).

Why this way: the monitor reads the scheduler's decision, so the two agree for every row written after this change, whatever the stored schedule or exit code says. Rows without a usable ref keep the existing rule exactly, so an older monitor and a newer scheduler do not contradict each other in either direction. The two-tick bound (`_CHAIN_GRACE_TICKS`, `monitor.py:1289`) is not changed.

### D5: No schema change

- `schedule_runs.status_evidence_refs` is `JSON` (`lionagi/state/schema.sql:647`). The change adds one element to a list that the column already holds.
- The schema version does not change. A store is refused only when its version is above the reader's `SCHEMA_VERSION` (`lionagi/state/db.py:1804-1806`, `:1881`), so this change does not cause refusals.
- Existing readers see the new element. The readers table above lists what each does with it.

## Consequences

1. **No admission receipt; the wait is bounded.** The monitor waits at most two ticks for a child. A child whose row is inserted after the grace is not followed, and `li monitor run` returns the parent's result. A follow-up issue tracks admission acknowledgement and waiting beyond the grace. It is #4207.
2. **A crash in the window looks like grace expiry.** A crash after the terminal write (with `on_success` or `on_fail` recorded) and before the child row is inserted leaves a terminal parent and no child. The monitor cannot tell this from a refusal or a grace expiry, and returns the parent's result. Startup reconciliation selects only rows still `running` (`lionagi/state/db.py:7068-7077`; `engine.py:1258`), so the terminal row's follow-up is not re-fired.
3. **A child refused before its row exists looks the same.** `_fire_inner` can return False before the occurrence write (`engine.py:3083-3095`). For a child, that leaves the parent's selection recorded and no child row. The monitor takes the grace path.
4. **Recovery keeps the current-definition lookup.** Recovery selects from the definition read at recovery time (`engine.py:1360`). A chain child's overlay (`lionagi/cli/_schedule_parsing.py:185-196`) is not persisted, so after a restart a recovered child cannot recover the overlay the original fire used. Edits made after a selection is recorded do not change that selection, because D1 writes it once.
5. **Behavior change for lost compare-and-sets.** A run that another finalizer ended, such as the reaper's `timed_out` write (`lifecycle.py:578-584`), no longer launches its follow-up when its process later exits. Today it does (P3). Such rows carry no `chain_decision` ref, so the monitor applies the existing rule to them.
6. **`li wait` does not follow chains.** It returns when the waited row is terminal (`lionagi/cli/wait.py:121-218`). A caller who needs a follow-up's result must use `li monitor run`. This ADR does not change `li wait`.
7. **Rows written before this change keep the old rule.** That includes rows written by the queued-task worker and by any other finalizer. The old disagreement with the scheduler remains for those rows. The change does not repair history.
8. **Each terminal write and its history copy grow by one element.** The status history row copies the same list (`lionagi/state/lifecycle/service.py:343-367`, `lionagi/state/schema.sql:787-796`). Of the readers of `status_transitions`, only `get_schedule_history` returns its `evidence_refs`, and only for schedule definitions (`lionagi/state/db.py:5944-5982`). The others select counts, identifiers or reason columns, or delete rows.
9. **The queued-task worker lane keeps the existing rule.** Its schedule_run terminal writes go through the legacy route with reason columns off (`lionagi/studio/scheduler/worker.py:277-299`, `:449`, `:556-594`), so no element is stored and the worker has no chain logic. Under D4 such a row can still open the two-tick grace for a stored branch that will never launch, exactly as today. Moving the worker to the reason-column route is a separate change; this ADR neither requires nor blocks it.
10. **Maintenance.** A contributor who adds a reader of `status_evidence_refs` must ignore unknown kinds, and the table above is the list to keep current. A contributor who routes schedule-run terminal writes through `transition()` silently drops the element (`lionagi/state/lifecycle/adapters.py:143-144`). The gate test in the Tests section guards against that.
11. **Reversal cost.** Removing D4 leaves the monitor on the existing rule, which is the behavior before this change. Removing D2 reintroduces P3. Neither needs a migration.

## Tests

New tests (none exist at this checkout):

1. `tests/studio/test_scheduler_engine.py::test_normal_path_terminal_write_records_chain_decision`: an exit-0 child with an `on_fail` branch and a `completed_empty` invocation. The row's `status_evidence_refs` contains `{"kind": "chain_decision", "v": 1, "ref": "on_fail"}` and the invocation ref.
2. `tests/studio/test_scheduler_engine.py::test_recovery_terminal_write_records_chain_decision`: a dispatched orphan with a stored branch. The row carries the same element, and the definition is read before the compare-and-set.
3. `tests/studio/test_scheduler_engine.py::test_selection_is_none_at_depth_cap`: a run at `_MAX_CHAIN_DEPTH` records `none` and launches nothing.
4. `tests/state/lifecycle/test_schedule_adapter.py::test_schedule_run_status_write_keeps_reason_columns`: a `db.update_status` write for a schedule_run persists `status_evidence_refs`. A companion case shows that the `transition()` route drops them. This is the reason-column gate test (D1).
5. `tests/studio/test_scheduler_engine.py::test_lost_terminal_cas_does_not_chain_normal_path` and `::test_lost_terminal_cas_does_not_chain_recovery`: a lost compare-and-set on each path launches no child and writes no selection.
6. `tests/cli/test_monitor_run.py::test_chain_decision_none_returns_without_sleep`: a `none` row resolves with no sleep, whatever its exit code. `::test_chain_decision_branch_with_no_chain_returns_without_sleep`: with `--no-chain`, an `on_success` row resolves with no sleep and the wait returns the row's own result.
7. `tests/cli/test_monitor_run.py::test_chain_decision_branch_follows_child`: an `on_fail` selection on an exit-0 parent waits for and prints the child.
8. `tests/cli/test_monitor_run.py::test_chain_decision_survives_schedule_edit`: after the stored schedule's `on_success` is removed, a recorded `on_success` still waits for the child.
9. `tests/cli/test_monitor_run.py::test_chain_decision_unknown_version_is_legacy_with_one_warning` and `::test_chain_decision_malformed_is_legacy_with_one_warning`: each treats the row as having no ref and emits one warning line for that row.
10. `tests/state/test_evidence_ref_reader_contract.py` (new module): each Python reader in R1 to R11 receives a list that includes `{"kind": "chain_decision", "v": 1, "ref": "none"}` and behaves as the table says. The TypeScript and extension readers (R12 to R15) get the same case in their own suites.

Existing tests that must stay unchanged, because their fixtures carry no ref and so exercise the existing rule:

- `tests/cli/test_monitor_run.py::test_advance_chains_extends_frontier_and_child_exit_code_decides`
- `tests/cli/test_monitor_run.py::test_advance_chains_no_declared_action_resolves_without_grace`
- `tests/cli/test_monitor_run.py::test_advance_chains_waits_only_for_usable_selected_action`
- `tests/cli/test_monitor_run.py::test_advance_chains_grace_expires_without_child_resolves_on_parent_exit_code`
- `tests/cli/test_monitor_run.py::test_dispatch_wait_chain_follow_on_fail_recovery_final_link_wins`
- `tests/cli/test_monitor_run.py::test_dispatch_wait_chain_follow_no_declared_action_resolves_immediately`
- `tests/cli/test_monitor_run.py::test_dispatch_wait_chain_follow_grace_expiry_does_not_hang`
- `tests/studio/test_scheduler_engine.py::test_chain_selection_ignores_unselected_invalid_value`

## Alternatives considered

- **A dedicated `schedule_runs.chain_decision` column, with a schema version bump and a mutable admission receipt.** The receipt would move through pending, admitted, refused and unknown, and would be written together with the child insert. It would buy the monitor a way to tell a refused child from a late one, and a crash in the window from a grace expiry. It is rejected for this change, for two reasons. A version bump makes readers below the new version refuse the store (`lionagi/state/db.py:1804-1806`, `:1881`). The receipt also ties the parent row's update to the child's insert, which the scheduler does not do today. The disagreements in P1, P2 and P3 all arise when the parent row becomes terminal, before any grace window opens, so recording the decision in that write removes them; the receipt's extra states address a slow or lost child insert, which Consequences 1 to 3 record as limitations.
- **Keep inferring from `exit_code` and the stored schedule.** Rejected. This is P1 and P2. An exit-0 child whose invocation is `completed_empty` produces a branch disagreement (`engine.py:3291-3296`, `monitor.py:1526-1541`). An edit after the run changes the monitor's answer and nothing else.
- **Gate only the chain on the compare-and-set and keep inferring afterwards.** This is the smallest diff, and it is D2 alone. It removes P3 but leaves P1 and P2. The monitor still derives the branch from status and the stored schedule.
- **Record the choice on the invocation row.** Rejected. The monitor reads the schedule_run (`lionagi/cli/monitor.py:1485-1491`). The invocation's terminal write is a separate compare-and-set (`engine.py:3333-3344`) whose outcome is not tied to the schedule_run's. A choice on the invocation row could be absent, or could disagree with the schedule_run the monitor reads.
- **Lengthen the grace window.** Rejected. A longer wait hides the disagreement without removing it. The status-versus-exit-code and edit cases return the wrong answer at any grace length. The bound is part of the wait's contract (`lionagi/cli/monitor.py:1289`).

## Open questions

1. **Warning channel.** The CLI logging module re-exports `warn` (`lionagi/cli/_logging.py:12-20`, `:23-28`; `lionagi/_log_channels.py:43-45`). The channel for the D4 warning remains an implementation choice.

## Notes

- `lionagi/cli/monitor.py:1290-1291` repeats `_MAX_CHAIN_DEPTH` from `lionagi/cli/_schedule_parsing.py:83`. The existing rule still uses the copy (`monitor.py:1583-1584`). This ADR does not remove it.
- The cap in D1 is `_MAX_CHAIN_DEPTH = 10`, which ADR-0070 D4 describes as a safety cap with no measured basis.
