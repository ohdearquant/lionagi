"""Write a pilot run's summary.json from its results and its reading.

`results.jsonl` holds each leg's requests and what its task's check decided, as
`run_pilot.py` wrote it. `reading.jsonl` holds the reading of each answer
against its task's source line, one row per leg: `id`, `loop`, `check` (the
check's result, which must agree with `results.jsonl`), `reading` (`right` or
`wrong`) and the source line the reading used at `6fa69b19` and at `208bcc08`.
The totals and `d4_rule_by_checks` are `run_pilot.summarize` on the results;
`d4_rule_by_substance` applies the same rule to the reading, which is the one
ADR-0132 D4 decides by.

    python summarize.py runs/r5            # write runs/r5/summary.json
    python summarize.py runs/r5 --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from run_pilot import summarize

LOOPS = ["react", "lndl"]


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def build_summary(run_dir: Path) -> dict:
    results = _rows(run_dir / "results.jsonl")
    reading = _rows(run_dir / "reading.jsonl")
    read_by_key = {}
    for row in reading:
        key = (row["id"], row["loop"])
        if key in read_by_key:
            raise ValueError(f"{run_dir.name}: two readings for {key}")
        if row["reading"] not in ("right", "wrong"):
            raise ValueError(f"{run_dir.name}: reading {row['reading']!r} for {key}")
        read_by_key[key] = row
    result_keys = {(r["id"], r["loop"]) for r in results}
    if len(result_keys) != len(results) or result_keys != set(read_by_key):
        raise ValueError(f"{run_dir.name}: the reading does not cover each leg exactly once")
    for r in results:
        check = "accepted" if r["accepted"] else "rejected"
        if read_by_key[(r["id"], r["loop"])]["check"] != check:
            raise ValueError(
                f"{run_dir.name}: {r['id']} {r['loop']}: results say the check {check} it, "
                "the reading row says otherwise"
            )

    summary = summarize(sorted(results, key=lambda r: (r["id"], r["loop"])), LOOPS)
    summary["d4_rule_by_checks"] = summary.pop("d4_rule")
    right = {
        loop: sum(row["reading"] == "right" for row in reading if row["loop"] == loop)
        for loop in LOOPS
    }
    per_accepted = {
        loop: round(summary[loop]["requests"] / right[loop], 3) if right[loop] else None
        for loop in LOOPS
    }
    summary["d4_rule_by_substance"] = {
        "react_accepted": right["react"],
        "lndl_accepted": right["lndl"],
        "react_requests_per_accepted": per_accepted["react"],
        "lndl_requests_per_accepted": per_accepted["lndl"],
        "lndl_fewer_requests_per_accepted": (
            per_accepted["lndl"] is not None
            and per_accepted["react"] is not None
            and per_accepted["lndl"] < per_accepted["react"]
        ),
        "lndl_accepts_no_fewer": right["lndl"] >= right["react"],
    }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    text = json.dumps(build_summary(run_dir), indent=2) + "\n"
    target = run_dir / "summary.json"
    if args.check:
        if target.read_text() != text:
            print(f"{target} differs from what summarize.py computes", file=sys.stderr)
            return 1
        print(f"{target} matches")
        return 0
    target.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
