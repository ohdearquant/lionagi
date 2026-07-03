"""Aggregate saved steering results into the committed provider-by-arm table.

    uv run python benchmarks/orchestration/score_steering.py            # full-N report
    uv run python benchmarks/orchestration/score_steering.py --smoke    # labels the table SMOKE

Reads every results/steering/*.json (written by run_steering.py) and writes
suites/steering/adherence_table.{md,json} — the committed evidence artifact
ADR-0088 requires before Mode B can be scheduled.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suites.steering.report import write_report  # noqa: E402
from suites.steering.runner import SteerRunResult  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results" / "steering"
REPORT_DIR = Path(__file__).resolve().parent / "suites" / "steering"


def load_results() -> list[SteerRunResult]:
    results = []
    for f in sorted(RESULTS.glob("*.json")):
        raw = json.loads(f.read_text())
        results.append(SteerRunResult(**raw))
    return results


def main(smoke: bool) -> None:
    results = load_results()
    if not results:
        print(f"No results found in {RESULTS}. Run run_steering.py first.")
        return
    write_report(results, REPORT_DIR, smoke=smoke)
    print((REPORT_DIR / "adherence_table.md").read_text())
    print(f"\nWrote {REPORT_DIR / 'adherence_table.md'} and adherence_table.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="label the table SMOKE, not evidence")
    args = ap.parse_args()
    main(args.smoke)
