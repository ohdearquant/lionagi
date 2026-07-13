# ADR-0024: Session Health Classification and Admin Surface

**Status**: Proposed — extended by [ADR-0033](ADR-0033-unified-entity-state-model.md)
**Date**: 2026-05-21
**Extends**: ADR-0017 (session lifecycle), ADR-0019 (run lifecycle management)

---

> **Extension notice**: [ADR-0033](ADR-0033-unified-entity-state-model.md) integrates this ADR's SessionHealth enum as the `process_health` dimension of the unified `NormalizedState`. The six health values defined here (`healthy, idle, unresponsive, stale, orphaned, zombie`) are preserved and feed into the central severity computation. The admin doctor endpoint described here is preserved as the operational interface; the underlying health classification now flows through NormalizedState across all entity reads, not just admin queries.

---

## Context

### "Phantom session" is under-defined

The admin `doctor` endpoint classifies sessions as phantom using three
reasons:

| Reason | Detection | What it actually means |
|--------|-----------|----------------------|
| `process_dead` | PID file check + `ps` scan + `updated_at` staleness | Process crashed or was killed |
| `missing_artifacts` | `artifacts_path` exists but directory doesn't | Session's save path was deleted or never created |
| `stale_lock` | `*.lock` files older than threshold | Lock file left behind by crashed process |

Problems:

1. **`process_dead` conflates two signals.** A session with a dead
   process but recent messages (last 5 min) might have crashed *just now*
   — that's a different urgency than one dead for 12 hours. The staleness
   threshold (`stale_hours`, default 1h) treats both identically.

2. **No message-activity signal.** The classification checks `updated_at`
   (session metadata timestamp) but not message progression. A session
   with `updated_at` from 2 hours ago but a message written 5 minutes
   ago is not stale — something is still writing messages even if the
   session metadata hasn't been updated. Conversely, a session with
   `updated_at` bumped by a metadata write but no messages for 8 hours
   is effectively dead.

3. **`missing_artifacts` is noisy.** Sessions without `--save` don't
   write to `artifacts_path`. The artifacts directory being absent is
   normal for these — but the classifier flags it as phantom.

4. **No graduated severity.** All three reasons produce the same UI:
   a red "phantom" badge with a prune button. There's no distinction
   between "probably safe to kill" and "might be a legitimate pause."

5. **Prune is the only action.** The admin page offers prune (delete
   the session row) but not transition (mark as failed), inspect (show
   session detail), or recover (restart).

### The admin page could do much more

The admin page currently shows:

- DB health strip (size, WAL size, WAL pending)
- Phantom sessions table (checkbox + prune)

It could be the operational control center:

- Session health overview (not just phantoms)
- Active resource usage (worktrees, disk, connections)
- Maintenance actions (checkpoint, vacuum, import, prune)
- Configuration view (agent profiles, chain status)
- Event log (recent hook events, errors, transitions)

## Decision

### Part A: Precise session health classification

Replace the binary "phantom or not" with a graduated health model:

```python
from enum import Enum

class SessionHealth(str, Enum):
    HEALTHY = "healthy"           # running, messages flowing (process visibility optional)
    IDLE = "idle"                 # running, no messages for >1h but under threshold
    UNRESPONSIVE = "unresponsive" # running, process alive, no messages for >threshold
    STALE = "stale"               # running, process confirmed dead — or liveness unknown and quiet >threshold
    ORPHANED = "orphaned"         # running, no process, no artifacts, no messages
    ZOMBIE = "zombie"             # completed/failed but resources not cleaned up
```

`process_alive` is tri-state (`bool | None`):

- `True` — observed alive: a recorded pid is running (with process start-time
  verification when one was recorded), or the session id appears in the
  process table.
- `False` — confirmed dead: a recorded pid is no longer running, or its start
  time no longer matches the recorded one (pid recycled). Positive death
  evidence skips the activity guard, so the session classifies STALE
  immediately regardless of how fresh its last message is.
- `None` — unknown: no recorded pid and no process match. This is the normal
  case for externally-driven sessions mirrored into the DB, so the activity
  guard applies: recent messages keep the session HEALTHY/IDLE, and only quiet
  past the kind threshold classifies STALE.

Limitation: a bare recycled pid (recorded pid, no recorded start time, pid
reused by an unrelated process) reads alive. This is rare and fails toward
treating the session as live rather than falsely dead.

Classification logic:

