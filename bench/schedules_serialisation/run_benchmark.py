#!/usr/bin/env python3
"""Measure where concurrent schedule-list calls serialize in one process.

This runner deliberately invokes the checked-out
``lionagi.studio.services.schedules.list_schedules`` function.  It redirects
only that process to an explicit synthetic SQLite fixture and installs
short-lived measurement wrappers around the read-only StateDB engine.

Run through ``uv run``; see README.md for the fixture commands.
"""

# ruff: noqa: S608
# The only dynamic SQL shape below is a fixed-count bound-parameter list for
# the synthetic fixture; neither identifiers nor values come from input SQL.

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import platform
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import result as result_mod

from lionagi.config import settings
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionagi.studio.services import schedules as schedule_service

ACTIVE_REQUEST: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "schedbench_active_request", default=None
)
ACTIVE_TRACE: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
    "schedbench_active_trace", default=None
)


def monotonic_ms() -> float:
    return time.perf_counter_ns() / 1_000_000


def load_average() -> list[float] | None:
    try:
        return [float(x) for x in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * p
    lo = int(index)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0 if values else None,
    }


class Trace:
    def __init__(self) -> None:
        self.phases: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.connections: list[dict[str, Any]] = []
        self.sql: list[dict[str, Any]] = []
        self.allrows: list[dict[str, Any]] = []

    def phase(self, name: str, start_ms: float, end_ms: float, request: str | None = None) -> None:
        self.phases[name].append(
            {
                "request": request,
                "duration_ms": end_ms - start_ms,
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )


@contextmanager
def explicit_fixture(path: Path):
    """Point the actual service's normal StateDB resolution at *path* only."""
    old_url = settings.LIONAGI_STATE_DB_URL
    old_path = db_mod.DEFAULT_DB_PATH
    url = f"sqlite+aiosqlite:///{path.resolve()}"
    # AppSettings is intentionally frozen.  This process-local override is the
    # same test seam used when tests redirect a configured store; it avoids
    # reading the normal LIONAGI_HOME path altogether.
    object.__setattr__(settings, "LIONAGI_STATE_DB_URL", url)
    db_mod.DEFAULT_DB_PATH = path.resolve()
    try:
        yield
    finally:
        object.__setattr__(settings, "LIONAGI_STATE_DB_URL", old_url)
        db_mod.DEFAULT_DB_PATH = old_path


@contextmanager
def instrument(trace: Trace):
    """Install non-semantic timing hooks for one benchmark trial."""
    original_make_engine = db_mod.make_readonly_engine
    original_read = StateDB._read
    original_methods: dict[str, Callable[..., Awaitable[Any]]] = {}
    original_allrows = result_mod.ResultInternal._allrows

    def traced_make_engine(url: str, **kwargs: Any):
        request = ACTIVE_REQUEST.get()
        made_start = monotonic_ms()
        engine = original_make_engine(url, **kwargs)
        trace.phase("engine_factory", made_start, monotonic_ms(), request)

        @event.listens_for(engine.sync_engine, "connect")
        def on_connect(dbapi_connection, _connection_record) -> None:
            # AsyncAdapt_aiosqlite_connection exposes the underlying aiosqlite
            # connection as driver_connection.  Its private thread field is
            # recorded only as diagnostic evidence of connection separation.
            driver = getattr(dbapi_connection, "driver_connection", dbapi_connection)
            thread = getattr(driver, "_thread", None)
            trace.connections.append(
                {
                    "request": request,
                    "engine_id": id(engine),
                    "dbapi_connection_id": id(dbapi_connection),
                    "driver_connection_id": id(driver),
                    "worker_thread_ident": getattr(thread, "ident", None),
                }
            )

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ) -> None:
            context._schedbench_started_ms = monotonic_ms()

        @event.listens_for(engine.sync_engine, "after_cursor_execute")
        def after_cursor_execute(conn, cursor, statement, parameters, context, executemany) -> None:
            started = getattr(context, "_schedbench_started_ms", None)
            if started is not None:
                ended = monotonic_ms()
                trace.sql.append(
                    {
                        "request": request,
                        "duration_ms": ended - started,
                        "start_ms": started,
                        "end_ms": ended,
                        "statement_kind": classify_sql(statement),
                    }
                )

        return engine

    @asynccontextmanager
    async def traced_read(self: StateDB):
        request = ACTIVE_REQUEST.get()
        started = monotonic_ms()
        async with original_read(self) as conn:
            acquired = monotonic_ms()
            trace.phase("read_checkout", started, acquired, request)
            yield conn

    def make_method_wrapper(name: str, original: Callable[..., Awaitable[Any]]):
        async def wrapped(self: StateDB, *args: Any, **kwargs: Any) -> Any:
            started = monotonic_ms()
            try:
                return await original(self, *args, **kwargs)
            finally:
                trace.phase(name, started, monotonic_ms(), ACTIVE_REQUEST.get())

        return wrapped

    def traced_allrows(result):
        started = monotonic_ms()
        try:
            return original_allrows(result)
        finally:
            ended = monotonic_ms()
            trace.allrows.append(
                {
                    "request": ACTIVE_REQUEST.get(),
                    "duration_ms": ended - started,
                    "start_ms": started,
                    "end_ms": ended,
                }
            )

    db_mod.make_readonly_engine = traced_make_engine
    StateDB._read = traced_read
    for name in ("list_schedules", "count_schedule_runs_batch", "schedule_run_streaks"):
        original_methods[name] = getattr(StateDB, name)
        setattr(StateDB, name, make_method_wrapper(name, original_methods[name]))
    result_mod.ResultInternal._allrows = traced_allrows
    try:
        yield
    finally:
        db_mod.make_readonly_engine = original_make_engine
        StateDB._read = original_read
        for name, original in original_methods.items():
            setattr(StateDB, name, original)
        result_mod.ResultInternal._allrows = original_allrows


