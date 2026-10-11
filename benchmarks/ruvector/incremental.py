"""Reproducible full RVF versus incremental RVF durability acceptance benchmark."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
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
from lionagi.integrations.ruvector.journaled import JournaledRuVectorStore  # noqa: E402
from lionagi.integrations.ruvector.lifecycle import LifecycleRuVectorStore  # noqa: E402
from lionagi.integrations.ruvector.persistent import PersistentRuVectorStore  # noqa: E402


def elapsed(call):
    start = time.perf_counter_ns()
    value = call()
    return value, (time.perf_counter_ns() - start) / 1e6


def stats(values):
    return {
        "p50_ms": float(np.percentile(values, 50)),
        "p95_ms": float(np.percentile(values, 95)),
        "max_ms": max(values),
        "raw_ms": values,
    }


def disk(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def corpus(seed):
    random = np.random.default_rng(seed)
    vectors = random.normal(size=(10000, 64)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return [
        Node(
            id=UUID(int=i + 1),
            content=f"document {i}: reproducible corpus {seed}",
            metadata={"position": i, "scope": "benchmark", "seed": seed},
            embedding=v.tolist(),
        )
        for i, v in enumerate(vectors)
    ], vectors


def state_hash(store):
    digest = hashlib.sha256()
    for key, (content, metadata, vector) in store._records.items():
        for value in (key.encode(), content.encode(), metadata.encode(), vector.tobytes()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    digest.update(json.dumps(store._events, sort_keys=True).encode())
    return digest.hexdigest()


def run_store(root, kind, nodes):
    path = root / kind
    path.mkdir(mode=0o700)
    cls = PersistentRuVectorStore if kind == "full" else JournaledRuVectorStore
    argument = path / "memory.rvf" if kind == "full" else path / "journal"
    store = cls(argument, 64, embedding_id="benchmark:fixed-unit-v1")
    ingestion = []
    for offset in range(0, len(nodes), 1000):
        _, duration = elapsed(lambda offset=offset: store.add_nodes(nodes[offset : offset + 1000]))
        ingestion.append(duration)
    key = str(nodes[0].id)
    before = disk(path)
    samples = []
    written = 0
    for index in range(40):
        previous = (
            set(argument.parent.glob("*.rvf")) if kind == "full" else set(argument.glob("*.rvf"))
        )
        _, duration = elapsed(
            lambda index=index: store.record_feedback("stable-query", key, 1.0, f"feedback-{index}")
        )
        samples.append(duration)
        if kind == "full":
            written += argument.stat().st_size
        else:
            new = set(argument.glob("*.rvf")) - previous
            written += sum(p.stat().st_size for p in new) + store.path.stat().st_size
    after = disk(path)
    original_hash = state_hash(store)
    assert store.feedback("stable-query", [key]) == {key: (40.0, 40)}
    store.close()
    store, reopen_ms = elapsed(lambda: cls(argument, 64, embedding_id="benchmark:fixed-unit-v1"))
    reopened_hash = state_hash(store)
    assert reopened_hash == original_hash
    assert store.feedback("stable-query", [key]) == {key: (40.0, 40)}
    result = {
        "ingestion": stats(ingestion),
        "feedback": stats(samples),
        "feedback_disk_growth_bytes": after - before,
        "feedback_logical_published_bytes": written,
        "disk_bytes_before_feedback": before,
        "disk_bytes_after_feedback": after,
        "reopen_ms": reopen_ms,
        "exact_state_sha256": original_hash,
        "reopen_exact_state": reopened_hash == original_hash,
    }
    if kind == "journal":
        result["segment_count_before_compaction"] = store.segment_count
        automatic = []
        triggers = []
        for index in range(70):
            count = store.segment_count
            _, duration = elapsed(
                lambda index=index: store.record_feedback("stable-query", key, 1.0, f"auto-{index}")
            )
            automatic.append(duration)
            if store.segment_count < count:
                triggers.append(
                    {
                        "mutation_index": index,
                        "before_segments": count,
                        "after_segments": store.segment_count,
                        "latency_ms": duration,
                    }
                )
        result["automatic_compaction_mutations"] = stats(automatic)
        result["automatic_compaction_triggers"] = triggers
        assert triggers, "automatic compaction was not exercised"
        before_compact = disk(path)
        _, compact_ms = elapsed(store.compact)
        result["manual_compaction"] = {
            "elapsed_ms": compact_ms,
            "disk_before_bytes": before_compact,
            "disk_after_bytes": disk(path),
            "rvf_files_after": len(list(argument.glob("segment-*.rvf"))),
            "segments_after": store.segment_count,
        }
        assert store.segment_count == 1
        store.close()
        store = cls(argument, 64, embedding_id="benchmark:fixed-unit-v1")
        assert store.feedback("stable-query", [key]) == {key: (110.0, 110)}
        result["all_feedback_survives_compaction"] = True
    store.close()
    return result


def lifecycle(root, nodes, vectors):
    clock = [1000.0]
    path = root / "lifecycle"
    store = LifecycleRuVectorStore(
        path, 64, embedding_id="benchmark:fixed-unit-v1", clock=lambda: clock[0]
    )
    for offset in range(0, len(nodes), 1000):
        store.add_nodes(nodes[offset : offset + 1000], ttl_seconds=100.0)

    def searches(expected_live):
        samples = []
        for vector in vectors[:40]:
            hits, duration = elapsed(lambda vector=vector: store.search(vector, k=5))
            samples.append(duration)
            assert bool(hits) == expected_live
        return stats(samples)

    before = searches(True)
    disk_before = disk(path)
    clock[0] = 1200.0
    expired = searches(False)
    assert store.get(str(nodes[0].id)) is None and store.scan() == [] and len(store) == 0
    segments_before = store.segment_count
    removed, purge_ms = elapsed(store.purge_expired)
    assert removed == 10000 and store.segment_count == segments_before + 1
    disk_after_purge = disk(path)
    purged = searches(False)
    _, compact_ms = elapsed(store.compact)
    disk_after_compact = disk(path)
    store.close()
    with LifecycleRuVectorStore(
        path, 64, embedding_id="benchmark:fixed-unit-v1", clock=lambda: clock[0]
    ) as reopened:
        assert len(reopened) == 0 and reopened.scan() == []
    return {
        "before_expiry_search": before,
        "expired_search": expired,
        "purged_search": purged,
        "purge_ms": purge_ms,
        "purged_records": removed,
        "purge_single_transaction": True,
        "compact_ms": compact_ms,
        "disk_before_expiry_bytes": disk_before,
        "disk_after_purge_bytes": disk_after_purge,
        "disk_after_compact_bytes": disk_after_compact,
        "no_expired_hits": True,
        "reopened_empty": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    native_path = Path(native.__file__)
    wheel = args.wheel or Path(
        "/home/ruv/work/lionagi-rvf-next/wheels/ruvector-0.1.4-cp39-abi3-manylinux_2_28_x86_64.whl"
    )
    result = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "numpy": np.__version__,
            "ruvector": importlib.metadata.version("ruvector"),
            "native_sha256": hashlib.sha256(native_path.read_bytes()).hexdigest(),
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()
            if wheel.is_file()
            else None,
            "blas_threads": 1,
        },
        "method": {
            "records": 10000,
            "dimensions": 64,
            "feedback_mutations": 40,
            "ingest_batch_size": 1000,
            "max_segments": 64,
            "seeds": [20261011, 20261012],
            "orders": [["full", "journal"], ["journal", "full"]],
            "threshold": "each trial feedback p95 reduction >= 0.50",
            "published_bytes_note": "logical new RVF contents and CURRENT, excludes device amplification",
        },
        "trials": [],
    }
    with tempfile.TemporaryDirectory(prefix="lionagi-rvf-benchmark-") as temporary:
        root = Path(temporary)
        for trial, (seed, order) in enumerate(
            zip([20261011, 20261012], [["full", "journal"], ["journal", "full"]], strict=True)
        ):
            trial_root = root / f"trial-{trial}"
            trial_root.mkdir(mode=0o700)
            nodes, vectors = corpus(seed)
            values = {kind: run_store(trial_root, kind, nodes) for kind in order}
            reduction = (
                1 - values["journal"]["feedback"]["p95_ms"] / values["full"]["feedback"]["p95_ms"]
            )
            assert values["journal"]["exact_state_sha256"] == values["full"]["exact_state_sha256"]
            result["trials"].append(
                {
                    "seed": seed,
                    "order": order,
                    **values,
                    "p95_reduction": reduction,
                    "accepted": reduction >= 0.50,
                }
            )
            print(f"trial {trial}: reduction={reduction:.4f}", flush=True)
        nodes, vectors = corpus(20261011)
        result["lifecycle"] = lifecycle(root, nodes, vectors)
    result["accepted"] = all(t["accepted"] for t in result["trials"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    assert result["accepted"], "feedback p95 acceptance threshold not met"


if __name__ == "__main__":
    main()
