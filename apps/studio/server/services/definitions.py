from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from lionagi.cli._runs import LIONAGI_HOME
from lionagi.state.db import DEFAULT_DB_PATH

from ._db import open_db as _open_db
from ._path_safety import validate_name_component

# ---------------------------------------------------------------------------
# Per-(kind, name) concurrency lock — shared across all requests in this
# process.  Spans the DB write inside StateDB.save_definition() AND the
# subsequent disk write so that both operations are atomic from the service's
# perspective.  See "HIGH: definition save current-file race" in ADR-0016.
# ---------------------------------------------------------------------------

_DEFINITION_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_DEFINITION_LOCKS_GUARD = asyncio.Lock()


async def _lock_for(kind: str, name: str) -> asyncio.Lock:
    """Return (or create) the per-(kind, name) asyncio.Lock."""
    async with _DEFINITION_LOCKS_GUARD:
        return _DEFINITION_LOCKS.setdefault((kind, name), asyncio.Lock())

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

        seen_names: set[str] = set()
        all_files: list[Path] = []
        for ext in ("*.md", "*.playbook.yaml", "*.yaml"):
            all_files.extend(sorted(base.glob(ext)))
            all_files.extend(sorted(base.glob(f"*/{ext}")))
        for f in all_files:
            fname = f.name
            if fname.endswith(".playbook.yaml"):
                name = fname.removesuffix(".playbook.yaml")
            elif fname.endswith(".yaml"):
                name = fname.removesuffix(".yaml")
            else:
                name = f.stem
            if f.parent != base:
                name = f.parent.name
            if name in seen_names:
                continue
            seen_names.add(name)

            entry = {
                "kind": k,
                "name": name,
                "path": _relative_path(f),
                "disk_path": _relative_path(f),
                "has_versions": False,
                "version": 0,
                "updated_at": f.stat().st_mtime,
            }

            result.append(entry)

    # Batch-enrich all entries with version info in one DB round-trip (#989).
    if result and await _ensure_db():
        conditions = " OR ".join("(kind = ? AND name = ?)" for _ in result)
        params = [value for item in result for value in (item["kind"], item["name"])]
        async with _open_db(_DB) as db:
            cur = await db.execute(
                f"SELECT kind, name, MAX(version) AS v, MAX(created_at) AS ts"  # noqa: S608
                f" FROM definitions WHERE {conditions} GROUP BY kind, name",
                params,
            )
            rows = await cur.fetchall()
        versions = {(row["kind"], row["name"]): row for row in rows}
        for entry in result:
            row = versions.get((entry["kind"], entry["name"]))
            if row and row["v"] is not None:
                entry["has_versions"] = True
                entry["version"] = row["v"]
                entry["updated_at"] = row["ts"] or entry["updated_at"]

    return result


