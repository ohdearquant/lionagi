# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Per-invocation files-read overlap: a cheap duplication indicator over the
tool-call file paths a scheduled invocation's child sessions ("workers")
touched. Measure-only, read-side aggregation over already-persisted
messages -- never touches a live agent.

Mirrors the per-session file-set union `get_session()` builds in
lionagi/studio/services/sessions.py (`message_stats.files`), but reads
directly off StateDB's async engine instead of that module's aiosqlite
connection, so the scheduler's finalize path (lionagi/studio/scheduler/
engine.py) stays on the connection it already holds rather than opening a
second DB layer.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ("compute_files_overlap",)

# Legacy rows may carry the bare class name instead of the fully-qualified
# path (see sessions.py's _ACTION_LION_CLASSES for the same accommodation).
_ACTION_REQUEST_CLASSES = (
    "lionagi.protocols.messages.action_request.ActionRequest",
    "ActionRequest",
)

_DEFAULT_TOP_N = 5
_CHUNK_SIZE = 500  # stays under SQLite's bound-variable ceiling per query


def _parse_content(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_file_path(content: dict[str, Any]) -> str | None:
    arguments = content.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    file_path = arguments.get("file_path") or arguments.get("path")
    return file_path if isinstance(file_path, str) and file_path else None


async def _worker_file_sets(db: Any, invocation_id: str) -> dict[str, set[str]]:
    """Per-session (worker) file-path sets, unioned across all of that
    session's branches -- one entry per child session that touched any
    file, keyed by session id."""
    session_rows = await db.fetch_all(
        "SELECT id FROM sessions WHERE invocation_id = ?", (invocation_id,)
    )
    if not session_rows:
        return {}

    type_placeholders = ",".join("?" for _ in _ACTION_REQUEST_CLASSES)
    type_rows = await db.fetch_all(
        f"SELECT type_id FROM message_types WHERE lion_class IN ({type_placeholders})",  # noqa: S608
        list(_ACTION_REQUEST_CLASSES),
    )
    type_ids = [r["type_id"] for r in type_rows]
    if not type_ids:
        return {}

    file_sets: dict[str, set[str]] = {}
    for srow in session_rows:
        session_id = srow["id"]
        branch_rows = await db.fetch_all(
            "SELECT progression_id FROM branches WHERE session_id = ?", (session_id,)
        )
        msg_ids: list[str] = []
        for brow in branch_rows:
            prog_id = brow.get("progression_id")
            if not prog_id:
                continue
            prog_row = await db.fetch_one(
                "SELECT collection FROM progressions WHERE id = ?", (prog_id,)
            )
            collection = (prog_row or {}).get("collection")
            if not collection:
                continue
            try:
                ids = json.loads(collection) if isinstance(collection, str) else collection
            except (ValueError, TypeError):
                continue
            if isinstance(ids, list):
                msg_ids.extend(ids)
        if not msg_ids:
            continue

        files: set[str] = set()
        type_ph = ",".join("?" for _ in type_ids)
        for chunk_start in range(0, len(msg_ids), _CHUNK_SIZE):
            chunk = msg_ids[chunk_start : chunk_start + _CHUNK_SIZE]
            id_ph = ",".join("?" for _ in chunk)
            rows = await db.fetch_all(
                f"SELECT content FROM messages WHERE id IN ({id_ph}) "  # noqa: S608
                f"AND lion_class IN ({type_ph})",
                [*chunk, *type_ids],
            )
            for row in rows:
                content = _parse_content(row.get("content"))
                file_path = _extract_file_path(content)
                if file_path:
                    files.add(file_path)
        if files:
            file_sets[session_id] = files
    return file_sets


def _overlap_from_file_sets(file_sets: dict[str, set[str]], *, top_n: int) -> dict[str, Any]:
    """Files touched by >=2 distinct workers; count + top-N paths by worker
    count (ties broken by path for deterministic output)."""
    worker_counts: dict[str, int] = {}
    for files in file_sets.values():
        for path in files:
            worker_counts[path] = worker_counts.get(path, 0) + 1
    overlapping = {path: n for path, n in worker_counts.items() if n >= 2}
    top = sorted(overlapping.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return {
        "count": len(overlapping),
        "top": [{"path": path, "workers": n} for path, n in top],
    }


async def compute_files_overlap(
    db: Any, invocation_id: str, *, top_n: int = _DEFAULT_TOP_N
) -> dict[str, Any]:
    """``{"count": N, "top": [{"path": ..., "workers": ...}, ...]}`` for
    *invocation_id*'s child sessions ("workers"). Zero-worker or
    zero-overlap invocations return ``{"count": 0, "top": []}``, never an
    error -- this runs unconditionally at invocation finalize."""
    file_sets = await _worker_file_sets(db, invocation_id)
    return _overlap_from_file_sets(file_sets, top_n=top_n)