```python
def classify_session_health(
    session: dict,
    *,
    now: float,
    process_alive: bool | None,
    has_artifacts: bool,
    has_stale_locks: bool,
) -> SessionHealth:
    status = session.get("status", "completed")

    # Terminal sessions
    if status in ("completed", "failed", "aborted", "timed_out", "cancelled"):
        if has_stale_locks or (has_artifacts and _has_temp_files(session)):
            return SessionHealth.ZOMBIE
        return SessionHealth.HEALTHY  # terminal = done, nothing wrong

    # Running sessions only below
    last_activity = (
        session.get("last_message_at")
        or session.get("updated_at")
        or session.get("started_at")
        or 0
    )
    idle_seconds = now - last_activity

    kind = session.get("invocation_kind")
    threshold = STALE_THRESHOLDS.get(kind, DEFAULT_STALE_THRESHOLD)

    if process_alive is not True:
        # Orphan check first — no artifacts AND no messages means
        # the session never produced output (regardless of age).
        if not has_artifacts and session.get("message_count", 0) == 0:
            return SessionHealth.ORPHANED
        if process_alive is False:
            # Confirmed dead: positive evidence outranks the activity
            # guard below — the process is gone no matter how fresh
            # the last message is.
            return SessionHealth.STALE
        # Unknown liveness: recent messages outrank process visibility —
        # externally-driven sessions (CLI seats mirrored into the DB)
        # expose no matchable pid, so an unmatched process only means
        # dead once activity has also gone quiet past the kind threshold.
        if idle_seconds <= threshold:
            if idle_seconds > 3600:  # 1 hour
                return SessionHealth.IDLE
            return SessionHealth.HEALTHY
        return SessionHealth.STALE

    # Process is alive
    if idle_seconds > threshold:
        return SessionHealth.UNRESPONSIVE
    if idle_seconds > 3600:  # 1 hour
        return SessionHealth.IDLE

    return SessionHealth.HEALTHY
```

#### Health → action mapping

Color assignments adopt the review's token system (see ChatGPT frontend design
review, section 3):

| Health | Color | Available actions |
|--------|-------|------------------|
| `HEALTHY` | Green (calm) | Inspect |
| `IDLE` | Neutral/blue (muted) | Inspect |
| `UNRESPONSIVE` | Amber | Inspect, inspect logs |
| `STALE` | Orange | Inspect, Transition to failed |
| `ORPHANED` | Purple | Repair or transition |
| `ZOMBIE` | Red | Cleanup/prune |

#### Thresholds (kind-aware, from ADR-0019)

```python
STALE_THRESHOLDS = {
    "agent": 6 * 3600,        # 6h
    "play": 6 * 3600,         # 6h
    "flow": 12 * 3600,        # 12h
    "fanout": 12 * 3600,      # 12h
    "show-play": 12 * 3600,   # 12h
}
DEFAULT_STALE_THRESHOLD = 6 * 3600
```

### Part B: Admin API expansion

#### New endpoints

```text
GET  /api/admin/health          # Full health report
POST /api/admin/transition      # Change session status (with guard)
POST /api/admin/checkpoint      # Force WAL checkpoint
POST /api/admin/vacuum          # VACUUM the DB
GET  /api/admin/resources       # Active resources (worktrees, disk)
GET  /api/admin/events          # Recent admin events log
```

#### Health endpoint response

```json
{
  "sessions": {
    "total": 376,
    "by_status": {
      "running": 8,
      "completed": 350,
      "failed": 15,
      "aborted": 3
    },
    "by_health": {
      "healthy": 371,
      "idle": 2,
      "unresponsive": 1,
      "stale": 3,
      "orphaned": 1,
      "zombie": 0
    },
    "unhealthy": [
      {
        "session_id": "abc123",
        "name": "play:backend",
        "health": "stale",
        "status": "running",
        "last_message_at": 1716250000.0,
        "idle_seconds": 21600,
        "process_alive": false,
        "invocation_kind": "play",
        "agent_name": "architect",
        "model": "claude/claude-sonnet-4-6",
        "message_count": 42
      }
    ]
  },
  "db": {
    "size_bytes": 4521984,
    "wal_bytes": 32768,
    "page_count": 1100,
    "freelist_count": 12,
    "journal_mode": "wal",
    "auto_checkpoint": 1000,
    "foreign_keys": true,
    "busy_timeout": 5000,
    "schema_version": "1"
  },
  "resources": {
    "worktrees": [
      {
        "path": "/path/to/work/worktrees/lionagi-issue-sweep-backend",
        "branch": "show/lionagi-issue-sweep/backend",
        "status": "stale",
        "last_modified": 1716250000.0,
        "uncommitted_changes": false
      }
    ],
    "disk": {
      "state_db": 4521984,
      "runs_dir": 125829120,
      "teams_dir": 8192,
      "total": 130359296
    }
  },
  "diagnostic_run_at": "2026-05-21T16:45:00Z"
}
```

