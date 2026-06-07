from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

from lionagi.ln import alcall, bcall, to_list
from lionagi.ln._hash import hash_dict
from lionagi.ln._json_dump import json_dumps


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


async def _bench_once(fn: Callable[[], Coroutine[Any, Any, Any]]) -> float:
    t0 = time.perf_counter()
    await fn()
    return time.perf_counter() - t0


async def _bench_repeat(name: str, repeat: int, fn: Callable[[], Coroutine[Any, Any, Any]]) -> Stat:
    runs = []
    for _ in range(repeat):
        runs.append(await _bench_once(fn))
    return _aggregate(name, runs)


# --- Scenario implementations ---


def scenario_alcall_async_noop_1000_conc_100() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def noop(x):
            await anyio.sleep(0)
            return x

        input_ = list(range(1000))
        await alcall(input_, noop, max_concurrent=100)

    return _run


def scenario_alcall_sync_noop_1000_conc_64() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        def noop(x):
            return x

        input_ = list(range(1000))
        await alcall(input_, noop, max_concurrent=64)

    return _run


def scenario_bcall_async_noop_1000_batch_50() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def noop(x):
            await anyio.sleep(0)
            return x

        input_ = list(range(1000))
        # Consume the generator to exercise batching path. Ensure no timeout.
        async for _ in bcall(input_, noop, 50, max_concurrent=100, retry_timeout=None):
            pass

    return _run


def scenario_to_list_flatten_nested_10000() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        # Build moderately nested structure
        nested = [[i, (i, {i, i + 1})] for i in range(5000)]
        # Sync function, run inside async wrapper
        to_list(nested, flatten=True, dropna=True, flatten_tuple_set=True)

    return _run


def scenario_to_list_flatten_unique_2000_mixed() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        mixed = [
            ({"a": i, "b": i % 5} if i % 3 == 0 else (i, i % 7) if i % 3 == 1 else [i, i])
            for i in range(2000)
        ]
        to_list(
            mixed,
            flatten=True,
            dropna=True,
            unique=True,
            flatten_tuple_set=True,
        )

    return _run


def scenario_hash_dict_complex_1000() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        obj = {
            "ints": list(range(50)),
            "floats": [i / 10 for i in range(50)],
            "nested": {f"k{i}": {"v": i, "arr": list(range(i % 10))} for i in range(50)},
            "mix": [{"i": i, "t": (i, str(i))} for i in range(50)],
        }
        for _ in range(1000):
            hash_dict(obj)

    return _run


def scenario_json_dumps_medium_1000() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        obj = {
            "name": "benchmark",
            "items": [
                {"id": i, "text": f"item-{i}", "values": list(range(10))} for i in range(200)
            ],
            "flags": {"a": True, "b": False, "n": None},
        }
        for _ in range(1000):
            json_dumps(obj, decode=False)

    return _run


SCENARIOS: list[tuple[str, Callable[[], Coroutine[Any, Any, Any]]]] = [
    (
        "alcall_async_noop_1000_conc_100",
        scenario_alcall_async_noop_1000_conc_100(),
    ),
    (
        "alcall_sync_noop_1000_conc_64",
        scenario_alcall_sync_noop_1000_conc_64(),
    ),
    (
        "bcall_async_noop_1000_batch_50",
        scenario_bcall_async_noop_1000_batch_50(),
    ),
    ("to_list_flatten_nested_10000", scenario_to_list_flatten_nested_10000()),
    (
        "to_list_flatten_unique_2000_mixed",
        scenario_to_list_flatten_unique_2000_mixed(),
    ),
    ("hash_dict_complex_1000", scenario_hash_dict_complex_1000()),
    ("json_dumps_medium_1000", scenario_json_dumps_medium_1000()),
]


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
    parser = argparse.ArgumentParser(description="LN function micro-benchmarks")
    parser.add_argument("--backend", choices=["asyncio", "trio"], default="asyncio")
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

    # Run under selected backend to ensure alcall/bcall operate deterministically
    results = anyio.run(run_benchmarks_async, args.repeat, backend=args.backend)

    payload: dict[str, Any] = {
        "meta": {
            "backend": args.backend,
            **system_info(),
        },
        "results": results,
    }

    # Save results
    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = Path(__file__).parent / "results"
        out_path = out_dir / f"ln-{ts}-{args.backend}.json"
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
