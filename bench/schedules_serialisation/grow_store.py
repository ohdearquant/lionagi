#!/usr/bin/env python3
"""Build an isolated, deterministic StateDB fixture for schedule-read benchmarks.

Run only through ``uv run``.  The generated database is intentionally not
version-controlled; it contains a schedule-shaped workload plus message bodies
that make the SQLite file large without borrowing any production data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import time
from pathlib import Path

SCHEDULE_COUNT = 64
SCHEDULE_RUN_COUNT = 50_000
MESSAGE_BYTES = 96 * 1024
BATCH_SIZE = 500


async def create_schema(path: Path) -> None:
    # Import after the command has selected an explicit fixture path.  This
    # opens only ``path`` and creates the schema used by the checked-out code.
    from lionagi.state.db import StateDB

    async with StateDB(path=path):
        pass


def rows_for_messages(target_mib: int) -> int:
    return math.ceil(target_mib * 1024 * 1024 / MESSAGE_BYTES)


def grow(path: Path, *, message_mib: int) -> dict[str, object]:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing fixture: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(create_schema(path))

    now = time.time()
    message_count = rows_for_messages(message_mib)
    # This is deliberately JSON, moderately sized, and repeated.  SQLite does
    # not compress it; using a constant keeps fixture generation deterministic.
    body = json.dumps({"kind": "assistant_response", "text": "x" * (MESSAGE_BYTES - 64)})
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")

        schedules = [
            (
                f"sched-{i:03d}",
                f"synthetic-schedule-{i:03d}",
                1,
                "interval" if i % 3 else "github_poll",
                300,
                "agent",
                "synthetic benchmark schedule",
                now - 86_400 + i,
                now - 43_200 + i,
            )
            for i in range(SCHEDULE_COUNT)
        ]
        conn.executemany(
            """
            INSERT INTO schedules
              (id, name, enabled, trigger_type, interval_sec, action_kind,
               action_prompt, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            schedules,
        )

        run_sql = """
            INSERT INTO schedule_runs
              (id, schedule_id, trigger_context, action_kind, action_args,
               status, chain_depth, fired_at, ended_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        run_rows: list[tuple[object, ...]] = []
        for i in range(SCHEDULE_RUN_COUNT):
            schedule_id = f"sched-{i % SCHEDULE_COUNT:03d}"
            # A mixed terminal history exercises both aggregate count and the
            # streak query's status loop without adding artificial wide rows.
            status = "failed" if i % 17 in (0, 1) else "completed"
            fired_at = now - i * 7
            run_rows.append(
                (
                    f"run-{i:06d}",
                    schedule_id,
                    '{"origin":"synthetic"}',
                    "agent",
                    '{"prompt":"synthetic"}',
                    status,
                    0,
                    fired_at,
                    fired_at + 1,
                    fired_at,
                    fired_at + 1,
                )
            )
            if len(run_rows) == BATCH_SIZE:
                conn.executemany(run_sql, run_rows)
                run_rows.clear()
        if run_rows:
            conn.executemany(run_sql, run_rows)

        message_sql = """
            INSERT INTO messages
              (id, created_at, content, sender, recipient, channel, role, lion_class)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        message_rows: list[tuple[object, ...]] = []
        for i in range(message_count):
            message_rows.append(
                (
                    f"message-{i:06d}",
                    now - i,
                    body,
                    "synthetic-agent",
                    "synthetic-user",
                    "bench",
                    "assistant",
                    3,
                )
            )
            if len(message_rows) == BATCH_SIZE:
                conn.executemany(message_sql, message_rows)
                message_rows.clear()
        if message_rows:
            conn.executemany(message_sql, message_rows)
        conn.commit()
        # A normal SQLite checkpoint-free read-only open needs no WAL file.
        conn.execute("VACUUM")
        conn.commit()
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        counts = {
            "schedules": conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0],
            "schedule_runs": conn.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        }
    finally:
        conn.close()

    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "page_bytes": page_count * page_size,
        "message_target_mib": message_mib,
        "message_rows": message_count,
        "schedule_rows": SCHEDULE_COUNT,
        "schedule_run_rows": SCHEDULE_RUN_COUNT,
        "table_counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--message-mib", type=int, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    if args.message_mib < 1:
        raise SystemExit("--message-mib must be positive")
    result = grow(args.output.resolve(), message_mib=args.message_mib)
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
