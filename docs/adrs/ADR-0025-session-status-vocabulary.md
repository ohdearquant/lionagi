# ADR-0025: Session Status Vocabulary

**Status**: Proposed
**Date**: 2026-05-21
**Supersedes**: ADR-0017 §"Status vocabulary (sessions)" (partially — extends the vocabulary)

## Context

ADR-0017 defined four session statuses: `running`, `completed`, `failed`,
`aborted`. This was deliberately minimal. In practice, the vocabulary is
too coarse:

### 1. Timeout is invisible

`li play --timeout 900` fires a `LionTimeoutError` when the 15-minute
deadline hits. The CLI catches it and sets `_terminal_status = "failed"`.
But "failed because the model returned an error" and "failed because we
hit a deliberate timeout" are operationally different:

- **Timeout**: expected behavior when bounding execution time. The work
  may be partially complete. Retry with more time is the natural response.
- **Failed**: unexpected error. The work is invalid. Retry may produce
  the same error.

The runs list shows both as red "failed" pills. An operator can't tell
whether a "failed" run needs debugging or just a longer timeout.

### 2. No distinction between crash and error

A session can fail because:
- The model returned a malformed response → **error** (recoverable)
- The Python process segfaulted → **crash** (infrastructure)
- A dependency raised an exception → **error** (potentially recoverable)
- The process was OOM-killed → **crash** (resource limit)

Today all of these are `failed`. The health classifier (ADR-0024) can
distinguish post-hoc (process_dead = crash, process alive with error =
error), but the status column doesn't record the distinction.

### 3. "Aborted" conflates user-initiated and system-initiated

`aborted` covers both:
- User pressed Ctrl-C → **user cancelled** (intentional)
- Anyio task group cancelled a child → **system cancelled** (cascade)
- Show abort sentinel → **orchestrator cancelled** (intentional, higher level)

These have different follow-up actions: user cancellation needs no
follow-up; system cancellation may indicate a bug; orchestrator
cancellation is normal lifecycle.

### 4. Slow run threshold

The dashboard uses `SLOW_RUN_SECONDS = 30 * 60` (30 minutes) and
`STUCK_RUN_SECONDS = 30 * 60` (30 minutes). Both are too aggressive:

- A typical `li play` run with 3-5 agents takes 45-90 minutes.
- A `/show` play routinely runs 60-90 minutes.
- 30 minutes flags nearly every flow as "slow" — useless signal.

## Decision

### Expanded session status vocabulary

The six-value status vocabulary replaces the four-value CHECK:

```python
# Python validation (source of truth — replaces SQLite CHECK,
# see Schema Migration section below for rationale)
VALID_SESSION_STATUSES = frozenset({
    "running", "completed", "failed", "timed_out", "aborted", "cancelled"
})
```

