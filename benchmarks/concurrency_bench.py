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

from lionagi.ln.concurrency import (
    CompletionStream,
    bounded_map,
    create_task_group,
    gather,
    race,
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


async def _bench_once(fn: Callable[[], Coroutine[Any, Any, Any]]) -> float:
    t0 = time.perf_counter()
    await fn()
    return time.perf_counter() - t0


async def _bench_repeat(name: str, repeat: int, fn: Callable[[], Coroutine[Any, Any, Any]]) -> Stat:
    runs = []
    for _ in range(repeat):
        runs.append(await _bench_once(fn))
    return _aggregate(name, runs)


# Scenario implementations
def scenario_gather_100_yield() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def work(i: int):
            # Yield once to exercise scheduling without adding long sleeps
            await anyio.sleep(0)
            return i

        await gather(*(work(i) for i in range(100)))

    return _run


def scenario_bounded_map_2000_limit_100() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def mapper(x: int):
            # Async no-op to measure scheduler+pattern overhead
            await anyio.sleep(0)
            return x

        await bounded_map(mapper, range(2000), limit=100)

    return _run


def scenario_completion_stream_1000_limit_100() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def work(i: int):
            await anyio.sleep(0)
            return i

        aws = [work(i) for i in range(1000)]
        async with CompletionStream(aws, limit=100) as stream:
            async for _ in stream:
                pass

    return _run


def scenario_race_first_completion_10() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def fast():
            await anyio.sleep(0)
            return 1

        async def slow():
            await anyio.sleep(0.01)
            return 2

        await race(*(fast(), *(slow() for _ in range(9))))

    return _run


def scenario_cancel_propagation_500() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def bad():
            await anyio.sleep(0.01)
            raise RuntimeError("boom")

        async def sleeper():
            try:
                await anyio.sleep(0.5)
            except BaseException:
                pass

        try:
            async with create_task_group() as tg:
                tg.start_soon(bad)
                for _ in range(499):
                    tg.start_soon(sleeper)
        except BaseException:
            # Expected: failure cancels peers and exits quickly
            pass

    return _run


def scenario_taskgroup_start_1000_noop() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _run():
        async def noop():
            await anyio.sleep(0)

        async with create_task_group() as tg:
            for _ in range(1000):
                tg.start_soon(noop)

    return _run


SCENARIOS: list[tuple[str, Callable[[], Coroutine[Any, Any, Any]]]] = [
    ("gather_100_yield", scenario_gather_100_yield()),
    ("bounded_map_2000_limit_100", scenario_bounded_map_2000_limit_100()),
    (
        "completion_stream_1000_limit_100",
        scenario_completion_stream_1000_limit_100(),
    ),
    ("race_first_completion_10", scenario_race_first_completion_10()),
    ("cancel_propagation_500", scenario_cancel_propagation_500()),
    ("taskgroup_start_1000_noop", scenario_taskgroup_start_1000_noop()),
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
    parser = argparse.ArgumentParser(description="Concurrency micro-benchmarks")
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

    # Run benchmarks under selected backend
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
        out_path = out_dir / f"{ts}-{args.backend}.json"
    save_results(payload, out_path)

    print(f"Saved results -> {out_path}")

    # Optionally print JSON
    if args.json:
        print(json.dumps(payload, indent=2))

    # Optional comparison
    if args.compare:
        try:
            baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            print()
            print(compare_results(payload, baseline))
        except Exception as e:
            print(f"Failed to compare with baseline: {e}")


if __name__ == "__main__":
    main()
