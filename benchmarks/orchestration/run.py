"""Run a benchmark matrix: configs × tasks × trials → saved RunResults.

MVP experiment (tests H1 single-vs-multi, H2 adversarial critic, H3 grounding):

  configs:  single baseline · flow default · flow adversarial+grounded
  tasks:    bus_mutant (real defect) · bus_clean (intended-behavior bait)
  trials:   3 each  → 18 runs

Each RunResult is saved as JSON under results/ so outputs can be hand-scored
AND machine-scored (judge.py), letting us validate the judge against hand labels.

    uv run python benchmarks/orchestration/run.py            # full 18-run matrix
    uv run python benchmarks/orchestration/run.py --smoke    # 1 config × 1 task × 1 trial
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

from harness.config import OrchestrationConfig  # noqa: E402
from harness.runner import run_once  # noqa: E402
from harness.task import Label, Task  # noqa: E402
from suites.mutation.mutate import build_tasks  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

# Design-intent grounding — the H3 lever. Injected into every worker prompt
# for grounded configs. States the totality contract the FP keeps tripping on.
GROUNDING = (
    "This codebase follows a TOTALITY contract: business `Exception`s are "
    "captured and logged; `BaseException` (asyncio CancelledError, "
    "KeyboardInterrupt, SystemExit) MUST propagate by design. A function that "
    "lets BaseException propagate is CORRECT, not a defect. Do not flag "
    "intended cancellation-propagation as an issue."
)

CONFIGS = [
    OrchestrationConfig(name="single", pattern="single", roles=("auditor",)),
    OrchestrationConfig(name="flow_default", pattern="flow"),
    OrchestrationConfig(
        name="flow_adversarial_grounded",
        pattern="flow",
        critic_modes=("adversarial",),
        grounding=GROUNDING,
    ),
]


def load_tasks() -> list[Task]:
    specs = build_tasks()
    tasks: list[Task] = []
    for s in specs:
        labels = tuple(Label(**lbl) for lbl in s["labels"])
        tasks.append(Task(id=s["id"], prompt="", labels=labels, context={"file": s["file"]}))
    return tasks


async def main(smoke: bool, trials: int) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks()
    configs = CONFIGS[:1] if smoke else CONFIGS
    n_trials = 1 if smoke else trials
    if smoke:
        tasks = tasks[:1]

    plan = [(t, c, i) for c in configs for t in tasks for i in range(n_trials)]
    print(
        f"Matrix: {len(configs)} configs × {len(tasks)} tasks × {n_trials} trials = {len(plan)} runs"
    )

    for idx, (task, config, trial) in enumerate(plan, 1):
        out = RESULTS / f"{config.key()}__{task.id}__t{trial}.json"
        # Resumable: skip trials already completed WITHOUT error. Lets us bump
        # --trials to add runs (N=3 → N=5) without re-paying for done trials,
        # and survive a mid-matrix crash. Errored runs are retried.
        if out.exists():
            prev = json.loads(out.read_text())
            if not prev.get("error"):
                print(
                    f"[{idx}/{len(plan)}] {config.name} / {task.id} / trial {trial} — cached, skip",
                    flush=True,
                )
                continue
        print(f"[{idx}/{len(plan)}] {config.name} / {task.id} / trial {trial} …", flush=True)
        t0 = time.monotonic()
        res = await run_once(task, config, trial)
        dt = time.monotonic() - t0
        status = "ERR" if res.error else f"{len(res.outputs)} outputs, {res.spawned} spawns"
        print(f"    -> {status} in {dt:.0f}s", flush=True)
        out = RESULTS / f"{config.key()}__{task.id}__t{trial}.json"
        out.write_text(json.dumps(dataclasses.asdict(res), indent=2), encoding="utf-8")

    print(f"\nDone. {len(plan)} results in {RESULTS}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="1 config × 1 task × 1 trial")
    ap.add_argument("--trials", type=int, default=3)
    args = ap.parse_args()
    asyncio.run(main(args.smoke, args.trials))
