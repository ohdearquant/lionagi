"""Reproducible durable ingestion and explicit preference adaptation benchmark."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import tempfile
import time
from pathlib import Path
from uuid import UUID

for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[key] = "1"

import numpy as np  # noqa: E402
import ruvector._native as native  # noqa: E402

from lionagi import Node  # noqa: E402
from lionagi.integrations.ruvector import FeedbackRanker, PersistentRuVectorStore  # noqa: E402


def milliseconds(start):
    return (time.perf_counter_ns() - start) / 1e6


def persistence(root):
    rng = np.random.default_rng(20261013)
    n, dimensions = 300, 64
    vectors = rng.standard_normal((n, dimensions), dtype=np.float32)
    nodes = [
        Node(
            id=UUID(int=i + 1),
            content=f"document {i}",
            embedding=vectors[i].tolist(),
            metadata={"group": i % 7, "nested": {"record": i}},
        )
        for i in range(n)
    ]
    results = {}
    for label, batch_size in (("individual", 1), ("batch", 100)):
        path = root / f"{label}.rvf"
        with PersistentRuVectorStore(
            path, dimensions, embedding_id="fixture-v1", capacity=n
        ) as store:
            durations = []
            start_all = time.perf_counter_ns()
            for offset in range(0, n, batch_size):
                start = time.perf_counter_ns()
                store.add_nodes(nodes[offset : offset + batch_size])
                durations.append(milliseconds(start))
            elapsed = milliseconds(start_all)
            before = {str(node.id): store.get(str(node.id)) for node in nodes}
        start = time.perf_counter_ns()
        with PersistentRuVectorStore(
            path, dimensions, embedding_id="fixture-v1", capacity=n
        ) as store:
            reopen_ms = milliseconds(start)
            after = {str(node.id): store.get(str(node.id)) for node in nodes}
            assert before == after, "IDs or payloads changed across restart"
            assert len(store) == n
        results[label] = {
            "batch_size": batch_size,
            "total_ms": elapsed,
            "per_record_ms": elapsed / n,
            "commit_times_ms": durations,
            "reopen_build_ms": reopen_ms,
            "database_bytes_after_close": path.stat().st_size,
            "all_ids_and_payloads_equal_after_restart": True,
        }
    return {
        "documents": n,
        "dimensions": dimensions,
        "seed": 20261013,
        "dataset_sha256": hashlib.sha256(vectors.tobytes()).hexdigest(),
        "runs": results,
        "batch_total_reduction": 1
        - results["batch"]["total_ms"] / results["individual"]["total_ms"],
    }


def learning(root):
    dimensions, contexts = 64, 20
    rng = np.random.default_rng(20261014)
    vectors = np.eye(dimensions, dtype=np.float32)[:contexts]
    nodes, relevant_ids = [], []
    for i, vector in enumerate(vectors):
        preferred = vector.copy()
        preferred[40 + i] = 0.2
        nodes.extend(
            [
                Node(
                    id=UUID(int=1001 + i * 2),
                    content=f"cosine nearest {i}",
                    embedding=vector.tolist(),
                ),
                Node(
                    id=UUID(int=1002 + i * 2),
                    content=f"preferred {i}",
                    embedding=preferred.tolist(),
                ),
            ]
        )
        relevant_ids.append(str(nodes[-1].id))
    # Separate deterministic seed generates evaluation perturbations never used for feedback.
    evaluation = [
        vector + rng.normal(0, 0.001, (5, dimensions)).astype(np.float32) for vector in vectors
    ]
    path = root / "learning.rvf"
    with PersistentRuVectorStore(path, dimensions, embedding_id="fixture-v1") as store:
        store.add_nodes(nodes)
        ranker = FeedbackRanker(store)
        timings = {}

        def measure(stage, use_feedback=True):
            timings[stage] = []
            outcomes = []
            for i, queries in enumerate(evaluation):
                for query in queries:
                    start = time.perf_counter_ns()
                    hit = ranker.search(
                        query, query=f"context {i}", k=1, use_feedback=use_feedback
                    )[0]
                    timings[stage].append(milliseconds(start))
                    outcomes.append(hit.node_id == relevant_ids[i])
            return sum(outcomes) / len(outcomes)

        before = measure("no_feedback")
        unknown_before = [ranker.search(q, query="unseen context", k=1) for q in vectors]
        start = time.perf_counter_ns()
        for i in range(contexts):
            for event in range(8):
                ranker.feedback(f"context {i}", relevant_ids[i], 1.0, event_id=f"event-{i}-{event}")
        feedback_ms = milliseconds(start)
        after = measure("feedback")
        unknown_after = [ranker.search(q, query="unseen context", k=1) for q in vectors]
        assert unknown_before == unknown_after, "feedback leaked into unrelated context"
        aggregates = [
            store.feedback(ranker._context(f"context {i}"), relevant_ids) for i in range(contexts)
        ]
    with PersistentRuVectorStore(path, dimensions, embedding_id="fixture-v1") as store:
        ranker = FeedbackRanker(store)
        reopened_aggregates = [
            store.feedback(ranker._context(f"context {i}"), relevant_ids) for i in range(contexts)
        ]
        assert aggregates == reopened_aggregates, "feedback changed across restart"
        reopened = measure("reopened")
        reopened_baseline = measure("reopened_without_feedback", use_feedback=False)
        for i in range(contexts):
            ranker.reset(f"context {i}")
        reset = measure("reset")
        assert reset == reopened_baseline, "reset did not restore current-index baseline"
    return {
        "contexts": contexts,
        "documents": len(nodes),
        "dimensions": dimensions,
        "evaluation_seed": 20261014,
        "heldout_queries_per_context": 5,
        "feedback_events": contexts * 8,
        "feedback_total_ms": feedback_ms,
        "hit_at_1_before": before,
        "hit_at_1_after": after,
        "hit_at_1_after_restart": reopened,
        "hit_at_1_after_reset": reset,
        "hit_at_1_reopened_without_feedback": reopened_baseline,
        "feedback_aggregates_equal_after_restart": True,
        "unknown_context_unchanged": True,
        "search_timings": {
            stage: {
                "p50_ms": float(np.percentile(values, 50)),
                "p95_ms": float(np.percentile(values, 95)),
                "raw_ms": values,
            }
            for stage, values in timings.items()
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lionagi-durable-benchmark-") as directory:
        root = Path(directory)
        result = {
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "packages": {p: metadata.version(p) for p in ("numpy", "ruvector", "lionagi")},
                "native_extension_sha256": hashlib.sha256(
                    Path(native.__file__).read_bytes()
                ).hexdigest(),
            },
            "persistence": persistence(root),
            "learning": learning(root),
            "limitations": [
                "Synthetic repeated-query preference adaptation, not general model learning.",
                "Heldout vectors perturb known query contexts; unknown contexts do not improve.",
                "Ingestion timing includes native index maintenance and full RVF checkpoints.",
                "Each checkpoint rewrites all records; batching amortizes this O(N) work.",
                "Batching changes acknowledgment granularity from one to 100 records.",
                "Filesystem and hardware determine durability costs; one process, fixed run order.",
                "Retrieval uses only native candidates and bounded explicit feedback boosts.",
                "Rebuilding approximate HNSW may change candidate recall after reopening.",
            ],
        }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "environment"}, indent=2))
