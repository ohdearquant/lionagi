"""Paired comparison with the pre-optimization exact filtered retrieval path."""

from __future__ import annotations

import argparse
import heapq
import importlib.metadata as metadata
import json
import os
import platform
import time
from pathlib import Path

for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[key] = "1"

import numpy as np  # noqa: E402

from lionagi import Node  # noqa: E402
from lionagi.integrations.ruvector import RuVectorStore, SearchHit  # noqa: E402
from lionagi.integrations.ruvector.store import _integer  # noqa: E402


def baseline(store, vector, k, predicate):
    """Filtered path from upstream proposal cd6e3686c1586a271864c323d4998a1e3c9369df."""
    _integer(k, "k", store.max_k)
    query = store._vector(vector)
    with store._lock:
        store._check()
        if not store._records:
            return []

        def candidates():
            for node_id, (_, payload, stored_vector) in store._records.items():
                if predicate(json.loads(payload)):
                    distance = float(np.clip(1.0 - np.dot(query, stored_vector), 0.0, 2.0))
                    yield distance, node_id

        ranked = heapq.nsmallest(k, candidates())
        return [
            SearchHit(
                node_id,
                store._records[node_id][0],
                json.loads(store._records[node_id][1]),
                distance,
            )
            for distance, node_id in ranked
        ]


def run(seed, n=5000, dimensions=64, queries=40):
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((n, dimensions), dtype=np.float32)
    query_vectors = rng.standard_normal((queries, dimensions), dtype=np.float32)
    store = RuVectorStore(dimensions, capacity=n)
    for offset in range(0, n, 1000):
        store.add_nodes(
            [
                Node(content=str(i), embedding=vectors[i].tolist(), metadata={"group": i % 4})
                for i in range(offset, min(offset + 1000, n))
            ]
        )
    results = []
    for selectivity in (0.25, 1.0):
        predicate = (lambda m: m["group"] == 0) if selectivity == 0.25 else (lambda m: True)
        for query in query_vectors[:5]:
            assert baseline(store, query, 10, predicate) == store.search(
                query, 10, predicate=predicate
            )
        timings = {"baseline": [], "optimized": []}
        for round_id in range(3):
            for i, query in enumerate(query_vectors):
                methods = [
                    ("baseline", lambda q=query, p=predicate: baseline(store, q, 10, p)),
                    ("optimized", lambda q=query, p=predicate: store.search(q, 10, predicate=p)),
                ]
                if (i + round_id) % 2:
                    methods.reverse()
                outputs = []
                for name, method in methods:
                    start = time.perf_counter_ns()
                    outputs.append(method())
                    timings[name].append((time.perf_counter_ns() - start) / 1e6)
                assert outputs[0] == outputs[1], "retrieval semantics changed"
        summary = {
            name: {"p50_ms": float(np.percentile(v, 50)), "p95_ms": float(np.percentile(v, 95))}
            for name, v in timings.items()
        }
        results.append(
            {
                "selectivity": selectivity,
                "identical_results": True,
                **summary,
                "p50_reduction": 1 - summary["optimized"]["p50_ms"] / summary["baseline"]["p50_ms"],
            }
        )
    return {
        "seed": seed,
        "n": n,
        "dimensions": dimensions,
        "queries": queries,
        "rounds": 3,
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-reduction", type=float, default=0.0)
    args = parser.parse_args()
    if not 0 <= args.min_reduction < 1:
        parser.error("min-reduction must be in [0, 1)")
    result = {
        "baseline_commit": "cd6e3686c1586a271864c323d4998a1e3c9369df",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": {p: metadata.version(p) for p in ("numpy", "ruvector", "lionagi")},
        },
        "runs": [run(seed) for seed in (20261011, 20261012)],
        "limitations": [
            "Synthetic filtered queries, not unfiltered HNSW acceleration.",
            "Same process/index and alternating order; timing remains hardware dependent.",
            "Exact equality to baseline is checked on every timed pair.",
        ],
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))

    if any(
        row["p50_reduction"] < args.min_reduction
        for run in result["runs"]
        for row in run["results"]
    ):
        raise SystemExit(
            "median reduction below requested acceptance threshold; inspect result JSON"
        )
