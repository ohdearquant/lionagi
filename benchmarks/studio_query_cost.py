# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Measure what a studio list endpoint costs on a large store.

Answers three questions with numbers rather than inspection:

1. What plan does the runs listing actually get, and is any part of it bounded
   by the caller's page size?
2. How long does the endpoint take end to end, split into SQL and Python?
3. While it runs, can other requests still be served -- or does one expensive
   read make the whole daemon unresponsive?

Point it at a synthetic store built by ``tests.fixtures.synthetic_state_db``::

    uv run python benchmarks/studio_query_cost.py /tmp/big.db
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import statistics
import time
from pathlib import Path

RUNS_LIST_SQL = """
SELECT s.id, s.updated_at,
       COUNT(DISTINCT b.id) AS branch_count,
       COALESCE(SUM(json_array_length(p.collection)), 0) AS message_count
FROM sessions s
LEFT JOIN branches b ON b.session_id = s.id
LEFT JOIN progressions p ON p.id = b.progression_id
GROUP BY s.id
ORDER BY s.updated_at DESC
"""

PAGE_SQL = """
SELECT s.id, s.updated_at,
       COUNT(DISTINCT b.id) AS branch_count,
       COALESCE(SUM(json_array_length(p.collection)), 0) AS message_count
FROM sessions s
LEFT JOIN branches b ON b.session_id = s.id
LEFT JOIN progressions p ON p.id = b.progression_id
GROUP BY s.id
ORDER BY s.updated_at DESC
LIMIT 200
"""

WINDOWED_SQL = """
WITH page AS (
    SELECT id, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 200
)
SELECT page.id, page.updated_at,
       COUNT(DISTINCT b.id) AS branch_count,
       COALESCE(SUM(json_array_length(p.collection)), 0) AS message_count
FROM page
LEFT JOIN branches b ON b.session_id = page.id
LEFT JOIN progressions p ON p.id = b.progression_id
GROUP BY page.id
ORDER BY page.updated_at DESC
"""


def explain(db: sqlite3.Connection, sql: str) -> str:
    rows = db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return "\n".join("    " + " ".join(str(c) for c in r[3:]) for r in rows)


def time_sql(path: Path, sql: str, repeats: int = 3) -> float:
    samples = []
    for _ in range(repeats):
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        started = time.perf_counter()
        db.execute(sql).fetchall()
        samples.append(time.perf_counter() - started)
        db.close()
    return min(samples)


async def measure_endpoints(path: Path, concurrency: int) -> None:
    import httpx

    from lionagi.studio.app import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        for label, url, params in (
            ("GET /api/runs/?per_page=200", "/api/runs/", {"per_page": 200}),
            ("GET /api/sessions/", "/api/sessions/", {}),
            ("GET /api/admin/health", "/api/admin/health", {}),
            ("GET /api/runs/projects", "/api/runs/projects", {}),
        ):
            started = time.perf_counter()
            resp = await client.get(url, params=params)
            elapsed = time.perf_counter() - started
            body = resp.json() if resp.status_code == 200 else {}
            served = len(body.get("runs", body.get("sessions", []))) or ""
            print(f"\n{label} -> {resp.status_code} in {elapsed:.2f}s  rows={served}")

        # Does one expensive read make the rest of the daemon unresponsive?
        # Probe with a cheap read that still touches the store, not with a
        # stateless route -- a stateless route answers a different question.
        for probe_label, probe_url in (
            ("/health (stateless)", "/health"),
            ("/api/runs/projects (store-backed)", "/api/runs/projects"),
        ):
            baseline = []
            for _ in range(10):
                t0 = time.perf_counter()
                await client.get(probe_url)
                baseline.append(time.perf_counter() - t0)

            latencies: list[float] = []
            done = asyncio.Event()

            async def poll(url: str, out: list[float], stop: asyncio.Event) -> None:
                while not stop.is_set():
                    t0 = time.perf_counter()
                    await client.get(url)
                    out.append(time.perf_counter() - t0)

            poller = asyncio.create_task(poll(probe_url, latencies, done))
            heavy = [client.get("/api/runs/", params={"per_page": 200}) for _ in range(concurrency)]
            t0 = time.perf_counter()
            await asyncio.gather(*heavy)
            heavy_elapsed = time.perf_counter() - t0
            done.set()
            await poller

            print(
                f"\n{probe_label} latency while {concurrency} concurrent "
                f"runs listings run ({heavy_elapsed:.2f}s total):"
            )
            print(f"    idle   median {statistics.median(baseline) * 1000:.1f} ms")
            if latencies:
                print(
                    f"    loaded median {statistics.median(latencies) * 1000:.1f} ms, "
                    f"max {max(latencies) * 1000:.0f} ms, n={len(latencies)}"
                )
            else:
                print("    loaded: NO probe completed while the listings ran")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--skip-endpoints", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)

    # Studio resolves the store from LIONAGI_HOME at import time, so the store
    # must be named state.db and the home must be set before lionagi loads.
    if args.path.name != "state.db":
        parser.error("store must be named state.db (studio resolves it from LIONAGI_HOME)")
    os.environ["LIONAGI_HOME"] = str(args.path.parent)

    size_mb = args.path.stat().st_size / 1024 / 1024
    db = sqlite3.connect(f"file:{args.path}?mode=ro", uri=True)
    counts = {
        t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("sessions", "branches", "progressions", "messages")
    }
    print(f"store: {args.path} ({size_mb:.0f} MB)")
    for name, n in counts.items():
        print(f"    {name}: {n:,}")

    for label, sql in (
        ("runs listing, as shipped (no LIMIT)", RUNS_LIST_SQL),
        ("same query with LIMIT 200 appended", PAGE_SQL),
        ("page selected first, then joined", WINDOWED_SQL),
    ):
        print(f"\n{label}:")
        print(explain(db, sql))
        print(f"    wall: {time_sql(args.path, sql):.3f}s")
    db.close()

    if not args.skip_endpoints:
        asyncio.run(measure_endpoints(args.path, args.concurrency))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
