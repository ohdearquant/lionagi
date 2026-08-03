# `/api/schedules/` in-process serialisation benchmark

Date: 2026-08-03.  Ref measured: `07c1e3241`.  Every measured database was
generated under `bench/schedules_serialisation/artifacts/`; this work did not
open, read, copy, or write the normal LIONAGI state directory.

## Verdict

**The defensible serialisation point is the three queries serialised *within
each request* (`list_schedules` → `count_schedule_runs_batch` →
`schedule_run_streaks`), not a shared engine, a shared aiosqlite worker, or
Python row-to-mapping materialisation across requests.**  The dominant phase
in every synthetic burst was the awaited `schedule_run_streaks` SQL execution.
Under an eight-request diagnostic burst its SQL execute intervals overlapped
eight-deep on eight distinct aiosqlite workers.

This does not identify the production daemon's multi-second stalls: the
host's in-phase load was exceptionally high and the synthetic file is 1.19
GiB, not the production 7.3 GiB.  It does establish that the captured
`Result.all()` stack frame alone is not evidence that synchronous mapping is
the multi-second queue.

| Candidate | Verdict | Discriminating result |
| --- | --- | --- |
| A. Per-request read-only engine construction/disposal | **Present, but not the serialisation point** | Source creates/disposes an engine per route call. In 15 alternating small/large lifecycle pairs, total median was 7.61 ms (138.6 MiB) vs 7.24 ms (1.19 GiB), with no monotonic file-size trend. This is overhead on every request, not a cross-request mutex. |
| B. One shared aiosqlite worker thread | **Refuted for these route calls** | Eight concurrent real `list_schedules` calls created 8 engines, 8 driver connections, and 8 different aiosqlite worker thread IDs. Instrumented SQL execute intervals had maximum overlap 8/8, whereas a shared-worker explanation predicts one connection/thread and no overlapping execute intervals. |
| C. Event-loop `Result.all()`/mapping materialisation | **Refuted as the multi-second mechanism in this workload** | The exact streak SQL returned 3,200 rows. Its all-row consume/mapping median was 1.31 ms (small) and 1.70 ms (large); in eight-way route bursts the largest route `ResultInternal._allrows` interval was 25.5 ms. Awaited SQL, not the synchronous post-result conversion, was 1.0–1.8 s per request under the loaded host. |

The only deterministic source-level serial sequence is visible in
`list_schedules`: it awaits the schedule list, count aggregate, and streak
query one after another.  `_read()` contains no application lock.  The
read-only engine is made inside the request's `StateDB` context and disposed
when it exits, so it is neither reused nor shared by separate HTTP requests.

## What was measured

The real service function was called directly in one Python process:
`lionagi.studio.services.schedules.list_schedules`.  The route handler only
wraps this function's return value, so this retains the database path under
test without introducing an HTTP server/client scheduler as a confounder.

The schedule UI refreshes every 30 seconds, but it makes **one**
`/api/schedules/` request before starting its `Promise.all` fan of
`/api/schedules/{id}/runs` calls.  Thus the honest production fan-out for this
specific route is N=1.  I measured N=1 as the endpoint baseline and used
N=2/N=8 as controlled, simultaneous route-call bursts to falsify B and C;
the later per-schedule run-history fan is not mislabelled as this route.

Each synthetic store contained 64 schedules and 50,000 `schedule_runs` rows;
the streak query returns at most 50 rows per schedule (3,200 rows here).
Additional realistic assistant-message JSON bodies supplied the file bulk.

| Fixture | Exact file size | Schedules | `schedule_runs` | `messages` |
| --- | ---: | ---: | ---: | ---: |
| small | 145,375,232 B (138.6 MiB) | 64 | 50,000 | 1,366 |
| large | 1,276,502,016 B (1.19 GiB) | 64 | 50,000 | 12,800 |

For every burst the runner recorded request wall time, request phase times,
SQL execute intervals, connection/worker identities, a 2 ms event-loop
heartbeat, and `getloadavg()` samples inside the timed phase.  It also ran an
exact-SQL first-row/all-rows probe, and an alternating 15-pair engine
lifecycle probe (`construct → open → connect/SELECT 1 → dispose`).

## Discriminating measurements

### A — engine lifecycle and file size

`StateDB.open()`'s read-only branch calls `make_readonly_engine()` and returns;
actual connection creation is lazy, so the lifecycle probe deliberately
included `SELECT 1` before disposal.

| 15 interleaved samples per file | 138.6 MiB median | 1.19 GiB median |
| --- | ---: | ---: |
| Python/SQLAlchemy engine construction | 0.189 ms | 0.100 ms |
| `open()` | 0.488 ms | 0.204 ms |
| first checkout/connection | 3.384 ms | 3.507 ms |
| `SELECT 1` | 1.591 ms | 2.322 ms |
| dispose | 0.593 ms | 0.547 ms |
| total | 7.605 ms | 7.245 ms |

**Pre-registered discriminator:** a file-size-dependent engine cost would
produce a consistent higher large-file lifecycle total in alternating pairs.
It did not.  The large-file 95th percentile (38.7 ms) and small-file 95th
percentile (31.6 ms) are noisy host outliers, not a size trend.  The
production path still pays this per-request lifecycle, so it remains a
reasonable optimization candidate, but this measurement does not support it
as a 2.5–11.3 s serial queue.

