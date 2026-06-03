# studio-lifecycle — self-healing Studio session/invocation lifecycle

Branch: `show/lionagi-sweep/studio-lifecycle` (local only — not pushed, no PR).

Four self-healing mechanisms for the Studio session/invocation lifecycle. Every status
mutation routes through the sanctioned `StateDB.update_status()` path (writes the entity row
+ a `status_transitions` history row atomically) — no bare `UPDATE ... SET status`.

## #1170 — invocation timeout + reaper
`reap_stale_invocations()` (in `lifecycle.py`) transitions `running` invocations to
`timed_out` when either:
- `started_at + deadline < now`, where the deadline is resolved **per `action_kind`** via
  `LIONAGI_STUDIO_INVOCATION_DEADLINE_<KIND>_SECONDS`, falling back to the global
  `INVOCATION_DEADLINE_SECONDS` (default 7200s / 2h); or
- the invocation has `session_count == 0` and has been idle past
  `ZERO_SESSION_GRACE_SECONDS` (default 300s).

`_deadline_for_kind(action_kind, global_default)` is the pure helper doing the per-kind
env lookup (added to satisfy critic finding MAJ-1 — the contract's "overridable per action
kind" clause).

## #1171 — terminal status enforcement for sessions
`reap_null_status_sessions()` scans `WHERE status IS NULL`, checks process liveness, and
transitions dead sessions to `failed` with reason `process_exited_without_status`. The
`status IS NULL` guard ensures already-terminal sessions are invisible to the reaper (no
double-write).

## #1172 — automatic phantom session reaper
`reap_phantom_sessions()` reuses the existing `admin_svc.list_phantom_sessions()` detection
and transitions each phantom to `failed` with reason `phantom_reaped` via `update_status()`
— it no longer `DELETE`s rows. The manual Admin "Prune all phantom" action
(`prune_phantom_sessions()`) now delegates to this transition-based reaper (behavior
improved: row preserved with history, not deleted). Runs on Studio startup AND periodically.
`get_phantom_count()` surfaces the count in dashboard health data (`stats.get_stats()`).

## #1173 — state.db lifecycle
`db_maintenance.py`:
- `checkpoint_state_db()` runs `PRAGMA wal_checkpoint(TRUNCATE)` on startup and on a throttled
  scheduler tick (`CHECKPOINT_INTERVAL_SECONDS`, default 3600s); records an
  `admin_events(action="checkpoint")` row.
- `get_last_checkpoint_at()` feeds the System Health panel "Last checkpoint" field.
- `get_db_size_alert()` compares db size against `DB_SIZE_ALERT_BYTES` (default 500 MB).
- `prune_old_data()` — single transaction: nullifies soft FK references, deletes terminal
  sessions/`schedule_runs` older than `PRUNE_KEEP_DAYS` (default 30d, branches CASCADE),
  records `admin_events(action="prune")`. Exposed at `POST /api/admin/prune-old-data`.

## Files

New:
- `lionagi/studio/services/lifecycle.py` (#1170/#1171/#1172 reapers + startup/periodic entry points)
- `lionagi/studio/services/db_maintenance.py` (#1173)
- `tests/apps_studio_server/test_lifecycle_reapers.py`
- `tests/apps_studio_server/test_db_maintenance.py`
- `tests/apps_studio_server/test_adversarial_reapers.py`

Modified:
- `lionagi/studio/config.py` — reaper/checkpoint/prune env constants
- `lionagi/studio/app.py` — lifespan runs startup reconciliation + checkpoint
- `lionagi/studio/scheduler/engine.py` — throttled periodic reapers + checkpoint in `_tick()`
- `lionagi/studio/services/admin.py` — `prune_phantom_sessions()` delegates to transition reaper
- `lionagi/studio/services/stats.py` — `phantom_count`, `last_checkpoint_at`, `size_alert`, `size_threshold_bytes`

## Test result

```
uv run pytest tests/apps_studio_server/ -q
56 passed, 19 skipped in 1.82s
```

(19 skipped = FastAPI `TestClient` endpoint tests that require the `studio` extra — same
pattern as the rest of the suite.) `uv run ruff check` clean on all touched files.

## Critic verdict
`CRIT:0 | MAJ:1 | MIN:2 | PASS:3` — #1171/#1172/#1173 APPROVE; #1170 APPROVE-WITH-FIXES.
The one blocker (MAJ-1: per-kind deadline override unimplemented) was subsequently resolved
(`_deadline_for_kind` + two focused tests). Non-blocking MIN-1 (phantom TOCTOU re-guard) and
MIN-2 (incidental orphan-message GC) left as-is.
