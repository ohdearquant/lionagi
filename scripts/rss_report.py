#!/usr/bin/env python3
"""Summarize per-worker peak-RSS logs written by tests/conftest.py.

Usage: rss_report.py <dir with rss-*.jsonl>

Prints, per xdist worker: the final peak RSS, and the tests that raised the
process high-water mark the most (nonzero delta_kb). When a CI worker dies
with "node down: Not properly terminated", the killed worker's log ends at
the moment of death.

Each test writes a "start" row before running and an "end" row after. A row
with phase="start" and no matching "end" row is a test that was IN FLIGHT
when its worker died — that is the actual crash suspect, distinct from the
last-COMPLETED test (which older logs from before this row pairing existed
could only approximate).
"""

import json
import sys
from pathlib import Path


def summarize(log_dir: Path) -> str:
    """Render the peak-RSS / in-flight-crash-suspect report for *log_dir* as text."""
    out: list[str] = []
    files = sorted(log_dir.glob("rss-*.jsonl"))
    if not files:
        return "no RSS logs found"

    all_deltas: list[dict] = []
    for f in files:
        # A worker killed mid-write (the exact failure mode this tool exists
        # for) leaves a truncated final line — skip malformed lines, keep the
        # complete rows.
        rows = []
        skipped = 0
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
        if skipped:
            out.append(f"({f.name}: skipped {skipped} malformed line(s) — truncated write)")
        if not rows:
            continue

        worker = rows[-1]["worker"]
        ends = [r for r in rows if r.get("phase") == "end"]
        starts = [r for r in rows if r.get("phase") == "start"]
        completed_tests = {r["test"] for r in ends}
        in_flight = [r for r in starts if r["test"] not in completed_tests]

        peak_mb = (ends[-1]["peak_kb"] if ends else rows[-1]["peak_kb"]) / 1024
        out.append(
            f"\n== {worker}: {len(ends)} tests completed, final peak RSS {peak_mb:.0f} MB =="
        )
        if in_flight:
            crash_suspect = in_flight[-1]
            out.append(
                f"   IN-FLIGHT AT DEATH (never reached an end row — crash suspect): "
                f"{crash_suspect['test']} (RSS at start: {crash_suspect['peak_kb'] / 1024:.0f} MB)"
            )
        elif ends:
            out.append(f"   last test completed: {ends[-1]['test']}")

        growers = sorted(
            (r for r in ends if r.get("delta_kb", 0) > 0),
            key=lambda r: r["delta_kb"],
            reverse=True,
        )
        for r in growers[:10]:
            out.append(f"   +{r['delta_kb'] / 1024:7.1f} MB  {r['test']}")
        all_deltas.extend(growers)

    out.append("\n== top 20 peak-raisers across all workers ==")
    for r in sorted(all_deltas, key=lambda r: r["delta_kb"], reverse=True)[:20]:
        out.append(f"   +{r['delta_kb'] / 1024:7.1f} MB  [{r['worker']}]  {r['test']}")
    return "\n".join(out)


def main() -> int:
    print(summarize(Path(sys.argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
