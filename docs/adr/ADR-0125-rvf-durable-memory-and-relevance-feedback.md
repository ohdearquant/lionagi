# ADR-0125: RVF durable memory and explicit relevance feedback

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: partial (adapter and acceptance tests in this proposal; native SDK release and upstream adoption remain prerequisites)
- **Area**: substrates
- **Date**: 2026-10-11
- **Relations**: extends ADR-0092; relates to ADR-0093 and ADR-0122

## Context

P1. The optional RuVector adapter keeps canonical Node records in process memory.
Applications cannot reuse branch memory after restart without implementing storage
and preserving LionAGI UUIDs, memory timestamps, tags and nested metadata.

P2. HNSW is a retrieval index. Making its internal serialization authoritative
would couple data recovery to an index implementation, while omitting application
metadata and embedding compatibility checks.

P3. A relevant result may be close to the query but consistently rank second.
Applications need a bounded way to record explicit preferences and recover those
preferences on restart. Automatically interpreting conversations as training data
would create an unreviewed write channel and unclear privacy semantics.

P4. The RVF Rust runtime already stores vectors and metadata. Its initial Python
surface exposes vector ingestion and queries, but lacks metadata access and vector
export. A native prerequisite must be delivered and tested rather than emulated
with a Python file merely named `.rvf`.

| Concern | Decision |
| --- | --- |
| Canonical format | D1: RVF holds vectors, records and explicit feedback |
| Commit and ownership | D2: Atomic complete checkpoints with a stable exclusive lease |
| Recovery and identity | D3: Validate a complete generation and rebuild HNSW |
| Adaptation | D4: Bounded query specific relevance feedback |
| Integration | D5: Explicit optional APIs preserve the baseline memory contract |
| Performance and evidence | D6: Batch writes and report measured limits |

Out of scope:

- Training embeddings, SONA weights or language models. This proposal changes only
  the order of retrieved candidates through explicit ratings.
- Distributed writers and high throughput ingestion. The initial contract is one
  local owner and a bounded corpus.
- Tenant authentication, encryption, backup and retention. These remain host
  responsibilities and cannot be inferred from a storage format.
- Executing RVF embedded programs. The adapter uses storage APIs only.
- Adopting the separate `ruvector-lionagi` distribution. The implementation remains
  owned by LionAGI and directly depends on the native SDK.

## Decision

### D1: Canonical RVF records

The public constructor in `lionagi/integrations/ruvector/persistent.py` is:

```python
PersistentRuVectorStore(
    path: str | Path,
    dimensions: int,
    *,
    embedding_id: str,
    **store_options: Any,
)
```

The class extends `RuVectorStore`, preserving bounded Node CRUD and search. It
adds `close()` and context manager ownership. The constructor does not download
models or infer embedding identity.

A committed RVF file contains normalized float32 vectors with integer identifiers
and a versioned application metadata envelope. The envelope contains:

- The immutable embedding identity and every effective store configuration option.
- The ordered mapping from native integer vector identifiers to canonical Node
  UUIDs, text content and JSON metadata.
- Explicit feedback events: event ID, context digest, Node UUID and signed reward.

The envelope is split into chunks of at most 1 MiB Unicode code points, each
no more than 4 MiB UTF-8, below the native 16 MiB value limit. The full envelope
is bounded at 80 MiB. A final header records chunk count, encoded byte count and
SHA256. It detects accidental corruption but does not authenticate the artifact.

Vectors are not duplicated inside the JSON envelope. Native vector export supplies
exact float32 values for rebuilding. Application payloads remain JSON and no Python
pickle deserialization is introduced.

The prerequisite SDK API consists of:

```python
RvfStore.create(path, dimension)
RvfStore.open(path, read_only=True)
store.ingest(vectors, ids)
store.set_metadata(key, value)  # UTF-8 strings, bounded by the native binding
store.get_metadata(key)
store.export_vectors()
store.metadata_recovery()
store.close()
```

