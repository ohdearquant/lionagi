from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from lionagi.state.db import DEFAULT_DB_PATH

from ..config import SHOWS_ROOT
from ._db import open_db as _open_db
from ._path_safety import public_path, safe_path_join

_log = __import__("logging").getLogger(__name__)

_DB = str(DEFAULT_DB_PATH)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _play_dirs(show_dir: Path) -> list[Path]:
    try:
        return [p for p in sorted(show_dir.iterdir()) if p.is_dir()]
    except OSError:
        return []


def _extract_goal(show_md: str | None) -> str | None:
    if not show_md:
        return None
    m = re.search(r"^## Goal\s*\n(.+?)(?=\n## |\Z)", show_md, re.MULTILINE | re.DOTALL)
    if m:
        return m.group(1).strip()[:500]
    return None


def _extract_repo_and_branches(show_md: str | None) -> tuple[str | None, str | None, str | None]:
    if not show_md:
        return None, None, None
    repo = base = integration = None
    for line in show_md.splitlines():
        if line.strip().startswith("- Repo:"):
            repo = line.split(":", 1)[1].strip()
        elif line.strip().startswith("- Integration branch:") or line.strip().startswith(
            "- Integration:"
        ):
            integration = line.split(":", 1)[1].strip().split("(")[0].strip()
        elif line.strip().startswith("- Base for final merge:") or line.strip().startswith(
            "- Base:"
        ):
            base = line.split(":", 1)[1].strip()
    return repo, base, integration


# ---------------------------------------------------------------------------
# SQLite-backed list/detail — fast queries, cross-ref to sessions
# ---------------------------------------------------------------------------


async def _db_available() -> bool:
    return DEFAULT_DB_PATH.exists()


async def list_shows() -> list[dict[str, Any]]:
    if await _db_available():
        try:
            return await _list_shows_db()
        except Exception:
            _log.warning("list_shows DB query failed, falling back to filesystem", exc_info=True)
    return _list_shows_fs()


async def _list_shows_db() -> list[dict[str, Any]]:
    async with _open_db(_DB) as db:
        cur = await db.execute("""
            SELECT s.id, s.topic, s.goal, s.status, s.show_dir,
                   s.created_at, s.updated_at,
                   COUNT(p.id) AS play_count,
                   MAX(p.updated_at) AS latest_play_update
            FROM shows s
            LEFT JOIN plays p ON p.show_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
        """)
        rows = await cur.fetchall()

    if not rows:
        return _list_shows_fs()

    return [
        {
            "topic": row["topic"],
            "path": public_path(Path(row["show_dir"])),
            "play_count": row["play_count"],
            "latest_status": row["status"],
            # F-A1-5 (ADR-0011 §"Show status provenance"): status_source field.
            # The status_source column is defined in ADR-0011's schema block but
            # is absent from the current schema.sql (deferred migration — tracked
            # separately; adding it requires ALTER TABLE and a backfill pass that
            # is out of scope for this fix PR).  We derive it in code: db-loaded
            # rows get "sqlite", filesystem fallback rows get "filesystem".
            "status_source": "sqlite",
            "last_update": row["latest_play_update"] or row["updated_at"],
            "goal": row["goal"],
            "id": row["id"],
        }
        for row in rows
    ]


def _list_shows_fs() -> list[dict[str, Any]]:
    if not SHOWS_ROOT.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(SHOWS_ROOT.iterdir()):
        if not path.is_dir():
            continue
        plays = _play_dirs(path)
        metas = [m for m in (_read_json(play / "_meta.json") for play in plays) if m]
        latest = max(
            metas,
            key=lambda m: str(m.get("started_at") or m.get("ended_at") or ""),
            default={},
        )
        latest_status = str(latest.get("status") or "unknown")
        try:
            last_update = max(
                [path.stat().st_mtime, *[play.stat().st_mtime for play in plays]],
            )
        except OSError:
            last_update = None
        out.append(
            {
                "topic": path.name,
                "path": public_path(path),
                "play_count": len(plays),
                "latest_status": latest_status,
                # F-A1-5: filesystem-loaded rows carry "filesystem" provenance
                "status_source": "filesystem",
                "last_update": last_update,
            }
        )
    return out


