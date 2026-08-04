# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li state` — inspect and migrate lionagi state.db."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from ._runs import RUNS_ROOT
from ._util import EXIT_CODE_BY_STATUS

__all__ = [
    # import helpers
    "RUNS_ROOT",
    "_mtime_as_float",
    "_msg_from_collection_entry",
    "_import_runs",
    "_STATUS_MAP",
    "_EXIT_CODE_STATUS_MAP",
    "_derive_import_status",
    "_derive_timestamps",
    "_import_one_run",
    "_import_teams",
    # ops helpers
    "_format_bytes",
    "_list_sessions",
    "_print_stats",
    "_collect_message_breakdown",
    "_checkpoint",
    "_vacuum",
    "_prune",
    "_prune_candidates",
    "_doctor",
    # CLI entrypoints
    "add_state_subparser",
    "run_state",
    "machine_result",
]


# ── run/team import helpers ───────────────────────────────────────────────────


def _mtime_as_float(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        import time

        return time.time()


def _msg_from_collection_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a branch-collection message dict to the DB insert shape."""
    return {
        "id": raw["id"],
        "created_at": raw["created_at"],
        "node_metadata": raw.get("metadata"),
        "content": raw.get("content", {}),
        "embedding": raw.get("embedding"),
        "sender": raw.get("sender"),
        "recipient": raw.get("recipient"),
        "channel": raw.get("channel"),
        "role": raw["role"],
    }


_STATUS_MAP = {
    "running": "running",
    "completed": "completed",
    "completed_empty": "completed_empty",
    "failed": "failed",
    "aborted": "aborted",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "success": "completed",
    "error": "failed",
    "timeout": "timed_out",
}

_EXIT_CODE_STATUS_MAP = {v: k for k, v in EXIT_CODE_BY_STATUS.items()}


def _derive_import_status(manifest: dict[str, Any]) -> str:
    """Derive session status from run.json fields."""
    raw_status = manifest.get("status")
    if raw_status is not None:
        return _STATUS_MAP.get(str(raw_status).lower(), "completed")

    exit_code = manifest.get("exit_code")
    if exit_code is not None:
        return _EXIT_CODE_STATUS_MAP.get(exit_code, "failed")

    return "completed"


def _derive_timestamps(
    manifest: dict[str, Any],
    run_dir: Path,
) -> tuple[float, float]:
    """Return (started_at, ended_at) as floats; falls back to fs timestamps."""
    import time as _time

    started_at = manifest.get("started_at")
    ended_at = manifest.get("ended_at")

    try:
        stat = run_dir.stat()
        fs_ctime = stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_ctime
        fs_mtime = stat.st_mtime
    except OSError:
        now = _time.time()
        fs_ctime = now
        fs_mtime = now

    if started_at is None:
        started_at = fs_ctime
    if ended_at is None:
        ended_at = fs_mtime

    if isinstance(started_at, str):
        import datetime

        try:
            started_at = datetime.datetime.fromisoformat(started_at).timestamp()
        except ValueError:
            started_at = fs_ctime
    if isinstance(ended_at, str):
        import datetime

        try:
            ended_at = datetime.datetime.fromisoformat(ended_at).timestamp()
        except ValueError:
            ended_at = fs_mtime

    return float(started_at), float(ended_at)


async def _import_one_run(
    db: Any,
    run_id: str,
    run_dir: Path,
    manifest: dict[str, Any],
) -> tuple[int, int, int]:
    created_at = _mtime_as_float(run_dir)
    session_name = manifest.get("kind") or "agent"

    status = _derive_import_status(manifest)
    started_at, ended_at = _derive_timestamps(manifest, run_dir)

    session_prog_id = str(uuid.uuid4())
    await db.create_progression(session_prog_id)

    raw_kind = (manifest.get("kind") or "").lower()
    legacy_kind_map = {
        "agent": "agent",
        "play": "play",
        "flow": "flow",
        "fanout": "fanout",
    }
    invocation_kind = legacy_kind_map.get(raw_kind)

    artifacts_path = manifest.get("artifact_root") or manifest.get("artifacts_path")
    if artifacts_path is None:
        candidate = run_dir / "artifacts"
        if candidate.exists():
            artifacts_path = str(candidate)

    await db.create_session(
        {
            "id": run_id,
            "created_at": created_at,
            "node_metadata": None,
            "name": session_name,
            "user": None,
            "progression_id": session_prog_id,
            "first_msg_id": None,
            "last_msg_id": None,
            # Enriched provenance — written so imported rows are
            # queryable by the same fields live runs use.
            "invocation_kind": invocation_kind,
            "playbook_name": manifest.get("playbook_name") or manifest.get("playbook"),
            "agent_name": manifest.get("agent_name") or manifest.get("agent"),
            "artifacts_path": artifacts_path,
            "source_kind": "imported_fs",
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
        }
    )

    branches_dir = run_dir / "branches"

    branch_files: list[Path] = []
    if branches_dir.exists():
        branch_files = list(branches_dir.glob("*.json"))

    total_branches = 0
    total_messages = 0
    session_msg_ids: list[str] = []

    for branch_file in sorted(branch_files, key=lambda p: p.stat().st_mtime):
        try:
            branch_data = json.loads(branch_file.read_text())
        except Exception as exc:
            print(f"    [warn] {branch_file.name}: failed to read — {exc}")
            continue

        branch_id = branch_data.get("id") or branch_file.stem
        branch_created_at = branch_data.get("created_at") or _mtime_as_float(branch_file)

        messages_pile = branch_data.get("messages", {})
        raw_collection: list[dict] = messages_pile.get("collections", [])
        progression_info = messages_pile.get("progression", {})
        order: list[str] = progression_info.get("order", [])

        by_id: dict[str, dict] = {m["id"]: m for m in raw_collection if "id" in m}
        if order:
            ordered_msgs = [by_id[mid] for mid in order if mid in by_id]
        else:
            ordered_msgs = raw_collection

        system_msg_id: str | None = None
        for raw_msg in ordered_msgs:
            if raw_msg.get("role") == "system":
                system_msg_id = raw_msg["id"]
                break

        branch_msg_ids: list[str] = []
        for raw_msg in ordered_msgs:
            msg = _msg_from_collection_entry(raw_msg)
            await db.insert_message(msg)
            branch_msg_ids.append(msg["id"])
            total_messages += 1

        # Create branch progression with ordered message IDs.
        branch_prog_id = str(uuid.uuid4())
        await db.create_progression(branch_prog_id, branch_msg_ids)

        manifest_branch_meta = {}
        for mb in manifest.get("branches", []):
            if mb.get("id") == branch_id:
                manifest_branch_meta = mb
                break

        node_meta: dict[str, Any] = {}
        provider = manifest_branch_meta.get("provider") or manifest.get("provider")
        model = manifest_branch_meta.get("model") or manifest.get("model")
        if provider:
            node_meta["provider"] = provider
        if model:
            node_meta["model"] = model
        branch_name = manifest_branch_meta.get("name") or manifest.get("kind")

        await db.create_branch(
            {
                "id": branch_id,
                "created_at": branch_created_at,
                "node_metadata": node_meta or None,
                "user": branch_data.get("user"),
                "name": branch_name,
                "session_id": run_id,
                "progression_id": branch_prog_id,
                "system_msg_id": system_msg_id,
            }
        )

        session_msg_ids.extend(branch_msg_ids)
        total_branches += 1

    if session_msg_ids:
        # Through the DB operation rather than a local UPDATE: the ids come from
        # an imported file, and a hand-rolled json.dumps here would hand the
        # driver a finished string that no serializer gets to check.
        await db.set_progression(session_prog_id, session_msg_ids)
        await db.update_session(
            run_id,
            first_msg_id=session_msg_ids[0],
            last_msg_id=session_msg_ids[-1],
        )

    print(f"  imported {run_id}: {total_branches} branch(es), {total_messages} message(s)")
    return 1, total_branches, total_messages


# ── DB maintenance helpers ────────────────────────────────────────────────────


def _format_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


async def _collect_sessions(db: Any, *, limit: int, status: str | None) -> list[dict[str, Any]]:
    """Sessions newest-first, each with its branch and message counts.

    The one query the listing runs, so the printed table and the machine result
    report the same rows and the same counts rather than two readings of the
    store taken by two pieces of code.
    """
    from sqlalchemy import text

    async with db._read() as conn:
        if status:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, name, status, updated_at FROM sessions "
                            "WHERE status = :st ORDER BY updated_at DESC LIMIT :lim"
                        ),
                        {"st": status, "lim": limit},
                    )
                )
                .mappings()
                .all()
            )
        else:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, name, status, updated_at FROM sessions "
                            "ORDER BY updated_at DESC LIMIT :lim"
                        ),
                        {"lim": limit},
                    )
                )
                .mappings()
                .all()
            )

    collected: list[dict[str, Any]] = []
    for row in rows:
        sid = row["id"]
        async with db._read() as conn:
            bc = (
                (
                    await conn.execute(
                        text("SELECT COUNT(*) AS n FROM branches WHERE session_id = :sid"),
                        {"sid": sid},
                    )
                )
                .mappings()
                .first()["n"]
            )

            prog_row = (
                (
                    await conn.execute(
                        text("SELECT progression_id FROM sessions WHERE id = :id"),
                        {"id": sid},
                    )
                )
                .mappings()
                .first()
            )
        msg_count = 0
        if prog_row and prog_row["progression_id"]:
            prog_data = await db.get_progression(prog_row["progression_id"])
            msg_count = len(prog_data)
        collected.append(
            {
                "id": sid,
                "name": row["name"],
                "status": row["status"],
                "updated_at": row["updated_at"],
                "branch_count": bc,
                "message_count": msg_count,
            }
        )
    return collected


async def _list_sessions(*, limit: int = 50, status: str | None = None) -> None:
    import time

    from lionagi.state.db import StateDB

    async with StateDB() as db:
        rows = await _collect_sessions(db, limit=limit, status=status)

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
            name = (row["name"] or "")[:16]
            sstat = (row["status"] or "")[:10]
            updated = row["updated_at"]
            updated_str = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated)) if updated else ""
            )
            print(
                f"{row['id']:<36}  {name:<16}  {sstat:<10}  "
                f"{row['branch_count']:>8}  {row['message_count']:>8}  {updated_str:<20}"
            )


# The tables a size report counts, and the pragmas it reads, in the order both
# are printed. Named once so the machine result and the printout cannot come to
# describe different databases.
_STATS_TABLES = (
    "messages",
    "progressions",
    "sessions",
    "branches",
    "definitions",
    "shows",
    "plays",
)

_STATS_PRAGMAS = (
    "journal_mode",
    "wal_autocheckpoint",
    "busy_timeout",
    "synchronous",
    "foreign_keys",
)

# Age thresholds the message breakdown reports, in days. Every one is printed
# whether or not a row falls in it, because a bucket reading zero is itself the
# answer: it says a prune keeping that many days can free nothing.
_MESSAGE_AGE_DAYS = (7, 14, 30, 90)


def _db_sizes() -> dict[str, Any]:
    """Bytes on disk for the configured store, when the configured store is a file.

    Size and WAL are questions about a file. A server or in-memory URL still has
    rows to report; it just has no bytes on disk to report them next to, and
    answering with the default path's size there would describe a file nothing
    is reading. ``is_file`` says which case this is, so a null size means "not
    answerable" and can never be read as "empty".
    """
    from lionagi.state.db import StateDB, state_db_file
    from lionagi.state.engine import mask_credentials, mask_db_url

    db_path = state_db_file()
    if db_path is None:
        # A store with no file behind it is a server URL, so this name is the
        # one that most obviously carries a password. `is_file` is already
        # false here, so nothing downstream treats it as a path to open.
        return {
            "path": mask_db_url(StateDB().url),
            "is_file": False,
            "exists": False,
            "size_bytes": None,
            "wal_size_bytes": None,
        }
    wal_path = db_path.with_name(db_path.name + "-wal")
    return {
        # And so is this one, less obviously. A store URL with no scheme is
        # read as a filesystem path, credential and all, which puts a password
        # in the name of a real file and sends it down the branch that looks
        # like it has nothing to hide.
        "path": mask_credentials(str(db_path)),
        "is_file": True,
        "exists": db_path.exists(),
        "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "wal_size_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
    }


async def _collect_message_breakdown(db: Any) -> dict[str, Any]:
    """Messages by role and by age — the two axes a retention decision is made on.

    A total row count cannot say whether a retention setting is able to reclaim
    anything. A store whose oldest message is newer than the prune's keep-window
    frees nothing at any ``--keep-days`` the prune accepts, and reports success
    while doing it. The age histogram is what makes that readable before the
    prune runs rather than after it has run and changed nothing.

    Counts only. Summing content size is the one query here no index can serve
    (57s against 1.68M rows, versus under 2s for everything else), and the
    command that reclaims that content reports the size of the exact population
    it is about to touch — which is the same number, measured where the decision
    is actually taken.
    """
    import time as _time

    from sqlalchemy import text

    now = _time.time()

    async with db._read() as conn:
        role_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT role AS r, COUNT(*) AS n FROM messages GROUP BY role ORDER BY n DESC"
                    )
                )
            )
            .mappings()
            .all()
        )

    by_role: list[dict[str, Any]] = [{"role": r["r"], "count": r["n"]} for r in role_rows]

    by_age: list[dict[str, Any]] = []
    for days in _MESSAGE_AGE_DAYS:
        async with db._read() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT COUNT(*) AS n FROM messages WHERE created_at < :cutoff"),
                        {"cutoff": now - days * 86400},
                    )
                )
                .mappings()
                .first()
            )
        by_age.append({"older_than_days": days, "count": row["n"]})

    async with db._read() as conn:
        row = (
            (await conn.execute(text("SELECT MIN(created_at) AS oldest FROM messages")))
            .mappings()
            .first()
        )
    oldest = row["oldest"] if row else None
    # None on an empty table, and reported as None rather than as an age of
    # zero: no messages and messages all written this instant are different
    # states, and only one of them says a prune has nothing to reach.
    oldest_age_days = None if oldest is None else (now - oldest) / 86400.0

    return {
        "messages_by_role": by_role,
        "messages_by_age": by_age,
        "oldest_message_age_days": oldest_age_days,
    }


async def _collect_stats(db: Any) -> dict[str, Any]:
    """Row counts, the message breakdown, the session status distribution, and the pragmas."""
    from sqlalchemy import text

    counts: dict[str, int] = {}
    for table in _STATS_TABLES:
        async with db._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
                    )
                )
                .mappings()
                .first()
            )
        counts[table] = row["n"]

    async with db._read() as conn:
        status_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT status AS s, COUNT(*) AS n "
                        "FROM sessions GROUP BY status ORDER BY n DESC"
                    )
                )
            )
            .mappings()
            .all()
        )

    pragmas: dict[str, Any] = {}
    for pragma in _STATS_PRAGMAS:
        async with db._read() as conn:
            row = (await conn.execute(text(f"PRAGMA {pragma}"))).first()
        pragmas[pragma] = row[0] if row else None

    breakdown = await _collect_message_breakdown(db)

    return {
        "row_counts": counts,
        # A list of pairs, not an object: a session whose status was never
        # recorded is a null key, and the printout's "(null)" placeholder for it
        # would be indistinguishable from a status literally spelled that way.
        "sessions_by_status": [{"status": r["s"], "count": r["n"]} for r in status_rows],
        "pragmas": pragmas,
        **breakdown,
    }


async def _print_stats() -> None:
    from lionagi.state.db import StateDB

    sizes = _db_sizes()

    print(f"state.db path:   {sizes['path']}")
    if sizes["is_file"]:
        print(f"state.db size:   {_format_bytes(sizes['size_bytes'])}")
        print(f"state.db-wal:    {_format_bytes(sizes['wal_size_bytes'])}")
    else:
        print("state.db size:   (not a local file)")
    print()

    if sizes["is_file"] and not sizes["exists"]:
        print("(no state.db yet — first run will create it)")
        return

    async with StateDB() as db:
        collected = await _collect_stats(db)

    print("Row counts:")
    for table, n in collected["row_counts"].items():
        print(f"  {table:<14} {n:>10}")
    print()

    print("Messages by role:")
    for entry in collected["messages_by_role"]:
        label = "(null)" if entry["role"] is None else entry["role"]
        print(f"  {label:<14} {entry['count']:>10}")
    if not collected["messages_by_role"]:
        print("  (none)")
    print()

    print("Messages by age:")
    for entry in collected["messages_by_age"]:
        print(f"  older than {entry['older_than_days']:>3}d {entry['count']:>10}")
    oldest = collected["oldest_message_age_days"]
    if oldest is None:
        print("  oldest          (no messages)")
    else:
        print(f"  oldest       {oldest:>10.1f}d")
        # Said about MESSAGES, deliberately, and not about the prune as a whole.
        # Message lifetime and session lifetime are independent here: `li state
        # prune` selects by session age and then frees only messages nothing
        # surviving still references, so it can delete thousands of old sessions
        # and free no message rows at all. Since messages hold nearly all the
        # bytes, "sessions deleted" is not a claim about space.
        print("  (messages only — prune selects by SESSION age, see prune's own output)")
    print()

    print("Sessions by status:")
    for entry in collected["sessions_by_status"]:
        label = "(null)" if entry["status"] is None else entry["status"]
        print(f"  {label:<14} {entry['count']:>10}")
    print()

    print("PRAGMAs:")
    for pragma, value in collected["pragmas"].items():
        print(f"  {pragma:<22} {'?' if value is None else value}")


async def _checkpoint(mode: str) -> str:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        row = await db.checkpoint(mode)
        if row is None:
            return "(not applicable on this backend)"
        return f"busy={row[0]}, log_pages={row[1]}, checkpointed={row[2]}"


async def _vacuum() -> None:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        await db.vacuum()


# Which sessions the prune deletes, written once. The prune selects ids with it
# and the check below counts what it still selects afterwards; two copies of this
# predicate drifting apart would make the pair agree while describing different
# populations, which is worse than having no check.
_PRUNE_VICTIMS = (
    "FROM sessions "
    "WHERE id NOT IN ("
    "  SELECT id FROM sessions ORDER BY updated_at DESC LIMIT :keep_n"
    ") AND (updated_at < :cutoff OR updated_at IS NULL)"
)


async def _prune_candidates(*, keep_days: int, keep_n: int) -> dict[str, Any]:
    """Recount what the prune's own predicate selects, and how old the oldest session is.

    Printed beside the prune's result so the command cannot report an outcome
    nothing contradicts. A single number is undetectable when it is wrong; a
    pair is not. After a real run this must read zero, and after a preview it
    must equal the count the preview reported, so either disagreement is visible
    without anyone querying anything.

    The age is here because of the failure this was built for: a keep-window
    wider than the whole store selects nothing, so the prune deletes nothing and
    says so in the words of a success. Beside the oldest session's age that
    sentence stops being ambiguous.
    """
    import time as _time

    from sqlalchemy import text

    from lionagi.state.db import StateDB

    cutoff = _time.time() - (keep_days * 86400)
    now = _time.time()

    async with StateDB() as db:
        async with db._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(f"SELECT COUNT(*) AS n {_PRUNE_VICTIMS}"),  # noqa: S608
                        {"keep_n": keep_n, "cutoff": cutoff},
                    )
                )
                .mappings()
                .first()
            )
            remaining = row["n"]
            row = (
                (await conn.execute(text("SELECT MIN(updated_at) AS oldest FROM sessions")))
                .mappings()
                .first()
            )
    oldest = row["oldest"] if row else None
    return {
        "candidates": remaining,
        # None rather than an age of zero on an empty store: no sessions and
        # sessions all touched this instant are different states.
        "oldest_session_age_days": None if oldest is None else (now - oldest) / 86400.0,
    }


class _PreviewOnlyError(Exception):
    """Signals the end of a preview so its transaction unwinds instead of committing.

    A preview has to answer "how many rows would this free", and the only answer
    that cannot drift from the real one is the real one. The preview therefore
    runs the prune and then refuses to commit it, carrying the counts out with it.
    """

    def __init__(self, counts: dict[str, int]) -> None:
        super().__init__("preview complete")
        self.counts = counts


async def _prune(
    *,
    keep_days: int,
    keep_n: int,
    dry_run: bool,
) -> dict[str, int]:
    """Delete old sessions and the branches, progressions and messages they owned.

    ``dry_run`` runs exactly the same statements and then rolls the transaction
    back, so the reported counts are measurements of the prune rather than an
    estimate of it.
    """
    import time as _time

    from lionagi.state.db import StateDB

    cutoff = _time.time() - (keep_days * 86400)

    from sqlalchemy import text

    counts = {"sessions": 0, "branches": 0, "messages": 0}

    async with StateDB() as db:
        try:
            async with db._tx() as conn:
                rows = (
                    (
                        await conn.execute(
                            text(f"SELECT id {_PRUNE_VICTIMS}"),  # noqa: S608
                            {"keep_n": keep_n, "cutoff": cutoff},
                        )
                    )
                    .mappings()
                    .all()
                )
                victim_ids = [r["id"] for r in rows]

                if not victim_ids:
                    raise _PreviewOnlyError(counts)

                placeholders = ",".join(f":v{i}" for i in range(len(victim_ids)))
                id_params = {f"v{i}": vid for i, vid in enumerate(victim_ids)}

                branch_count = (
                    (
                        await conn.execute(
                            text(
                                f"SELECT COUNT(*) AS n FROM branches "  # noqa: S608
                                f"WHERE session_id IN ({placeholders})"
                            ),
                            id_params,
                        )
                    )
                    .mappings()
                    .first()["n"]
                )

                # progressions carry no ownership edge back to their session/
                # branch, so they're collected here (while owners are still
                # readable) into a scratch table rather than a parameter list,
                # to avoid the bound-parameter ceiling on a large prune.
                await conn.execute(text("DROP TABLE IF EXISTS prune_orphan_progressions"))
                await conn.execute(
                    text("CREATE TEMPORARY TABLE prune_orphan_progressions (id TEXT PRIMARY KEY)")
                )
                await conn.execute(
                    text(
                        "INSERT INTO prune_orphan_progressions (id) "  # noqa: S608
                        f"SELECT progression_id FROM sessions WHERE id IN ({placeholders}) "
                        "  AND progression_id IS NOT NULL "
                        "UNION "
                        f"SELECT progression_id FROM branches WHERE session_id IN ({placeholders})"
                        "  AND progression_id IS NOT NULL"
                    ),
                    id_params,
                )

                # Direct message references disappear with their owner and may
                # not also occur in a progression. Keep them as candidates
                # until survivor references can be checked after the cascade.
                await conn.execute(text("DROP TABLE IF EXISTS prune_direct_messages"))
                await conn.execute(
                    text("CREATE TEMPORARY TABLE prune_direct_messages (id TEXT PRIMARY KEY)")
                )
                await conn.execute(
                    text(
                        "INSERT INTO prune_direct_messages (id) "  # noqa: S608
                        f"SELECT first_msg_id FROM sessions WHERE id IN ({placeholders}) "
                        "  AND first_msg_id IS NOT NULL "
                        "UNION "
                        f"SELECT last_msg_id FROM sessions WHERE id IN ({placeholders}) "
                        "  AND last_msg_id IS NOT NULL "
                        "UNION "
                        f"SELECT system_msg_id FROM branches WHERE session_id IN ({placeholders}) "
                        "  AND system_msg_id IS NOT NULL"
                    ),
                    id_params,
                )

                await conn.execute(
                    text(
                        f"DELETE FROM sessions WHERE id IN ({placeholders})"  # noqa: S608
                    ),
                    id_params,
                )

                # Branches cascade with their session, so what is still
                # referenced now is what survives the prune. A progression a
                # survivor still points at is not an orphan, whoever else
                # pointed at it.
                await conn.execute(
                    text(
                        "DELETE FROM prune_orphan_progressions WHERE id IN ("
                        "  SELECT progression_id FROM sessions WHERE progression_id IS NOT NULL"
                        "  UNION"
                        "  SELECT progression_id FROM branches WHERE progression_id IS NOT NULL"
                        ")"
                    )
                )

                # Messages are addressed only through a progression's collection
                # or through one of the three id columns that name one directly,
                # so a message no surviving referent mentions is unreachable.
                # This runs while the orphaned progressions still exist, since
                # their collections are what says which messages are in play.
                deleted_messages = (
                    await conn.execute(
                        text(
                            "DELETE FROM messages WHERE id IN ("
                            "  SELECT value FROM progressions, json_each(progressions.collection)"
                            "  WHERE progressions.id IN (SELECT id FROM prune_orphan_progressions)"
                            "  UNION"
                            "  SELECT id FROM prune_direct_messages"
                            ") AND id NOT IN ("
                            "  SELECT value FROM progressions, json_each(progressions.collection)"
                            "  WHERE progressions.id NOT IN"
                            "    (SELECT id FROM prune_orphan_progressions)"
                            ") AND id NOT IN ("
                            "  SELECT first_msg_id FROM sessions WHERE first_msg_id IS NOT NULL"
                            "  UNION"
                            "  SELECT last_msg_id FROM sessions WHERE last_msg_id IS NOT NULL"
                            "  UNION"
                            "  SELECT system_msg_id FROM branches WHERE system_msg_id IS NOT NULL"
                            ")"
                        )
                    )
                ).rowcount

                await conn.execute(
                    text(
                        "DELETE FROM progressions "
                        "WHERE id IN (SELECT id FROM prune_orphan_progressions)"
                    )
                )
                await conn.execute(text("DROP TABLE IF EXISTS prune_orphan_progressions"))
                await conn.execute(text("DROP TABLE IF EXISTS prune_direct_messages"))

                counts = {
                    "sessions": len(victim_ids),
                    "branches": branch_count,
                    "messages": deleted_messages,
                }
                if dry_run:
                    raise _PreviewOnlyError(counts)
        except _PreviewOnlyError as preview:
            return preview.counts

    return counts


async def _doctor(
    *,
    stale_hours: int,
    dry_run: bool,
    new_status: str = "aborted",
) -> dict[str, int]:
    """Sweep sessions stuck at status='running' older than stale_hours."""
    import time as _time

    from lionagi.state.db import StateDB
    from lionagi.state.reasons import SessionReasons

    cutoff = _time.time() - (stale_hours * 3600)

    from sqlalchemy import text

    from lionagi.cli._util import pid_alive
    from lionagi.cli.kill import _check_pid_identity, _read_pid_from_entity

    async with StateDB() as db:
        async with db._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, started_at, node_metadata, artifacts_path "
                            "FROM sessions WHERE status = 'running'"
                        )
                    )
                )
                .mappings()
                .all()
            )
        total = len(rows)
        victims: list[str] = []
        skipped = 0
        for row in rows:
            started = row["started_at"]
            if started is not None and started >= cutoff:
                skipped += 1
                continue
            # Age answers "how long since this session first started", which is
            # the same question as "how long has this process been running" only
            # for a session that ran once. A branch picked up again keeps the
            # start time of the session while the process is new, so the command
            # asks the process itself before calling anything stuck.
            entity = dict(row)
            meta = entity.get("node_metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError:
                    meta = None
            entity["node_metadata"] = meta if isinstance(meta, dict) else None
            pid = _read_pid_from_entity(entity)
            if pid is not None and pid_alive(pid):
                # A live PID is not by itself proof: the OS can hand a dead
                # session's number to an unrelated process, which would protect
                # a genuinely stuck row for as long as that process lives. The
                # stale-kill sweep already answers this, so it answers it here
                # too rather than a second, weaker rule being written.
                raw_ct = (entity["node_metadata"] or {}).get("pid_create_time")
                try:
                    expected_create_time = float(raw_ct) if raw_ct is not None else None
                except (TypeError, ValueError):
                    expected_create_time = None
                verdict = _check_pid_identity(
                    pid,
                    "lionagi",
                    expected_session_id=row["id"],
                    expected_create_time=expected_create_time,
                )
                # "unverifiable" means the process could not be inspected, not
                # that it is gone; skipping it leaves a row for the next run
                # rather than reaping one out from under a worker. "zombie" is
                # the opposite: the process has exited and is only waiting to
                # be reaped, so the row is stale and belongs in the sweep.
                if verdict in ("ours", "unverifiable"):
                    skipped += 1
                    continue
            victims.append(row["id"])

        swept_count = 0
        if dry_run:
            swept_count = len(victims)
        else:
            # Per-row through the guarded write path (ADR-0035): update_status()
            # re-asserts the CAS and records a reason_code + audit row.
            for vid in victims:
                transitioned = await db.update_status(
                    "session",
                    vid,
                    new_status=new_status,
                    reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
                    reason_summary=f"doctor sweep: running longer than {stale_hours}h",
                    source="admin",
                    actor="doctor",
                    expected_statuses={"running"},
                    extra_fields={"ended_at": _time.time()},
                )
                if transitioned:
                    swept_count += 1

        return {"running": total, "swept": swept_count, "skipped": skipped}


# ── _import_runs / _import_teams ─────────────────────────────────────────────


async def _import_runs() -> dict[str, int]:
    """Scan RUNS_ROOT and import every run with a run.json manifest."""
    from lionagi.state.db import StateDB

    counts = {"sessions": 0, "branches": 0, "messages": 0, "skipped": 0, "errors": 0}

    if not RUNS_ROOT.exists():
        print(f"runs directory not found: {RUNS_ROOT}")
        return counts

    run_dirs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()]
    run_dirs.sort(key=lambda p: p.stat().st_mtime)

    print(f"scanning {len(run_dirs)} run directories in {RUNS_ROOT} ...")

    async with StateDB() as db:
        for run_dir in run_dirs:
            manifest_path = run_dir / "run.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception as exc:
                print(f"  [error] {run_dir.name}: failed to read run.json — {exc}")
                counts["errors"] += 1
                continue

            run_id = manifest.get("run_id") or run_dir.name

            existing = await db.get_session(run_id)
            if existing is not None:
                counts["skipped"] += 1
                continue

            try:
                session_count, branch_count, msg_count = await _import_one_run(
                    db, run_id, run_dir, manifest
                )
            except Exception as exc:
                print(f"  [error] {run_dir.name}: {exc}")
                counts["errors"] += 1
                continue

            counts["sessions"] += session_count
            counts["branches"] += branch_count
            counts["messages"] += msg_count

    return counts


async def _import_teams() -> dict[str, int]:
    """Backfill ~/.lionagi/teams/*.json into the teams + team_messages tables."""
    from sqlalchemy import JSON, bindparam, text

    from lionagi.state.db import StateDB

    from .team import read_team_json

    teams_dir = (RUNS_ROOT.parent / "teams").resolve()
    counts = {"teams": 0, "messages": 0, "skipped_teams": 0, "errors": 0}
    if not teams_dir.exists():
        return counts

    json_files = sorted(teams_dir.glob("*.json"))
    if not json_files:
        return counts

    async with StateDB() as db:
        for path in json_files:
            data = read_team_json(path)  # shared-flock read; None on torn/corrupt
            if data is None:
                counts["errors"] += 1
                continue
            team_id = data.get("id")
            if not team_id:
                counts["errors"] += 1
                continue

            async with db._read() as conn:
                existing = (
                    await conn.execute(
                        text("SELECT 1 FROM teams WHERE id = :id LIMIT 1"),
                        {"id": team_id},
                    )
                ).first()
            if existing is not None:
                counts["skipped_teams"] += 1
                continue

            members = data.get("members") or []
            created_at = _mtime_as_float(path)

            rows_to_insert: list[dict] = []
            msg_rows: list[dict] = []

            rows_to_insert.append(
                {
                    "id": team_id,
                    "name": data.get("name") or team_id,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "member_count": len(members),
                    "members": members,
                    "status": "active",
                }
            )
            counts["teams"] += 1

            for msg in data.get("messages") or []:
                msg_id = msg.get("id") or uuid.uuid4().hex[:12]
                to = msg.get("to") or []
                if isinstance(to, str):
                    recipient = to or "all"
                else:
                    recipient = "all" if to == ["*"] else ",".join(to) or "all"
                content = msg.get("content") or ""
                ts_raw = msg.get("timestamp")
                try:
                    from datetime import datetime

                    created = datetime.fromisoformat(ts_raw).timestamp()
                except (TypeError, ValueError):
                    created = created_at
                read_by = msg.get("read_by") or {}
                if isinstance(read_by, dict):
                    read_by_arr = sorted(read_by.keys())
                elif isinstance(read_by, list):
                    read_by_arr = list(read_by)
                else:
                    read_by_arr = []
                msg_rows.append(
                    {
                        "id": msg_id,
                        "team_id": team_id,
                        "created_at": created,
                        "sender": msg.get("from") or "_unknown",
                        "recipient": recipient,
                        "content": content,
                        "summary": (content[:200] + "…") if len(content) > 200 else None,
                        "read_by": read_by_arr,
                        "session_id": None,
                    }
                )
                counts["messages"] += 1

            # members and read_by are bound as JSON so the engine's serializer
            # encodes them; imported team files are outside data, and a
            # pre-serialized string would be handed to the driver unchecked.
            team_stmt = text(
                "INSERT INTO teams "
                "(id, name, created_at, updated_at, member_count, members, status) "
                "VALUES (:id, :name, :created_at, :updated_at, "
                ":member_count, :members, :status)"
            ).bindparams(bindparam("members", type_=JSON))
            msg_stmt = text(
                "INSERT INTO team_messages "
                "(id, team_id, created_at, sender, recipient, content, "
                "summary, read_by, session_id) "
                "VALUES (:id, :team_id, :created_at, :sender, :recipient, "
                ":content, :summary, :read_by, :session_id)"
            ).bindparams(bindparam("read_by", type_=JSON))

            async with db._tx() as conn:
                for row in rows_to_insert:
                    await conn.execute(team_stmt, row)
                for mrow in msg_rows:
                    await conn.execute(msg_stmt, mrow)

    return counts


# ── CLI parser + runner ───────────────────────────────────────────────────────


def add_state_subparser(subparsers: argparse._SubParsersAction) -> None:
    state = subparsers.add_parser(
        "state",
        help="Inspect and migrate lionagi state.db.",
        description="Manage the lionagi SQLite state database.",
    )
    state_sub = state.add_subparsers(dest="state_command", required=True)

    # li state import
    state_sub.add_parser(
        "import",
        help="Import all runs from ~/.lionagi/runs/ into state.db.",
        description=(
            "Scan ~/.lionagi/runs/ for run directories with run.json manifests "
            "and load their sessions, branches, and messages into state.db. "
            "Already-imported sessions are skipped (idempotent)."
        ),
    )

    # li state import-teams
    state_sub.add_parser(
        "import-teams",
        help="Backfill team JSON files (~/.lionagi/teams/*.json) into state.db.",
        description=(
            "Scan ~/.lionagi/teams/*.json and INSERT each team + its messages "
            "into the `teams` and `team_messages` tables. Idempotent: "
            "existing rows (matched by team id) are left alone. Run once after "
            "upgrading; the runtime can keep using JSON until the dual-write "
            "path ships."
        ),
    )

    # li state ls
    ls = state_sub.add_parser(
        "ls",
        help="List sessions in state.db.",
        description="Print a table of sessions stored in state.db.",
    )
    ls.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum sessions to print, newest first (default 50).",
    )
    ls.add_argument(
        "--status",
        default=None,
        help=(
            "Show only sessions in this status: running, completed, failed or aborted. "
            "Omit to list every status."
        ),
    )

    # li state stats — no flags, and the machine surface takes none either, so
    # its projected schema stays empty and the two cannot describe different
    # commands.
    state_sub.add_parser(
        "stats",
        help="Print DB/WAL size, row counts, message role/age breakdown, and lifecycle health.",
        description=(
            "Report state.db + state.db-wal sizes, per-table row counts, "
            "messages broken down by role and by age, session status "
            "distribution, and SQLite PRAGMAs (journal_mode, "
            "wal_autocheckpoint, busy_timeout). Use to spot growth and "
            "lock contention, and to check before pruning whether the "
            "keep-window can reach anything at all."
        ),
    )

    # li state checkpoint
    cp = state_sub.add_parser(
        "checkpoint",
        help="Force a WAL checkpoint (frees disk if no readers active).",
        description=(
            "Run PRAGMA wal_checkpoint(TRUNCATE|PASSIVE|RESTART|FULL). "
            "Default is TRUNCATE — most aggressive, frees the WAL file if "
            "no readers are active."
        ),
    )
    cp.add_argument(
        "--mode",
        default="TRUNCATE",
        choices=["PASSIVE", "FULL", "RESTART", "TRUNCATE"],
        help=(
            "How hard to push the WAL back into the database. TRUNCATE (default) frees the WAL "
            "file outright but needs no active readers; PASSIVE, FULL and RESTART give up "
            "completeness to avoid blocking. No run data is lost in any mode."
        ),
    )

    # li state vacuum
    state_sub.add_parser(
        "vacuum",
        help="Rebuild the DB file to reclaim free pages.",
        description=(
            "Run VACUUM — rebuilds the entire DB file, reclaiming pages "
            "freed by previous deletes. Holds an exclusive lock for the "
            "duration. Run after `li state prune`."
        ),
    )

    # li state prune
    prune = state_sub.add_parser(
        "prune",
        help="Delete old sessions (and their branches/messages).",
        description=(
            "Delete sessions older than --keep-days (default 30), keeping "
            "the most recent --keep-n (default 100). Foreign key cascades "
            "drop branches; messages are dropped if no other session "
            "references them via progression. Use --dry-run to preview.\n\n"
            "SESSIONS ARE THE AXIS, AND MESSAGES HOLD THE BYTES. A message a "
            "surviving progression still names is kept whatever its age, so a "
            "run can delete thousands of sessions and free no message rows. "
            "Read the message counts, not the session count, to judge whether "
            "this reclaimed space; `li state stats` shows both distributions.\n\n"
            "--dry-run is not a read. It runs the real deletes and rolls the "
            "transaction back, which is what makes its counts exact rather "
            "than estimated, and which means it takes the same write lock for "
            "the same duration as the real thing."
        ),
    )
    prune.add_argument(
        "--keep-days",
        type=int,
        default=30,
        help="Keep sessions updated within the last N days (default 30).",
    )
    prune.add_argument(
        "--keep-n",
        type=int,
        default=100,
        help="Always keep the N most recent sessions (default 100).",
    )
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD be deleted, but don't actually delete.",
    )

    # li state doctor — sweep stale 'running' sessions
    doctor = state_sub.add_parser(
        "doctor",
        help="Sweep sessions stuck at status='running' after a crash.",
        description=(
            "A SIGKILL or unclean exit between session-open and teardown "
            "leaves the session row at status='running' forever. This "
            "command resets such rows (older than --stale-hours, default "
            "24) to --new-status (default 'aborted'). Conservative: a "
            "session is swept only if its started_at is older than the "
            "threshold AND its recorded process is not running, so an "
            "actively-running CLI process is left alone even when the "
            "session it resumed started long ago. Use --dry-run first."
        ),
    )
    doctor.add_argument(
        "--stale-hours",
        type=int,
        default=24,
        help="Threshold in hours since started_at (default 24).",
    )
    doctor.add_argument(
        "--new-status",
        default="aborted",
        choices=["aborted", "failed"],
        help="Status to assign swept sessions (default 'aborted').",
    )
    doctor.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD be swept, but don't update rows.",
    )


def run_state(args: argparse.Namespace) -> int:
    from lionagi.ln.concurrency import run_async

    if args.state_command == "import":
        counts = run_async(_import_runs())
        print(
            f"\nimported {counts['sessions']} session(s), "
            f"{counts['branches']} branch(es), "
            f"{counts['messages']} message(s) "
            f"[skipped={counts['skipped']}, errors={counts['errors']}]"
        )
        return 0 if counts["errors"] == 0 else 1

    if args.state_command == "import-teams":
        counts = run_async(_import_teams())
        print(
            f"\nimported {counts['teams']} team(s), "
            f"{counts['messages']} team message(s) "
            f"[skipped_teams={counts['skipped_teams']}, errors={counts['errors']}]"
        )
        return 0 if counts["errors"] == 0 else 1

    if args.state_command == "ls":
        run_async(
            _list_sessions(
                limit=args.limit,
                status=args.status,
            )
        )
        return 0

    if args.state_command == "stats":
        run_async(_print_stats())
        return 0

    if args.state_command == "checkpoint":
        freed = run_async(_checkpoint(args.mode))
        print(f"checkpoint({args.mode}) → {freed}")
        return 0

    if args.state_command == "vacuum":
        run_async(_vacuum())
        print("vacuum complete")
        return 0

    if args.state_command == "prune":
        result = run_async(
            _prune(
                keep_days=args.keep_days,
                keep_n=args.keep_n,
                dry_run=args.dry_run,
            )
        )
        prefix = "(dry-run) would delete" if args.dry_run else "deleted"
        print(
            f"{prefix} {result['sessions']} session(s), "
            f"{result['branches']} branch(es), "
            f"{result['messages']} orphan message(s)"
        )
        # Recomputed after the fact, never carried out of the prune's own
        # transaction: a count the prune produced cannot contradict the prune.
        check = run_async(_prune_candidates(keep_days=args.keep_days, keep_n=args.keep_n))
        expected = result["sessions"] if args.dry_run else 0
        print(
            f"  still selected by --keep-days {args.keep_days} --keep-n {args.keep_n}: "
            f"{check['candidates']} (expected {expected})"
        )
        age = check["oldest_session_age_days"]
        if age is None:
            print("  oldest session: (none)")
        else:
            print(f"  oldest session: {age:.1f}d")
            if age < args.keep_days:
                print(
                    f"  NOTHING IS OLDER THAN --keep-days {args.keep_days}, so this "
                    "reclaimed nothing and no smaller change to the data would have "
                    "helped. Lower the window or prune on a different axis."
                )
        return 0

    if args.state_command == "doctor":
        result = run_async(
            _doctor(
                stale_hours=args.stale_hours,
                dry_run=args.dry_run,
                new_status=args.new_status,
            )
        )
        prefix = "(dry-run) would sweep" if args.dry_run else "swept"
        print(
            f"running={result['running']}, "
            f"{prefix}={result['swept']} → {args.new_status}, "
            f"skipped_recent={result['skipped']} "
            f"(threshold: {args.stale_hours}h)"
        )
        return 0

    return 1


# ── machine result ────────────────────────────────────────────────────────────


async def _machine_ls_data(*, limit: int, status: str | None) -> dict[str, Any]:
    from .machine import available, readonly_state_db

    result: dict[str, Any] = {"filters": {"status": status}, "limit": limit}
    async with readonly_state_db() as (db, why):
        if db is None:
            result["sessions"] = why
            return result
        rows = await _collect_sessions(db, limit=limit, status=status)
    result["sessions"] = available(rows)
    return result


async def _machine_stats_data() -> dict[str, Any]:
    from .machine import available, readonly_state_db

    sizes = _db_sizes()
    result: dict[str, Any] = {"database": sizes}
    async with readonly_state_db() as (db, why):
        if db is None:
            absent = why
            result["row_counts"] = absent
            result["sessions_by_status"] = absent
            result["messages_by_role"] = absent
            result["messages_by_age"] = absent
            result["journal_mode"] = absent
            return result
        collected = await _collect_stats(db)
    result["row_counts"] = available(collected["row_counts"])
    result["sessions_by_status"] = available(collected["sessions_by_status"])
    # Carried here for the same reason _STATS_TABLES is named once: a machine
    # caller and the printout must not come to describe different databases.
    result["messages_by_role"] = available(collected["messages_by_role"])
    result["messages_by_age"] = available(collected["messages_by_age"])
    result["oldest_message_age_days"] = available(collected["oldest_message_age_days"])
    # Only the one pragma that describes the database rather than the connection
    # that asked. busy_timeout, synchronous and the rest are settings of whichever
    # connection reads them, so reporting them here would hand a caller this
    # reader's configuration under a name that reads like the store's.
    result["journal_mode"] = available(collected["pragmas"]["journal_mode"])
    return result


def _machine_ls(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li state ls")
    parser.add_argument("--limit", type=int, default=50, help=argparse.SUPPRESS)
    parser.add_argument("--status", default=None, help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)
    if args.limit < 1:
        raise MachineError("invalid_input", "--limit must be at least 1")
    return run_async(_machine_ls_data(limit=args.limit, status=args.status))


def _machine_stats(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import MachineError

    if argv:
        raise MachineError("invalid_input", f"li state stats takes no arguments: {' '.join(argv)}")
    return run_async(_machine_stats_data())


def machine_result(argv: list[str]) -> dict[str, Any]:
    """`li state <sub> --machine`.

    `migrate` is not among the subcommands routed here and is not reachable from
    the MCP surface at all; it rewrites the store every other reader reports on.
    """
    from .machine import machine_subcommand

    return machine_subcommand(
        "state",
        argv,
        {"ls": _machine_ls, "stats": _machine_stats},
        without_seam={
            "import": "it loads run directories into the store, which is a write",
            "import-teams": "it loads team files into the store, which is a write",
            "checkpoint": "it checkpoints the write-ahead log",
            "vacuum": "it rebuilds the database file",
            "prune": "it deletes rows",
            "doctor": "it sweeps stale rows to a new status, which is a write",
        },
    )