The `schema.sql` CHECK constraint is **removed** and replaced with
Python-only validation. See [Schema migration](#schema-migration) for
the migration path.

| Status | Meaning | Set by | Exit code |
|--------|---------|--------|-----------|
| `running` | Session is active | CLI at session creation | — |
| `completed` | Finished normally | CLI at session close | 0 |
| `failed` | Terminated with error | CLI on uncaught exception | != 0 |
| `timed_out` | Killed by `--timeout` deadline | CLI on `LionTimeoutError` | 124 (UNIX convention) |
| `aborted` | User-initiated interruption | CLI on KeyboardInterrupt / SIGINT | 130 (128 + SIGINT) |
| `cancelled` | System/orchestrator-initiated cancellation | CLI on CancelledError / abort sentinel | 143 (128 + SIGTERM) |

Key distinctions:

- **`failed` vs `timed_out`**: timeout is a deliberate bound, not an error.
  "Retry with more time" vs "investigate the error."
- **`aborted` vs `cancelled`**: aborted = user pressed Ctrl-C (intentional).
  cancelled = system killed the task (cascade, orchestrator decision, OOM).
- **Exit codes**: follow UNIX convention. 124 for timeout (GNU coreutils
  `timeout` uses 124). 130 for SIGINT (128 + 2). 143 for SIGTERM (128 + 15).

### Terminal vs non-terminal

```python
SESSION_TERMINAL_STATUSES = frozenset({
    "completed", "failed", "timed_out", "aborted", "cancelled"
})
```

All non-`running` statuses are terminal. The SSE done-condition check
(ADR-0017 `is_session_stream_done`) uses this set.

### Legal transitions

The session status FSM is deterministic. All transitions originate from
`running`; terminal statuses have no outgoing transitions.

| From | To | Trigger | Who writes |
|------|----|---------|------------|
| `running` | `completed` | Normal completion | CLI teardown |
| `running` | `failed` | Unhandled exception | CLI teardown |
| `running` | `timed_out` | `TimeoutError` from `anyio.fail_after` | CLI teardown |
| `running` | `aborted` | `KeyboardInterrupt` (user Ctrl-C) | CLI teardown |
| `running` | `cancelled` | `CancelledError` / orchestrator abort / admin decision | CLI teardown or admin API |

No transitions between terminal statuses are permitted.

**CLI transition logic** uses the full terminal set:

```python
def can_transition(current: str, target: str) -> bool:
    return current == "running" and target in SESSION_TERMINAL_STATUSES
```

**Admin transitions** use a restricted subset — operators should not
mark sessions as `completed` or `timed_out` (those are system-determined):

```python
ADMIN_TRANSITION_TARGETS = frozenset({"failed", "aborted", "cancelled"})
```

Both are modeled on khive's GTD `transition` verb with its Lean4-proven
FSM properties (`can_transition` validation).

### Write points (updated from ADR-0017)

```python
# lionagi/cli/agent.py — updated teardown
_terminal_status = "completed"
try:
    res = await branch.operate(...)
except KeyboardInterrupt:
    _terminal_status = "aborted"
    raise
except BaseException as exc:
    from lionagi._errors import TimeoutError as LionTimeoutError
    from lionagi.ln.concurrency import get_cancelled_exc_class

    if isinstance(exc, LionTimeoutError):
        _terminal_status = "timed_out"
    elif isinstance(exc, get_cancelled_exc_class()):
        _terminal_status = "cancelled"
    else:
        _terminal_status = "failed"
    raise
finally:
    await _teardown_live_persist(live, status=_terminal_status)
```

For flows and fanout:

```python
# lionagi/cli/orchestrate/flow.py
except LionTimeoutError:
    # Don't catch here — let it propagate to the session teardown
    raise
```

The `LionTimeoutError` should propagate to the session-level handler
rather than being caught in the orchestrate dispatcher (which currently
catches it, logs, and returns exit code 1 without setting the session
status).

### Dashboard thresholds (updated)

```python
SLOW_RUN_SECONDS = 60 * 60    # 60 minutes (was 30)
STUCK_RUN_SECONDS = 60 * 60   # 60 minutes (was 30)
```

Rationale: typical flow runs are 45-90 minutes. 60 minutes as the "slow"
threshold flags only the actual outliers. The "stuck" threshold also
moves to 60 minutes — a running session that hasn't finished after an
hour warrants attention, but 30 minutes was flagging normal runs.

### Status display mapping

```python
DISPLAY_MAP = {
    "running": {"label": "Running", "tone": "running"},
    "completed": {"label": "Completed", "tone": "ok"},
    "failed": {"label": "Failed", "tone": "failed"},
    "timed_out": {"label": "Timed out", "tone": "pending"},
    "aborted": {"label": "Aborted", "tone": "neutral"},
    "cancelled": {"label": "Cancelled", "tone": "neutral"},
}
```

`timed_out` gets "pending" tone (amber/yellow) — it's not an error,
it's a boundary. The operator's response is "retry with more time,"
not "investigate an error."

Color tokens use soft backgrounds with strong foreground text for accessibility
at small label sizes (see ChatGPT frontend design review, section 6):

| Semantic | Light bg | Light fg | Contrast | Dark bg | Dark fg | Contrast |
|----------|----------|----------|----------|---------|---------|----------|
| success | `#ECFDF3` | `#067647` | 5.4 | `#062C1B` | `#86EFAC` | 10.8 |
| running | `#EFF6FF` | `#175CD3` | 5.5 | `#0B1E3D` | `#93C5FD` | 9.2 |
| error | `#FEF3F2` | `#B42318` | 6.0 | `#3B0A0A` | `#FCA5A5` | 9.0 |
| warning | `#FFFAEB` | `#B54708` | 5.2 | `#3A2604` | `#FCD34D` | 10.0 |
| stale/orange | `#FFF7ED` | `#C2410C` | 4.9 | `#331C05` | `#FDBA74` | 9.5 |
| neutral | `#F3F4F6` | `#374151` | 9.4 | `#1F2937` | `#D1D5DB` | 10.0 |
| review/purple | `#F5F3FF` | `#6D28D9` | 6.5 | `#23153E` | `#C4B5FD` | 9.1 |

Use color + icon + text together. Never rely on color alone.

### Frontend pill colors

| Status | Color token | Icon |
|--------|-------------|------|
| `running` | running (blue) | activity/spinner |
| `completed` | success (green) | check |
| `failed` | error (red) | x-circle |
| `timed_out` | warning (amber) | hourglass |
| `aborted` | neutral (gray) | stop |
| `cancelled` | neutral (gray) | slash |
| `legacy` | neutral, dotted border | dash |

#### Pill anatomy

```
[ icon  label ]
```

Sizing:

```
height: 20px
padding-x: 6px
font-size: 10px or 11px
font-weight: 600
border-radius: 999px
border: 1px solid semantic border
```

Dense table variant:

```
height: 18px
font-size: 10px
```

The `StatusPill` component requires a `taxonomy` prop to prevent status
values from different vocabularies drifting into inconsistent colors:

```tsx
<StatusPill taxonomy="session" value="running" />
<StatusPill taxonomy="health" value="stale" />
<StatusPill taxonomy="verdict" value="request_changes" />
```

#### The "legacy" pseudo-status

Sessions with `status IS NULL` (created before the current status vocabulary
was in place) are displayed as `legacy unclassified`:

- Pill: gray, dotted border, dash icon
- Label: "legacy"
- Excluded from live metrics, trend calculations, and failure counts
- Not treated as `running` or `failed` in the attention queue

On the dashboard, legacy sessions appear only as a muted chip:

```
376 legacy sessions
```

Tooltip: "Sessions created before the current status vocabulary. Excluded
from live operational metrics."

#### Effective state display

When the reported status is `running` but the derived health is `stale`,
the UI must not show a blue Running pill alone. Show the compound state:

```
[stale running]
```

Rules:
- **Do not show a blue Running pill alone when health is stale.**
- In tables with separate Status and Health columns, show both:
  `Status: running` / `Health: stale`
- In dashboards and grouped rows, use the composed form: `stale running`
- For `completed` sessions with `healthy` classification, show only the
  status pill — health is not operationally relevant for terminal sessions.

### Storing timeout metadata

When a session times out, the timeout value that was configured should be
recorded for context:

```python
# On timed_out, write timeout config to session
await db.update_session(session_id, {
    "status": "timed_out",
    "ended_at": time.time(),
    "node_metadata": {
        **existing_metadata,
        "timeout_seconds": timeout,
        "timeout_elapsed": elapsed,
    },
})
```

The run detail page can then show: "Timed out after 900s (configured
timeout: 900s)" — confirming the timeout hit the configured bound,
not some other deadline.

