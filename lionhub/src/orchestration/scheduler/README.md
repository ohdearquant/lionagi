# lionhub.orchestration.scheduler

The daemon's in-process scheduler: an asyncio tick loop that fires schedules, a worker that claims queued runs under capability matching and admission rules, subprocess spawning of scheduled runs, GitHub polling for event-triggered schedules, metric threshold alerts and typed outcome signals. It is not the HTTP application (that is the Studio backend) and not schedule declaration or validation, which live in `lionhub.orchestration.schedule`.

Import path: `lionhub.orchestration.scheduler` (`lionhub/src/lionhub/orchestration/`, folder `scheduler/`).

| Slot | Holds | Source |
|---|---|---|
| `__init__.py` | package marker; the current package init is empty | `lionagi/studio/scheduler/__init__.py` |

Modules that arrive with the move (the draft reserves no slot for them yet):

| Module | Holds | Source |
|---|---|---|
| `engine.py` | the tick loop and the `scheduler` singleton | `lionagi/studio/scheduler/engine.py` |
| `subprocess.py` | spawning scheduled runs as subprocesses | `lionagi/studio/scheduler/subprocess.py` |
| `worker.py` | the local worker and claim loop, with leases and capability matching | `lionagi/studio/scheduler/worker.py` |
| `admit.py` | the `admit()` admission predicate | `lionagi/studio/scheduler/admit.py` |
| `github.py` | GitHub polling for event-triggered schedules | `lionagi/studio/scheduler/github.py` |
| `signals.py` | typed schedule-run outcome signals and a handler registry | `lionagi/studio/scheduler/signals.py` |
| `threshold.py` | metric threshold schema and comparison helpers, with no database import | `lionagi/studio/scheduler/threshold.py` |
| `coordination.py` | a measure-only files-read overlap indicator for scheduled invocations | `lionagi/studio/scheduler/coordination.py` |
| `capabilities.py` | capability-class matching: eligibility and serialization | `lionagi/studio/scheduler/capabilities.py` |
| `_timezone_rules.py` | timezone rules the scheduler resolves schedules with | `lionagi/studio/_timezone_rules.py` |

## Who uses it

- The daemon: `lionagi/studio/app.py` starts the scheduler when the app starts.
- `li schedule`: `lionagi/cli/machine_schedule.py` imports `resolve_schedule_timezone` and `scheduler` from the engine.
- `li monitor`: `lionagi/cli/monitor.py` imports the subprocess helpers.
- `lionagi/studio/scheduler/worker.py` hands notifications to the outbox in `lionhub/src/lionhub/orchestration/dispatch/outbox.py`.

## Status

Moves next, with the daemon (`lionhub.serve.daemon`). Today the code is at `lionagi/studio/scheduler/`; the old import paths keep working through 1.x with a `DeprecationWarning` once it moves.
