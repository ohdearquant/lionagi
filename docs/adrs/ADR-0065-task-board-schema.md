# ADR-0065: Task Board Schema

**Status**: proposed
**Date**: 2026-05-27
**Depends on**: ADR-0064 (work system integration), ADR-0009 (SQLite state layer)
**Related**: ADR-0030 (attention queue), ADR-0031 (entity header pattern), ADR-0063 (task board work center — this ADR supersedes ADR-0063's schema subsection for the lionagi.work projection; ADR-0063's work_items table governs the operator UI layer, this ADR's work_tasks table governs lionagi.work persistence)

## Context

ADR-0064 introduces `lionagi.work` — an in-memory dispatch layer for structured
forms and workers.  The work system answers "what is happening right now" but
loses its history on process restart.

Studio operators need a persistent view of work: which tasks were submitted
today, which failed, which are queued, and what the throughput looks like over
time.  This is the *task board*.

The task board is not a replacement for the work engine's in-memory state — it
is the **persistence projection** of that state, suitable for Studio's SQLite
state layer (ADR-0009) and the Attention Queue (ADR-0030).

### Why a separate ADR?

ADR-0064 scoped itself to the in-memory work system with no persistence.
Persistence introduces its own decisions:

1. **Schema shape** — which fields survive to the database?
2. **Write path** — when and how does the engine flush to SQLite?
3. **Query surface** — what does the task board API expose?
4. **Studio integration** — how does the UI consume this data?

These decisions touch ADR-0009 (SQLite), ADR-0030 (attention queue), and
ADR-0031 (entity header), so they warrant a separate record.

## Decision

Add a `work_tasks` table to the Studio SQLite database and a minimal write
path that flushes `WorkTask` snapshots to it.  The table shape mirrors
`WorkTask` (ADR-0064) with two additions: a `project` column for filtering
and a `form_snapshot` JSON column for forensic inspection.

### 1. Table schema

```sql
CREATE TABLE IF NOT EXISTS work_tasks (
    task_id         TEXT    NOT NULL PRIMARY KEY,
    form_id         TEXT    NOT NULL,
    worker_id       TEXT    NOT NULL,
    project         TEXT,                       -- optional project label
    status          TEXT    NOT NULL DEFAULT 'queued',
    error           TEXT,
    submitted_at    REAL    NOT NULL,
    completed_at    REAL,
    form_snapshot   TEXT,                       -- JSON of WorkForm.model_dump()
    result_snapshot TEXT                        -- JSON of WorkResult.value (if serialisable)
);

CREATE INDEX IF NOT EXISTS ix_work_tasks_status   ON work_tasks(status);
CREATE INDEX IF NOT EXISTS ix_work_tasks_worker   ON work_tasks(worker_id);
CREATE INDEX IF NOT EXISTS ix_work_tasks_project  ON work_tasks(project);
CREATE INDEX IF NOT EXISTS ix_work_tasks_submitted ON work_tasks(submitted_at DESC);
```

`form_snapshot` stores the serialised `WorkForm` at submission time for
forensic replay.  `result_snapshot` stores the JSON-serialisable portion of
the handler's return value (skipped for non-serialisable results).

### 2. Python schema (Pydantic)

```python
from pydantic import BaseModel
from typing import Any

class WorkTaskRow(BaseModel):
    """SQLite row model for the work_tasks table."""
    task_id:         str
    form_id:         str
    worker_id:       str
    project:         str | None  = None
    status:          str         = "queued"
    error:           str | None  = None
    submitted_at:    float
    completed_at:    float | None = None
    form_snapshot:   str | None  = None   # JSON
    result_snapshot: str | None  = None   # JSON
```

### 3. Write path

The engine does not write to SQLite directly.  Callers attach a **post-submit
callback** that persists state transitions.  This keeps the engine free of I/O
dependencies (per ADR-0064's standalone constraint).

```python
import json
import sqlite3
from lionagi.work import WorkEngine, WorkTask

def _upsert_task(conn: sqlite3.Connection, task: WorkTask, form_json: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO work_tasks
          (task_id, form_id, worker_id, status, error,
           submitted_at, completed_at, form_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
          status       = excluded.status,
          error        = excluded.error,
          completed_at = excluded.completed_at
        """,
        (
            task.task_id, task.form_id, task.worker_id, task.status,
            task.error, task.submitted_at, task.completed_at, form_json,
        ),
    )
    conn.commit()

# Attach via a thin wrapper around the engine
class PersistentWorkEngine(WorkEngine):
    def __init__(self, conn: sqlite3.Connection, **kwargs):
        super().__init__(**kwargs)
        self._conn = conn

    def submit(self, form, worker_id=None):
        task_id = super().submit(form, worker_id=worker_id)
        task = self.get_task(task_id)
        if task:
            try:
                form_json = form.model_dump_json()
            except Exception:
                form_json = None
            _upsert_task(self._conn, task, form_json)
        return task_id
```

### 4. Studio API surface

```text
GET /api/work/tasks
    ?status=queued|running|completed|failed
    ?worker_id=<str>
    ?project=<str>
    ?limit=<int>   (default 50)
    ?offset=<int>

Response:
{
  "total": 142,
  "tasks": [WorkTaskRow, ...]
}

GET /api/work/tasks/<task_id>
Response: WorkTaskRow (with form_snapshot and result_snapshot)

GET /api/work/workers
Response: {worker_id: {definition_id, name, in_flight, max_concurrent}, ...}
```

### 5. Attention Queue integration (ADR-0030)

Failed and timed-out work tasks surface as Attention Queue items with:

```python
AttentionItem(
    kind="work_task",
    id=task.task_id,
    severity="warning",
    reason_code="work.task.failed",
    summary=f"Worker {task.worker_id!r} task failed: {task.error[:80]}",
    evidence_refs=[{"kind": "work_task", "id": task.task_id}],
    actions=[
        EntityAction(id="inspect", label="Inspect task",
                     href=f"/work/tasks/{task.task_id}"),
        EntityAction(id="retry",   label="Retry",
                     endpoint=f"/api/work/tasks/{task.task_id}/retry",
                     method="POST"),
    ],
)
```

### 6. Task board Studio page

A new `/work` page in Studio lists `work_tasks` with:

- Status filter pills (queued / running / completed / failed / all)
- Worker filter dropdown
- Columns: task_id (truncated), form_id, worker, status pill, submitted_at,
  duration, error (truncated)
- Row click → detail sheet showing `form_snapshot` and `result_snapshot`
- Retry button on failed rows (calls `POST /api/work/tasks/<id>/retry`)

The page consumes `EntityHeader` (ADR-0031) at the top with:

```json
{
  "kind": "work_task",
  "title": "<task_id>",
  "status": "failed",
  "status_taxonomy": "work_task",
  "goal": "Worker echo_worker processing form user_form",
  "last_event": {"summary": "ValueError: handler failed", "at": 1716517300.0},
  "actions": [
    {"id": "inspect", "label": "Inspect form snapshot", "kind": "secondary"},
    {"id": "retry",   "label": "Retry",                  "kind": "primary",
     "endpoint": "/api/work/tasks/<id>/retry", "method": "POST"}
  ]
}
```

## Consequences

**Positive**

- Persistence is additive: the base `WorkEngine` (ADR-0064) stays I/O-free;
  `PersistentWorkEngine` wraps it with a two-line override.

- `form_snapshot` enables forensic replay without re-running the pipeline.

- Studio task board gives operators visibility into in-process work without
  instrumenting every pipeline individually.

- Attention Queue integration surfaces failures automatically; operators do
  not need to poll the task list.

**Negative**

- `ON CONFLICT ... DO UPDATE` requires at least two writes per task lifecycle
  (submit → complete/fail).  For high-frequency workers (thousands of tasks
  per minute), this may create write pressure on SQLite.  Mitigation:
  batch writes or switch to WAL mode.

- `form_snapshot` can be large for forms with embedded binary or large text.
  Callers should cap snapshot size before storing.

- `result_snapshot` is silently skipped for non-JSON-serialisable results.
  Workers that return complex objects (ML tensors, file handles) will show
  `null` in Studio.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Engine writes directly to SQLite | Violates ADR-0064's standalone constraint. Introduces I/O in the hot path. |
| Separate time-series DB for metrics | Overkill for current scale; Studio already uses SQLite (ADR-0009). |
| No persistence, rely on ADR-0030 attention queue | Attention Queue only surfaces failures, not the full task history. |
| Event sourcing (append-only log) | Correct architecture for high-frequency work, but premature for current usage. Can migrate to it when write pressure becomes a real constraint. |

## Non-Goals

- **No multi-process coordination.**  The task board is for a single Studio
  instance.
- **No real-time push.**  The Studio page polls; SSE can be added later
  (ADR-0006 pattern).
- **No retention policy.**  Operators manage table growth manually or via a
  cron job; automatic TTL is deferred.

## References

- [ADR-0064](ADR-0064-work-system-integration.md) — work system (in-memory engine this ADR persists).
- [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite state layer (target database).
- [ADR-0030](ADR-0030-attention-queue.md) — Attention Queue (consumes failed task events).
- [ADR-0031](ADR-0031-entity-header-pattern.md) — entity header (used on task detail page).
- `lionagi/work/` — work system implementation.
