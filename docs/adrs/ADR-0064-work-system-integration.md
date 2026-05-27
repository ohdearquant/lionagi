# ADR-0064: Work System Integration

**Status**: Accepted
**Date**: 2026-05-27
**Related**: ADR-0023 (unified hook system), ADR-0029 (artifact contract), ADR-0031 (entity header pattern)

## Context

LionAGI's agent and session layer handles free-form LLM conversations.  There
is a gap between that layer and *structured, repeatable work*: tasks that have
defined inputs, produce defined outputs, and need validation before they can be
safely dispatched to a worker.

The need appears in three recurring scenarios:

1. **CLI pipelines** (`li o flow`) route instructions between workers.  Each
   worker expects a specific set of fields.  Mis-spelled or missing fields
   cause silent downstream failures.

2. **Human-in-the-loop checkpoints** require operators to fill a form before
   a restricted action (deploy, publish) proceeds.  Currently there is no
   structured capture mechanism — values are passed as unvalidated strings.

3. **Multi-step automation** (sessions that call external services, transform
   data, and write artifacts) needs a contract between producer and consumer
   stages.  Without a shared schema, each stage duplicates ad-hoc parsing.

### Why not just Pydantic models per worker?

Worker-specific Pydantic models solve the schema problem but leave the
*runtime* problems unaddressed:

- **Status tracking**: which forms have been filled and validated?
- **Dispatch routing**: which worker handles this form?
- **Concurrency control**: how many tasks can a worker run simultaneously?
- **Result retrieval**: where is the output of a completed task?

A thin layer above raw Pydantic models — one that provides lifecycle status,
a dispatch engine, and a result store — answers all four questions without
prescribing a message broker or a database.

## Decision

Introduce `lionagi.work`, a standalone sub-package with four components:

| Component | Module | Responsibility |
|---|---|---|
| `FieldSpec` + `WorkForm` | `form.py` | Typed schema + lifecycle container |
| `Rule` + `RuleSet` | `rules.py` | Declarative cross-field validation |
| `WorkerDefinition` | `definition.py` | Static worker descriptor |
| `WorkEngine` | `engine.py` | Dispatch, concurrency, result storage |

The sub-package has **no hard dependencies** beyond Pydantic.  It does not
import from `lionagi.session`, `lionagi.service`, or any provider.  It can
be embedded in CLI pipelines, used in tests without a live model, and
composed with the hook system (ADR-0023) as needed.

---

## Form Lifecycle

Every `WorkForm` instance progresses through a defined set of statuses.

```text
draft
  │
  ▼  fill_form(form, values)
filled
  │
  ▼  validate_form(form)  ──── validation failures ───▶  error
  │
validated
  │
  ▼  engine.submit(form)
submitted
  │
  ▼  handler completes
completed
```

**draft** — Initial state when a `WorkForm` is constructed.  Fields are
declared but no values have been supplied.

**filled** — `fill_form()` has merged values into the form.  Validation runs
automatically; the form moves to `validated` or `error` immediately.

**validated** — All required fields are present and all values coerce to their
declared types.  The form is ready to be submitted to the engine.

**error** — One or more validation failures were recorded in
`form.validation_errors`.  The form must be re-filled (with corrected values)
to proceed.

**submitted** — The engine has accepted the form and assigned it a `WorkTask`.
The originating form object is not mutated; the engine tracks status on the
`WorkTask`.

**completed** — The worker handler returned successfully.  The result is
available via `engine.get_result(task_id)`.

Transitions that are not shown (e.g., `validated → draft`) are not valid.
Nothing prevents creating a new form from scratch, but the lifecycle status
of an existing form is append-only within the normal flow.

---

## Worker Composition Patterns

### Pattern 1 — Single-stage transform

The simplest case: one form in, one form out.

```python
from lionagi.work import FieldSpec, WorkForm, WorkEngine, WorkerDefinition, fill_form

def word_count_handler(form: WorkForm) -> dict:
    text = form.values.get("text", "")
    return {"word_count": len(text.split())}

defn = WorkerDefinition(
    definition_id="word_count",
    name="Word Count Worker",
    input_form="text_input",
    output_form="count_output",
    handler="mypackage.workers.word_count_handler",
)

engine = WorkEngine()
engine.register_worker(defn, word_count_handler)

input_form = WorkForm(
    form_id="text_input",
    fields={"text": FieldSpec(name="text", type="str", required=True)},
)
filled = fill_form(input_form, {"text": "hello world foo"})
task_id = engine.submit(filled, worker_id="word_count")

result = engine.get_result(task_id)
# result.value == {"word_count": 3}
```

### Pattern 2 — Chained workers (producer → consumer)

The output of one worker becomes the input form values for the next.

