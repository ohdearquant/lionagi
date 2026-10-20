"""Write a pilot run's summary.json from its results and its reading.

`results.jsonl` holds each leg's requests and what its task's check decided, as
`run_pilot.py` wrote it. `reading.jsonl` holds the reading of each answer
against its task's source line, one row per leg: `id`, `loop`, `check` (the
check's result, which must agree with `results.jsonl`), `reading` (`right` or
`wrong`) and the source line the reading used at `6fa69b19` and at `208bcc08`.
Both files must hold one row per task in the run's task file (named in its
`header.json` and matching its `tasks_sha256`) and per loop, and each reading's
`6fa69b19` source line must be one its task's `truth` cites.
The totals and `d4_rule_by_checks` are `run_pilot.summarize` on the results;
`d4_rule_by_substance` applies the same rule to the reading, which is the one
ADR-0132 D4 decides by.

    python summarize.py runs/r5            # write runs/r5/summary.json
    python summarize.py runs/r5 --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml
from run_pilot import HERE, summarize

LOOPS = ["react", "lndl"]


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _task_truths(run_dir: Path) -> dict[str, str]:
    header = json.loads((run_dir / "header.json").read_text())
    tasks_path = HERE / header["tasks_file"]
    if not tasks_path.is_file():
        raise ValueError(f"{run_dir.name}: {header['tasks_file']} is not in {HERE.name}/")
    tasks_bytes = tasks_path.read_bytes()
    if hashlib.sha256(tasks_bytes).hexdigest() != header["tasks_sha256"]:
        raise ValueError(f"{run_dir.name}: {header['tasks_file']} is not the task set the run used")
    return {task["id"]: str(task["truth"]) for task in yaml.safe_load(tasks_bytes)["tasks"]}


def _cited(truth: str, source: str) -> bool:
    """Whether ``truth`` cites ``source`` (``path:line``), by its path or, where the truth
    names that path elsewhere, by the file's name alone (``config.py:19``)."""
    if not re.fullmatch(r"[\w./-]+\.py:\d+(?:-\d+)?", source):
        return False
    path, _, lines = source.rpartition(":")
    forms = [source]
    if path in truth:
        forms.append(f"{Path(path).name}:{lines}")
    return any(re.search(rf"(?<![\w./-]){re.escape(form)}(?![\w-])", truth) for form in forms)


def build_summary(run_dir: Path) -> dict:
    truths = _task_truths(run_dir)
    expected = {(task_id, loop) for task_id in truths for loop in LOOPS}
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
    if len(result_keys) != len(results) or result_keys != expected:
        raise ValueError(f"{run_dir.name}: the results do not hold each task and loop exactly once")
    if set(read_by_key) != expected:
        raise ValueError(f"{run_dir.name}: the reading does not cover each leg exactly once")
    for key, row in read_by_key.items():
        if not _cited(truths[key[0]], row["source_6fa69b19"]):
            raise ValueError(
                f"{run_dir.name}: {key[0]} {key[1]}: the reading cites "
                f"{row['source_6fa69b19']}, which the task's truth does not"
            )
    for r in results:
        check = "accepted" if r["accepted"] else "rejected"
        if read_by_key[(r["id"], r["loop"])]["check"] != check:
            raise ValueError(
                f"{run_dir.name}: {r['id']} {r['loop']}: results say the check {check} it, "
                "the reading row says otherwise"
            )

    summary = summarize(sorted(results, key=lambda r: (r["id"], r["loop"])), LOOPS)
    summary["d4_rule_by_checks"] = summary.pop("d4_rule")
    if summary["d4_rule_by_checks"] is None:
        # The request counts are incomplete for the reading too.
        summary["d4_rule_by_substance"] = None
        return summary
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