def classify_sql(statement: str) -> str:
    normalized = " ".join(statement.lower().split())
    if "row_number() over" in normalized:
        return "schedule_run_streaks"
    if "count(*)" in normalized and "schedule_runs" in normalized:
        return "count_schedule_runs_batch"
    if normalized.startswith("select * from schedules"):
        return "list_schedules"
    return "other"


def max_interval_overlap(entries: list[dict[str, Any]]) -> int:
    """Maximum simultaneously-open SQL execute intervals in one trace."""
    points: list[tuple[float, int]] = []
    for entry in entries:
        points.append((entry["start_ms"], 1))
        points.append((entry["end_ms"], -1))
    # Starts sort before ends at an exact tie, which is conservative for an
    # overlap witness and cannot invent a positive-length interval.
    active = 0
    maximum = 0
    for _, delta in sorted(points, key=lambda item: (item[0], -item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


async def heartbeat(
    stop: asyncio.Event, samples: list[float], *, interval_s: float = 0.002
) -> None:
    target = time.perf_counter()
    while not stop.is_set():
        target += interval_s
        await asyncio.sleep(max(target - time.perf_counter(), 0))
        samples.append(max(0.0, (time.perf_counter() - target) * 1_000))


async def load_sampler(stop: asyncio.Event, samples: list[list[float] | None]) -> None:
    while not stop.is_set():
        samples.append(load_average())
        await asyncio.sleep(0.005)


async def invoke_service(request: str) -> dict[str, Any]:
    token = ACTIVE_REQUEST.set(request)
    start = monotonic_ms()
    load_start = load_average()
    try:
        rows = await schedule_service.list_schedules()
        return {
            "request": request,
            "wall_ms": monotonic_ms() - start,
            "rows": len(rows),
            "load_start": load_start,
            "load_end": load_average(),
        }
    finally:
        ACTIVE_REQUEST.reset(token)


async def run_burst(path: Path, *, fanout: int) -> dict[str, Any]:
    trace = Trace()
    stop = asyncio.Event()
    jitter: list[float] = []
    loads: list[list[float] | None] = []
    release = asyncio.Event()

    async def gated_request(i: int) -> dict[str, Any]:
        await release.wait()
        return await invoke_service(f"request-{i}")

    with explicit_fixture(path), instrument(trace):
        tasks = [asyncio.create_task(gated_request(i)) for i in range(fanout)]
        heartbeat_task = asyncio.create_task(heartbeat(stop, jitter))
        load_task = asyncio.create_task(load_sampler(stop, loads))
        # Both samplers are live before the timed window; this makes a blocked
        # loop visible as a delayed heartbeat rather than an absent sampler.
        await asyncio.sleep(0)
        burst_start = monotonic_ms()
        release.set()
        requests = await asyncio.gather(*tasks)
        burst_end = monotonic_ms()
        stop.set()
        await asyncio.gather(heartbeat_task, load_task)

    return {
        "fanout": fanout,
        "burst_wall_ms": burst_end - burst_start,
        "requests": requests,
        "heartbeat_jitter_ms": summary(jitter),
        "load_samples_inside_phase": [sample for sample in loads if sample is not None],
        "phases": {
            name: [entry["duration_ms"] for entry in entries]
            for name, entries in trace.phases.items()
        },
        "phase_entries": dict(trace.phases),
        "sql": trace.sql,
        "allrows": trace.allrows,
        "connections": trace.connections,
    }


async def engine_lifecycle_once(path: Path) -> dict[str, Any]:
    # This has a deliberate SELECT 1.  AsyncEngine construction is lazy; an
    # open-only number would not include aiosqlite connection creation.
    load_start = load_average()
    constructed = monotonic_ms()
    db = StateDB(path=path, readonly=True)
    made = monotonic_ms()
    try:
        opened_start = monotonic_ms()
        await db.open()
        opened = monotonic_ms()
        checkout_start = monotonic_ms()
        async with db._read() as conn:
            checked_out = monotonic_ms()
            await conn.execute(text("SELECT 1"))
        query_done = monotonic_ms()
    finally:
        dispose_start = monotonic_ms()
        await db.close()
        disposed = monotonic_ms()
    return {
        "construct_engine_ms": made - constructed,
        "open_engine_ms": opened - opened_start,
        "connect_checkout_ms": checked_out - checkout_start,
        "select_1_ms": query_done - checked_out,
        "dispose_engine_ms": disposed - dispose_start,
        "total_ms": disposed - constructed,
        "load_start": load_start,
        "load_end": load_average(),
    }


async def materialisation_probe_once(path: Path) -> dict[str, Any]:
    # Exact SQL used by StateDB.schedule_run_streaks.  The two fresh executions
    # distinguish awaited DB work / first row from synchronous all-row mapping.
    schedule_ids = [f"sched-{i:03d}" for i in range(64)]
    placeholders = ", ".join(f":id{i}" for i in range(len(schedule_ids)))
    sql = text(  # noqa: S608 -- placeholder names are generated from the fixed 64-id fixture.
        "SELECT schedule_id, status FROM ("
        " SELECT schedule_id, status, fired_at,"
        " ROW_NUMBER() OVER (PARTITION BY schedule_id ORDER BY fired_at DESC, id DESC) AS rn"
        f" FROM schedule_runs WHERE schedule_id IN ({placeholders}) AND chain_depth = 0"  # noqa: S608
        ") ranked WHERE rn <= 50 ORDER BY schedule_id, rn"
    )
    params = {f"id{i}": sid for i, sid in enumerate(schedule_ids)}

    async def fetch(mode: str) -> dict[str, Any]:
        db = StateDB(path=path, readonly=True)
        await db.open()
        try:
            async with db._read() as conn:
                started = monotonic_ms()
                result = await conn.execute(sql, params)
                execute_done = monotonic_ms()
                if mode == "first":
                    row = result.mappings().first()
                    consumed = 1 if row is not None else 0
                else:
                    rows = result.mappings().all()
                    consumed = len(rows)
                done = monotonic_ms()
            return {
                "execute_ms": execute_done - started,
                "consume_ms": done - execute_done,
                "to_result_ms": done - started,
                "rows": consumed,
            }
        finally:
            await db.close()

    return {"first_row": await fetch("first"), "all_rows": await fetch("all")}


async def warmup(path: Path) -> None:
    with explicit_fixture(path):
        await schedule_service.list_schedules()


def reduce_trial(trial: dict[str, Any]) -> dict[str, Any]:
    return {
        "burst_wall_ms": trial["burst_wall_ms"],
        "request_wall_ms": [item["wall_ms"] for item in trial["requests"]],
        "heartbeat_jitter_ms": trial["heartbeat_jitter_ms"],
        "phase_summary_ms": {name: summary(values) for name, values in trial["phases"].items()},
        "sql_summary_ms": {
            kind: summary(
                [entry["duration_ms"] for entry in trial["sql"] if entry["statement_kind"] == kind]
            )
            for kind in sorted({entry["statement_kind"] for entry in trial["sql"]})
        },
        "allrows_ms": summary([entry["duration_ms"] for entry in trial["allrows"]]),
        "max_concurrent_sql_executes": max_interval_overlap(trial["sql"]),
        "connection_count": len(trial["connections"]),
        "unique_driver_connections": len(
            {entry["driver_connection_id"] for entry in trial["connections"]}
        ),
        "unique_worker_threads": len(
            {
                entry["worker_thread_ident"]
                for entry in trial["connections"]
                if entry["worker_thread_ident"] is not None
            }
        ),
        "load_samples_inside_phase": trial["load_samples_inside_phase"],
    }


async def benchmark_store(path: Path, *, rounds: int, fanouts: list[int]) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    await warmup(path)
    lifecycle = [await engine_lifecycle_once(path) for _ in range(rounds)]
    materialisation = [await materialisation_probe_once(path) for _ in range(rounds)]
    bursts: dict[str, list[dict[str, Any]]] = {}
    for fanout in fanouts:
        trials = [await run_burst(path, fanout=fanout) for _ in range(rounds)]
        bursts[str(fanout)] = trials
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "engine_lifecycle": lifecycle,
        "engine_lifecycle_summary": {
            key: summary([item[key] for item in lifecycle])
            for key in (
                "construct_engine_ms",
                "open_engine_ms",
                "connect_checkout_ms",
                "select_1_ms",
                "dispose_engine_ms",
                "total_ms",
            )
        },
        "materialisation_probe": materialisation,
        "materialisation_summary": {
            "first_to_result_ms": summary(
                [item["first_row"]["to_result_ms"] for item in materialisation]
            ),
            "all_to_result_ms": summary(
                [item["all_rows"]["to_result_ms"] for item in materialisation]
            ),
            "all_consume_ms": summary([item["all_rows"]["consume_ms"] for item in materialisation]),
        },
        "bursts": bursts,
        "burst_summaries": {
            fanout: [reduce_trial(trial) for trial in trials] for fanout, trials in bursts.items()
        },
    }


async def amain(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "method": {
            "service_path": "lionagi.studio.services.schedules.list_schedules",
            "route_equivalence": "list_schedules_route is a one-line call to this function",
            "frontend_fanout": 1,
            "diagnostic_fanouts": args.fanouts,
            "rounds": args.rounds,
            "heartbeat_interval_ms": 2,
            "source_note": "The frontend waits for listSchedules before Promise.all of per-schedule /runs calls; those later calls do not invoke this service.",
        },
        "machine": {
            "python": sys.version,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "initial_load_average": load_average(),
        },
        "stores": [
            await benchmark_store(path, rounds=args.rounds, fanouts=args.fanouts)
            for path in args.stores
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stores", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--fanouts", type=int, nargs="+", default=[1, 2, 8])
    args = parser.parse_args()
    if args.rounds < 3:
        raise SystemExit("--rounds must be at least 3 for replicated measurements")
    if any(fanout < 1 for fanout in args.fanouts):
        raise SystemExit("--fanouts values must be positive")
    args.stores = [path.resolve() for path in args.stores]
    result = asyncio.run(amain(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    store_summaries = [
        {
            "path": store["path"],
            "bytes": store["bytes"],
            "engine_total_median_ms": store["engine_lifecycle_summary"]["total_ms"]["median"],
            "streak_all_consume_median_ms": store["materialisation_summary"]["all_consume_ms"][
                "median"
            ],
        }
        for store in result["stores"]
    ]
    print(json.dumps({"output": str(args.output), "stores": store_summaries}, indent=2))


if __name__ == "__main__":
    main()
