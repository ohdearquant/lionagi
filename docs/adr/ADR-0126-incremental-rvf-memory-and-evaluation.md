# ADR-0126: Incremental RVF memory, retention and evaluated retrieval policies

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: partial (implementation and native acceptance tests in this proposal; SDK release and upstream adoption pending)
- **Area**: substrates
- **Date**: 2026-10-11
- **Relations**: extends ADR-0125; relates to ADR-0092

## Context

Complete checkpoints rewrite the corpus for every feedback event. Durable memory
also needs explicit expiration and a way to reject retrieval policies that reduce
held-out relevance. These are storage and retrieval decisions; they do not train
language models or connect LoRA adapters to generation.

## Decision

### D1: Immutable native RVF transactions and atomic visibility

`JournaledRuVectorStore(directory, dimensions, *, embedding_id,
max_segments=64, max_journal_bytes=1073741824, **store_options)` extends the
durable store. One exclusive stable lease owns the directory. Every mutation
writes a native RVF segment, closes and syncs it, then atomically replaces the
synced `CURRENT` control file. Readers recover only its referenced generation.

CURRENT is bounded to 64 KiB. Its version 1 schema contains integer generation
and ordered segment entries with generated basename and SHA-256. Document
payloads, vectors and explicit feedback remain in native RVF segments.
Transactions record configuration, generation, operation and bounded records.
Recovery validates names, sizes, checksums, generation continuity, embedding
identity and schema before rebuilding HNSW once. Failed publication poisons the
open instance; reopening recovers a complete visible generation.

Segment limits are 2 through 256. The active byte budget defaults to 1 GiB and
accepts 1 MiB through 2 GiB. Recovery checks aggregate referenced bytes before
native parsing. Segment count or byte pressure compacts current state into one
base RVF. An oversized base fails without publishing a partial generation.
Compaction removes unreferenced managed segments only after publishing the base.

The active budget does not cap total disk usage: old and new generations coexist
during compaction, and interrupted writes can leave orphan segments until cleanup.
The host must provision temporary space. Checksums detect corruption, not
authenticate an attacker. Use a private directory, trusted application artifacts
and one writer; no distributed ownership or hostile-file sandbox is provided.

### D2: Explicit TTL and physical reclamation

`LifecycleRuVectorStore` adds an injected epoch clock and
`add_nodes(nodes, ttl_seconds=...)`. Reserved lifecycle metadata is validated,
hidden from public records and rejected on direct caller injection. Expired
records are excluded from get, scan, search and feedback. Expiration alone does
not free physical capacity; `purge_expired()` commits a bounded bulk deletion.
`compact(purge_expired=True)` then removes obsolete managed journal segments.

A cached next expiry preserves ordinary HNSW retrieval while all records are
eligible. Expired physical records require exact filtered search until purge;
this is an explicit O(N) tradeoff. A backward clock in one process fails closed.
Across restart the host must supply a trustworthy epoch clock. Reclamation is
not secure erasure of backups, filesystem snapshots or storage remnants.

### D3: Evaluated policy promotion with runtime rollback

`GovernedFeedbackRanker.promote(candidate, cases, gate=None, k=5)` compares
baseline and candidate against caller supplied trusted labels. Defaults require
20 cases, at least 10% relative MRR improvement, no individual case regressions
and candidate p95 no more than 1.5 times baseline. Cases are bounded at 1000.
Paired search order alternates after warmup. The store lock blocks concurrent
mutation and a write guard rejects callback writes during evaluation. Lifecycle
eligibility uses one captured epoch for both paths.

A frozen report records MRR, p95, regressions, effective gate and ranker settings,
embedding identity and a query/vector/label digest. The digest excludes predicate
code and corpus state; it is not a complete provenance fingerprint. Failure keeps
the active approved configuration. Acceptance freezes the candidate settings;
`rollback()` restores the preceding runtime policy, with bounded history of 16.

The comparison is against native retrieval, not the preceding approved policy.
Policies and history are process local. Feedback and corpus are durable, but a
restart uses baseline until explicit reevaluation. Later corpus or feedback
changes can invalidate previous evidence. Evaluation is an application quality
gate, not tenant authorization, continuous monitoring or model training.

## Alternatives

- Full snapshots remain simpler and available through PersistentRuVectorStore,
  but write latency scales with corpus size for each feedback event.
- SQLite would add a second authoritative persistence format and split recovery
  from RVF vectors. This proposal keeps canonical records in RVF.
- Unbounded journals avoid compaction pauses but leave unbounded disk and replay.
- Persisting policy approval without binding corpus and labels risks treating
  obsolete evidence as approval. Durable policy governance is deferred.
- Background compaction and distributed leases require additional concurrency
  and failure contracts and are deferred.

## Consequences and validation

Incremental feedback reduces ordinary write cost, while compaction retains
occasional full corpus work. Expiration may slow search until reclamation. Native
SDK metadata/export support is required; tests use the 0.1.4 candidate CI wheel
from RuVector PR 1190, not an asserted published release.

Focused tests exercise interrupted publication, corrupt pointers, byte/count
limits, restart equivalence, expiration, bulk purge, rejected gates, rollback,
callback writes and fixed eligibility. Reproducible 10000-record benchmark data
and raw samples are in `benchmarks/ruvector/results/`; the guide reports ordinary
feedback separately from automatic compaction and expired-record search.

This proposal depends on ADR-0125 and its upstream PR. It does not implement the
ruvllm generation adapter work described in RuVector issue 1191.
