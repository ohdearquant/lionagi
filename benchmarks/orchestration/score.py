"""Score saved RunResults against ground truth, then aggregate per config.

    uv run python benchmarks/orchestration/score.py            # judge + report
    uv run python benchmarks/orchestration/score.py --report   # report cached scores only

Reports quality WITH 95% CIs (agentic eval is high-variance — a point estimate
is not a result) AND compute/cost, because a multi-agent "win" is meaningless
unless it holds PER DOLLAR (the matched-compute confound, arxiv 2604.02460).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.judge import score  # noqa: E402
from harness.stats import disjoint, f1, wilson  # noqa: E402
from harness.task import RunResult, ScoredResult  # noqa: E402
from run import load_tasks  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
SCORES = RESULTS / "_scores.json"


def _task_kinds() -> dict[str, str]:
    """task_id -> 'defect' | 'intended' from the first label."""
    return {t.id: (t.labels[0].kind if t.labels else "defect") for t in load_tasks()}


async def score_all() -> list[ScoredResult]:
    labels_by_task = {t.id: t.labels for t in load_tasks()}
    scored: list[ScoredResult] = []
    files = sorted(p for p in RESULTS.glob("*.json") if not p.name.startswith("_"))
    for i, f in enumerate(files, 1):
        raw = json.loads(f.read_text())
        res = RunResult(**raw)
        print(
            f"[{i}/{len(files)}] judging {res.config_key.split('__')[0]} / {res.task_id} t{res.trial} …",
            flush=True,
        )
        sr = await score(res, labels_by_task[res.task_id])
        scored.append(sr)
    SCORES.write_text(json.dumps([dataclasses.asdict(s) for s in scored], indent=2))
    return scored


def _valid(items: list[ScoredResult]) -> list[ScoredResult]:
    """Scored runs that actually executed (no harness/run error)."""
    return [s for s in items if not s.notes.startswith("run error")]


def _split(scored: list[ScoredResult], kinds: dict[str, str]):
    """config name -> (defect_runs, clean_runs), valid only."""
    by_cfg: dict[str, dict[str, list[ScoredResult]]] = defaultdict(
        lambda: {"defect": [], "intended": []}
    )
    for s in scored:
        cfg = s.config_key.split("__")[0]
        kind = kinds.get(s.task_id, "defect")
        by_cfg[cfg][kind].append(s)
    return by_cfg


def report(scored: list[ScoredResult], kinds: dict[str, str]) -> None:
    by_cfg = _split(scored, kinds)

    # ---- Quality table (with 95% Wilson CIs) --------------------------------
    print("\n" + "=" * 118)
    print("QUALITY  (proportions with 95% CI; CIs that overlap are NOT distinguishable at this N)")
    print("-" * 118)
    print(
        f"{'config':<26}{'n':<6}{'recall':<20}{'FP-avoid':<20}{'engaged':<20}{'precision':<11}{'F1':<6}{'SNR':<8}"
    )
    print("-" * 118)
    recalls = {}
    for cfg in sorted(by_cfg):
        defect = _valid(by_cfg[cfg]["defect"])
        clean = _valid(by_cfg[cfg]["intended"])
        tp = sum(s.found_defect for s in defect)
        fp = sum(s.false_positive for s in clean)
        recall = wilson(tp, len(defect))
        fp_avoid = wilson(len(clean) - fp, len(clean))
        engaged = wilson(sum(s.engaged for s in clean), len(clean))
        prec = tp / (tp + fp) if (tp + fp) else float("nan")
        f1v = f1(prec, recall.p) if not (prec != prec) else float("nan")
        snr = f"{tp}:{fp}" if fp else (f"{tp}:0" if tp else "0:0")
        recalls[cfg] = recall
        n = len(defect) + len(clean)
        prec_s = "N/A" if prec != prec else f"{prec:.2f}"
        f1_s = "N/A" if f1v != f1v else f"{f1v:.2f}"
        print(
            f"{cfg:<26}{n:<6}{recall.fmt():<20}{fp_avoid.fmt():<20}{engaged.fmt():<20}{prec_s:<11}{f1_s:<6}{snr:<8}"
        )
    print("=" * 118)

    # ---- Compute / cost table (the matched-compute view) --------------------
    print("\nCOMPUTE & COST  (lift is meaningless unless it holds per-dollar)")
    print("-" * 118)
    print(
        f"{'config':<26}{'in tok':<10}{'out tok':<10}{'$/run':<11}{'$/defect-found':<16}{'wall':<8}{'usage':<10}{'reasoning':<10}"
    )
    print("-" * 118)
    costs = {}
    for cfg in sorted(by_cfg):
        runs = _valid(by_cfg[cfg]["defect"]) + _valid(by_cfg[cfg]["intended"])
        if not runs:
            print(f"{cfg:<26}{'— no valid runs —'}")
            continue
        mean_in = statistics.mean([s.input_tokens for s in runs])
        mean_out = statistics.mean([s.output_tokens for s in runs])
        mean_cost = statistics.mean([s.est_cost_usd for s in runs])
        tp = sum(s.found_defect for s in _valid(by_cfg[cfg]["defect"]))
        total_cost = sum(s.est_cost_usd for s in runs)
        per_found = f"${total_cost / tp:.4f}" if tp else "∞ (0 found)"
        mean_wall = statistics.mean([s.wall_seconds for s in runs])
        src = runs[0].usage_source
        reasoning = "full" if all(s.reasoning_disclosed for s in runs) else "FLOOR*"
        costs[cfg] = mean_cost
        print(
            f"{cfg:<26}{mean_in:<10.0f}{mean_out:<10.0f}{f'${mean_cost:.4f}':<11}{per_found:<16}{f'{mean_wall:.0f}s':<8}{src:<10}{reasoning:<10}"
        )
    print("=" * 118)
    print("recall = P(flag planted defect | mutant)   FP-avoid = P(not flag intended | clean)")
    print(
        "engaged = P(examined the baited path | clean)  — low engaged ⇒ FP-avoid is laziness, not skill"
    )
    print(
        "precision = TP/(TP+FP)   SNR = true-flags : false-flags   $/defect-found = total $ / defects found"
    )
    print("usage: reported=provider counts, estimated=tokenized fallback (undercounts CLI turns)")
    print(
        "reasoning FLOOR* = a codex agent contributed; its reasoning tokens are unbilled here ⇒ cost is a LOWER BOUND"
    )
    print(
        "judge = claude-code/sonnet (different family from codex agents — non-circular), BLIND to the label kind/summary"
    )

    # ---- Verdict: does any multi-agent config beat single, per dollar? -------
    if "single" in recalls:
        base_r, base_c = recalls["single"], costs.get("single", 0.0)
        print("\nVERDICT vs single baseline (the win condition: ≥ recall at ≤ cost):")
        for cfg in sorted(recalls):
            if cfg == "single":
                continue
            r = recalls[cfg]
            better = "↑" if r.p > base_r.p else ("=" if r.p == base_r.p else "↓")
            sig = "DISTINGUISHABLE" if disjoint(r, base_r) else "not distinguishable (CIs overlap)"
            cmul = (costs.get(cfg, 0.0) / base_c) if base_c else float("inf")
            print(f"  {cfg:<26} recall {better} ({sig}); cost {cmul:.1f}x single")
        if all(not disjoint(recalls[c], base_r) for c in recalls if c != "single"):
            print(
                "  ⇒ No multi-agent config is statistically distinguishable from single. "
                "Either the task is non-discriminative (single already aces it) or N is too small."
            )


async def main(report_only: bool) -> None:
    kinds = _task_kinds()
    if report_only and SCORES.exists():
        scored = [ScoredResult(**s) for s in json.loads(SCORES.read_text())]
    else:
        scored = await score_all()
    report(scored, kinds)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="aggregate cached scores, no judging")
    args = ap.parse_args()
    asyncio.run(main(args.report))
