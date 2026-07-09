# ADR-0001: Element Identity and Polymorphic Serialization Envelope

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: none

## Context

LionAGI needs persisted and externally addressable model objects to retain identity across
collections, graph edges, messages, logs, and serialization boundaries. `Element` supplies that
shared envelope through a UUID, a creation timestamp, mutable metadata, and Pydantic validation.
The UUID and creation timestamp are immutable after construction, while equality and hashing use
the UUID. `lionagi/protocols/generic/element.py` is the defining module.

Serialization adds a fully qualified `lion_class` discriminator to metadata. Deserialization reads
the discriminator from either `metadata` or the database-shaped `node_metadata`, resolves the
concrete class, and delegates reconstruction to that class. Python, JSON, and database-shaped
dictionaries are supported representations of the same envelope.

Class resolution is compatibility-oriented rather than a uniform extension registry.
`lionagi/_class_registry.py` first checks the process registry, then permits an importable dotted
path, then searches a fixed set of built-in modules and registered suffixes for older short class
names. `Node` subclasses register automatically in `lionagi/protocols/graph/node.py`; other
`Element` subclasses do not share that automatic registration contract.

The boundary is addressable or persisted framework models, not all data. Nodes, edges, events,
graphs, collections, progressions, and logs use the envelope because they are referenced or
round-tripped as model objects. Configuration and query value objects may remain plain Pydantic
models. `Node` adds serializable content and a nullable embedding field; the latter is currently a
persisted compatibility field, not evidence that every node participates in vector retrieval.

## Decision

`Element` is the common identity and polymorphic-serialization envelope for addressable or
persisted LionAGI models. Its load-bearing invariants are:

- `id` is a UUID, is immutable after construction, and defines `Element` equality and hashing;
- `created_at` is an immutable UTC timestamp, while `metadata` remains mutable extension data;
- serialized forms carry the concrete, fully qualified class name in `metadata.lion_class`;
- database serialization may rename `metadata` to `node_metadata`, and deserialization accepts
  either name;
- deserialization delegates to the resolved concrete type and continues to read built-in short
  class names written by older data; and
- inheriting from `Element` is not required for configuration, query, or other non-addressable
  value objects.

The implementation anchors are `lionagi/protocols/generic/element.py`,
`lionagi/_class_registry.py`, and `lionagi/protocols/graph/node.py`.

## Consequences

UUID references, mixed-type collections, graph relationships, and persisted model round trips use
one interoperable envelope. Concrete model types can preserve their own reconstruction behavior,
and existing built-in data containing short class names remains readable.

The resolver has an asymmetric extension and trust boundary: automatic registration is tied to
`Node`, while an unregistered qualified discriminator can trigger a module import. The inherited
timestamp and metadata also appear on descendants that do not independently need them. Retaining
`Node.embedding` preserves stored shape but leaves an optional concern on a broad base class.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Add an explicit polymorphic-model registration and trusted-deserialization policy; acceptance requires qualified third-party model names to round-trip without `Node` inheritance, built-in short names to remain readable, and dotted-path imports to follow the documented trust policy. | M | (filled at issue-open time) |
| 2 | Separate `Node`'s stable content contract from optional vector capability; acceptance requires an inventory and migration plan for persisted embeddings before any base-field deprecation, plus an opt-in capability for new vector operations. | M | (filled at issue-open time) |

## Notes

Alternatives considered were using no shared model envelope and serializing only short class names.
The former would duplicate identity and persistence rules across model families; the latter cannot
uniquely identify extension types and would preserve the ambiguity that qualified names avoid.