```python
# Stage 1: extract keywords
def extract_handler(form: WorkForm) -> dict:
    words = form.values["text"].lower().split()
    keywords = [w for w in words if len(w) > 4]
    return {"keywords": keywords}

# Stage 2: rank keywords
def rank_handler(form: WorkForm) -> dict:
    kws = form.values.get("keywords", [])
    return {"ranked": sorted(set(kws))}

engine.register_worker(extract_defn, extract_handler)
engine.register_worker(rank_defn, rank_handler)

# Submit stage 1
t1 = engine.submit(fill_form(text_form, {"text": "the quick brown fox"}), worker_id="extract")
r1 = engine.get_result(t1)

# Feed stage 1 output into stage 2
t2 = engine.submit(fill_form(kw_form, r1.value), worker_id="rank")
r2 = engine.get_result(t2)
```

The engine does not manage the chain itself — the orchestrator (a CLI flow,
a Branch, or application code) drives the handoff.  This keeps the engine
simple and the chain topology flexible.

### Pattern 3 — Governed workers (gate before dispatch)

A governed worker requires a human-filled form (or a gate check) before
submission.  The hook system (ADR-0023) attaches the gate as a pre-submit
callable.

```python
from lionagi.work import RuleSet, Rule

# Define business rules beyond type checking
rules = RuleSet()
rules.add(Rule(rule_id="budget_limit", field="budget_usd",
               check="range", params={"max": 10_000},
               message="Budget exceeds approval threshold."))
rules.add(Rule(rule_id="env_prod", field="target_env",
               check="pattern", params={"pattern": r"^(staging|prod)$"},
               message="target_env must be 'staging' or 'prod'."))

def governed_handler(form: WorkForm) -> dict:
    errors = rules.apply_all(form)
    if errors:
        raise ValueError(f"Gate failed: {errors}")
    return {"deployed": True, "env": form.values["target_env"]}
```

Governance logic lives inside the handler or a pre-submit hook — the engine
itself is policy-neutral.

---

## Engine Dispatch Algorithm

```text
submit(form, worker_id=None)
  ├─ LOCK acquired
  ├─ resolve_slot(worker_id)
  │    ├─ if worker_id is None and exactly one worker → use it
  │    ├─ if worker_id is None and multiple workers → raise ValueError
  │    └─ if worker_id not in registry → raise ValueError
  ├─ slot.at_capacity? → raise RuntimeError (caller must retry or queue)
  ├─ create WorkTask(form_id, worker_id, status="queued")
  ├─ store task in _tasks dict
  ├─ slot.in_flight += 1
  ├─ LOCK released
  └─ _run_task(task, slot, form)
       ├─ LOCK: task.status = "running"
       ├─ call slot.handler(form) [with optional SIGALRM timeout on Unix]
       ├─ on success:
       │    LOCK: task.status="completed", task.result=value, task.completed_at=now()
       └─ on exception:
            LOCK: task.status="failed", task.error=repr(exc), task.completed_at=now()
       └─ LOCK: slot.in_flight = max(0, slot.in_flight - 1)
```

Key properties:

- The lock is held only during state mutations, not during handler execution.
  This means multiple threads can submit and execute tasks concurrently.
- `at_capacity` is checked under the lock to avoid TOCTOU races.
- The `max_concurrent=0` sentinel means unlimited concurrency (no capacity
  check).
- Timeout enforcement on synchronous handlers uses `signal.SIGALRM` on Unix.
  On Windows, the timeout parameter is accepted but not enforced for sync
  handlers (async handlers use `asyncio.wait_for`).

---

## Error Handling and Retry Strategy

### Form-level errors

`validate_form` returns a new form with `status="error"` and a list of
human-readable messages in `validation_errors`.  The caller inspects the list,
corrects the input, and calls `fill_form` again.  There is no automatic retry
at the form level.

### Task-level failures

When a handler raises, the `WorkTask` moves to `status="failed"` with
`task.error` set to `"ExcType: message"`.  The `WorkResult.success` property
is False.

Retry strategy is the caller's responsibility — the engine does not implement
automatic retry.  A typical retry loop:

```python
MAX_RETRIES = 3
for attempt in range(MAX_RETRIES):
    task_id = engine.submit(form, worker_id="my_worker")
    result = engine.get_result(task_id)
    if result.success:
        break
    if attempt < MAX_RETRIES - 1:
        time.sleep(2 ** attempt)  # exponential back-off
else:
    raise RuntimeError(f"Worker failed after {MAX_RETRIES} attempts: {result.error}")
```

### Concurrency limit reached

When a worker is at its `max_concurrent` limit, `submit` raises `RuntimeError`
immediately rather than blocking.  The caller may queue the form, reduce
concurrency, or raise to the operator.  Blocking queues are outside the scope
of this module (use `threading.Semaphore` or an async queue in the calling
layer).

---

## Integration with ADR-0023 (Hook System)

The engine is hook-neutral but composes cleanly:

```python
# Pre-submit hook: validate business rules before the task enters the engine
async def pre_submit_hook(tool_name, action, args):
    form = args.get("form")
    if form:
        errors = my_ruleset.apply_all(form)
        if errors:
            raise PermissionError(f"Pre-submit gate failed: {errors}")

# Post-submit hook: emit telemetry after every task completes
async def post_submit_hook(tool_name, action, args, result):
    task_id = result.get("task_id")
    log.info("task=%s completed", task_id)
```

