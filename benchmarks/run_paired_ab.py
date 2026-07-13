from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

# Drives one suite's baseline and current arms as alternating short chunks
# (baseline chunk 0, current chunk 0, baseline chunk 1, current chunk 1, ...)
# instead of two long back-to-back runs, then merges each arm's chunks back
# into a single result JSON in the exact shape ci_check_provenance.py and
# ci_compare.py already read -- neither of those needs to change.
#
# Why: a same-machine A/B comparison assumes the machine's own speed is
# constant across the job. Running the whole baseline suite (tens of
# seconds) and then the whole current suite back to back does not hold that
# assumption -- if the runner's effective speed drifts over the job
# (burstable-CPU credit decay, thermal throttling, a noisy neighbor), the
# arm that runs later systematically measures a different machine, and that
# shows up as a uniform, one-directional "regression" or "improvement"
# across every CPU-bound scenario regardless of any code change. Splitting
# each arm into several chunks and alternating them shrinks the wall-clock
# gap between any baseline measurement and its paired current measurement
# to roughly 1/chunks of the old gap, which is what actually cancels this
# kind of drift -- reordering the comparison math without shrinking that
# gap would not.


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
    # Arguments are workflow-controlled CLI paths/flags (interpreter paths
    # from prior steps, this repo's own benchmark module names), not
    # untrusted input.
    subprocess.run(cmd, check=True, cwd=cwd)  # noqa: S603
    wall = time.time() - t0
    data = json.loads(output_path.read_text(encoding="utf-8"))
    meta = data.setdefault("meta", {})
    meta["chunk_wall_seconds"] = wall
    meta["chunk_started_at_epoch"] = t0
    return data


def merge_arm(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge K per-chunk result dicts (each already in the normal bench
    JSON shape) into one result dict of that same shape.

    Per-scenario stats are combined without needing the raw per-repeat
    samples (each chunk process only ever wrote its own aggregated Stat to
    disk): runs sums across chunks, min/max take the extremes across
    chunks, mean is the runs-weighted mean of the chunk means, and median
    is the median of the per-chunk medians -- a standard, outlier-resistant
    way to combine already-aggregated batches (one slow chunk, e.g. from a
    scheduling hiccup during that window, is down-weighted rather than
    setting the whole result).
    """
    if not chunks:
        raise ValueError("no chunks to merge")

    all_scenarios: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        for name, stat in chunk.get("results", {}).items():
            all_scenarios.setdefault(name, []).append(stat)

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

    # All chunks of one arm share the same venv/install, so provenance and
    # dependency versions must be identical across them -- assert that
    # instead of silently picking the first chunk's value, so a bug that
    # somehow varies the interpreter mid-arm is caught here rather than
    # producing a quietly-wrong merged result.
    base_meta = dict(chunks[0].get("meta", {}))
    for key in ("lionagi_file", "lionagi_version", "python_executable"):
        values = {c.get("meta", {}).get(key) for c in chunks}
        if len(values) > 1:
            raise ValueError(f"chunks disagree on meta.{key}: {sorted(values)}")

    # Per-chunk timing and CPU-probe series, so drift across the interleave
    # is directly inspectable in the uploaded artifact rather than only
    # inferable from a failed compare.
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
        # Alternate baseline then current for the SAME chunk index so each
        # pair sits back-to-back in wall-clock time -- that adjacency, not
        # the merge math above, is what cancels drift.
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