#### Transition endpoint

```python
class TransitionBody(BaseModel):
    session_ids: list[str]
    target_status: Literal["failed", "aborted", "cancelled"]
    reason: str  # required: why are you transitioning?

# POST /api/admin/transition
async def transition_sessions(body: TransitionBody) -> dict:
    """Transition running sessions to a terminal status.

    Guards:
    1. Only running sessions can be transitioned.
    2. Healthy/idle sessions are rejected (process still alive + messages flowing).
    3. Transition writes an admin event log entry with reason.
    """
```

This replaces the blunt "prune" (delete the row) with "transition"
(mark as failed/aborted). The session and its messages are preserved
for debugging. Prune remains available for cleanup after inspection.

#### Checkpoint and vacuum endpoints

```python
# POST /api/admin/checkpoint
async def checkpoint(mode: str = "TRUNCATE") -> dict:
    """Force WAL checkpoint. Modes: PASSIVE, FULL, RESTART, TRUNCATE."""
    # TRUNCATE reclaims the WAL file when no readers are active.

# POST /api/admin/vacuum
async def vacuum() -> dict:
    """VACUUM the DB to reclaim freed pages. Holds exclusive lock."""
    # Returns before/after size.
```

These expose `li state checkpoint` and `li state vacuum` via the API.

### Part C: Admin page redesign

The admin page becomes a full operational console with a top action row,
two-column layout (Session health + Maintenance), an intervention queue,
and side-by-side Resources + Event log panels.

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Admin                                                    Checked 12:52 PM    │
│ Studio maintenance and diagnostics                                          │
│                                                                             │
│ [Refresh] [Checkpoint WAL] [Vacuum DB] [Export snapshot]                    │
├─────────────────────────────────────────────────────────────────────────────┤
│ DB 847.3 MB · WAL 4.0 MB · WAL pending 4.0 MB · Conn 0 · Disk OK · SSE Live │
├──────────────────────────────────┬──────────────────────────────────────────┤
│ Session health                   │ Maintenance                              │
│                                  │                                          │
│ healthy          37              │ WAL checkpoint available                 │
│ idle              2              │ Last checkpoint unavailable              │
│ unresponsive      0              │ Vacuum recommended: no                   │
│ stale             4              │ Export size estimate: 851 MB             │
│ orphaned          0              │                                          │
│ zombie            0              │ [Checkpoint] [Export]                    │
├──────────────────────────────────┴──────────────────────────────────────────┤
│ Intervention queue                                                          │
│                                                                             │
│ State       Session       Kind       Invocation         Last event   Action  │
│ stale       f82488be      agent      /show issue-sweep  1h ago       Inspect │
│ stale       462bb5ed      agent      /show issue-sweep  9h ago       Fail    │
│ stale       c3e69388      agent      /show issue-sweep  17h ago      Fail    │
│ stale       cf1b48f8      agent      /show issue-sweep  17h ago      Fail    │
│                                                                             │
│ Bulk: [Transition selected to failed] [Prune terminal only] [Export selected]│
├──────────────────────────────────┬──────────────────────────────────────────┤
│ Resources                        │ Admin event log                          │
│                                  │                                          │
│ Worktrees                        │ 12:48  checkpoint requested              │
│ 9 total · 4 active · 2 stale     │ 12:48  checkpoint completed              │
│                                  │ 12:31  session transitioned to failed    │
│ Disk usage                       │        f82488be · missing artifacts      │
│ DB          847 MB               │                                          │
│ WAL           4 MB               │                                          │
│ Artifacts     2.1 GB             │                                          │
│ Worktrees     6.8 GB             │                                          │
└──────────────────────────────────┴──────────────────────────────────────────┘
```

#### Transition modal

Bulk transitions open a confirmation modal that preserves session records
while marking them terminal:

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Transition 4 sessions to failed?                                    │
│                                                                     │
│ This preserves session records and artifacts                        │
│ while marking them terminal.                                        │
│                                                                     │
│ Reason                                                              │
│ [ Missing artifacts / stale heartbeat                            v ]│
│                                                                     │
│ Note                                                                │
│ [ optional operator note                                          ] │
│                                                                     │
│ Affected sessions                                                   │
│ f82488be · 462bb5ed · c3e69388 · cf1b48f8                           │
│                                                                     │
│ [Cancel] [Transition to failed]                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Prune remains visually secondary and is disabled unless sessions are terminal
or explicitly eligible:

```text
Prune selected
disabled: active sessions must be transitioned or exported first
```

#### Dashboard metric cards

The dashboard (ADR-0019 §B4) replaces the four current cards with an
operator-focused layout. Cards:

```text
1. Attention
2. Active Executions
3. Outcomes
4. Latency / SLA
```

**Attention** (most important card):

```text
attention =
  stale running sessions
  + zombie sessions
  + orphaned sessions
  + failed/timed_out in current range
  + request_changes / reject verdicts
  + blocked shows / chains
