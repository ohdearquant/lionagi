from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

# Alternates short baseline/current chunks (rather than two long back-to-back
# runs) so wall-clock runner-speed drift can't masquerade as a code regression,
# then merges each arm's chunks into the same result JSON shape ci_check_provenance.py
# and ci_compare.py already read.

# meta keys that legitimately vary per chunk (timing canaries, not provenance); everything
# else a bench script writes to meta (lionagi/python identity, dependency versions) must be
# an identical non-empty string across every chunk of one arm -- a later chunk silently
# running under a different interpreter/install than chunk 0 must not be discarded.
_CHUNK_VARYING_META_KEYS = {"cpu_probe_seconds", "chunk_wall_seconds", "chunk_started_at_epoch"}


def _validate_provenance_across_chunks(chunks: list[dict[str, Any]]) -> None:
    all_keys: set[str] = set()
    for c in chunks:
        all_keys.update(c.get("meta", {}).keys())
    for key in sorted(all_keys - _CHUNK_VARYING_META_KEYS):
        values = [c.get("meta", {}).get(key) for c in chunks]
        if any(not isinstance(v, str) or v == "" for v in values):
            raise ValueError(
                f"chunks disagree on meta.{key} (missing/non-string on some chunk): {values!r}"
            )
        if len(set(values)) > 1:
            raise ValueError(f"chunks disagree on meta.{key}: {sorted(set(values))}")


def run_chunk(
    python_bin: str,
    module: str,
    repeat: int,
    extra_args: list[str],
    output_path: Path,
    cwd: Path,
) -> dict[str, Any]:
    cmd = [
        python_bin,
        "-m",
        module,
        "--repeat",
        str(repeat),
        "--output",
        str(output_path),
        *extra_args,
    ]
    t0 = time.time()
    # Arguments are workflow-controlled paths/flags, not untrusted input.
    subprocess.run(cmd, check=True, cwd=cwd)  # noqa: S603
    wall = time.time() - t0
    data = json.loads(output_path.read_text(encoding="utf-8"))
    meta = data.setdefault("meta", {})
    meta["chunk_wall_seconds"] = wall
    meta["chunk_started_at_epoch"] = t0
    return data


def merge_arm(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-chunk result dicts into one result dict of the same shape."""
    if not chunks:
        raise ValueError("no chunks to merge")

    all_scenarios: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        for name, stat in chunk.get("results", {}).items():
            all_scenarios.setdefault(name, []).append(stat)

    # Median of per-chunk medians (not a merge of raw samples) is more outlier-resistant to one slow chunk.
    merged_results: dict[str, Any] = {}
    for name, stats in all_scenarios.items():
        total_runs = sum(s["runs"] for s in stats)
        weighted_mean = sum(s["mean"] * s["runs"] for s in stats) / total_runs
        merged_results[name] = {
            "name": name,
            "runs": total_runs,
            "min": min(s["min"] for s in stats),
            "mean": weighted_mean,
            "median": statistics.median(s["median"] for s in stats),
            "max": max(s["max"] for s in stats),
        }

    # Validate every provenance field across ALL chunks (not just chunk 0) so a mid-arm interpreter/install/dependency change is caught here.
    _validate_provenance_across_chunks(chunks)
    base_meta = dict(chunks[0].get("meta", {}))

    # Per-chunk series for artifact inspection.
    base_meta["chunk_count"] = len(chunks)
    base_meta["chunk_cpu_probe_seconds"] = [
        c.get("meta", {}).get("cpu_probe_seconds") for c in chunks
    ]
    base_meta["chunk_started_at_epoch"] = [
        c.get("meta", {}).get("chunk_started_at_epoch") for c in chunks
    ]
    base_meta["chunk_wall_seconds"] = [c.get("meta", {}).get("chunk_wall_seconds") for c in chunks]

    return {"meta": base_meta, "results": merged_results}


def chunk_sizes(total_repeat: int, chunks: int) -> list[int]:
    if chunks < 1:
        raise ValueError("chunks must be >= 1")
    if total_repeat < 1:
        raise ValueError("total_repeat must be >= 1")
    chunks = min(chunks, total_repeat)  # never emit a zero-size chunk
    base_n, rem = divmod(total_repeat, chunks)
    return [base_n + (1 if i < rem else 0) for i in range(chunks)]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run a benchmark module's baseline and current arms as alternating "
        "chunks and merge each arm's chunks into one paired-in-time result JSON."
    )
    ap.add_argument("--module", required=True, help="e.g. benchmarks.concurrency_bench")
    ap.add_argument("--baseline-python", required=True)
    ap.add_argument("--current-python", required=True)
    ap.add_argument("--total-repeat", type=int, required=True)
    ap.add_argument("--chunks", type=int, default=5)
    ap.add_argument(
        "--extra-args", default="", help="Extra args forwarded to the module, e.g. '--backend trio'"
    )
    ap.add_argument("--baseline-output", required=True, type=Path)
    ap.add_argument("--current-output", required=True, type=Path)
    ap.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="cwd for chunk subprocesses AND scratch dir for per-chunk files",
    )
    args = ap.parse_args()

    extra = shlex.split(args.extra_args)
    sizes = chunk_sizes(args.total_repeat, args.chunks)

    scratch = args.work_dir / "_chunks"
    scratch.mkdir(parents=True, exist_ok=True)

    baseline_chunks: list[dict[str, Any]] = []
    current_chunks: list[dict[str, Any]] = []
    for i, size in enumerate(sizes):
        b_path = scratch / f"baseline-chunk-{i}.json"
        c_path = scratch / f"current-chunk-{i}.json"
        # Adjacency (baseline then current, same chunk index) is what cancels drift, not the merge math above.
        print(f"[run_paired_ab] chunk {i + 1}/{len(sizes)}: baseline (repeat={size})", flush=True)
        baseline_chunks.append(
            run_chunk(args.baseline_python, args.module, size, extra, b_path, args.work_dir)
        )
        print(f"[run_paired_ab] chunk {i + 1}/{len(sizes)}: current (repeat={size})", flush=True)
        current_chunks.append(
            run_chunk(args.current_python, args.module, size, extra, c_path, args.work_dir)
        )

    args.baseline_output.parent.mkdir(parents=True, exist_ok=True)
    args.current_output.parent.mkdir(parents=True, exist_ok=True)
    args.baseline_output.write_text(
        json.dumps(merge_arm(baseline_chunks), indent=2), encoding="utf-8"
    )
    args.current_output.write_text(
        json.dumps(merge_arm(current_chunks), indent=2), encoding="utf-8"
    )
    print(
        f"[run_paired_ab] merged {len(sizes)} paired chunks -> {args.baseline_output}, {args.current_output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
