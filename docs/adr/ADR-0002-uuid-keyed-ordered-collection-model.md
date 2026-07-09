# ADR-0002: UUID-Keyed Ordered Collection Model

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: extends ADR-0001

## Context

LionAGI collections need both stable identity lookup and deterministic traversal order. `Pile`
implements those concerns with a UUID-to-item dictionary and one `Progression` containing the UUIDs
in traversal order. Construction rejects duplicate order entries and requires the order and
dictionary to contain exactly the same members. `lionagi/protocols/generic/pile.py` and
`lionagi/protocols/generic/progression.py` define these structures.

UUID and element references address dictionary entries, while integer and slice access resolve
through the progression. Mutators maintain both structures so that an item is not stored without a
position and an ordered UUID does not lack a stored item. A Pile may restrict items to declared
nominal `Observable` types, optionally excluding subclasses. This is narrower than the structural
ID-only `Observable` protocol exported by `lionagi/protocols/contracts.py`.

`Progression` is also usable independently. Its membership cache is set-based, but `append`,
`extend`, and `insert` can preserve duplicate UUIDs in the sequence. Pile construction and its
default ordering require uniqueness, so callers cannot infer one universal multiplicity rule from
the shared type.

Multiple named orderings belong to `Flow`, not to Pile. `lionagi/protocols/generic/flow.py` keeps one
item Pile and a Pile of named Progressions, validates that every progression UUID exists in the item
Pile, and allows the same item to appear in multiple views. Session observation and exchange
mailboxes use this split between storage and named ordering.

Pile also exposes a synchronous reentrant lock and a separate asynchronous lock. Decorated
synchronous and asynchronous operations use their respective locks, but several public reads and
mutations are unlocked, and the two lock domains do not serialize against each other. The current
model supports disciplined, cooperative use; it is not a general linearizability or cross-context
snapshot guarantee.

## Decision

Pile is one UUID-keyed store with one default ordered view, while Flow owns multiple named ordered
views over one Pile. The load-bearing invariants are:

- a Pile's dictionary keys and default Progression contain the same unique UUIDs;
- reference access is identity-based, while integer and slice access are order-based;
- Pile item admission follows the nominal `Observable` and optional `item_type` checks implemented
  by Pile, not the structural ID-only protocol;
- every Flow progression references only items resident in that Flow's item Pile;
- standalone and Flow-owned Progressions may currently contain repeated UUIDs, even though a
  Pile's default Progression may not; and
- synchronous and asynchronous locking are separate facilities and do not promise safe arbitrary
  mixing of threads, async callers, unlocked mutators, and live reads.

The implementation anchors are `lionagi/protocols/generic/pile.py`,
`lionagi/protocols/generic/progression.py`, `lionagi/protocols/generic/flow.py`, and
`lionagi/protocols/contracts.py`.

## Consequences

Identity lookup remains independent from presentation or processing order, and a single stored item
can participate in several named sequences without duplication. Pile can round-trip heterogeneous
`Element` subclasses while preserving one deterministic default order.

The public surface carries real ambiguity. Structural observability does not imply Pile admission,
valid UUID strings are declared references but are normalized incorrectly on some Pile paths,
Progression multiplicity depends on its owner, and callers today must know which lock and mutation
discipline protects a compound operation. These limitations constrain safe extension and concurrent
use until their contracts are made explicit.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Fix Pile UUID-string reference normalization; acceptance requires valid UUID strings to work as one-item references for get, set, pop, and construction paths with regression coverage. | S | (filled at issue-open time) |
| 2 | Define one public Pile-item contract and align the exported Observable protocol with it; acceptance requires structural or nominal admission to be stated once and enforced by item-type validation and extension tests. | M | (filled at issue-open time) |
| 3 | Decide and enforce Progression multiplicity semantics for standalone Progressions, Pile default order, and Flow named views; acceptance requires append, extend, insert, serialization, and membership behavior to match the documented rule. | M | (filled at issue-open time) |
| 4 | Publish and enforce Pile synchronization and snapshot semantics; acceptance requires every public mutator and read API to be audited against a stated sync, async, and mixed-context contract, with stable snapshot APIs where live traversal is unsafe. | M | (filled at issue-open time) |

## Notes

Alternatives considered were relying only on dictionary insertion order and storing several orders
inside every Pile. The first couples identity storage to every reordering operation; the second adds
unused state to the common collection and does not match the existing Flow abstraction.
