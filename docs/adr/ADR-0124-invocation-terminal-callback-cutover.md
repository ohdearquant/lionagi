# ADR-0124: Invocation terminal callback cutover

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: orchestration
- **Date**: 2026-08-16
- **Relations**: depends on ADR-0123 (canonical Run identity and execution projections) for the
  Invocation/Run vocabulary and on ADR-0058's `TerminalProjectionParticipant` seam; prospectively
  amends ADR-0095 D1/D2 (terminal callback source and v2 envelope cutover); split out of
  ADR-0123 D13 so that the Run identity model can be accepted independently of this protocol

## Context

ADR-0123 decides what a Run *is*. This record decides who is allowed to tell the outside world
that one finished, during the window in which both the old answer and the new answer exist.

The two questions have different shapes. Run identity is a modelling decision: it is either
coherent or it is not, and a reviewer can evaluate it by reading it. A callback cutover is a
distributed protocol running against live consumers, and its failure modes are not incoherence.
They are:

- **silent double delivery** — one logical completion emitted twice because two sources each
  believed they were the selected one, so a consumer bills twice, retries twice, or fans out
  twice;
- **silent non-delivery** — one logical completion emitted by neither source because each
  believed the other was selected, so a consumer waits forever on something that already
  finished.

Neither raises anywhere in this system. Both are observable only from the consumer's side, often
much later, and both survive a test suite that asserts every transition wrote its own rows
correctly. That is why this is a separate record rather than a clause: bundled, it makes
acceptance of the Run identity model hostage to the hardest protocol in the program, and it
invites a reviewer to approve the protocol at the confidence level they formed while reading the
model.

The concrete situation is that ADR-0095's current v1 callback is a projection of legacy lifecycle
entities, while canonical Run introduces a v2 envelope whose `entity.kind` is `run`. During
migration a single completion can be described by an Invocation terminal transition, by a Run
terminal transition, or by both. There is no generic v2-to-v1 downgrade available to paper over
the difference: v1 cannot represent `entity.kind="run"`, and a Run need not have exactly one
legacy entity.

## Decision

### D1 — Callback mode is frozen per Invocation, in the first Run creation transaction

The **first Run creation transaction** atomically freezes the Invocation's callback mode, creates
its `InvocationTerminalCallbackBinding`, creates the Run, mints its fact ID, and creates its
`RunTerminalCallbackBinding`. The two modes are:

- `legacy_invocation_v1_single_run` permits at most one Run. When that Run is created, the
  Invocation binding atomically records its Run/fact ID; the Invocation terminal transition emits
  the selected v1 fact, while the Run and other correlated legacy transitions suppress public
  callback emission.
- `canonical_run_v2` permits one or many Runs. The Invocation terminal transition is an aggregate
  control fact and is suppressed; every winning Run terminal transition emits its own v2 fact.
  This mode requires all required consumers to advertise v2 support before the first Run.

Freezing at first-Run creation rather than at ingress is what makes the selection observable from
inside the transaction that creates the thing being described. A mode chosen earlier would have to
be re-validated later against a Run that did not exist when it was chosen.

If any of those first-Run writes fails, all four logical records and updates roll back, leaving
the Invocation with no mode and no binding, and therefore standalone-v1 semantics. An Invocation
that never reaches `ALLOW`, or whose first-Run transaction rolls back, never freezes a mode or
creates a binding; its terminal projection remains standalone legacy-v1 compatibility. Mode
selection is part of admitted Run creation and is not part of ingress denial or deferral
semantics.

### D2 — Every subsequent Run binds before any work that can fail

Every subsequent canonical Run mints one opaque `terminal_fact_id` and atomically creates its
`RunTerminalCallbackBinding` with the Run, before any compile or provision work that can fail. Its
source is derived from, and must agree with, the immutable Invocation mode.

A second Run under a legacy-single-run Invocation is rejected before creation with
`LegacyCallbackProfileMultiplicityError`. It cannot reuse an already-terminal Invocation event. An
Invocation configured for automatic retry or concurrent Run expansion must therefore choose
canonical-v2 readiness before its first Run. If readiness is unavailable, automatic retry is
reported unavailable; an operator may submit a new Invocation rather than silently changing
identity or callback semantics.

Additional per-Run legacy refs may be attached by guarded binding-version CAS, but each ref must
commit before that entity enters a terminal-capable dual-write path. It cannot be discovered after
terminal observation. The Run binding freezes against further attachment when finalization starts.
A unique active-binding constraint applies to those per-Run legacy refs, not to the shared
Invocation binding. Read-only or historical RunSession links remain many-to-many but carry no
callback authority. Runs created before these bindings exist remain standalone legacy behavior and
do not enter canonical dual-write. Historical imports emit neither source.

### D3 — Absence of an active binding is never proof of anything

Closing a binding retains its immutable ledger marker. Absence of an active row is never treated
as proof that the entity was never canonical-bound, because the two states that produce an empty
read here are "never bound" and "bound then closed", and only one of them permits standalone
emission.

A later standalone reclassification therefore requires an audited `StandaloneLegacyRelease` tied
to the entity generation and to every prior binding. Without that release, a late or recovery
transition stages attention and emits nothing. Binding and decision retention is coupled to the
source transition and terminal-delivery retention gates in ADR-0095.

### D4 — The selection is validated inside the owner transaction, never inferred

