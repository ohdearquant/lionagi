"""Run the ADR-0088 steer-adherence matrix: providers x arms x trials.

    uv run python benchmarks/orchestration/run_steering.py                  # full matrix, N=20
    uv run python benchmarks/orchestration/run_steering.py --smoke          # N=2, claude_code only
    uv run python benchmarks/orchestration/run_steering.py --n 20 --providers claude_code codex

Each trial's SteerRunResult is saved as JSON under results/steering/ so a
crashed matrix resumes without re-paying for completed trials. Run
score_steering.py afterward to build the committed provider-by-arm table.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suites.steering.arms import Arm  # noqa: E402
from suites.steering.providers import PROVIDER_KEYS  # noqa: E402
from suites.steering.runner import run_steering_once  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results" / "steering"


async def main(providers: list[str], arms: list[Arm], n: int) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    plan = [(p, a, t) for p in providers for a in arms for t in range(n)]
    print(f"Matrix: {len(providers)} providers x {len(arms)} arms x {n} trials = {len(plan)} runs")

    for idx, (provider, arm, trial) in enumerate(plan, 1):
        out = RESULTS / f"{provider}__{arm.value}__t{trial}.json"
        # Resumable: skip trials already completed without error.
        if out.exists():
            prev = json.loads(out.read_text())
            if not prev.get("error"):
                print(
                    f"[{idx}/{len(plan)}] {provider} / {arm.value} / trial {trial} — cached, skip"
                )
                continue
        print(f"[{idx}/{len(plan)}] {provider} / {arm.value} / trial {trial} …", flush=True)
        t0 = time.monotonic()
        res = await run_steering_once(provider, arm, trial)
        dt = time.monotonic() - t0
        status = "ERR" if res.error else f"adherent={res.adherent}"
        print(f"    -> {status} in {dt:.0f}s", flush=True)
        out.write_text(json.dumps(dataclasses.asdict(res), indent=2), encoding="utf-8")

    print(f"\nDone. {len(plan)} results in {RESULTS}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="N=2, claude_code only, all built arms")
    ap.add_argument("--n", type=int, default=20, help="repetitions per (provider, arm) cell")
    ap.add_argument(
        "--providers",
        nargs="+",
        default=list(PROVIDER_KEYS),
        choices=list(PROVIDER_KEYS),
        help="provider families to run",
    )
    args = ap.parse_args()

    if args.smoke:
        providers_arg = ["claude_code"]
        n_arg = 2
    else:
        providers_arg = args.providers
        n_arg = args.n

    arms_arg = [Arm.NO_STEER, Arm.STEER_BURIED, Arm.STEER_RENDERED]  # Arm 3 (Mode B) not built
    asyncio.run(main(providers_arg, arms_arg, n_arg))