async def get_show(topic: str) -> dict[str, Any] | None:
    show_dir = safe_path_join(SHOWS_ROOT, topic)
    if not show_dir.is_dir():
        return None

    show_md = _read_text(show_dir / "_show.md")

    db_plays: list[dict[str, Any]] = []
    show_row: dict[str, Any] | None = None
    if await _db_available():
        try:
            async with _open_db(_DB) as db:
                cur = await db.execute("SELECT * FROM shows WHERE topic = ?", (topic,))
                row = await cur.fetchone()
                if row:
                    show_row = dict(row)

                    play_cur = await db.execute(
                        """
                        SELECT p.*, s.name AS session_name
                        FROM plays p
                        LEFT JOIN sessions s ON s.id = p.session_id
                        WHERE p.show_id = ?
                        ORDER BY p.sort_order, p.created_at
                    """,
                        (show_row["id"],),
                    )
                    play_rows = await play_cur.fetchall()
                    db_plays = [dict(r) for r in play_rows]
        except Exception:
            _log.warning("get_show DB query failed for topic %r", topic, exc_info=True)

    if db_plays:
        plays = []
        for p in db_plays:
            play_dir = show_dir / p["name"]
            plays.append(
                {
                    "name": p["name"],
                    "meta": {
                        "worktree": p["worktree"],
                        "branch": p["branch"],
                        "attempt": p["attempt"],
                        "started_at": p["started_at"],
                        "ended_at": p["ended_at"],
                        "exit_code": p["exit_code"],
                        "merged_at": p["merged_at"],
                        "merge_sha": p["merge_sha"],
                        "status": p["status"],
                    },
                    "verdict": {
                        "gate_passed": bool(p["gate_passed"])
                        if p["gate_passed"] is not None
                        else None,
                        "feedback": p["gate_feedback"],
                    }
                    if p["gate_passed"] is not None
                    else _read_json(play_dir / "_verdict.json"),
                    "session_id": p["session_id"],
                    "session_name": p.get("session_name"),
                    "intent": _read_text(play_dir / "_intent.md"),
                    "updated_at": p["updated_at"],
                    "depends_on": json.loads(p["depends_on"])
                    if isinstance(p["depends_on"], str)
                    else (p["depends_on"] or []),
                }
            )
    else:
        plays = []
        for play_dir in _play_dirs(show_dir):
            meta = _read_json(play_dir / "_meta.json") or {}
            verdict = _read_json(play_dir / "_verdict.json")
            try:
                updated_at = play_dir.stat().st_mtime
            except OSError:
                updated_at = None
            plays.append(
                {
                    "name": play_dir.name,
                    "meta": meta,
                    "verdict": verdict,
                    "updated_at": updated_at,
                }
            )

    # F-A1-5 (ADR-0011 §"Show status provenance"): status_source mirrors the
    # derivation in list_shows() — "sqlite" when the row came from the DB,
    # "filesystem" for filesystem fallback (show_row is None).
    status_source = "sqlite" if show_row else "filesystem"

    return {
        "topic": topic,
        "path": public_path(show_dir),
        "show_md": show_md,
        "goal": show_row["goal"] if show_row else _extract_goal(show_md),
        "status": show_row["status"] if show_row else "unknown",
        "status_source": status_source,
        "plays": plays,
    }


# ---------------------------------------------------------------------------
# Import filesystem shows into SQLite
# ---------------------------------------------------------------------------


