from __future__ import annotations

import argparse
import json
import json as std_json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import orjson

from lionagi.ln.fuzzy import (
    extract_json,
    fuzzy_json,
    fuzzy_match_keys,
    string_similarity,
)


@dataclass
class Stat:
    name: str
    runs: int
    min: float
    mean: float
    median: float
    max: float


def _aggregate(name: str, values: list[float]) -> Stat:
    return Stat(
        name=name,
        runs=len(values),
        min=min(values),
        mean=sum(values) / len(values),
        median=statistics.median(values),
        max=max(values),
    )


async def _bench_once(fn: Callable[[], None]) -> float:
    t0 = time.perf_counter()
    # Functions are synchronous; run in current event loop thread
    fn()
    return time.perf_counter() - t0


async def _bench_repeat(name: str, repeat: int, fn: Callable[[], None]) -> Stat:
    runs = []
    for _ in range(repeat):
        runs.append(await _bench_once(fn))
    return _aggregate(name, runs)


# --- Scenario implementations ---


def scenario_fuzzy_json_valid_1000() -> Callable[[], None]:
    data = {"a": 1, "b": list(range(100)), "s": "hello"}
    s = json.dumps(data)

    def _run():
        for _ in range(1000):
            fuzzy_json(s)

    return _run


def scenario_fuzzy_json_dirty_single_quotes_500() -> Callable[[], None]:
    # Requires cleaning: single quotes + trailing comma
    s = "{a: 1, 'b': [1,2,3], }"

    def _run():
        for _ in range(500):
            fuzzy_json(s)

    return _run


def scenario_extract_json_direct_1000() -> Callable[[], None]:
    s = json.dumps({"x": [i for i in range(20)], "ok": True})

    def _run():
        for _ in range(1000):
            extract_json(s)

    return _run


def scenario_extract_json_markdown_blocks_200() -> Callable[[], None]:
    block = json.dumps({"i": 1, "vals": list(range(10))})
    content = "\n".join([f"```json\n{block}\n```" for _ in range(10)])

    def _run():
        for _ in range(200):
            extract_json(content)

    return _run


def scenario_fuzzy_match_keys_2000() -> Callable[[], None]:
    expected = [
        "username",
        "email",
        "first_name",
        "last_name",
        "age",
        "country",
        "city",
        "postal_code",
        "newsletter",
        "signup_ts",
    ]
    # Create small variations and typos
    samples = [
        {"usrname": "a", "emial": "a@x", "FirstName": "A", "ag": 10},
        {"username": "b", "email": "b@x", "first-name": "B"},
        {"user_name": "c", "mail": "c@x", "firstName": "C"},
    ]

    def _run():
        for i in range(2000):
            fuzzy_match_keys(samples[i % len(samples)], expected)

    return _run


def scenario_string_similarity_bulk_2000() -> Callable[[], None]:
    word = "username"
    candidates = [
        "usrname",
        "user_name",
        "username",
        "usernmae",
        "uname",
        "name",
        "email",
        "first_name",
        "last_name",
        "handle",
    ]

    def _run():
        for _ in range(2000):
            string_similarity(word, candidates, algorithm="jaro_winkler", threshold=0.6)

    return _run


SCENARIOS: list[tuple[str, Callable[[], None]]] = [
    ("fuzzy_json_valid_1000", scenario_fuzzy_json_valid_1000()),
    (
        "fuzzy_json_dirty_single_quotes_500",
        scenario_fuzzy_json_dirty_single_quotes_500(),
    ),
    ("extract_json_direct_1000", scenario_extract_json_direct_1000()),
    (
        "extract_json_markdown_blocks_200",
        scenario_extract_json_markdown_blocks_200(),
    ),
    ("fuzzy_match_keys_2000", scenario_fuzzy_match_keys_2000()),
    ("string_similarity_bulk_2000", scenario_string_similarity_bulk_2000()),
]


# Optional reference decoders for context (not used by library)
def scenario_std_json_valid_1000() -> Callable[[], None]:
    data = {"a": 1, "b": list(range(100)), "s": "hello"}
    s = std_json.dumps(data)

    def _run():
        for _ in range(1000):
            std_json.loads(s)

    return _run


def scenario_orjson_valid_1000() -> Callable[[], None]:
    data = {"a": 1, "b": list(range(100)), "s": "hello"}
    s = orjson.dumps(data)

    def _run():
        for _ in range(1000):
            orjson.loads(s)

    return _run


SCENARIOS.extend(
    [
        ("ref_std_json_valid_1000", scenario_std_json_valid_1000()),
        ("ref_orjson_valid_1000", scenario_orjson_valid_1000()),
    ]
)


async def run_benchmarks_async(repeat: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, fn in SCENARIOS:
        stat = await _bench_repeat(name, repeat, fn)
        results[name] = asdict(stat)
    return results


def system_info() -> dict[str, Any]:
    import anyio as _anyio

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "anyio": getattr(_anyio, "__version__", "unknown"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def save_results(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def compare_results(current: dict[str, Any], baseline: dict[str, Any]) -> str:
    lines = []
    lines.append("Comparison vs baseline (negative = faster):")
    for name, cur in current.get("results", {}).items():
        base = baseline.get("results", {}).get(name)
        if not base:
            lines.append(f"- {name}: no baseline")
            continue
        cur_med = cur["median"]
        base_med = base["median"]
        if base_med == 0:
            delta = float("inf")
        else:
            delta = (cur_med - base_med) / base_med
        lines.append(f"- {name}: median {cur_med:.6f}s vs {base_med:.6f}s -> {delta:+.1%}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fuzzy utilities micro-benchmarks")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")
    parser.add_argument("--output", type=str, default="")
    parser.add_argument(
        "--compare",
        type=str,
        default="",
        help="Compare against a baseline JSON file",
    )
    args = parser.parse_args()

    results = anyio.run(run_benchmarks_async, args.repeat)

    payload: dict[str, Any] = {
        "meta": {**system_info()},
        "results": results,
    }

    # Save results
    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = Path(__file__).parent / "results"
        out_path = out_dir / f"fuzzy-{ts}.json"
    save_results(payload, out_path)

    print(f"Saved results -> {out_path}")

    if args.json:
        print(json.dumps(payload, indent=2))

    if args.compare:
        try:
            baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            print()
            print(compare_results(payload, baseline))
        except Exception as e:
            print(f"Failed to compare with baseline: {e}")


if __name__ == "__main__":
    main()