The SDK prerequisite is independently reviewed. Older SDKs must fail clearly when
constructing the durable adapter while retaining support for the original in
memory adapter. Native metadata bounds and vector export bounds are applied before
creating large owned copies wherever the binding supports doing so.

### D2: Complete checkpoints and exclusive ownership

Each mutation serializes a complete new RVF file in the same directory as the
current generation. Native ingestion and metadata writes finish before native close.
The adapter atomically replaces the destination and synchronizes the parent
where the platform supports the required durable directory operation.

The destination is never updated by two independently committed record and
feedback files. RVF is the data store; the stable sidecar is a lease only.

One instance holds the lease until close. A second instance targeting the same
path fails rather than silently racing. The stable lock inode must not be removed
or replaced during normal close. In process calls remain serialized by the
existing reentrant store lock.

Commit ordering is:

1. Validate the requested operation and apply it to the bounded in memory state.
2. Produce and close a complete sibling RVF generation.
3. Replace the canonical path atomically.
4. Synchronize the containing directory where supported.
5. Return success to the caller.

A failure after in memory mutation poisons the instance. The host closes and
reopens to recover the canonical generation. A failure after replacement can mean
the new generation is already visible, so callers cannot assume an exception is a
rollback. Event IDs make feedback retries idempotent; Node UUIDs identify inserts
whose outcome must be checked before retrying.

Empty or invalid operations must not create partially published generations.
Unpublished temporary files do not become authoritative on reopen. Cleanup is best
effort and may leave a sibling artifact after process termination.

### D3: Recovery, identity and file trust

The embedding identity includes the model revision and preprocessing chosen by the
host. The adapter compares it and the full configuration with the persisted
configuration. An identity mismatch rejects opening for use; no automatic mixing
or reembedding occurs.

Reopen validates the envelope version, record counts, UUIDs, content and metadata
budgets, vector dimensions, finiteness and normalization, and feedback references.
Invalid or missing canonical application metadata must fail closed. The adapter
rebuilds HNSW from validated vectors rather than accepting persisted native index
objects as its canonical source.

On POSIX the parent directory must be private and owned by the current user;
regular data files must be private and have a single link. Symlink paths are
rejected. These checks assume a trusted application directory and owner. They are
not an adversarial multiuser filesystem sandbox.

Native RVF decoding precedes some Python checks. Only application produced trusted
files are supported. Malformed file tests provide evidence for named failure modes,
not a claim of universal hostile file safety. A digest of predictable query text
is not anonymization. Documents and metadata remain plaintext.

Reopen costs are proportional to corpus size. Filesystem atomicity and persistence
are distinct from arbitrary hardware power loss survival. Process exit tests do
not establish network filesystem semantics or universal power failure guarantees.

### D4: Explicit, bounded relevance feedback

The public contract in `learning.py` is:

```python
FeedbackRanker(store, *, max_boost=0.1, prior=4.0, candidate_multiplier=4)
ranker.feedback(query, node_id, reward, *, event_id) -> bool
ranker.reset(query) -> int
ranker.search(vector, *, query, k=5, predicate=None, use_feedback=True)
await ranker.asearch(vector, *, query, k=5, predicate=None, use_feedback=True)
```

The ranker must bind to a `PersistentRuVectorStore`. Its context digest includes a
versioned namespace, length delimited embedding identity and exact query bytes.
There is no transfer between differently spelled queries. Empty query behavior at
the memory adapter remains the existing scan contract.

Reward is a finite real number in `[-1, 1]`, excluding booleans. Each event ID is
unique across the store. An identical repeated event returns `False` without a
second observation. Conflicting reuse raises. Unknown Node IDs are rejected.
There are at most 10,000 events. Reset removes a context's events; deletion removes
the deleted Node's events. Removed event IDs can subsequently be reused.

For eligible candidates the ranking score is:

```text
cosine_distance - max_boost * reward_sum / (count + prior)
```

`max_boost` is in `[0, 0.25]`, `prior` in `[1, 1000]`, and candidate multiplier
in `[1, 16]`. Defaults provide shrinkage for small observation counts. Sorting
retains original order on ties. Returned `SearchHit.distance` remains the original
cosine distance and must not be interpreted as the adjusted ranking score.