---

## Concrete Code Examples

### Minimal end-to-end

```python
from lionagi.work import (
    FieldSpec, WorkForm, WorkEngine, WorkerDefinition,
    fill_form, Rule, RuleSet,
)

# 1. Define the schema
spec = {
    "fields": {
        "name": FieldSpec(name="name", type="str", required=True),
        "age":  FieldSpec(name="age",  type="int",  required=True),
    }
}
template = WorkForm(form_id="user_form", title="User Registration", **spec)

# 2. Define validation rules
rs = RuleSet()
rs.add(Rule(rule_id="age_range", field="age", check="range", params={"min": 0, "max": 150}))

# 3. Define and register a worker
def register_user(form: WorkForm) -> dict:
    errors = rs.apply_all(form)
    if errors:
        raise ValueError(errors)
    return {"registered": True, "name": form.values["name"]}

engine = WorkEngine(name="registration")
engine.register_worker(
    WorkerDefinition(
        definition_id="register",
        name="User Registration Worker",
        input_form="user_form",
        output_form="registration_result",
        handler="myapp.workers.register_user",
    ),
    handler=register_user,  # pass directly to skip dotted-path import
)

# 4. Fill, validate, submit
form = fill_form(template, {"name": "Alice", "age": 30})
assert form.status == "validated"

task_id = engine.submit(form, worker_id="register")
result  = engine.get_result(task_id)
assert result.success
# result.value == {"registered": True, "name": "Alice"}
```

### Loading a worker definition from YAML

```yaml
# workers/register.yaml
definition_id: register
name: User Registration Worker
description: Validates and persists new user records.
input_form: user_form
output_form: registration_result
handler: myapp.workers.register_user
max_concurrent: 4
timeout_seconds: 30
tags: [auth, users]
```

```python
from lionagi.work import load_definition, WorkEngine

defn = load_definition("workers/register.yaml")
engine = WorkEngine()
engine.register_worker(defn)  # resolves handler from dotted path
```

---

## Consequences

**Positive**

- No runtime dependency on LLM providers, databases, or message brokers.
  `lionagi.work` is importable in any Python 3.10+ environment with Pydantic.

- Form lifecycle is explicit and inspectable.  Callers always know the current
  status of a form (`draft`, `validated`, `error`, …) without out-of-band
  communication.

- Thread-safe by default.  The lock-per-engine design allows concurrent
  submission from multiple threads without external synchronisation.

- Composable with hooks (ADR-0023).  Pre-submit and post-submit hooks attach
  governance logic without coupling it to the engine.

- Validation errors are collected in full, not short-circuited.  The operator
  sees all problems at once.

**Negative**

- Synchronous execution model.  Long-running handlers block the submitting
  thread.  For truly async pipelines, callers should use `submit_async` or
  wrap handlers in thread pools.

- No built-in retry or back-off.  Callers implement their own retry loop.
  This keeps the engine simple but shifts responsibility to callers.

- Timeout for synchronous handlers is Unix-only (`SIGALRM`).  On Windows,
  the `timeout_seconds` field is stored but not enforced for sync handlers.

- Result storage is in-memory only.  Completed tasks are lost on process
  restart.  Persistence is the caller's responsibility (serialize
  `WorkTask.model_dump()` to disk or a database).

---

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Celery / RQ / other task queue | Introduces broker dependency, complex deployment. Unnecessary for in-process work. |
| Raw Pydantic models per worker | Solves schema but leaves status, dispatch, concurrency unaddressed. |
| Reuse `lionagi.protocols.generic.Element` as base | Element carries UUID + timestamp + metadata; useful for persistence, but adds mandatory UUID generation overhead for lightweight forms. WorkForm uses plain Pydantic BaseModel to stay standalone. |
| asyncio.Queue as the engine | Coupling to asyncio would exclude sync use cases and complicate CLI integration. |
| JSON Schema instead of FieldSpec | More portable but loses Python type coercion and Pydantic integration. |

## Non-Goals

- **No message broker.**  The engine runs in-process.
- **No persistence.**  Task history is in-memory; callers serialise to disk.
- **No tenant isolation.**  Single-process, single-user.
- **No workflow graph.**  DAG execution belongs to `Session.flow()`.
- **No automatic retry.**  Retry is a caller concern.

## References

- [ADR-0023](ADR-0023-unified-hook-system.md) — hook system (pre/post callables).
- [ADR-0029](ADR-0029-artifact-contract.md) — artifact contract (form outputs may be artifacts).
- [ADR-0031](ADR-0031-entity-header-pattern.md) — entity header (WorkTask maps to EntityHeader shape).
- `lionagi/work/` — implementation.
- `tests/work/test_work_system.py` — 67 tests covering all public surfaces.
