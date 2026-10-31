# lionhub.canon

The governance layer for charter-defined workflows. It holds the charter and policy records, the declarative language and compiler for workflow text, the runtime that advances a compiled charter through its phases, evidence and enforcement. The package is written from the governance concepts as new code. It is not the core's security-control verdict contract or per-tool permission policy (`lionagi/agent/gate.py`, `lionagi/agent/permissions.py`), and it is not the Studio application's operator or demo redaction helpers (`lionagi/studio/operator/redact.py`, `lionagi/studio/services/redaction.py`). It runs no commands and serves no routes.

Final import path: `lionhub.canon`. The folder name in this draft is spelled the same; it sits at `canon/` here and moves under the `lionhub` package.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | The package surface. Reserved; it exports nothing until the first port has code. | new; no existing code. It is the package marker the draft reserves. |
| `charter.py` | Tenant-scoped charter records: the versioned workflow source and compiled form, its lifecycle and validation state, and the runs and per-phase executions that record a charter in progress. | the governance concepts: charter records and charter runs |
| `policy.py` | Policy records: the authored definition and its authority, the engineering adapter, the versioned release, and the decision and scope values used when a policy is evaluated. | the governance concepts: policy definitions and releases |
| `evidence.py` | Immutable evidence records and their hash-linked audit chain, plus automatic recording for phase, grant and workflow events. | the governance concepts: evidence and audit chains |
| `governance.py` | The governance decision layer: resolves the policy that applies to a request and turns its evaluation into a verdict with an enforcement level. | the governance concepts: policy resolution and enforcement levels; the core's gate contract stays at `lionagi/agent/gate.py`. |
| `enforcement.py` | Compliance enforcement: reusable constraints and their exceptions, the service base class and action metadata, the action executor, workflow vocabulary, and the policy engine protocols. | the governance concepts: enforcement constraints, exceptions, service and vocabulary |
| `authorization.py` | The authorization boundary for a governed action: authentication-token records and validity states, and the allow/deny/escalate decision contract used to gate a tool action. | the governance concepts: authentication tokens and authorization checks; the core's per-tool allow/deny/escalate policy stays at `lionagi/agent/permissions.py`. |
| `dsl/` | The charter language and its compiler; see its README for the language stages. | the governance concepts: the charter DSL |
| `runtime/` | Phase lifecycle, grants, triggers and the completion cascade; see its README. | the governance concepts: the charter runtime |
| `services/` | Infrastructure called by governance modules, currently redaction; see its README. | the governance concepts: the redaction service |

## Who uses it

No module under `lionagi/` or `lionhub/` imports `lionhub.canon` today. The core's pre-invoke authorization seam is `lionagi/session/branch.py`; no wiring to this package exists. Once ported, `lionhub.canon.dsl` compiles charter text for `lionhub.canon.runtime`; the runtime records events through `lionhub.canon.evidence` and updates the records in `lionhub.canon.charter`; and `lionhub.canon.governance` and `lionhub.canon.enforcement` supply policy decisions for governed actions. No CLI command, MCP verb or Studio route calls the package yet.

## Status

New code: concept ports, one change per port with its own tests; it adds no old-to-new import row and the core never imports it.
