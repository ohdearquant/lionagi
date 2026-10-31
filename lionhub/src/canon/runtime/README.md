# lionhub.canon.runtime

The runtime that carries a compiled charter through its phases. It owns the phase lifecycle (the states a phase can be in and the transitions allowed between them), the access grants tied to a phase, the external triggers that release a phase waiting on an event, and the cascade that decides which phases start once one completes. It does not parse or compile charter text (`lionhub.canon.dsl`), define the charter, policy or evidence records (`lionhub.canon.charter`, `lionhub.canon.policy`, `lionhub.canon.evidence`) or check constraints (`lionhub.canon.enforcement`).

Final import path: `lionhub.canon.runtime`. The folder name in this draft is spelled the same; it sits at `canon/runtime/` here and moves under the `lionhub` package.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | The package surface. The concept's runtime package exports the phase-state checks, condition evaluators, grant operations, evidence recorders, inbox queries and workflow operations; the port will choose the subset its modules expose. | the governance concepts: the charter runtime package surface |
| `phase.py` | The lifecycle states of a phase (pending, waiting on a gate, waiting for a user, waiting for a trigger, in progress, completed, failed, skipped), the table of allowed transitions, the terminal states, and a typed error for an invalid transition. | the governance concepts: the phase state machine |
| `grant.py` | Just-in-time access grants tied to a phase: created when the phase activates, revoked when it completes, either scoped to the phase or given an explicit expiry. | the governance concepts: grants |
| `trigger.py` | Firing an external event so that a phase held on an `await` directive moves forward, and the check that a phase requiring an event has seen it. | the governance concepts: triggers |
| `cascade.py` | What happens when a phase completes: mark it done, record the evidence, find the phases that depend on it, evaluate their requirements, start those now satisfied, and report when the whole workflow is complete. | the governance concepts: the completion cascade |

Not in this draft: requirement evaluation (which the cascade calls), conditions, the workflow registry, inbox and role handling, and run visualization have no slot here. In the governance concepts these functions take a database connection; how the port persists runs, grants and evidence is settled when it is written.

## Who uses it

Nothing calls it today: no module under `lionagi/` or `lionhub/` on the development line imports `lionhub.canon`. In the governance concepts, the events these modules emit are recorded through `lionhub.canon.evidence`, and the run and phase records they update are the ones `lionhub.canon.charter` holds.

## Status

New code, written from the governance concepts, one change per port with its own tests. It is not a move, so it adds no row to the old-to-new import table, and the core never imports it.
