# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""DB import / migration helpers for `li state import` and `li state import-teams`.

Pure helpers: single-run parsing, status derivation, message normalisation.
The scan loops (_import_runs, _import_teams) live in state.py so they can
reference RUNS_ROOT from the module namespace (test monkeypatch-friendly).

All public names are re-exported from ``cli/state.py`` so existing import
paths remain stable.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ._lifecycle import EXIT_CODE_BY_STATUS


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
            # ADR-0012 enriched provenance — written so imported rows are
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

    print(f"  imported {run_id}: {total_branches} branch(es), {total_messages} message(s)")
    return 1, total_branches, total_messages
