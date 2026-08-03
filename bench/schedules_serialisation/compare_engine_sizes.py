#!/usr/bin/env python3
"""Interleave clean read-only engine lifecycles for two synthetic SQLite files.

This is the discriminating probe for a *file-size-dependent* per-request
engine cost.  Pairing adjacent small/large samples reduces the effect of a
changing host load compared with running every small sample before every large
sample.  It imports the lifecycle probe from run_benchmark.py and therefore
uses the checked-out StateDB implementation unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from run_benchmark import engine_lifecycle_once, load_average, summary


async def amain(small: Path, large: Path, rounds: int) -> dict:
    samples = {"small": [], "large": []}
    for pair in range(rounds):
        # Reverse every second pair so a monotonic change in host load cannot
        # always favour the same file size.
        order = (("small", small), ("large", large))
        if pair % 2:
            order = tuple(reversed(order))
        for label, path in order:
            result = await engine_lifecycle_once(path)
            result["pair"] = pair
            samples[label].append(result)
    fields = (
        "construct_engine_ms",
        "open_engine_ms",
        "connect_checkout_ms",
        "select_1_ms",
        "dispose_engine_ms",
        "total_ms",
    )
    return {
        "method": "alternating small/large StateDB(readonly=True) construction, SELECT 1, disposal",
        "rounds_per_size": rounds,
        "initial_load_average": load_average(),
        "samples": samples,
        "summary": {
            label: {field: summary([x[field] for x in rows]) for field in fields}
            for label, rows in samples.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--small", type=Path, required=True)
    parser.add_argument("--large", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 3:
        raise SystemExit("--rounds must be at least 3")
    result = asyncio.run(amain(args.small.resolve(), args.large.resolve(), args.rounds))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "summary": result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