async def import_shows() -> dict[str, int]:
    if not SHOWS_ROOT.exists():
        return {"shows_imported": 0, "plays_imported": 0}

    from lionagi.state.db import StateDB
    from lionagi.state.reasons import PlayReasons, ShowReasons

    shows_count = 0
    plays_count = 0

    async with StateDB() as db:
        for show_path in sorted(SHOWS_ROOT.iterdir()):
            if not show_path.is_dir():
                continue

            topic = show_path.name
            now = time.time()

            cur = await db.db.execute("SELECT id FROM shows WHERE topic = ?", (topic,))
            existing = await cur.fetchone()
            if existing:
                show_id = existing["id"]
            else:
                show_id = str(uuid.uuid4())
                show_md = _read_text(show_path / "_show.md")
                goal = _extract_goal(show_md)
                repo, base_branch, integration = _extract_repo_and_branches(show_md)

                all_plays = _play_dirs(show_path)
                all_metas = [_read_json(p / "_meta.json") or {} for p in all_plays]
                all_statuses = [m.get("status", "pending") for m in all_metas]
                has_escalated = "escalated" in all_statuses
                all_merged = all(s == "merged" for s in all_statuses) if all_statuses else False
                final_verdict = _read_json(show_path / "_final_verdict.json")
                abort_file = (show_path / "_ABORT").exists()

                if abort_file:
                    show_status = "aborted"
                elif final_verdict and final_verdict.get("show_passed"):
                    show_status = "completed"
                elif has_escalated:
                    show_status = "active"
                elif all_merged and all_statuses:
                    show_status = "completed"
                else:
                    show_status = "active"

                try:
                    created_at = show_path.stat().st_mtime
                except OSError:
                    created_at = now

                show_reason_code: str | None = None
                show_reason_summary = ""
                show_evidence_refs: list[dict[str, Any]] = []
                if abort_file:
                    show_reason_code = ShowReasons.ABORTED_OPERATOR
                    show_reason_summary = "Show was imported with an operator abort marker."
                    show_evidence_refs = [{"kind": "file", "path": str(show_path / "_ABORT")}]
                elif final_verdict and final_verdict.get("show_passed"):
                    show_reason_code = ShowReasons.COMPLETED_FINAL_GATE
                    show_reason_summary = "Show was imported with a passing final gate verdict."
                    show_evidence_refs = [
                        {"kind": "file", "path": str(show_path / "_final_verdict.json")}
                    ]
                elif show_status == "completed":
                    _log.warning(
                        "show %s imported as completed without final gate evidence; "
                        "no ADR-0028 reason code matched",
                        topic,
                    )

                await db.db.execute(
                    """INSERT OR IGNORE INTO shows
                       (id, topic, goal, repo, base_branch, integration_branch,
                        status, show_dir, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        show_id,
                        topic,
                        goal,
                        repo,
                        base_branch,
                        integration,
                        show_status,
                        str(show_path),
                        created_at,
                        now,
                    ),
                )
                shows_count += 1

                if show_reason_code is not None:
                    await db.db.commit()
                    await db.update_status(
                        "show",
                        show_id,
                        new_status=show_status,
                        reason_code=show_reason_code,
                        reason_summary=show_reason_summary,
                        evidence_refs=show_evidence_refs,
                        source="system",
                        actor="shows_import",
                        metadata={"topic": topic},
                    )

            for idx, play_dir in enumerate(_play_dirs(show_path)):
                play_name = play_dir.name
                meta = _read_json(play_dir / "_meta.json") or {}
                verdict = _read_json(play_dir / "_verdict.json")

                play_cur = await db.db.execute(
                    "SELECT id FROM plays WHERE show_id = ? AND name = ?",
                    (show_id, play_name),
                )
                if await play_cur.fetchone():
                    continue

                play_id = str(uuid.uuid4())

                session_id = None
                session_name = f"show_{topic}_{play_name}"
                sess_cur = await db.db.execute(
                    "SELECT id FROM sessions WHERE name = ? ORDER BY created_at DESC LIMIT 1",
                    (session_name,),
                )
                sess_row = await sess_cur.fetchone()
                if sess_row:
                    session_id = sess_row["id"]

                gate_passed = None
                gate_feedback = None
                if verdict:
                    gp = verdict.get("gate_passed")
                    if isinstance(gp, bool):
                        gate_passed = 1 if gp else 0
                    gate_feedback = verdict.get("feedback")

                started_at = meta.get("started_at")
                ended_at = meta.get("ended_at")
                if isinstance(started_at, str):
                    from datetime import datetime

                    try:
                        started_at = datetime.fromisoformat(started_at).timestamp()
                    except (ValueError, TypeError):
                        started_at = None
                if isinstance(ended_at, str):
                    from datetime import datetime

                    try:
                        ended_at = datetime.fromisoformat(ended_at).timestamp()
                    except (ValueError, TypeError):
                        ended_at = None

                merged_at = meta.get("merged_at")
                if isinstance(merged_at, str):
                    from datetime import datetime

                    try:
                        merged_at = datetime.fromisoformat(merged_at).timestamp()
                    except (ValueError, TypeError):
                        merged_at = None

                try:
                    play_created = play_dir.stat().st_mtime
                except OSError:
                    play_created = time.time()

                imported_play_status = str(meta.get("status", "pending"))
                play_attempt = int(meta.get("attempt", 1) or 1)

                play_reason_code: str | None = None
                play_reason_summary = ""
                play_evidence_refs: list[dict[str, Any]] = []
                if imported_play_status == "blocked":
                    block_reason = meta.get("block_reason") or meta.get("blocked_reason")
                    if block_reason == "invalid_deps":
                        play_reason_code = PlayReasons.BLOCKED_INVALID_DEPS
                        play_reason_summary = (
                            "Play was imported as blocked because dependencies were invalid."
                        )
                    elif block_reason == "dep_failed":
                        play_reason_code = PlayReasons.BLOCKED_DEP_FAILED
                        play_reason_summary = (
                            "Play was imported as blocked because a dependency failed."
                        )
                    else:
                        _log.warning(
                            "play %s/%s imported as blocked without invalid_deps or dep_failed "
                            "evidence; no ADR-0028 reason code matched",
                            topic,
                            play_name,
                        )
                elif imported_play_status == "gate_failed" and gate_passed == 0:
                    play_reason_code = PlayReasons.GATE_FAILED_VERDICT
                    play_reason_summary = "Play was imported with a failing gate verdict."
                    play_evidence_refs = [{"kind": "file", "path": str(play_dir / "_verdict.json")}]
                elif imported_play_status == "escalated" and play_attempt >= 2:
                    play_reason_code = PlayReasons.ESCALATED_GATE_TWICE
                    play_reason_summary = (
                        "Play was imported as escalated after a second gate failure."
                    )
                elif imported_play_status == "merged":
                    play_reason_code = PlayReasons.MERGED_OK
                    play_reason_summary = "Play was imported as merged."
                elif imported_play_status not in {
                    "pending",
                    "running",
                    "running_complete",
                    "prepared",
                    "gated",
                    "redoing",
                    "aborted_after_finish",
                }:
                    _log.warning(
                        "play %s/%s imported with status %s but no ADR-0028 reason code matched",
                        topic,
                        play_name,
                        imported_play_status,
                    )

                await db.db.execute(
                    """INSERT OR IGNORE INTO plays
                       (id, show_id, name, playbook, effort, status, attempt,
                        session_id, started_at, ended_at, exit_code,
                        worktree, branch, merge_sha, merged_at,
                        gate_passed, gate_feedback, depends_on,
                        sort_order, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        play_id,
                        show_id,
                        play_name,
                        None,
                        meta.get("effort"),
                        imported_play_status,
                        play_attempt,
                        session_id,
                        started_at,
                        ended_at,
                        meta.get("exit_code"),
                        meta.get("worktree"),
                        meta.get("branch"),
                        meta.get("merge_sha"),
                        merged_at,
                        gate_passed,
                        gate_feedback,
                        json.dumps([]),
                        idx,
                        play_created,
                        play_created,
                    ),
                )
                plays_count += 1

                if play_reason_code is not None:
                    await db.db.commit()
                    await db.update_status(
                        "play",
                        play_id,
                        new_status=imported_play_status,
                        reason_code=play_reason_code,
                        reason_summary=play_reason_summary,
                        evidence_refs=play_evidence_refs,
                        source="system",
                        actor="shows_import",
                        metadata={"topic": topic, "play": play_name, "attempt": play_attempt},
                    )

        await db.db.commit()

    return {"shows_imported": shows_count, "plays_imported": plays_count}


