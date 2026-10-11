# Incremental memory benchmark and security evidence

Measured 2026-10-11 on Linux, 2 CPUs, Python 3.10.22, NumPy 2.2.6,
native SDK 0.1.4 candidate CI wheel. These are local measurements, not deployment
service level guarantees. Wheel SHA-256:
`21143c71b7760d6d9bce84435359c9ee9601a5ddc65c8647c2985da4272daa23`.

## Reproducible benchmark

Run `uv run --no-sync python benchmarks/ruvector/incremental.py` after installing
the candidate SDK wheel. The checked in raw JSON contains all latency samples,
fixed seeds, environment and equality assertions. Two trials use 10000 records,
64 dimensions, batches of 1000 and 40 feedback mutations, alternating store order.

| Measurement | Trial 0 | Trial 1 |
| --- | ---: | ---: |
| Full checkpoint feedback p95 | 392.119 ms | 226.777 ms |
| Incremental feedback p95 | 4.364 ms | 4.333 ms |
| p95 reduction | 98.887% | 98.089% |
| Full published bytes, 40 events | 162449485 | 162449485 |
| Incremental published bytes, 40 events | 232390 | 232390 |
| Automatic compaction spike | 299.748 ms | 126.428 ms |

Both trials exceeded the predeclared 50% feedback p95 improvement gate.
Canonical UUIDs, payloads, vectors, metadata and event aggregates matched after
reopening. Reopening 10000 records still took approximately 10 through 13 seconds
because recovery rebuilds the index. Ordinary feedback samples precede automatic
compaction; compaction is separate full corpus work and creates tail latency.

The lifecycle experiment used 10000 records with the same expiry. Search p95 was
3.554 ms before expiration, 71.824 ms while expired physical records remained and
0.0749 ms after purge. Purge took 181.287 ms; subsequent compaction took 6.730 ms,
reducing managed files from 5023761 to 1709 bytes. The purged corpus is empty:
these timings are not a comparison on equal search populations. No expired hits
were returned, including after reopening. Schedule purge to avoid exact scans.

## Security assessment

Scope: adapter code, malformed local journals, application supplied metadata,
evaluation callbacks and one local writer. Trusted native runtime, private
application directory and trusted host clock are prerequisites. Untrusted RVF
artifacts, compromised same-user processes and distributed writers are excluded.

Read-only `@claude-flow/cli@3.25.6 security scan --target
lionagi/integrations/ruvector --depth deep --type all` reported zero issues.
It ran from a separate temporary directory against the absolute adapter path.
This is source scanner evidence, not a dependency advisory clearance or sandbox
proof. The scanner did not disclose an advisory feed timestamp. No dependency
versions or lockfile were changed. Inspected uv.lock SHA-256:
`4465c0be3751012ed2ffd16ae25c9431b060261725188febe080c1beb3041b86`.

| Boundary | Checked mitigation | Residual risk |
| --- | --- | --- |
| Local pointer and segments | Fixed filenames, exact integer version, bounded pointer and aggregate bytes, SHA checks, generations and schema | Same OS user can replace checksummed artifacts; trusted native parser required |
| Ownership and commit | Private paths, symlink rejection, exclusive lease, synced immutable segments and atomic pointer | Filesystem durability semantics and transient disk exhaustion remain host concerns |
| Retention | Reserved metadata validation, read filtering, single transaction purge and compaction | Trusted epoch across restart; no backup or media erasure |
| Evaluation | Frozen epoch, store lock, rejected reentrant writes, bounded labels and configurations | Predicates are executable trusted host code, not sandboxed |
| Approval | Frozen parameter copy, rejection preserves policy, bounded rollback | Baseline comparison does not guarantee improvement over prior approval; changes require reevaluation |

Small fixes include fail-closed clock rollback, strict pointer version types,
aggregate active byte bounds and type-safe evaluation guards. Regression tests
exercise these boundaries. Source scan coverage does not replace native parser
fuzzing or a full dependency audit.

## Validation

The focused adapter suite passed 190 tests with the actual native candidate wheel.
Ruff passed, and pinned Pyright 1.1.402 reported zero errors and warnings.
The LionAGI wheel built successfully. The governed-memory example passed
from an isolated environment installed with that wheel and the native SDK
candidate wheel, outside the source checkout. Whole-repository results are recorded
in the pull request separately; optional application dependencies are not part
of this adapter's runtime requirements.
