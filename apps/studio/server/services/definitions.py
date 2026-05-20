from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from lionagi.cli._runs import LIONAGI_HOME
from lionagi.state.db import DEFAULT_DB_PATH

_DB = str(DEFAULT_DB_PATH)

AGENTS_DIR = LIONAGI_HOME / "agents"
PLAYBOOKS_DIR = LIONAGI_HOME / "playbooks"

KIND_DIRS: dict[str, Path] = {
    "agent": AGENTS_DIR,
    "playbook": PLAYBOOKS_DIR,
}


def _relative_path(full_path: Path) -> str:
    try:
        return str(full_path.relative_to(LIONAGI_HOME))
    except ValueError:
        return str(full_path)


async def _ensure_db() -> bool:
    return DEFAULT_DB_PATH.exists()


async def list_definitions(kind: str | None = None) -> list[dict[str, Any]]:
    """List current (latest version) definitions from disk, enriched with version info from DB."""
    result = []

    kinds = [kind] if kind else list(KIND_DIRS.keys())
    for k in kinds:
        base = KIND_DIRS.get(k)
        if not base or not base.exists():
            continue

        for f in sorted(base.glob("*.md")) + sorted(base.glob("*/*.md")):
            name = f.stem
            if f.parent != base:
                name = f.parent.name

            entry = {
                "kind": k,
                "name": name,
                "path": _relative_path(f),
                "disk_path": str(f),
                "has_versions": False,
                "version": 0,
                "updated_at": f.stat().st_mtime,
            }

            if await _ensure_db():
                async with aiosqlite.connect(_DB) as db:
                    await db.execute("PRAGMA journal_mode = WAL")
                    db.row_factory = aiosqlite.Row
                    cur = await db.execute(
                        "SELECT MAX(version) as v, MAX(created_at) as ts FROM definitions WHERE kind = ? AND name = ?",
                        (k, name),
                    )
                    row = await cur.fetchone()
                    if row and row["v"] is not None:
                        entry["has_versions"] = True
                        entry["version"] = row["v"]
                        entry["updated_at"] = row["ts"] or entry["updated_at"]

            result.append(entry)

    return result


async def get_definition(kind: str, name: str) -> dict[str, Any] | None:
    """Get current definition content from disk + version history from DB."""
    base = KIND_DIRS.get(kind)
    if not base:
        return None

    disk_file = _find_definition_file(base, name)
    if not disk_file:
        return None

    content = disk_file.read_text()

    versions: list[dict[str, Any]] = []
    if await _ensure_db():
        async with aiosqlite.connect(_DB) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, version, created_at, message FROM definitions WHERE kind = ? AND name = ? ORDER BY version DESC",
                (kind, name),
            )
            rows = await cur.fetchall()
            versions = [
                {"id": r["id"], "version": r["version"], "created_at": r["created_at"], "message": r["message"]}
                for r in rows
            ]

    current_version = versions[0]["version"] if versions else 0

    return {
        "kind": kind,
        "name": name,
        "path": _relative_path(disk_file),
        "content": content,
        "version": current_version,
        "versions": versions,
    }


async def get_version(kind: str, name: str, version: int) -> dict[str, Any] | None:
    """Get a specific historical version's content."""
    if not await _ensure_db():
        return None

    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, version, created_at, message FROM definitions WHERE kind = ? AND name = ? AND version = ?",
            (kind, name, version),
        )
        row = await cur.fetchone()
        if not row:
            return None

        return {
            "kind": kind,
            "name": name,
            "version": row["version"],
            "content": row["content"],
            "created_at": row["created_at"],
            "message": row["message"],
        }


async def save_definition(
    kind: str,
    name: str,
    content: str,
    message: str | None = None,
) -> dict[str, Any]:
    """Save definition: write to disk AND record version in SQLite.

    Returns the new version info.
    """
    base = KIND_DIRS.get(kind)
    if not base:
        raise ValueError(f"Unknown kind: {kind}")

    disk_file = _find_definition_file(base, name)
    if not disk_file:
        disk_file = base / f"{name}.md"

    disk_file.parent.mkdir(parents=True, exist_ok=True)
    disk_file.write_text(content)

    version = 1
    now = time.time()
    def_id = str(uuid.uuid4())

    if await _ensure_db():
        from lionagi.state.db import StateDB
        async with StateDB() as db:
            cur = await db.db.execute(
                "SELECT MAX(version) as v FROM definitions WHERE kind = ? AND name = ?",
                (kind, name),
            )
            row = await cur.fetchone()
            if row and row["v"] is not None:
                version = row["v"] + 1

            await db.db.execute(
                "INSERT INTO definitions (id, kind, name, path, content, version, created_at, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (def_id, kind, name, _relative_path(disk_file), content, version, now, message),
            )
            await db.db.commit()

    return {
        "kind": kind,
        "name": name,
        "version": version,
        "created_at": now,
        "message": message,
    }


async def rollback_definition(kind: str, name: str, target_version: int) -> dict[str, Any] | None:
    """Restore a previous version: read old content from DB, write to disk, record as new version."""
    old = await get_version(kind, name, target_version)
    if not old:
        return None

    return await save_definition(
        kind, name, old["content"],
        message=f"rollback to v{target_version}",
    )


async def snapshot_current(kind: str | None = None) -> int:
    """Snapshot all current disk files that don't have a matching version in DB.

    Returns count of new versions recorded.
    """
    count = 0
    defs = await list_definitions(kind)

    for d in defs:
        disk_path = Path(d["disk_path"])
        if not disk_path.exists():
            continue

        content = disk_path.read_text()

        if d["has_versions"]:
            latest = await get_version(d["kind"], d["name"], d["version"])
            if latest and latest["content"] == content:
                continue

        await save_definition(d["kind"], d["name"], content, message="snapshot from disk")
        count += 1

    return count


def _find_definition_file(base: Path, name: str) -> Path | None:
    direct = base / f"{name}.md"
    if direct.exists():
        return direct

    nested = base / name / f"{name}.md"
    if nested.exists():
        return nested

    for f in base.glob(f"**/{name}.md"):
        return f

    return None