### Schema migration

SQLite doesn't support `ALTER TABLE ... DROP CONSTRAINT`. The CHECK
constraint lives in the table definition. Migration options:

1. **Rebuild the table** (VACUUM-based migration): rename old table,
   create new with updated CHECK, copy data, drop old. Heavy but clean.
2. **Relaxed CHECK** at runtime: `_reconcile_columns()` can't modify
   CHECKs. Instead, bypass the CHECK by storing the new values and
   updating the schema on next `StateDB.open()`.
3. **Remove CHECK entirely**, validate in Python: the CHECK is a
   defense-in-depth measure, not the primary validation. The CLI
   validates status before writing.

Recommended: **option 3**. Remove the CHECK constraint from the schema,
validate in Python at write time. The collapsed v1 schema (pre-release)
can be updated directly since there are no external consumers depending
on the CHECK. The Python validator is the source of truth:

```python
VALID_SESSION_STATUSES = frozenset({
    "running", "completed", "failed", "timed_out", "aborted", "cancelled"
})

def validate_session_status(status: str) -> str:
    if status not in VALID_SESSION_STATUSES:
        raise ValueError(f"Invalid session status: {status}")
    return status
```

### Relationship to health classification (ADR-0024)

Status and health are orthogonal:

- **Status**: what happened? (lifecycle state, written at transitions)
- **Health**: what's happening now? (derived, computed at read time)

