"""Write a pilot run's summary.json from its results and its reading.

`results.jsonl` holds each leg's requests and what its task's check decided, as
`run_pilot.py` wrote it. `reading.jsonl` holds the reading of each answer
against its task's source line, one row per leg: `id`, `loop`, `check` (the
check's result, which must agree with `results.jsonl`), `reading` (`right` or
`wrong`) and source lines under `source_<commit-prefix>` keys. The full
`read_tree_head` in `header.json` selects exactly one matching key, whose
prefix is 8 to 40 hexadecimal characters; that source must be one its task's
`truth` cites. Other trees' source keys do not substitute for it.
Both files must hold one row per task in the run's task file (named in its
`header.json` by a path inside this directory, and matching its `tasks_sha256`)
and per comparison loop. Recorded task or loop subsets are refused with the
`--only` or `--loops` cause; a refusal leaves the existing summary untouched.
The totals and `d4_rule_by_checks` are `run_pilot.summarize` on the results;
`d4_rule_by_substance` is `run_pilot.summarize` on the same rows with each leg
accepted as its reading says, which is the rule ADR-0132 D4 decides by.

    python summarize.py runs/r5            # write runs/r5/summary.json
    python summarize.py runs/r5 --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# The runner is loaded from its file beside this one, so the import holds however this file is
# started or imported.
_spec = importlib.util.spec_from_file_location("lndl_pilot_run_pilot", HERE / "run_pilot.py")
_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)
summarize = _runner.summarize

LOOPS = ["react", "lndl"]


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _task_truths(run_dir: Path, header: dict) -> dict[str, str]:
    # Read only from inside this directory: an absolute path, or one that resolves outside it
    # (through `..` or a link), names a file the pilot's record does not hold.
    tasks_path = (HERE / header["tasks_file"]).resolve()
    if (
        Path(header["tasks_file"]).is_absolute()
        or not tasks_path.is_relative_to(HERE)
        or not tasks_path.is_file()
    ):
        raise ValueError(f"{run_dir.name}: {header['tasks_file']} is not in {HERE.name}/")
    tasks_bytes = tasks_path.read_bytes()
    if hashlib.sha256(tasks_bytes).hexdigest() != header["tasks_sha256"]:
        raise ValueError(f"{run_dir.name}: {header['tasks_file']} is not the task set the run used")
    return {task["id"]: str(task["truth"]) for task in yaml.safe_load(tasks_bytes)["tasks"]}


def _cited(truth: str, source: str) -> bool:
    """Whether ``truth`` cites ``source`` (a full ``path:line``), by its path or, where the
    truth names that path elsewhere, by the file's name alone (``config.py:19``)."""
    if not re.fullmatch(r"[\w./-]+\.py:\d+(?:-\d+)?", source):
        return False
    path, _, lines = source.rpartition(":")
    if "/" not in path:
        return False
    forms = [source]
    if path in truth:
        forms.append(f"{Path(path).name}:{lines}")
    return any(re.search(rf"(?<![\w./-]){re.escape(form)}(?![\w-])", truth) for form in forms)


def _comparison_head(run_dir: Path, header: dict, task_count: int) -> str:
    head = header.get("read_tree_head")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError(f"{run_dir.name}: read_tree_head must be a full hexadecimal commit")
    count = header.get("task_count")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= task_count:
        raise ValueError(f"{run_dir.name}: task_count must be an integer from 1 to {task_count}")
    loops = header.get("loops")
    if (
        not isinstance(loops, list)
        or not loops
        or any(not isinstance(loop, str) or not loop for loop in loops)
        or len(set(loops)) != len(loops)
    ):
        raise ValueError(f"{run_dir.name}: loops must be a nonempty list of distinct loop names")
    causes = []
    if count < task_count:
        causes.append(f"--only recorded {count} of {task_count} tasks")
    if set(loops) != set(LOOPS):
        causes.append(f"--loops recorded {loops!r}, rather than both react and lndl")
    if causes:
        raise ValueError(f"{run_dir.name}: {'; '.join(causes)}; a whole comparison is required")
    return head


def _source_for_tree(run_dir: Path, key: tuple[str, str], row: dict, head: str) -> object:
    sources = [
        name
        for name in row
        if (match := re.fullmatch(r"source_([0-9a-f]{8,40})", name)) and head.startswith(match[1])
    ]
    leg = f"{key[0]} {key[1]}"
    if not sources:
        raise ValueError(f"{run_dir.name}: {leg}: no reading source for read_tree_head {head}")
    if len(sources) != 1:
        raise ValueError(
            f"{run_dir.name}: {leg}: ambiguous reading sources for read_tree_head {head}"
        )
    return row[sources[0]]


def build_summary(run_dir: Path) -> dict:
    header = json.loads((run_dir / "header.json").read_text())
    truths = _task_truths(run_dir, header)
    head = _comparison_head(run_dir, header, len(truths))
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
        source = _source_for_tree(run_dir, key, row, head)
        if not isinstance(source, str) or not _cited(truths[key[0]], source):
            raise ValueError(
                f"{run_dir.name}: {key[0]} {key[1]}: the reading cites "
                f"{source}, which the task's truth does not"
            )
    for r in results:
        check = "accepted" if r["accepted"] else "rejected"
        if read_by_key[(r["id"], r["loop"])]["check"] != check:
            raise ValueError(
                f"{run_dir.name}: {r['id']} {r['loop']}: results say the check {check} it, "
                "the reading row says otherwise"
            )

    results = sorted(results, key=lambda r: (r["id"], r["loop"]))
    summary = summarize(results, LOOPS)
    summary["d4_rule_by_checks"] = summary.pop("d4_rule")
    if summary["d4_rule_by_checks"] is None:
        # The request counts are incomplete for the reading too.
        summary["d4_rule_by_substance"] = None
        return summary
    by_reading = summarize(
        [
            dict(r, accepted=read_by_key[(r["id"], r["loop"])]["reading"] == "right")
            for r in results
        ],
        LOOPS,
    )
    summary["d4_rule_by_substance"] = {
        "react_accepted": by_reading["react"]["accepted"],
        "lndl_accepted": by_reading["lndl"]["accepted"],
        "react_requests_per_accepted": by_reading["react"]["requests_per_accepted"],
        "lndl_requests_per_accepted": by_reading["lndl"]["requests_per_accepted"],
        **by_reading["d4_rule"],
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
