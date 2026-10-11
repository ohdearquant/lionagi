# Durable RVF and explicit relevance feedback: measured evidence

Measured on 2026-10-11 with Python 3.10.22, NumPy 2.2.6, LionAGI 0.35.3 and a
locally built RuVector 0.1.4 release wheel on Linux x86_64. The native wheel uses
Rust optimization level 3, fat LTO and one code generation unit. This is candidate
SDK evidence, not a claim that the required wheel has been published on PyPI.

[Raw measurements](../../benchmarks/ruvector/results/durable-rvf-2026-10-11.json)
include every measured operation, dataset seed and hash, package versions and the
native extension SHA256. [Runner](../../benchmarks/ruvector/durable_learning.py):

```bash
uv run --no-sync python benchmarks/ruvector/durable_learning.py \
  --output benchmarks/ruvector/results/durable-rvf-2026-10-11.json
```

Install the candidate native wheel before running. `--no-sync` preserves that
installation instead of resolving the older published dependency. Timings vary
by hardware and filesystem; approximate HNSW construction also changes candidate
recall between runs.

## Durable insertion

Both stores receive the same 300 documents with 64 dimensional vectors. The
baseline acknowledges each document separately; the candidate acknowledges three
batches of 100. Each successful operation writes a complete native RVF checkpoint.

| Measurement | Individual writes | Batches of 100 |
| --- | ---: | ---: |
| Total insertion | 838.95 ms | 56.67 ms |
| Time per document | 2.797 ms | 0.189 ms |
| Reopen and index reconstruction | 40.27 ms | 44.72 ms |
| RVF bytes after close | 108,586 | 108,586 |
| IDs and complete payloads identical after reopen | Yes | Yes |

Batching reduced total insertion time by 93.24% in this small workload by
amortizing full checkpoint writes. This does not imply the same reduction for
larger datasets or another storage device. Each mutation still rewrites O(N)
records. The benchmark uses one process and a fixed baseline then candidate order;
it does not measure concurrent clients, production throughput or power loss.

## Explicit preference adaptation

The controlled fixture contains 20 query contexts and 40 documents. For each
context, trusted feedback favors a document slightly farther away in cosine
distance. Eight positive events per context provide 160 feedback records.
Evaluation uses 100 separately generated embedding perturbations for the same
exact query strings. This tests repeated query preference adaptation, not model
training or generalization to new questions.

| Stage | Preferred document hit@1 | Median search | p95 search |
| --- | ---: | ---: | ---: |
| Before feedback | 5% | 0.0397 ms | 0.1388 ms |
| With feedback | 90% | 0.0586 ms | 0.2638 ms |
| After reopening with feedback | 85% | 0.0558 ms | 0.1854 ms |
| Reopened index, feedback disabled | 0% | 0.0385 ms | 0.1963 ms |
| After reset on reopened index | 0% | 0.0395 ms | 0.1126 ms |

Feedback increased median search latency by about 48% in the initial index,
roughly 0.019 ms per query. Recording all 160 durable feedback events took 238.54 ms.
Feedback aggregates survived reopening exactly. HNSW reconstruction changes the
candidate set, so the different 90% and 85% results do not indicate lost feedback.
The ranker cannot promote a document absent from its eligible candidate set.
Queries without matching feedback were unchanged. Reset restored the native
baseline of the same reopened index, rather than asserting equality between
independently reconstructed approximate indexes.

## Validation and acceptance

The three durable memory integration tests pass with the release native wheel and
zero skips. They verify UUID, timestamp, tags and nested payload preservation;
explicit feedback opt in; unchanged default retrieval; filter exclusion; restart
and reset; and rejection of a ranker bound to a different store.

The broader focused integration result reported for this change is 133 tests.
The native SDK boundary suite is separately reported as 34 tests. The full LionAGI
suite is not certified by these focused results: collection encountered five
errors from missing `fastapi` and `jsonschema` dependencies, with 81 skips.

Acceptance requires a real RVF file, identical canonical records and feedback
aggregates after restart, unchanged unknown query behavior, and reset equality to
the same index with feedback disabled. No minimum synthetic hit rate is imposed
or tuned after observing results. Older SDKs skip RVF dependent integration tests;
those skips do not constitute release acceptance.