# ---------------------------------------------------------------------------
# SSE watcher (unchanged)
# ---------------------------------------------------------------------------


_SHOW_TERMINAL_STATUSES = frozenset({"completed", "aborted"})
_SHOW_DONE_STABLE_SECS = 60.0


async def watch_show(topic: str) -> AsyncGenerator[str, None]:
    """SSE stream of file changes under a show directory.

    F-A2-3 (ADR-0006 reconnect semantics): emits ``{"type":"done"}`` when the
    show status is terminal (completed or aborted) AND no file has changed for
    60 seconds, then closes.  This matches the session stream's done semantics.
    """
    topic_dir = safe_path_join(SHOWS_ROOT, topic)
    seen_files: dict[str, tuple[float, int]] = {}
    last_change: float = time.time()

    while True:
        any_change = False
        for path in sorted(topic_dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue

            key = str(path.relative_to(topic_dir))
            current = (stat.st_mtime, stat.st_size)
            previous = seen_files.get(key)
            if previous == current:
                continue

            seen_files[key] = current
            event_type = "new" if previous is None else "change"
            evt = {"type": event_type, "path": key, "size": stat.st_size}
            yield f"data: {json.dumps(evt)}\n\n"
            last_change = time.time()
            any_change = True

        # Check for terminal + stable condition (F-A2-3)
        if not any_change and (time.time() - last_change) >= _SHOW_DONE_STABLE_SECS:
            # Query current show status from DB (fast path) or filesystem
            show_status: str | None = None
            if await _db_available():
                try:
                    async with _open_db(_DB) as db:
                        cur = await db.execute("SELECT status FROM shows WHERE topic = ?", (topic,))
                        row = await cur.fetchone()
                        if row:
                            show_status = row["status"]
                except Exception:
                    _log.debug(
                        "watch_show DB status check failed for topic %r", topic, exc_info=True
                    )
            if show_status in _SHOW_TERMINAL_STATUSES:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

        await asyncio.sleep(0.5)
