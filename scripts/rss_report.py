#!/usr/bin/env python3
"""Summarize per-worker peak-RSS logs written by tests/conftest.py.

Usage: rss_report.py <dir with rss-*.jsonl>

Prints, per xdist worker: the final peak RSS, and the tests that raised the
process high-water mark the most (nonzero delta_kb). When a CI worker dies
with "node down: Not properly terminated", the killed worker's log ends at
the moment of death — its last lines and biggest deltas point at the culprit.
"""

import json
import sys
from pathlib import Path


def main() -> int:
    log_dir = Path(sys.argv[1])
    files = sorted(log_dir.glob("rss-*.jsonl"))
    if not files:
        print("no RSS logs found")
        return 0

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
            print(f"({f.name}: skipped {skipped} malformed line(s) — truncated write)")
        if not rows:
            continue
        worker = rows[-1]["worker"]
        peak_mb = rows[-1]["peak_kb"] / 1024
        print(f"\n== {worker}: {len(rows)} tests, final peak RSS {peak_mb:.0f} MB ==")
        print(f"   last test logged: {rows[-1]['test']}")
        growers = sorted(
            (r for r in rows if r["delta_kb"] > 0), key=lambda r: r["delta_kb"], reverse=True
        )
        for r in growers[:10]:
            print(f"   +{r['delta_kb'] / 1024:7.1f} MB  {r['test']}")
        all_deltas.extend(growers)

    print("\n== top 20 peak-raisers across all workers ==")
    for r in sorted(all_deltas, key=lambda r: r["delta_kb"], reverse=True)[:20]:
        print(f"   +{r['delta_kb'] / 1024:7.1f} MB  [{r['worker']}]  {r['test']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
