# ADR Glossary

Alphabetized reference for key terms used across the ADR set. Each entry gives a concise
definition and a pointer to the ADR that introduced or canonically defines the term.

---

**aborted** — A terminal lifecycle state indicating the run or session was stopped by the system
rather than by user request, typically due to an unrecoverable internal error. Distinct from
`cancelled` (user-initiated). Canonical: [ADR-0033](ADR-0033-unified-entity-state-model.md).

**Artifact** — A stored output produced during a run or skill invocation: a file, a structured
JSON blob, a log chunk, or any other addressable payload. Artifacts have MIME types, lifecycle
states, and optionally serve as evidence for state reasons. Canonical:
[ADR-0029](ADR-0029-artifact-contract.md).

**Attention Queue** — The ordered list of entities that require operator action, derived from
unresolved state reasons, stale evidence, or explicit escalation signals. Extended by knowledge
claims in ADR-0039. Canonical: [ADR-0030](ADR-0030-attention-queue.md).

**blocked** — A lifecycle state indicating the entity is waiting for an external dependency before
it can continue. The blocking reason must be recorded as a `StateReason`. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**Branch** — A single conversation thread managed by `MessageManager`, `ActionManager`,
`iModelManager`, and `DataLogger`. Branches are the primary unit of LLM interaction and are
owned by a Session. Canonical: [ADR-0017](ADR-0017-session-lifecycle-status.md) / CLAUDE.md.

**cancelled** — A terminal lifecycle state indicating a run or session was stopped by deliberate
user or operator action before completing normally. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**Claim** — The fundamental unit of the knowledge substrate: a statement with an associated
`claim_status`, a `confidence` score, a `scope_type`, and one or more evidence references.
Claims are the currency of [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md). Canonical:
[ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md).

**claim_status** — The lifecycle state of a claim: `proposed`, `supported`, `contested`,
`withdrawn`, or `superseded`. Tracks how confidence has evolved based on evidence. Canonical:
[ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md).

**Claim Card** — The frontend component that renders a single claim with its evidence chain,
confidence meter, and action affordances (support, contest, withdraw). Canonical:
[ADR-0035](ADR-0035-design-system-and-component-library.md).

**completed** — A terminal lifecycle state indicating a run, session, or step finished successfully
with all expected outputs produced. Canonical: [ADR-0033](ADR-0033-unified-entity-state-model.md).

**compat layer** — The thin adapter in the frontend that translates legacy `status` string fields
from older API responses into the `NormalizedState` shape expected by current components, enabling
incremental backend migration. Canonical:
[ADR-0034](ADR-0034-frontend-data-and-state-architecture.md).

**confidence** — A numeric score in [0, 1] attached to a Claim, reflecting the system's or
operator's current belief in the claim given available evidence. Not a probability in the strict
sense; it is a convenience signal for prioritisation. Canonical:
[ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md).

**critical** — The highest severity value in the severity enum. A `critical` state reason requires
immediate operator attention and typically blocks downstream progress. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**danger** — The frontend tone value corresponding to an error or critical condition. Maps to red
styling in the design system. Distinct from the backend `severity` axis. Canonical:
[ADR-0035](ADR-0035-design-system-and-component-library.md).

**delivery** — One of the three state axes in the Unified Entity State Model, alongside `lifecycle`
and `health`. Delivery tracks whether the entity's output has reached its intended consumer
(e.g., `pending`, `delivered`, `acknowledged`). Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**Domain Component** — A React component in the design system that composes primitives to
represent a specific product domain object (Run card, Session header, Claim card). Domain
components own the mapping from `NormalizedState` to visual representation. Canonical:
[ADR-0035](ADR-0035-design-system-and-component-library.md).

**due** — A health state indicating a scheduled entity has reached its trigger time and is
awaiting dispatch. Distinct from `misfired` (trigger time passed without dispatch). Canonical:
[ADR-0027](ADR-0027-scheduled-runs.md) / [ADR-0033](ADR-0033-unified-entity-state-model.md).

