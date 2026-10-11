# RuVector validation and benchmark evidence

Measured on 2026-10-11 using the proposed LionAGI source implementation on Linux x86_64, Python 3.10.22, RuVector 0.1.2 and NumPy 2.2.6. No embedding provider, remote service or LLM was called.

## Acceptance

43 integration and optional-import tests plus 29 existing MemoryStore/Branch memory regression tests passed: **72 passed**. The real Rust extension and real Branch.act implementation are exercised. Ruff check and formatting pass for all added Python files. The lockfile passes `uv lock --check`. An isolated source distribution and wheel build succeeded.

The full repository suite was attempted with the RuVector extra and development dependencies. Collection stopped with five missing-dependency errors in unrelated Studio/MCP tests (`fastapi` and `jsonschema`), with 81 skips. Therefore this is **not a claim that the full LionAGI suite or platform CI passed**. Maintainer CI uses all extras and remains the merge gate. The change does not alter existing CI or release permissions.

Tests cover identity, payload roundtrip, tag/filter semantics, real tools, concurrency, cancellation during embedding, missing and malformed vectors, bounded finite JSON, resource limits, detached snapshots, native insertion poisoning and deletion rollback. No fuzzing campaign, penetration test or new independent native-code audit is claimed.

## Synthetic retrieval benchmark

10,000 normalized Gaussian vectors, 64 dimensions, 200 independent queries, k=10, dataset seed 20261011. Ten queries warm the index. Exact NumPy cosine ranking provides ground truth. BLAS runs one thread. Each configuration runs once in its own process; rerun for variance. Dataset hashes and environment details are in `benchmarks/ruvector/gaussian-10k-ef*.json`.

| ef_search | Recall@10 | Adapter p50 ms | Adapter p95 ms | Exact NumPy p50 ms |
| --- | --- | --- | --- | --- |
| 100 | 95.60% | 0.881 | 1.266 | 0.150 |
| 200 | 99.60% | 1.446 | 2.158 | 0.160 |

**Exact NumPy was faster on this small dataset.** No speed advantage over NumPy is claimed. Higher ef_search trades additional search work for recall. The adapter honors the configured ef_search directly and uses bounded top-k selection for exact filtered queries. HNSW graph randomness is not fully controlled by the dataset seed. This is an exploratory benchmark, not a statistically established optimization claim.

Measurements include Python adapter overhead and exclude embedding, network and LLM costs. They do not cover production relevance, concurrent throughput, filtered scans, deletion latency, or other operating systems. High-water RSS includes Python and native allocations and is not exclusive index memory.

```bash
uv sync --extra ruvector
uv run pytest -n0 tests/integrations tests/protocols/test_memory.py tests/session/test_memory_access_surface.py
uv run python examples/ruvector_memory.py
uv run python benchmarks/ruvector/retrieval.py --ef 100 --output /tmp/ruvector-benchmark.json
```

The benchmark uses the Unix `resource` module and runs on Linux/macOS. The integration itself does not depend on that module.