### B — separate connections versus one aiosqlite funnel

For three N=8 bursts, every burst observed **8 unique engines / 8 distinct
driver connections / 8 distinct aiosqlite worker-thread IDs**.  The maximum
number of overlapping DBAPI SQL execute intervals was **8** in all three
bursts.  Separate threads and overlapping intervals are the opposite outcome
from a single aiosqlite connection funnel.

The request's own three statements remain sequential because the service
awaits each method before issuing the next.  That is an intra-request sequence,
not a connection-wide or process-wide serialisation lock.

### C — first row, all rows, and event-loop heartbeat

The exact window-function SQL used by `schedule_run_streaks` was run twice per
replicate: first-row consumption versus all-row consumption.  `await
conn.execute(...)` includes the database/driver phase; the following consume
time is the synchronous `mappings().all()` phase in question.

| Exact streak query, n=3 | 138.6 MiB median | 1.19 GiB median |
| --- | ---: | ---: |
| time through first row | 188.9 ms | 240.4 ms |
| time through all 3,200 rows | 190.5 ms | 241.4 ms |
| synchronous all-row consume | 1.312 ms | 1.696 ms |

The N=8 route bursts made the separation still clearer.  Per-request median
`schedule_run_streaks` DBAPI execute time was 1.02/1.12/1.45 s across the
small-store bursts and 1.82/1.52/1.55 s across large-store bursts; the largest
instrumented synchronous `_allrows` interval was 18.9 ms (small) and 25.5 ms
(large).  Therefore an observed slow stack inside `Result.all()` can include
time attributable to result fetching/driver scheduling, but the mapping loop
itself is not seconds long in this realistic row-count shape.

The heartbeat did not show a 1–2 s gap matching these short mapping intervals.
Its p95/max jitter in the N=8 bursts was 0.76–135.7 ms / 3.4–186.2 ms (small)
and 8.4–53.0 ms / 27.8–115.7 ms (large).  The isolated 25.5 ms mapper maximum
is bounded below the 2.5 s production minimum and cannot explain the reported
11.3 s tail.

## Load and reproducibility limits

This host was not quiet.  The N=8 overlap run began at load average
`[95.65, 87.51, 51.34]`; samples within its six timed store phases ranged from
`[94.23, 87.35, 51.49]` through `[100.94, 89.02, 52.50]`.  Earlier replicated
runs ranged roughly 105–118 on the one-minute metric.  These values are
included in the raw JSON for every phase.

Consequently, absolute wall-clock results have CV well above the requested
20% threshold (for example, N=8 burst wall time ranged 1.51–2.41 s small and
2.38–2.52 s large).  I do **not** claim a statistically significant latency
difference, p-value, or effect size.  The mechanistic conclusions above rely
on identity/overlap and phase-boundary observations, which remain
discriminating despite the noisy host.  A quiet-host repeat with the 7.3 GiB
synthetic-size variant is required before attributing the production tail to
SQLite compute, page-cache pressure, or another daemon workload.

## Code-path basis

- `lionagi/studio/services/schedules.py:416-426` opens a fresh `StateDB` and
  awaits the three queries in sequence.
- `lionagi/state/db.py:729-743` makes a read-only engine for that context and
  disposes it on exit; `lionagi/state/db.py:764-767` uses no read lock.
- `lionagi/state/engine.py:280-308` constructs the read-only aiosqlite engine.
- `lionagi/state/db.py:3924-3925` is the streak query's
  `mappings().all()` materialisation site.
- `apps/studio/frontend/src/components/schedules/data.ts:77-99` documents the
  30-second refresh and proves the run-history `Promise.all` begins only after
  the one schedule-list response.

## Reproduction

All commands use `uv run` and only explicit fixture paths:

```sh
uv sync --all-extras
uv run bench/schedules_serialisation/grow_store.py \
  --output bench/schedules_serialisation/artifacts/small.db \
  --message-mib 128 \
  --metadata bench/schedules_serialisation/artifacts/small-store.json
uv run bench/schedules_serialisation/grow_store.py \
  --output bench/schedules_serialisation/artifacts/large.db \
  --message-mib 1200 \
  --metadata bench/schedules_serialisation/artifacts/large-store.json
uv run bench/schedules_serialisation/run_benchmark.py \
  --stores bench/schedules_serialisation/artifacts/small.db \
           bench/schedules_serialisation/artifacts/large.db \
  --output bench/schedules_serialisation/artifacts/results.json
uv run bench/schedules_serialisation/compare_engine_sizes.py \
  --small bench/schedules_serialisation/artifacts/small.db \
  --large bench/schedules_serialisation/artifacts/large.db \
  --output bench/schedules_serialisation/artifacts/engine-size-pairs.json
```

Raw files from this run are intentionally ignored under
`bench/schedules_serialisation/artifacts/`:
`small-store.json`, `large-store.json`, `retry-results.json`,
`large-3-results.json`, `overlap-results.json`, and
`engine-size-pairs.json`.