**EvidenceRef** — A typed pointer to an artifact, external URL, or inline payload that substantiates
a `StateReason` or a `Claim`. Evidence references carry a `kind` field (e.g., `log`, `metric`,
`human_assertion`, `automated_check`). Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md) / [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md).

**failed** — A terminal lifecycle state indicating a run, step, or operation ended in error before
producing expected outputs. The failure reason must be recorded as a `StateReason`. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**freshness budget** — The maximum age (in seconds or polling intervals) a frontend cache entry
is considered valid before TanStack Query should re-fetch from the server. Defined per entity type
in the data architecture. Canonical: [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md).

**health** — One of the three state axes in the Unified Entity State Model. Health reflects the
operational condition of a live entity — whether it is responding normally, stalled, or dead —
independent of its lifecycle position. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**idle** — A health state indicating the entity is alive but has produced no activity within the
expected window. Does not imply failure; may precede stalling. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**info** — A severity value indicating an informational state reason that requires no immediate
action. Also a tone value in the frontend design system (maps to blue). Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md) / [ADR-0035](ADR-0035-design-system-and-component-library.md).

**Invocation** — A single execution of a skill or tool, with a bounded input/output envelope. An
invocation is a first-class entity with its own lifecycle, provenance linkage, and artifact
outputs. Canonical: [ADR-0020](ADR-0020-skill-invocations.md).

**KnowledgeStore** — The backend component that persists Claims, EvidenceRefs, and their
relationships. Implements the minimal interface defined by ADR-0039 without prescribing a specific
storage backend. Canonical: [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md).

**Knowledge Lens** — A frontend view mode that renders an entity through the lens of its
associated claims and evidence, rather than the standard operational timeline. Defined in the
design system as a composable panel. Canonical:
[ADR-0035](ADR-0035-design-system-and-component-library.md).

**lifecycle** — One of the three state axes in the Unified Entity State Model. Lifecycle tracks
the position of an entity within its state machine (e.g., `running`, `completed`, `failed`).
Canonical: [ADR-0033](ADR-0033-unified-entity-state-model.md).

**merged** — A terminal lifecycle state used for branches and worktrees: the entity's changes
have been incorporated into the target and the entity itself is no longer active. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**misfired** — A health state for scheduled entities indicating the trigger time passed but the
run was not dispatched (e.g., due to a system outage). Requires operator triage. Canonical:
[ADR-0027](ADR-0027-scheduled-runs.md).

**neutral** — A severity or tone value indicating no signal in either direction. Used for informational
display that should not draw the eye. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md) / [ADR-0035](ADR-0035-design-system-and-component-library.md).

**NormalizedState** — The canonical frontend shape that all entity-fetching hooks must return,
containing `lifecycle`, `health`, `delivery`, `severity`, `tone`, and `reasons` fields. Components
only consume `NormalizedState`; they never read raw `status` strings. Canonical:
[ADR-0034](ADR-0034-frontend-data-and-state-architecture.md).

**ok** — The baseline health state: the entity is alive and behaving within expected parameters.
Canonical: [ADR-0033](ADR-0033-unified-entity-state-model.md).

**orphaned** — A health state indicating the entity's parent context (session, team, or project)
no longer exists, leaving the entity without a valid owner. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**outcome** — One of the three state axes in some earlier ADRs; consolidated into `delivery` and
`lifecycle` in the unified model. Retained as a legacy field in some API responses via the compat
layer. Canonical: [ADR-0033](ADR-0033-unified-entity-state-model.md).

**Play** — A single execution episode within a Show: one scheduled or triggered invocation of a
playbook, with its own run lineage. Canonical: [ADR-0011](ADR-0011-shows-data-model.md).

**Primitive** — A low-level, stateless UI component in the design system (Button, Badge, Icon,
Skeleton) that encodes only structural and typographic concerns, not domain semantics. Canonical:
[ADR-0035](ADR-0035-design-system-and-component-library.md).

