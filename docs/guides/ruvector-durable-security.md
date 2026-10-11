# RVF persistence security review

Reviewed 2026-10-11. Scope: LionAGI's optional persistent RuVector adapter, explicit
feedback ranker and native RVF binding additions. This is a targeted assessment,
not a claim that the native runtime is safe against arbitrary hostile files.

## Trust boundaries and controls

| Boundary | Implemented control | Remaining responsibility |
| --- | --- | --- |
| Filesystem to native decoder | Private owned directory, regular single link files, symlink rejection, file size check | Open only application owned checkpoints; native decode and parent lineage resolution happen before full Python validation |
| State to retrieval index | Configuration and embedding identity match, UUID/vector correspondence, finite normalized vectors, bounded JSON metadata | Correct model revision and preprocessing identity supplied by host |
| Checkpoint publication | Complete sibling RVF, native close, atomic replace, directory sync on POSIX, stable exclusive lease | Supported local filesystem semantics, backups, handling ambiguous commit exceptions |
| Feedback to ranking | Existing Node reference, bounded reward and event count, idempotency keys, query scope, bounded score adjustment | Authenticate and authorize feedback writers; prevent misleading ratings |
| Retrieval to agents | Filters applied before ranking, detached results, existing read only tool | Treat all retrieved text as untrusted data |
| Persistent content to readers | Private file permissions | Encryption, retention, tenant authorization and backup access controls |

The native metadata value limit is 16 MiB. The adapter uses bounded chunks and a
manifest checksum inside the same RVF file. The checksum detects accidental damage;
it does not authenticate a file against an attacker who can rewrite it.

Review identified a disk budget mismatch caused by RVF metadata snapshot overhead.
The adapter now allows a conservative bounded overhead and validates the completed
temporary checkpoint against the reopen budget before publishing it. A regression
test verifies that rejecting an oversized checkpoint preserves the earlier file.

## Evidence

The real native wheel is used in restart and injected failure tests. Tests cover
exclusive ownership, private path restrictions, symlink rejection, malformed
vectors and metadata, manifest chunks and checksums, process exit before publication,
process exit after an acknowledged commit, rename failure, ambiguous synchronization
failure, feedback deduplication and reset, and MemoryStore payload fidelity.

Ruflo `@claude-flow/cli@3.25.6 security scan --depth deep --type code` reported zero
findings for `lionagi/integrations/ruvector`. The scan ran from a separate temporary
directory so its generated helper files did not modify this repository. Scanner
success is not proof of security; the trust boundaries and targeted tests above
provide the specific evidence for this change.

Linux was exercised. Windows lease behavior remains unverified. Process termination
is tested; hardware power loss and network filesystems are not. Native RVF lineage
resolution can follow parent paths, so arbitrary external RVF import remains
unsupported. An actor with write access can replace an entire checkpoint or replay
an older valid checkpoint; this adapter does not provide authenticated freshness.

A repository wide test attempt stopped at collection with five errors from missing
optional `fastapi` and `jsonschema` dependencies and 81 skipped tests. Focused native
integration validation is recorded in the benchmark guide and PR. No repository
wide green result is claimed.