```

Example card:

```text
ATTENTION
4
4 stale · 0 failed · 0 needs review
Oldest: 17h 56m
```

**Active Executions**: separates "active" from "healthy". A session can be
reported as `running` but effectively `stale`:

```text
ACTIVE EXECUTIONS
6 invocations
4 sessions · 6 plays
Oldest active: 17h 56m
```

**Outcomes**: summarizes quality, not just completion. Shows structured outcome
breakdown when verdicts exist:

```text
OUTCOMES 24H
37 completed
0 failed · 1 request changes
Pass rate: 97%
```

**Latency / SLA**: replaces the "Slow Runs" card with tail latency.
Threshold is **60 minutes** (confirmed by ADR-0025):

```text
LATENCY 24H
P95 58m
2 over 60m · median 5m 18s
```

| Signal | Meaning | Dashboard treatment |
|--------|---------|---------------------|
| Active run over 60m | needs intervention | included in Attention |
| Completed run over 60m | performance trend | included in Latency |

#### (Null) / legacy session handling on dashboard

Do not show `(Null)` sessions in the main status breakdown. Rename to:

```text
Legacy unclassified
```

Display as a muted data-hygiene chip in the system strip:

```text
System: healthy · DB 847.3 MB · WAL 4.0 MB · Conn 0 · Legacy 376
```

Legacy sessions are excluded from live health, trend, and failure metrics.
Tooltip: "Sessions created before the current status vocabulary. Excluded
from live operational metrics."

Admin provides: Backfill status | Archive legacy | Export legacy.

#### Key changes from current admin page

| Current | Proposed |
|---------|----------|
| Binary phantom/not | Graduated health (6 levels) |
| Prune only | Transition + Prune + Inspect |
| DB health strip (3 numbers) | Full DB stats + two-column maintenance panel |
| No action row | Refresh / Checkpoint WAL / Vacuum DB / Export snapshot |
| No resource view | Worktrees, disk usage (side-by-side with event log) |
| No event log | Admin action history with timestamp + target + reason |
| No context on phantom sessions | Intervention queue: State/Session/Kind/Invocation/Last event/Action columns |
| No transition modal | Reason dropdown + optional note + affected session list |
| No legacy session handling | Dashboard chip + admin bulk actions for null sessions |

### Part D: Admin event log

```sql
CREATE TABLE IF NOT EXISTS admin_events (
  id              TEXT    PRIMARY KEY,
  created_at      REAL    NOT NULL,
  action          TEXT    NOT NULL,  -- "transition", "prune", "checkpoint", "vacuum", "classify"
  target_id       TEXT,              -- session_id, or NULL for DB-wide actions
  details         JSON    NOT NULL,  -- action-specific data
  actor           TEXT    NOT NULL DEFAULT 'admin'  -- "admin", "doctor_auto", "chain"
);

CREATE INDEX IF NOT EXISTS idx_admin_events_created ON admin_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_events_action ON admin_events(action);
```

Every admin action (transition, prune, checkpoint, vacuum) writes an
event. The event log provides an audit trail and feeds the admin page's
event log section.

Doctor scans also write events when they classify sessions, creating a
timeline of health changes:

```json
{
  "action": "classify",
  "target_id": "abc123",
  "details": {
    "health": "stale",
    "previous_health": "unresponsive",
    "idle_seconds": 21600,
    "process_alive": false,
    "message_count": 42
  },
  "actor": "doctor_auto"
}
```

### Part E: Resource tracking

#### Worktrees

```python
async def list_worktrees() -> list[dict]:
    """Scan known worktree roots for lionagi worktrees."""
    worktrees = []
    for root in [
        Path.home() / "khive-work" / "worktrees",
        Path.home() / "khive-work" / "shows",
    ]:
        if not root.exists():
            continue
        for wt in root.iterdir():
            if not wt.is_dir():
                continue
            # Check if it's a git worktree
            git_file = wt / ".git"
            if not git_file.exists():
                continue
            worktrees.append({
                "path": str(wt),
                "branch": _get_worktree_branch(wt),
                "last_modified": wt.stat().st_mtime,
                "uncommitted_changes": _has_uncommitted(wt),
                "status": _classify_worktree(wt),
            })
    return worktrees