**process_dead** — A health state indicating the underlying process (OS process, container, or
async task) has exited unexpectedly while the entity's lifecycle state still shows `running`.
Canonical: [ADR-0033](ADR-0033-unified-entity-state-model.md).

**Project** — The context container resolved by the CLI project detection cascade: a name and
source (git root, explicit config, environment variable, or default fallback) that scopes sessions
and runs. Canonical: [ADR-0026](ADR-0026-project-detection.md).

**Run** — A discrete execution unit: one invocation of `li agent` or a flow pipeline that produces
a timestamped directory under `~/.lionagi/runs/`. Runs have their own lifecycle, step provenance,
and artifact outputs. Canonical: [ADR-0015](ADR-0015-runs-list-design.md) /
[ADR-0022](ADR-0022-run-step-provenance.md).

**running** — The active, non-terminal lifecycle state. An entity in this state is currently
executing and has not reached a terminal outcome. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**Schedule** — A trigger definition (cron expression or interval) that creates Runs automatically.
Schedules have their own health states (`due`, `misfired`) separate from the Runs they produce.
Canonical: [ADR-0027](ADR-0027-scheduled-runs.md).

**scope_type** — The domain boundary within which a Claim is asserted to hold: e.g., `global`,
`project`, `session`, or `run`. Claims with narrower scopes do not conflict with broader-scope
claims on the same subject. Canonical:
[ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md).

**Session** — The persistent container for a multi-turn agent conversation: owns one or more
Branches, a DataLogger, and a project association. Sessions survive process restarts and are
stored in SQLite. Canonical: [ADR-0017](ADR-0017-session-lifecycle-status.md).

**severity** — The urgency axis on a `StateReason`: `critical`, `warning`, `info`, or `neutral`.
Controls prioritisation in the attention queue and inbox. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**Show** — A named, persistent display surface that aggregates multiple Plays over time. Shows
are defined as playbook schedules with their own configuration and artifact history. Canonical:
[ADR-0011](ADR-0011-shows-data-model.md).

**stalled** — A health state more severe than `idle`: the entity has exceeded the expected activity
window by a significant margin and likely requires intervention. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**StateReason** — A structured annotation on a state transition: a machine-readable `code`, a
human-readable `message`, a `severity`, and an optional list of `EvidenceRef`s. StateReasons make
transitions auditable. Canonical: [ADR-0028](ADR-0028-status-reason-model.md) /
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**success** — A tone value in the frontend design system indicating a positive outcome. Maps to
green styling. Not a lifecycle state; use `completed` for lifecycle. Canonical:
[ADR-0035](ADR-0035-design-system-and-component-library.md).

**sync contract** — The agreement between the backend SSE stream and the TanStack Query cache: what
events invalidate which cache keys, and at what granularity the frontend re-fetches. Defined in
the frontend data architecture. Canonical:
[ADR-0034](ADR-0034-frontend-data-and-state-architecture.md).

**Team** — A named group of concurrent agent workers that share an inbox and coordinate via
`fcntl.flock`-protected JSON state. Teams are the unit of parallel fan-out for `li team`.
Canonical: [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md).

**timed_out** — A terminal lifecycle state indicating the entity exceeded its allowed execution
window and was forcibly terminated by the scheduler or watchdog. Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md).

**tone** — The visual signal axis: `danger`, `warning`, `info`, `success`, or `neutral`. Tone is
a frontend concern derived from `severity` and `lifecycle`; it is the input to the design system's
color and icon tokens. Canonical: [ADR-0035](ADR-0035-design-system-and-component-library.md).

**URL-as-state** — The convention that navigation state (selected entity, active filter, open
panels) is encoded in the URL rather than component-local React state, enabling deep-linking and
back-button correctness. Canonical: [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md).

**warning** — A severity value indicating a condition that may require attention but does not yet
block progress. Also a tone value in the frontend (maps to amber). Canonical:
[ADR-0033](ADR-0033-unified-entity-state-model.md) / [ADR-0035](ADR-0035-design-system-and-component-library.md).