Every bound terminal command carries the exact Run- or Invocation-binding ID **and expected
binding version** into ADR-0058's `TerminalProjectionParticipant`. Inside the owner transaction,
that participant validates the binding kind and version, entity membership, Invocation mode,
optional canonical Run ID, fact ID, selected source, and transition role, then stages an immutable
emit or suppress decision beside `status_transitions`. LifecycleService always stages lifecycle
audit; after commit it offers the registry only a decision selecting the current transition.

It never infers binding from the lossy `sessions.run_id`, from RunSession search order, from
timestamps, or from route aliases. Missing, stale, multiply-active, or ambiguous binding in a
canonical cohort stages typed attention and emits nothing. It cannot fall back to standalone v1,
because a fallback is exactly how one logical completion acquires a second public source.

This creation order also covers failures before Session creation: legacy-single-run mode emits
through its selected Invocation, while canonical-v2 mode emits through its Run transition and
suppresses the one aggregate Invocation transition regardless of Run cardinality. Compile failure,
provisioning failure, and cancellation while `preparing` are acceptance fixtures for both modes.

### D5 — The v2 envelope and its identity are fixed

The v2 envelope uses the canonical Run `status_transitions.id` as `event_id`, carries the Run's
separately minted durable `terminal_fact_id`, `schema="lionagi.run-terminal"`, `schema_version=2`,
`entity={kind:"run", id:run_id}`, and retains typed nullable legacy correlation. Reconciliation
and delivery acknowledgement use `(terminal_fact_id, consumer)`, independent of wire serialization
or retry, so a source retry redelivers the same fact instead of creating a second logical
callback.

There is no generic v2-to-v1 downgrade. A rollout may select `canonical_run_v2` only after every
required consumer for that Invocation cohort advertises v2 support; otherwise it may use
`legacy_invocation_v1_single_run`, and only when admission guarantees one Run. Optional consumers
that are not v2-ready receive no fabricated legacy event, because a fabricated event is
indistinguishable from a real one at the consumer and permanently corrupts the record it lands in.

### D6 — The default flips only after the race matrix passes

The cutover gate injects terminal races for every legacy entity correlation and proves: one
winning Run CAS; audit rows on both sides where dual-write requires them; one and only one
selected public source; stable terminal-fact identity across retries; required-consumer v2
readiness; and no callback when an imported record is merely materialized. It includes one legacy
entity linked to multiple historical or read-only Runs and proves that only the exact active
callback binding participates. It also covers a one-Run legacy Invocation, an attempted second
legacy Run, sequential automatic retry, and concurrent multi-Run expansion, the latter two
succeeding only in canonical-v2 mode.

Two properties in that list are stated as counts rather than as "the right thing happened",
deliberately. "One and only one selected public source" fails on both zero and two, and a gate
phrased as "the callback was delivered" passes the double-delivery case. Only after the matrix
passes may the default for new Runs become `canonical_run_v2`. Existing Runs retain their frozen
source.

## Consequences

### Positive

- one logical completion has exactly one public source at every point in the migration, and the
  selection is a committed value rather than a derivation;
- the Run identity model in ADR-0123 can be accepted, and its implementation issues cut, without
  waiting for this protocol to be settled;
- the two failure modes that are invisible at the source are made visible as gate conditions
  phrased as counts;
- a partially migrated fleet is a supported state rather than a window to be minimized.

### Negative

- an Invocation cannot change callback mode after its first Run, so a caller that wants multi-Run
  behavior later must submit a new Invocation;
- required-consumer v2 readiness becomes a precondition on Run creation, which couples rollout
  pace to consumers this system does not own;
- the binding records and their ledger markers are retained beyond the life of the entities they
  describe.

### Risks and mitigations

- **A consumer advertises v2 readiness it does not have.** The gate proves readiness at the
  cohort, not at first delivery. Mitigation: readiness is re-read at Run creation rather than
  cached from cohort configuration, and a readiness read that fails is treated as not ready.
- **The migration is abandoned midway.** Frozen modes mean the fleet is left in a mixed state
  indefinitely. That is by design and is why existing Runs retain their frozen source rather than
  being rewritten; the mixed state is correct, not a defect to be cleaned up under time pressure.

## Alternatives considered

### Keep this as ADR-0123 D13

Rejected. Run identity is a modelling decision a reviewer evaluates by reading; this is a
distributed protocol whose failure modes are silent at the source. Bundled, the protocol inherits
the confidence formed while reading the model, and the model cannot be accepted until the protocol
is settled. Neither of those is a property the packet wants.

### Emit from both sources during migration and deduplicate downstream

Rejected. It moves the correctness requirement into every consumer, including consumers this
system does not own, and it converts a source-side invariant that can be tested into a
distributed one that cannot.

### Downgrade v2 facts to v1 for consumers that are not ready

Rejected. v1 cannot represent `entity.kind="run"`, and a Run need not have exactly one legacy
entity, so a downgrade fabricates a legacy identity. A fabricated event is indistinguishable from
a real one at the consumer.

### Choose the mode at ingress rather than at first Run creation

Rejected. The mode describes a Run that does not exist at ingress, so it would have to be
re-validated later against the Run that eventually appeared, which reintroduces exactly the
inference this record forbids in D4.

## Notes

This record was split out of ADR-0123 D13 during review of the architectural consolidation
packet, on the grounds that the two decisions have different risk profiles and should be able to
fail independently. The technical content is the D13 content; the framing, the count-phrased gate
conditions, and the alternatives are added here.
