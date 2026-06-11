# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""DB operation helpers for `li state` maintenance subcommands.

Covers: ls, stats, checkpoint, vacuum, prune, doctor.

All public names are re-exported from ``cli/state.py`` so existing import
paths remain stable.
"""

from __future__ import annotations


def _format_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


async def _list_sessions(*, limit: int = 50, status: str | None = None) -> None:
    import time

    from lionagi.state.db import StateDB

    async with StateDB() as db:
        if status:
            cur = await db.db.execute(
                "SELECT id, name, status, updated_at FROM sessions "
                "WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = await db.db.execute(
                "SELECT id, name, status, updated_at FROM sessions "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()

        if not rows:
            print("(no sessions in state.db)")
            return

        header = (
            f"{'ID':<36}  {'NAME':<16}  {'STATUS':<10}  "
            f"{'BRANCHES':>8}  {'MESSAGES':>8}  {'UPDATED':<20}"
        )
        print(header)
        print("-" * len(header))
        for row in rows:
            sid = row["id"]
            name = (row["name"] or "")[:16]
            sstat = (row["status"] or "")[:10]
            updated = row["updated_at"]
            updated_str = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated)) if updated else ""
            )

            branch_cur = await db.db.execute(
                "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?", (sid,)
            )
            bc = (await branch_cur.fetchone())["n"]

            prog_cur = await db.db.execute(
                "SELECT progression_id FROM sessions WHERE id = ?", (sid,)
            )
            prog_row = await prog_cur.fetchone()
            msg_count = 0
            if prog_row and prog_row["progression_id"]:
                prog_data = await db.get_progression(prog_row["progression_id"])
                msg_count = len(prog_data)

            print(f"{sid:<36}  {name:<16}  {sstat:<10}  {bc:>8}  {msg_count:>8}  {updated_str:<20}")


async def _print_stats() -> None:
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    db_path = DEFAULT_DB_PATH
    db_size = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0

    print(f"state.db path:   {db_path}")
    print(f"state.db size:   {_format_bytes(db_size)}")
    print(f"state.db-wal:    {_format_bytes(wal_size)}")
    print()

    if not db_path.exists():
        print("(no state.db yet — first run will create it)")
        return

    async with StateDB() as db:
        print("Row counts:")
        for table in (
            "messages",
            "progressions",
            "sessions",
            "branches",
            "definitions",
            "shows",
            "plays",
        ):
            cur = await db.db.execute(
                f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608
            )
            row = await cur.fetchone()
            print(f"  {table:<14} {row['n']:>10}")
        print()

        cur = await db.db.execute(
            "SELECT COALESCE(status, '(null)') AS s, COUNT(*) AS n "
            "FROM sessions GROUP BY status ORDER BY n DESC"
        )
        print("Sessions by status:")
        for row in await cur.fetchall():
            print(f"  {row['s']:<14} {row['n']:>10}")
        print()

        print("PRAGMAs:")
        for pragma in (
            "journal_mode",
            "wal_autocheckpoint",
            "busy_timeout",
            "synchronous",
            "foreign_keys",
        ):
            cur = await db.db.execute(f"PRAGMA {pragma}")
            row = await cur.fetchone()
            val = row[0] if row else "?"
            print(f"  {pragma:<22} {val}")


async def _checkpoint(mode: str) -> str:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        cur = await db.db.execute(f"PRAGMA wal_checkpoint({mode})")
        row = await cur.fetchone()
        if not row:
            return "(no result)"
        return f"busy={row[0]}, log_pages={row[1]}, checkpointed={row[2]}"


async def _vacuum() -> None:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        await db.db.execute("VACUUM")
        await db.db.commit()


async def _prune(
    *,
    keep_days: int,
    keep_n: int,
    dry_run: bool,
) -> dict[str, int]:
    import time as _time

    from lionagi.state.db import StateDB

    cutoff = _time.time() - (keep_days * 86400)

    async with StateDB() as db:
        cur = await db.db.execute(
            """SELECT id FROM sessions
               WHERE id NOT IN (
                 SELECT id FROM sessions
                 ORDER BY updated_at DESC LIMIT ?
               )
               AND (updated_at < ? OR updated_at IS NULL)""",
            (keep_n, cutoff),
        )
        rows = await cur.fetchall()
        victim_ids = [r["id"] for r in rows]

        if not victim_ids:
            return {"sessions": 0, "branches": 0, "messages": 0}

        placeholders = ",".join("?" * len(victim_ids))
        cur = await db.db.execute(
            f"SELECT COUNT(*) AS n FROM branches "  # noqa: S608
            f"WHERE session_id IN ({placeholders})",
            victim_ids,
        )
        branch_count = (await cur.fetchone())["n"]

        cur = await db.db.execute("SELECT COUNT(*) AS n FROM messages")
        msgs_before = (await cur.fetchone())["n"]

        if dry_run:
            return {
                "sessions": len(victim_ids),
                "branches": branch_count,
                "messages": 0,  # can't preview without doing the delete
            }

        await db.db.execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})",  # noqa: S608
            victim_ids,
        )
        await db.db.commit()

        await db.db.execute(
            """DELETE FROM messages
               WHERE id NOT IN (
                 SELECT value FROM progressions, json_each(progressions.collection)
               )"""
        )
        await db.db.commit()

        cur = await db.db.execute("SELECT COUNT(*) AS n FROM messages")
        msgs_after = (await cur.fetchone())["n"]

        return {
            "sessions": len(victim_ids),
            "branches": branch_count,
            "messages": msgs_before - msgs_after,
        }


async def _doctor(
    *,
    stale_hours: int,
    dry_run: bool,
    new_status: str = "aborted",
) -> dict[str, int]:
    """Sweep sessions stuck at status='running' older than stale_hours."""
    import time as _time

    from lionagi.state.db import StateDB

    cutoff = _time.time() - (stale_hours * 3600)

    async with StateDB() as db:
        cur = await db.db.execute("SELECT id, started_at FROM sessions WHERE status = 'running'")
        rows = await cur.fetchall()
        total = len(rows)
        victims: list[str] = []
        skipped = 0
        for row in rows:
            started = row["started_at"]
            if started is None or started < cutoff:
                victims.append(row["id"])
            else:
                skipped += 1

        swept_count = 0
        if dry_run:
            swept_count = len(victims)
        elif victims:
            # Re-assert status='running' in the UPDATE to avoid race with
            # sessions that finish between select and update.
            placeholders = ",".join("?" * len(victims))
            params = [new_status, _time.time(), cutoff, *victims]
            cur = await db.db.execute(
                f"UPDATE sessions SET status = ?, ended_at = ? "  # noqa: S608
                f"WHERE status = 'running' "
                f"  AND (started_at IS NULL OR started_at < ?) "
                f"  AND id IN ({placeholders})",
                params,
            )
            swept_count = cur.rowcount or 0
            await db.db.commit()

        return {"running": total, "swept": swept_count, "skipped": skipped}