Candidate retrieval is bounded by the store's `max_k`. Predicates restrict the
eligible set before feedback ranking. Feedback cannot promote records that were
not retrieved. No feedback for the context, disabled feedback or zero boost uses
the baseline native path without candidate expansion.

Host authorization precedes a feedback call. The read only retrieval tool does not
expose feedback writes. Retrieved text and model output are not automatically
feedback. This design bounds influence; it does not verify honest ratings.

### D5: Optional memory integration

`RuVectorMemoryStore` gains a keyword only `ranker=None` parameter. A provided
ranker must refer to the identical underlying store. Existing callers retain their
original behavior. UUIDs, timestamps, tags and nested metadata continue to use the
existing memory envelope; persistence preserves that envelope without interpreting
arbitrary nested application metadata as control instructions.

Async retrieval uses worker threads. Disk mutation methods are synchronous;
applications can call them through `asyncio.to_thread`. Cancellation does not undo
a worker that has already begun a commit. Hosts reconcile by UUID or event ID.

### D6: Performance and evidence

Every checkpoint is O(N) in stored data. `add_nodes` groups inserts into one
checkpoint and reduces repeated serialization and synchronization. Feedback lookup
uses bounded in memory aggregation, avoiding a full event scan per search.

The benchmark records individual versus batched insertion, reopen and rebuild,
file size, payload identity after restart, query latency, and held out synthetic
ranking preference outcomes. Raw observations include seeds, dataset hashes and
runtime versions. Results are workload observations rather than service guarantees.

**DEFERRED:** Incremental RVF generations with an application transaction envelope.
A future SDK can expose atomic record, vector, configuration and feedback mutation
within one authoritative generation. Acceptance would require fault injection at
every publish boundary, deterministic recovery and benchmark evidence showing a
useful reduction in write amplification. Independent mutable sidecars do not meet
that contract. This future design must retain explicit identity and feedback scope.

## Alternatives considered

SQLite offered transactions, indexes and mature crash recovery. It was the initial
draft and is replaced here at the user's request to use the RuVector native format.
RVF keeps the artifact within the SDK ecosystem but currently requires additional
bindings and complete checkpoint writes. No SQLite fallback is introduced.

A JSON sidecar plus an RVF vector file would avoid native metadata bindings, but
independent writes permit UUID and vector generations to diverge after interruption.
An explicit generation manifest could solve that issue but increases the number of
artifacts and recovery paths. One complete RVF generation is simpler to audit.

Persisting HNSW directly could reduce reopen time, but commits application records
and implementation specific index structures to a shared compatibility contract.
Rebuilding from canonical vectors keeps the index disposable.

Implicit reinforcement from agent output would collect more events, but confuses
untrusted generated content with authorized preference evidence. Explicit feedback
keeps the caller and scope identifiable. Neural fine tuning is a separate proposal.

## Consequences

Applications can recover local branch memory and explicit preferences together.
The storage artifact is portable through RVF tooling that preserves application
metadata. Native SDK release coordination is required before ordinary installation
can enable the durable adapter.

Write amplification and reopen time constrain practical corpus size. The single
owner lease simplifies consistency at the cost of concurrent process access.
Applications must own backup, access control and feedback authorization policies.

Changing D1 requires a format migration. Changing D2 requires a new recovery proof
and fault tests. Changing D4 requires a versioned feedback namespace so old evidence
is not silently reinterpreted. None of these contracts imply general intelligence,
training convergence, hostile file isolation or a universal durability guarantee.

## Validation

Acceptance is executable in `tests/integrations/ruvector/test_persistence.py`,
`test_learning.py`, and `test_durable_memory.py`. The benchmark lives in
`benchmarks/ruvector/durable_learning.py`. The native SDK prerequisite has separate
binding tests. Evidence and limitations belong in the linked guide and PR rather
than silently changing this ADR's proposal status.
