# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li state` — inspect and migrate lionagi state.db.

Subcommands:
    li state import   Import all runs from ~/.lionagi/runs/ into state.db.
    li state ls       List sessions in state.db.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from ._runs import RUNS_ROOT

# ── helpers ──────────────────────────────────────────────────────────────────


def _mtime_as_float(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        import time
        return time.time()


def _msg_from_collection_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a branch-collection message dict to the DB insert shape.

    branch JSON uses ``metadata`` for the node metadata dict; the DB layer
    expects the key ``node_metadata``.  Everything else passes through.
    """
    return {
        "id": raw["id"],
        "created_at": raw["created_at"],
        "node_metadata": raw.get("metadata"),   # rename metadata → node_metadata
        "content": raw.get("content", {}),
        "embedding": raw.get("embedding"),
        "sender": raw.get("sender"),
        "recipient": raw.get("recipient"),
        "channel": raw.get("channel"),
        "role": raw["role"],
    }


# ── async import logic ────────────────────────────────────────────────────────


async def _import_runs() -> dict[str, int]:
    """Scan RUNS_ROOT and import every run that has a run.json manifest.

    Returns counts: {sessions, branches, messages}.
    """
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

            # Idempotent: skip runs already in the DB.
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


_STATUS_MAP = {
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "aborted": "aborted",
    # common aliases that may appear in run.json
    "success": "completed",
    "error": "failed",
    "cancelled": "aborted",
    "canceled": "aborted",
}


def _derive_import_status(manifest: dict[str, Any]) -> str:
    """Derive session status from run.json per ADR-0017.

    1. If manifest has "status" field → map to session vocabulary.
    2. If manifest has "exit_code" == 0 → completed.
    3. If manifest has "exit_code" != 0 → failed.
    4. Otherwise → completed (conservative default).
    """
    raw_status = manifest.get("status")
    if raw_status is not None:
        return _STATUS_MAP.get(str(raw_status).lower(), "completed")

    exit_code = manifest.get("exit_code")
    if exit_code is not None:
        return "completed" if exit_code == 0 else "failed"

    return "completed"


def _derive_timestamps(
    manifest: dict[str, Any],
    run_dir: Path,
) -> tuple[float, float]:
    """Return (started_at, ended_at) as floats.

    Prefer manifest fields; fall back to filesystem ctime / mtime.
    """
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

    # If the values came from manifest they may be ISO strings; coerce to float.
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
    """Import a single run into the DB.  Returns (sessions, branches, messages) imported."""
    created_at = _mtime_as_float(run_dir)
    session_name = manifest.get("kind") or "agent"

    status = _derive_import_status(manifest)
    started_at, ended_at = _derive_timestamps(manifest, run_dir)

    # Create session-level progression (empty for now; updated after branches).
    session_prog_id = str(uuid.uuid4())
    await db.create_progression(session_prog_id)

    # Session must exist before branches can reference it via FK.
    await db.create_session({
        "id": run_id,
        "created_at": created_at,
        "node_metadata": None,
        "name": session_name,
        "user": None,
        "progression_id": session_prog_id,
        "first_msg_id": None,
        "last_msg_id": None,
        "source_kind": "imported_fs",
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
    })

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

        # Extract messages and ordering from Pile format.
        messages_pile = branch_data.get("messages", {})
        raw_collection: list[dict] = messages_pile.get("collections", [])
        progression_info = messages_pile.get("progression", {})
        order: list[str] = progression_info.get("order", [])

        # Build an id→raw map, fall back to collection order if no explicit order.
        by_id: dict[str, dict] = {m["id"]: m for m in raw_collection if "id" in m}
        if order:
            ordered_msgs = [by_id[mid] for mid in order if mid in by_id]
        else:
            ordered_msgs = raw_collection

        # Detect system message (first message with role == "system").
        system_msg_id: str | None = None
        for raw_msg in ordered_msgs:
            if raw_msg.get("role") == "system":
                system_msg_id = raw_msg["id"]
                break

        # Insert all messages.
        branch_msg_ids: list[str] = []
        for raw_msg in ordered_msgs:
            msg = _msg_from_collection_entry(raw_msg)
            await db.insert_message(msg)
            branch_msg_ids.append(msg["id"])
            total_messages += 1

        # Create branch progression with ordered message IDs.
        branch_prog_id = str(uuid.uuid4())
        await db.create_progression(branch_prog_id, branch_msg_ids)

        # Derive branch metadata from manifest branches list.
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

        await db.create_branch({
            "id": branch_id,
            "created_at": branch_created_at,
            "node_metadata": node_meta or None,
            "user": branch_data.get("user"),
            "name": branch_name,
            "session_id": run_id,
            "progression_id": branch_prog_id,
            "system_msg_id": system_msg_id,
        })

        session_msg_ids.extend(branch_msg_ids)
        total_branches += 1

    # Back-fill session progression with all message IDs collected from branches.
    if session_msg_ids:
        await db.db.execute(
            "UPDATE progressions SET collection = ? WHERE id = ?",
            (json.dumps(session_msg_ids), session_prog_id),
        )
        await db.db.commit()
        await db.update_session(
            run_id,
            first_msg_id=session_msg_ids[0],
            last_msg_id=session_msg_ids[-1],
        )

    print(
        f"  imported {run_id}: {total_branches} branch(es), {total_messages} message(s)"
    )
    return 1, total_branches, total_messages


# ── async ls logic ────────────────────────────────────────────────────────────


async def _list_sessions() -> None:
    """Print a simple table of sessions in state.db."""
    import time

    from lionagi.state.db import StateDB

    async with StateDB() as db:
        cur = await db.db.execute(
            "SELECT id, name, updated_at FROM sessions ORDER BY updated_at DESC"
        )
        rows = await cur.fetchall()

        if not rows:
            print("(no sessions in state.db)")
            return

        # Gather branch / message counts per session using the same connection.
        header = f"{'ID':<36}  {'NAME':<16}  {'BRANCHES':>8}  {'MESSAGES':>8}  {'UPDATED':<20}"
        print(header)
        print("-" * len(header))
        for row in rows:
            sid = row["id"]
            name = row["name"] or ""
            updated = row["updated_at"]
            updated_str = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated))
                if updated else ""
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

            print(
                f"{sid:<36}  {name:<16}  {bc:>8}  {msg_count:>8}  {updated_str:<20}"
            )


# ── CLI wiring ────────────────────────────────────────────────────────────────


def add_state_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li state` with its subcommands."""
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

    # li state ls
    state_sub.add_parser(
        "ls",
        help="List sessions in state.db.",
        description="Print a table of sessions stored in state.db.",
    )


def run_state(args: argparse.Namespace) -> int:
    """Dispatch `li state` subcommands."""
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

    if args.state_command == "ls":
        run_async(_list_sessions())
        return 0

    return 1