A session can be `running` + `stale` (status is running, health says
it's dead). Or `timed_out` + `healthy` (status is terminal, health is
fine — nothing wrong, it just hit its deadline).

Health classification uses status as an input:

```python
if status in SESSION_TERMINAL_STATUSES:
    if has_stale_locks:
        return SessionHealth.ZOMBIE
    return SessionHealth.HEALTHY  # terminal = done

# Only running sessions can be idle/unresponsive/stale/orphaned
```

## Consequences

**Positive**
- Timeout, user abort, and system cancellation are distinguishable in
  the DB and UI — each has a clear follow-up action.
- Exit codes follow UNIX convention — familiar to operators, parseable
  by scripts.
- Dashboard thresholds at 60 minutes flag actual outliers, not routine runs.
- `timed_out` gets amber pill, not red — operators don't waste time
  "investigating" a deliberate timeout.
- Timeout metadata enables "retry with more time" workflow in Studio.
- Python validation replaces SQLite CHECK — more flexible, same safety.

**Negative**
- Schema migration for CHECK constraint removal requires table rebuild
  or constraint relaxation. Pre-release, so acceptable.
- Six statuses instead of four — slightly more to understand. Each maps
  to a clear operational meaning so cognitive load is manageable.
- `cancelled` vs `aborted` distinction may be too fine for some users.
  Mitigated by similar UI treatment (both gray pills) — the distinction
  matters for automation, not for quick visual scanning.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep four statuses, add metadata for timeout | Status pill still shows red "failed" — the metadata helps only on the detail page, not the list |
| Add `crashed` status for OOM/segfault | Can't reliably distinguish crash from error at the Python level; both look like uncaught exceptions. Health classifier handles this post-hoc |
| More granular statuses (e.g., `partial`, `degraded`) | Premature — add when there's a concrete use case. Six is enough for now |
| Timeout as a modifier on failed (`failed:timeout`) | Compound strings don't work with CHECK constraints or enum types |
| Keep 30-minute slow threshold | Flags nearly every flow — useless signal |

## References

- [ADR-0017](ADR-0017-session-lifecycle-status.md) — Original four-status vocabulary (extended here)
- [ADR-0024](ADR-0024-session-health-and-admin-surface.md) — Health classification (consumes status)
- ChatGPT frontend design review (external, not in repo) — Status pill system (section 6): color tokens with hex values and contrast ratios for light/dark mode, pill anatomy spec (height 20px, padding-x 6px, font-size 10-11px, border-radius 999px), legacy pseudo-status treatment, effective state display rules ("do not show blue Running when health is stale")
- `lionagi/cli/agent.py` — Session teardown with `_terminal_status`
- `lionagi/cli/orchestrate/__init__.py` — `LionTimeoutError` handling
- `lionagi/cli/orchestrate/flow.py` — Flow timeout wrapper
- `lionagi/state/schema.sql` — Current CHECK constraint
- `apps/studio/frontend/app/page.tsx` — Dashboard `SLOW_RUN_SECONDS`

### Prior art

- **khive GTD Task FSM** (khive ADR-003) — 7-state FSM with 5 Lean4-proven
  properties: fsm_validity, terminal_no_transitions, completed_reachable_from_pending,
  failed/cancelled_reachable, transition_deterministic. The session status
  transition table above mirrors this pattern.
- **Claude Code Task Lifecycle** (`Task.ts`) — 5 statuses: pending, running,
  completed, failed, killed. `killed` maps to our `cancelled` + `aborted`.