async def get_definition(kind: str, name: str) -> dict[str, Any] | None:
    """Get current definition content from disk + version history from DB."""
    # Validate at service boundary before any filesystem operation.
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    base = KIND_DIRS.get(kind)
    if not base:
        return None

    disk_file = _find_definition_file(base, name)
    if not disk_file:
        return None

    content = disk_file.read_text()

    versions: list[dict[str, Any]] = []
    if await _ensure_db():
        async with _open_db(_DB) as db:
            cur = await db.execute(
                "SELECT id, version, created_at, message FROM definitions WHERE kind = ? AND name = ? ORDER BY version DESC",
                (kind, name),
            )
            rows = await cur.fetchall()
            versions = [
                {
                    "id": r["id"],
                    "version": r["version"],
                    "created_at": r["created_at"],
                    "message": r["message"],
                }
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
    # Validate at service boundary — kind/name are used in SQL WHERE clauses
    # and, indirectly, in any path lookups that build on this function.
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    if not await _ensure_db():
        return None

    async with _open_db(_DB) as db:
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
    """Save definition: record version in SQLite FIRST, then write to disk.

    F-A3-4 (ADR-0016 §"Save semantics"): DB write must succeed before the
    file is written.  If the DB write fails, propagate the exception — do NOT
    return a success response without a row.  Using StateDB.save_definition()
    ensures correct locking and automatic schema creation on first use.

    Path safety: ``kind`` and ``name`` are validated as safe single-component
    names before any filesystem operation, rejecting traversal sequences and
    glob metacharacters (see ``_path_safety.validate_name_component``).

    Race safety: DB write + disk write execute inside a per-(kind, name)
    asyncio.Lock so concurrent saves for the same definition are serialised
    across both operations (not just within StateDB).

    Returns the new version info.
    """
    # Validate kind and name at the service boundary — reject traversal
    # sequences, path separators, NUL, and glob metacharacters.
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    base = KIND_DIRS.get(kind)
    if not base:
        raise ValueError(f"Unknown kind: {kind}")

    from lionagi.state.db import StateDB

    # Acquire per-(kind, name) lock — spans BOTH the DB write inside
    # StateDB.save_definition() AND the subsequent disk write so that two
    # concurrent saves cannot interleave their DB commit + file write.
    lock = await _lock_for(kind, name)
    async with lock:
        disk_file = _find_definition_file(base, name)
        if not disk_file:
            disk_file = base / f"{name}.md"

        now = time.time()

        # DB write first — StateDB handles schema creation, locking, and retries.
        # Raises on failure (e.g. unique-version retry exhaustion, schema error).
        # The caller (router) catches exceptions and propagates as 500.
        async with StateDB() as db:
            version = await db.save_definition(
                kind=kind,
                name=name,
                path=_relative_path(disk_file),
                content=content,
                message=message,
            )

        # Only write to disk after DB row is committed.
        disk_file.parent.mkdir(parents=True, exist_ok=True)
        disk_file.write_text(content)

    # F-A3-4 (ADR-0016 §"Save semantics"): response field is "saved_at", not "created_at"
    return {
        "kind": kind,
        "name": name,
        "version": version,
        "saved_at": now,
        "message": message,
    }


async def rollback_definition(kind: str, name: str, target_version: int) -> dict[str, Any] | None:
    """Restore a previous version: read old content from DB, write to disk, record as new version.

    F-A3-3 (ADR-0016 §"Rollback semantics"): returns
        { version: N+1, rolled_back_from: current_version, rolled_back_to: N }
    """
    # Validation is handled inside get_version() and save_definition() calls
    # below, but validate here too so rollback_definition() itself is safe at
    # its own boundary.
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    old = await get_version(kind, name, target_version)
    if not old:
        return None

    # Capture current version BEFORE the save so we can report rolled_back_from
    current_version = 0
    if await _ensure_db():
        async with _open_db(_DB) as db:
            cur = await db.execute(
                "SELECT MAX(version) AS v FROM definitions WHERE kind = ? AND name = ?",
                (kind, name),
            )
            row = await cur.fetchone()
            if row and row["v"] is not None:
                current_version = row["v"]

    save_result = await save_definition(
        kind,
        name,
        old["content"],
        message=f"rollback to v{target_version}",
    )

    return {
        "version": save_result["version"],
        "saved_at": save_result["saved_at"],
        "rolled_back_from": current_version,
        "rolled_back_to": target_version,
        "message": save_result["message"],
    }


async def snapshot_current(kind: str | None = None) -> int:
    """Snapshot all current disk files that don't have a matching version in DB.

    Returns count of new versions recorded.
    """
    count = 0
    defs = await list_definitions(kind)

    for d in defs:
        disk_path = _find_definition_file(KIND_DIRS[d["kind"]], d["name"])
        if disk_path is None:
            continue

        content = disk_path.read_text()

        if d["has_versions"]:
            latest = await get_version(d["kind"], d["name"], d["version"])
            if latest and latest["content"] == content:
                continue

        await save_definition(d["kind"], d["name"], content, message="snapshot from disk")
        count += 1

    return count


_EXTENSIONS = (".md", ".playbook.yaml", ".yaml")


def _find_definition_file(base: Path, name: str) -> Path | None:
    """Locate the on-disk file for a definition.

    ``name`` MUST already be validated by ``validate_name_component`` before
    this function is called — callers are responsible for that check.

    Security model:
    - Path injection is prevented by ``validate_name_component(name)`` which
      rejects ``..``, path separators, glob metacharacters, NUL, and empty/
      whitespace strings BEFORE this function is called.
    - The lexical candidate paths are constructed by joining ``base`` with the
      already-validated ``name``, so they are guaranteed to be children of
      ``base`` without resolving symlinks.
    - Symlinks MAY point outside ``base`` (e.g. ``~/.lionagi/agents/*.md``
      symlinked from ``firm/agents/``).  This is intentional and supported —
      the agent service also write-throughs symlinks.  We do NOT validate
      ``candidate.resolve()`` against ``base.resolve()`` because that breaks
      every symlinked agent definition.

    The original implementation used an unescaped recursive glob
    (``base.glob(f"**/{name}{ext}")``) which allowed glob metacharacters in
    *name* to expand across the filesystem.  That glob is replaced by
    deterministic literal-path checks that do not interpret ``name`` as a
    pattern, keeping the fix while allowing symlink targets anywhere.
    """
    # Fast path 1: direct child  (base/<name><ext>)
    for ext in _EXTENSIONS:
        candidate = base / f"{name}{ext}"
        if candidate.exists():
            return candidate

    # Fast path 2: nested subdir  (base/<name>/<name><ext>)
    for ext in _EXTENSIONS:
        candidate = base / name / f"{name}{ext}"
        if candidate.exists():
            return candidate

    # Slow path: scan one level of subdirectories for <name><ext>.
    # Deliberately NOT using Path.glob() with untrusted input — iterate
    # literal candidates instead so no metacharacter expansion can occur.
    # Guard against a missing base directory (e.g. fresh LIONAGI_HOME).
    if not base.exists():
        return None
    for subdir in base.iterdir():
        if not subdir.is_dir():
            continue
        for ext in _EXTENSIONS:
            candidate = subdir / f"{name}{ext}"
            if candidate.exists():
                return candidate

    return None