```

#### Disk usage

```python
async def disk_usage() -> dict:
    """Aggregate disk usage for lionagi-managed directories."""
    return {
        "state_db": _file_size(DEFAULT_DB_PATH),
        "runs_dir": _dir_size(RUNS_ROOT),
        "teams_dir": _dir_size(TEAMS_DIR),
        "shows_dir": _dir_size(Path.home() / "khive-work" / "shows"),
        "worktrees_dir": _dir_size(Path.home() / "khive-work" / "worktrees"),
        "agents_dir": _dir_size(LIONAGI_HOME / "agents"),
        "playbooks_dir": _dir_size(LIONAGI_HOME / "playbooks"),
    }
```

### Part F: Auto-doctor (optional background scan)

The Studio backend can run doctor scans periodically in the background:

```python
# In app.py lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background doctor scan every 5 minutes
    task = asyncio.create_task(_periodic_doctor())
    yield
    task.cancel()

async def _periodic_doctor():
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            await admin_svc.classify_all_running_sessions()
        except Exception:
            logger.exception("Background doctor scan failed")
```

This replaces the current reactive-only model. The admin page shows
health that's at most 5 minutes stale, not only when someone clicks
"Doctor."

Auto-doctor does NOT auto-transition. It only classifies and logs.
Transitions remain explicit admin actions.

## Consequences

**Positive**

- Graduated health model gives precise language for session state —
  "stale" and "orphaned" mean different things with different actions.
- Transition preserves session data for debugging; prune is a separate
  step after inspection.
- Admin page becomes useful beyond "clean up phantoms" — it's a full
  operational console.
- Event log provides audit trail for admin actions.
- Resource tracking surfaces worktrees and disk usage that currently
  require manual `ls` and `du`.
- Auto-doctor keeps health classifications fresh without operator
  intervention.

**Negative**

- More complex classification logic — 6 health states instead of 3
  phantom reasons. Mitigated by clear decision tree and tests.
- Background doctor scan adds CPU/IO load every 5 minutes. At our
  session count (< 400), the scan is sub-second.
- Admin event log grows unbounded. Mitigated by periodic cleanup (prune
  events older than 30 days, keep latest 1000).
- Worktree scanning requires filesystem access — adds latency to the
  health endpoint. Mitigated by caching with 60s TTL.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep binary phantom/not | Under-specified; different failure modes need different responses |
| Auto-transition stale sessions | Risk of killing legitimate pauses; operator should confirm |
| Store health as a DB column | Health is derived from multiple signals; storing it would require re-derivation triggers |
| Separate admin app | Fragmentation; admin belongs in the same Studio UI |
| Health checks via CLI only (`li state doctor`) | CLI is for operators; Studio is for monitoring. Both should work, but Studio is primary |
| Event log in a separate file | DB is already the persistence layer; another file adds sync complexity |

## References

- [ADR-0017](ADR-0017-session-lifecycle-status.md) — Session lifecycle status
- [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md) — Run lifecycle + staleness
- [ADR-0022](ADR-0022-run-step-provenance.md) — Session provenance (model, agent)
- ChatGPT frontend design review (external, not in repo) — Admin console layout (section 3): action button row, two-column layout, intervention queue columns, transition modal, Resources + Event log side-by-side; dashboard metric cards (section 2): Attention/Active Executions/Outcomes/Latency cards, legacy session chip, 60m threshold
- `apps/studio/server/services/admin.py` — Current phantom classification
- `apps/studio/server/routers/admin.py` — Current admin endpoints
- `apps/studio/frontend/app/admin/page.tsx` — Current admin page
- `apps/studio/frontend/app/page.tsx` — Current dashboard (client-side health heuristics)

### Prior art

- **NIST SP 800-92** ("Guide to Computer Security Log Management") — The
  `admin_events` table follows the append-only audit log pattern. Events are
  insert-only with no UPDATE/DELETE, matching the immutable log architecture.
- **khive GTD Task FSM** (ADR-003, Lean4-proven) — The `can_transition`
  validation pattern for `TransitionBody` mirrors khive's `transition` verb
  with formally verified state machine properties.
