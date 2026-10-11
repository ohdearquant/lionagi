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
## Contract reference

| Concern | Decision |
| --- | --- |
| Transaction visibility and replay | D1 |
| Expiration and physical retention | D2 |
| Retrieval approval and rollback | D3 |

### D1 control and transaction shapes

The literal control shape, with representative values, is:

```json
{
  "version": 1,
  "generation": 2,
  "segments": [
    {
      "name": "segment-0123456789abcdef0123456789abcdef.rvf",
      "sha256": "<64 lowercase hexadecimal characters>"
    }
  ]
}
```

The generated basename never supplies an arbitrary path or native lineage parent.
The first segment is a base operation; later generations are consecutive deltas.
The RVF application envelope is:

```python
{
    "configuration": {
        # Inherited effective store options and embedding identity,
        # plus journal version, max_segments and max_journal_bytes.
    },
    "generation": 2,
    "operation": "feedback",
    "records": [],  # For add/base: [UUID, content, metadata] rows.
    "payload": ["event-id", "context-digest", "node-UUID", 1.0],
}
```

Vector identifiers are the ordered row offsets in each segment, not global UUIDs.
The metadata codec and envelope byte bounds are inherited from ADR-0125; journal
replay also checks the final corpus and feedback capacities. Application metadata
must remain JSON serializable and all numeric vectors and rewards finite.

| Operation | Records | Payload | Replay effect |
| --- | --- | --- | --- |
| base | Complete live physical corpus | All explicit feedback events | Initialize records and events |
| add | New canonical rows and vectors | null | Reject duplicate UUIDs, append records |
| delete | Empty | UUID list | Remove records and associated feedback |
| feedback | Empty | One event tuple | Enforce event identity and update aggregate |
| reset | Empty | Context digest | Remove feedback for that context |

An application write progresses through these visibility states:

1. Validate request and mutate locked in-process state.
2. Write bounded native segment; close and sync it.
3. Check active count and byte limits; compact when needed.
4. Publish a synced temporary CURRENT by atomic replacement and directory sync.
5. Update in-process segment accounting; remove obsolete managed files after a
   successful compaction.

After a failed write the instance is unusable. Failure after atomic replacement
can mean the new generation is visible even though the method raised. Reopen and
inspect canonical state; retry feedback with the same event ID to avoid duplicate
ratings. The API does not promise an exception always means no durable change.

Files named by CURRENT must pass private regular file, ownership, single link,
basename, hash and budget checks. Unreferenced files are not replayed. Cleanup
only targets generated managed segment basenames that pass path validation.
Compaction does not recursively remove application directories or arbitrary files.

Migration from a single snapshot is explicit application export and ingestion
into a fresh journal directory. The constructor does not reinterpret an existing
single RVF file as CURRENT. Keep the source until verifying UUID, payload, vector,
embedding identity and feedback equivalence; there is no automatic migration API.

### D2 eligibility and retention contracts

```python
LifecycleRuVectorStore(
    directory, dimensions, *, embedding_id, clock=time.time, **store_options
)
add_nodes(nodes, *, ttl_seconds=None) -> list[str]
purge_expired(now=None) -> int
compact(*, purge_expired=True) -> None
```

The reserved record metadata shape is exactly
`{"_ruvector_lifecycle": {"version": 1, "expires_at": epoch_seconds}}`.
Caller supplied reserved keys are rejected. TTL must be finite and positive.
Expiry at the captured read time is ineligible. Public returned metadata omits
the envelope, and host predicates receive public metadata.

`len(store)` counts eligible records; physical capacity is still occupied until
purge. Feedback cannot be added to an expired record. Purge deletes all expired
UUIDs and their events in one transaction even when their count exceeds the
public mutation batch limit. A host supplied `now` is a trusted retention
decision, not tenant input. Async wrappers run these operations in threads;
cancelling an await does not stop a worker already performing native I/O.

### D3 evaluation and report contracts

```python
EvaluationCase(query, vector, relevant_ids, predicate=None)
EvaluationGate(
    min_cases=20, min_relative_improvement=0.1,
    max_p95_ratio=1.5, allow_case_regressions=False
)
GovernedFeedbackRanker(store)
promote(candidate, cases, gate=None, *, k=5) -> EvaluationReport
rollback() -> bool
```

The immutable report fields are `cases`, `k`, `baseline_mrr`,
`candidate_mrr`, `baseline_p95_ms`, `candidate_p95_ms`, `regressions`,
`accepted`, `reasons`, `embedding_id`, `configuration`,
`gate_configuration` and `dataset_digest`. Settings are stored as tuples of
name/value pairs. Reports omit raw query, content and labels, but a digest is not
an anonymization guarantee for guessable inputs.

A relevant ID must exist and be eligible under the optional predicate. Case
vectors must match the store dimension. Malformed or missing labels raise and
restore the evaluation guard. Zero quality gain is rejected even when the
relative improvement threshold is zero. p95 uses one paired sample per case;
the caller must choose enough representative cases for reliable timing evidence.
Synthetic repeated queries in the runnable example verify APIs only.

## Deferred contracts

### Durable policy evidence: DEFERRED

A future durable approval would need a versioned policy record bound to exact
corpus generation, embedding revision, feedback state, labeled evaluation
partition and executable predicate identity. Recovery would verify every binding
before activating the policy; mismatch would restore baseline. The current
digest does not satisfy these bindings, so runtime approval is intentionally
not serialized or inferred from persisted ratings.

### Background compaction: DEFERRED

A background implementation would snapshot a generation under the writer lease,
build its base off-thread, then check that generation before publishing. Concurrent
deltas would either be appended to the new base or force retry; file reclamation
must wait for all readers. Cancellation and disk failure require explicit state
and retry limits. The current synchronous lock avoids an unimplemented retry and
reader lifetime contract, at the cost of observed compaction pauses.

### Distributed ownership: DEFERRED

A distributed lease would need fencing tokens enforced at publication and
storage with conditional atomic writes. Local advisory locks do not implement
that guarantee. Until a backend supplies fencing and shared filesystem durability
semantics, multiple hosts must not open this store for writing.
