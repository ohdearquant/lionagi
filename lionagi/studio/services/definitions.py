from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from fastapi import HTTPException, Query
from pydantic import BaseModel

from lionagi._paths import LIONAGI_HOME, ensure_lionagi_dir

# Imported eagerly: the state package resolves attributes lazily, so this
# reaches the small URL-handling module without pulling the database layer in.
from lionagi.state.engine import mask_credentials

from ..registry import studio_route
from ._path_safety import safe_path_join, validate_name_component
from .agents import _canonicalize_casts, _is_protected_system
from .redaction import (
    RedactedPayloadError,
    abbreviate_path,
    demo_mode_enabled,
    redact_agent_markdown,
    reject_if_redacted_payload,
)

_log = logging.getLogger("lionagi.studio")

# Per-(kind, name) concurrency lock, shared across all requests in this process.
# Spans both the DB write and the disk write so a crash between them cannot leave disk ahead of history.

_DEFINITION_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_DEFINITION_LOCKS_GUARD = asyncio.Lock()


async def _lock_for(kind: str, name: str) -> asyncio.Lock:
    """Return (or create) the per-(kind, name) asyncio.Lock."""
    async with _DEFINITION_LOCKS_GUARD:
        return _DEFINITION_LOCKS.setdefault((kind, name), asyncio.Lock())


AGENTS_DIR = LIONAGI_HOME / "agents"
PLAYBOOKS_DIR = LIONAGI_HOME / "playbooks"
# Resolved independently of skills.py's own SKILLS_ROOT (same pattern as
# agents.py's _AGENTS_ROOT vs. this module's AGENTS_DIR) so this module's
# constant stays test-patchable without reaching into skills.py's module state.
SKILLS_DIR = LIONAGI_HOME / "skills"

KIND_DIRS: dict[str, Path] = {
    "agent": AGENTS_DIR,
    "playbook": PLAYBOOKS_DIR,
}

# Extension used when creating a new definition of a given kind (i.e. no
# existing on-disk file to infer it from). Must match what each kind's
# catalog scans for -- playbooks.py's list_playbooks() only globs
# *.playbook.yaml, so a new playbook has to land with that suffix.
_DEFAULT_EXT: dict[str, str] = {
    "agent": ".md",
    "playbook": ".playbook.yaml",
}


def _relative_path(full_path: Path) -> str:
    try:
        return str(full_path.relative_to(LIONAGI_HOME))
    except ValueError:
        return str(full_path)


class HistoryUnavailableError(Exception):
    """The configured store could not be read. Distinct from holding nothing."""


# What the caller is told. The reason is logged rather than returned: a caller
# can do nothing with a driver's connection error, and a driver that quotes the
# connection string it failed on quotes the store password with it.
#
# The masking below is a backstop, not a repair of a demonstrated leak. Five
# store failures were measured here — refused connection, unresolvable host,
# unknown driver, unparseable URL, unknown scheme — and every one names a
# socket or a plugin without quoting the URL. The set of drivers is open, and
# masking a message that has nothing to mask costs nothing.
_HISTORY_UNAVAILABLE_DETAIL = "Definition history is not readable right now"


async def _read_history(kind: str, name: str) -> list[dict[str, Any]]:
    """Version history for one definition, from the store that actually holds it.

    History is read through ``StateDB``, the same way ``save_definition``
    writes it, so a file, an in-memory database and a server all answer from
    the store the deployment is configured for. Reading SQLite directly instead
    could only ever see a local file, which for a server-backed deployment is a
    database nobody serves: version numbers and audit messages out of an old
    local file, laid over content read live from disk, in one payload that
    looks entirely consistent.

    Raising when the store cannot be read is the point. Returning an empty list
    would say this definition has no versions, which is a claim about the
    definition; the true statement is that this deployment cannot answer, which
    is a claim about the store. A caller told "no versions" reasonably concludes
    nothing was ever saved.
    """
    from lionagi.state.db import StateDB

    try:
        async with StateDB() as db:
            rows = await db.list_definition_versions(kind, name)
    except Exception as exc:  # noqa: BLE001 — any unreadable store is the same answer
        _log.warning(
            "definition history is unreadable for %s/%s: %s",
            kind,
            name,
            mask_credentials(repr(exc)),
        )
        raise HistoryUnavailableError(mask_credentials(str(exc))) from exc

    return [
        {
            "id": r["id"],
            "version": r["version"],
            "created_at": r["created_at"],
            "message": r["message"],
        }
        for r in rows
    ]


async def list_definitions(kind: str | None = None) -> list[dict[str, Any]]:
    """List current (latest version) definitions from disk, enriched with version info from DB."""

    def _scan_disk(kind_filter: str | None) -> list[dict[str, Any]]:
        """Synchronous disk scan — runs in a worker thread."""
        result: list[dict[str, Any]] = []
        kinds = [kind_filter] if kind_filter else list(KIND_DIRS.keys())
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
        return result

    result = await anyio.to_thread.run_sync(partial(_scan_disk, kind))

    # Same policy get_definition() applies to a single record: while demo mode
    # is on, an agent's on-disk location is abbreviated to a bare filename in
    # every response, not just the one reached by fetching it individually.
    if demo_mode_enabled():
        for entry in result:
            if entry["kind"] != "agent":
                continue
            entry["path"] = abbreviate_path(entry["path"])
            entry["disk_path"] = abbreviate_path(entry["disk_path"])

    if result:
        from lionagi.state.db import StateDB

        try:
            async with StateDB() as db:
                rows = await db.list_latest_definition_versions()
        except Exception as exc:  # noqa: BLE001 — any unreadable store is the same answer
            # Same distinction the single-definition route makes: unknown is
            # null, not False. Reporting has_versions=False here would say
            # these definitions have never been saved.
            _log.warning(
                "definition history is unreadable for the listing: %s",
                mask_credentials(repr(exc)),
            )
            for entry in result:
                entry["has_versions"] = None
                entry["history_available"] = False
            return result

        versions = {(row["kind"], row["name"]): row for row in rows}
        for entry in result:
            entry["history_available"] = True
            row = versions.get((entry["kind"], entry["name"]))
            if row and row["version"] is not None:
                entry["has_versions"] = True
                entry["version"] = row["version"]
                entry["updated_at"] = row["created_at"] or entry["updated_at"]

    return result


def _resolve_skill_file(name: str) -> Path | None:
    """Locate a skill's current content file, tolerating the legacy bare
    ``<name>.md`` shape that some installed skills still use.

    Mirrors ``skills.py``'s own resolution (canonical ``<name>/SKILL.md``
    first, then any other ``.md`` in the directory) so Studio's editor and
    the CLI's skill runner agree on what file a name refers to when reading.
    Writing does not carry the same tolerance -- see ``_save_skill_definition``,
    which always targets the canonical shape regardless of what this finds.
    """
    from .skills import _find_skill_md

    safe_path_join(SKILLS_DIR, name)

    skill_dir = SKILLS_DIR / name
    if skill_dir.is_dir():
        return _find_skill_md(skill_dir)
    bare = SKILLS_DIR / f"{name}.md"
    return bare if bare.exists() else None


async def _get_skill_definition(name: str) -> dict[str, Any] | None:
    disk_file = await anyio.to_thread.run_sync(partial(_resolve_skill_file, name))
    if disk_file is None:
        return None

    content = await anyio.to_thread.run_sync(disk_file.read_text)
    path = _relative_path(disk_file)

    try:
        versions = await _read_history("skill", name)
    except HistoryUnavailableError:
        return {
            "kind": "skill",
            "name": name,
            "path": path,
            "content": content,
            "version": None,
            "versions": None,
            "history_available": False,
        }

    return {
        "kind": "skill",
        "name": name,
        "path": path,
        "content": content,
        "version": versions[0]["version"] if versions else 0,
        "versions": versions,
        "history_available": True,
    }


async def get_definition(kind: str, name: str) -> dict[str, Any] | None:
    """Get current definition content from disk + version history from DB."""
    # Validate at service boundary before any filesystem operation.
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    # Skills live outside KIND_DIRS/_find_definition_file's generic multi-kind
    # scan -- see the KIND_DIRS comment on list_definitions_route for why --
    # so they get a dedicated resolution path instead.
    if kind == "skill":
        return await _get_skill_definition(name)

    base = KIND_DIRS.get(kind)
    if not base:
        return None

    disk_file = await anyio.to_thread.run_sync(partial(_find_definition_file, base, name))
    if not disk_file:
        return None

    content = await anyio.to_thread.run_sync(disk_file.read_text)
    path = _relative_path(disk_file)

    redact = kind == "agent" and demo_mode_enabled()
    if redact:
        content = redact_agent_markdown(content, redact=True)
        path = abbreviate_path(path)

    # The disk half is current and correct whatever the store is, so it is
    # answered either way. The history half is null rather than empty when the
    # store cannot be read: a client that does not handle it fails on a null
    # instead of quietly believing this definition was never versioned.
    try:
        versions = await _read_history(kind, name)
    except HistoryUnavailableError:
        return {
            "kind": kind,
            "name": name,
            "path": path,
            "content": content,
            "version": None,
            "versions": None,
            "history_available": False,
        }

    return {
        "kind": kind,
        "name": name,
        "path": path,
        "content": content,
        "version": versions[0]["version"] if versions else 0,
        "versions": versions,
        "history_available": True,
    }


async def _read_version_row(kind: str, name: str, version: int) -> dict[str, Any] | None:
    """Raw historical version row from the store, with no redaction applied.

    Internal use only: :func:`get_version` (the external, response-facing
    read) redacts what this returns before handing it to a caller.
    :func:`rollback_definition` reads through this directly instead, because
    a rollback has to write the real content back -- consuming already-
    redacted content would persist the placeholder text as the new version.
    """
    from lionagi.state.db import StateDB

    try:
        async with StateDB() as db:
            return await db.get_definition(kind, name, version=version)
    except Exception as exc:  # noqa: BLE001 — any unreadable store is the same answer
        _log.warning(
            "definition version is unreadable for %s/%s: %s",
            kind,
            name,
            mask_credentials(repr(exc)),
        )
        raise HistoryUnavailableError(mask_credentials(str(exc))) from exc


async def get_version(kind: str, name: str, version: int) -> dict[str, Any] | None:
    """Get a specific historical version's content.

    This route's whole answer is history, so it has nothing to fall back on:
    it either reads the store or it refuses. Raising ``HistoryUnavailableError``
    keeps that refusal distinct from ``None``, which means the store answered
    and does not have this version.
    """
    # Validate at service boundary — kind/name are used in SQL WHERE clauses
    # and, indirectly, in any path lookups that build on this function.
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    row = await _read_version_row(kind, name, version)
    if not row:
        return None

    content = row["content"]
    if kind == "agent" and demo_mode_enabled():
        content = redact_agent_markdown(content, redact=True)

    return {
        "kind": kind,
        "name": name,
        "version": row["version"],
        "content": content,
        "created_at": row["created_at"],
        "message": row["message"],
    }


async def save_definition(
    kind: str,
    name: str,
    content: str,
    message: str | None = None,
    *,
    validate: bool = True,
) -> dict[str, Any]:
    """Persist a definition version: DB write first, then disk (ADR-0077 D2); per-(kind, name) lock serialises concurrent saves.

    ``validate`` gates the cast role/mode check below, not the system-agent
    guard (which always runs). It defaults on for the direct save route --
    the door a client posts arbitrary content through, and the one the agents
    API's role/mode validation must also bind on (see ``_canonicalize_casts``
    in ``agents.py``). ``rollback_definition`` and ``snapshot_current`` pass
    ``validate=False``: they replay content that was already accepted once
    (a stored version, a pre-existing disk file), and a validator tightened
    after that content landed would make an old version un-rollback-able and
    an existing file un-importable.
    """
    # Validate at the service boundary — reject traversal sequences, path
    # separators, NUL, and glob metacharacters.
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    if kind == "skill":
        return await _save_skill_definition(name, content, message, validate=validate)

    base = KIND_DIRS.get(kind)
    if not base:
        raise ValueError(f"Unknown kind: {kind}")

    # The other write path onto agent files (PUT /agents/{name}) carries the
    # same guard -- see agents.py's update_agent(). Refusing here too closes
    # the bypass: this route upserts blindly (ADR-0077), so without this check
    # a redacted payload round-tripped through this route would overwrite the
    # real file even though the PUT route refuses the identical content.
    if kind == "agent" and demo_mode_enabled() and not content.strip():
        raise RedactedPayloadError("Refusing to save: content is missing while demo mode is active")
    if kind == "agent":
        reject_if_redacted_payload(content)

    from lionagi.libs.frontmatter import parse_frontmatter as _parse_fm

    if kind == "agent" and validate:
        new_fm, _ = _parse_fm(content)
        _canonicalize_casts(dict(new_fm))

    from lionagi.state.db import StateDB

    lock = await _lock_for(kind, name)
    async with lock:
        disk_file = await anyio.to_thread.run_sync(partial(_find_definition_file, base, name))
        if not disk_file:
            disk_file = base / f"{name}{_DEFAULT_EXT.get(kind, '.md')}"
        elif kind == "agent":
            # This route upserts blindly by design (ADR-0077), so it's the other write
            # path onto agent files besides PUT /agents/{name} -- the same "system
            # agent is not editable" rule (lionagi/studio/services/agents.py) has to
            # hold here too, or it's a bypass. Read straight off disk_file rather than
            # calling into agents.py, since that module resolves its own _AGENTS_ROOT
            # independently of this module's (test-patchable) AGENTS_DIR/KIND_DIRS.
            existing_text = await anyio.to_thread.run_sync(disk_file.read_text)
            existing_fm, _ = _parse_fm(existing_text)
            if _is_protected_system(existing_fm):
                raise PermissionError(f"Agent '{name}' is a system agent and cannot be edited")

        now = time.time()

        async with StateDB() as db:
            version = await db.save_definition(
                kind=kind,
                name=name,
                path=_relative_path(disk_file),
                content=content,
                message=message,
            )

        def _write_disk() -> None:
            ensure_lionagi_dir(disk_file.parent)
            disk_file.write_text(content)

        await anyio.to_thread.run_sync(_write_disk)

    # ADR-0077 D2: response field is "saved_at", not "created_at"
    return {
        "kind": kind,
        "name": name,
        "version": version,
        "saved_at": now,
        "message": message,
    }


async def _save_skill_definition(
    name: str, content: str, message: str | None, *, validate: bool
) -> dict[str, Any]:
    """Skill counterpart to ``save_definition``'s generic path.

    Always writes ``<SKILLS_DIR>/<name>/SKILL.md`` regardless of what shape (if
    any) currently exists on disk -- a save through Studio normalizes a skill
    to the one shape ``li skill`` actually resolves (see
    ``skills.py::_find_skill_md`` / ``lionagi/cli/skill.py``), rather than
    preserving whatever legacy layout it started from. Plugin-bundled skills
    live under a plugin directory, never under ``SKILLS_DIR``, so they are
    unreachable through this path by construction, not by an extra check.
    """
    if validate:
        from .skills import validate_skill_content

        errors = validate_skill_content(content, name)
        if errors:
            raise ValueError("; ".join(errors))

    disk_file = SKILLS_DIR / name / "SKILL.md"

    from lionagi.state.db import StateDB

    lock = await _lock_for("skill", name)
    async with lock:
        now = time.time()

        async with StateDB() as db:
            version = await db.save_definition(
                kind="skill",
                name=name,
                path=_relative_path(disk_file),
                content=content,
                message=message,
            )

        def _write_disk() -> None:
            ensure_lionagi_dir(disk_file.parent)
            disk_file.write_text(content)

        await anyio.to_thread.run_sync(_write_disk)

    return {
        "kind": "skill",
        "name": name,
        "version": version,
        "saved_at": now,
        "message": message,
    }


async def rollback_definition(kind: str, name: str, target_version: int) -> dict[str, Any] | None:
    """Restore a previous version by reading it from DB and saving it as a new version.

    Reads the target version through :func:`_read_version_row`, not
    :func:`get_version` -- the latter redacts agent content while demo mode is
    on, and a rollback that saved that redacted text would persist the
    placeholder as the new version instead of restoring the real one.
    Redaction applies to what a response shows a caller, never to what a
    write operation submits internally.
    """
    validate_name_component(kind, label="kind")
    validate_name_component(name, label="name")

    old = await _read_version_row(kind, name, target_version)
    if not old:
        return None

    from lionagi.state.db import StateDB

    # The read above already refused if the store was unreadable, so reaching
    # here means it answered once. It can still go away between the two reads,
    # and that is the same condition under the same route, so it gets the same
    # refusal rather than an uncaught error.
    try:
        async with StateDB() as db:
            latest = await db.get_definition(kind, name)
    except Exception as exc:  # noqa: BLE001 — any unreadable store is the same answer
        _log.warning(
            "definition history became unreadable mid-rollback for %s/%s: %s",
            kind,
            name,
            mask_credentials(repr(exc)),
        )
        raise HistoryUnavailableError(mask_credentials(str(exc))) from exc
    current_version = latest["version"] if latest else 0

    save_result = await save_definition(
        kind,
        name,
        old["content"],
        message=f"rollback to v{target_version}",
        validate=False,
    )

    return {
        "version": save_result["version"],
        "saved_at": save_result["saved_at"],
        "rolled_back_from": current_version,
        "rolled_back_to": target_version,
        "message": save_result["message"],
    }


async def snapshot_current(kind: str | None = None) -> int:
    """Snapshot all current disk files that don't have a matching version in DB; returns count recorded."""
    count = 0
    defs = await list_definitions(kind)

    for d in defs:
        disk_path = await anyio.to_thread.run_sync(
            partial(_find_definition_file, KIND_DIRS[d["kind"]], d["name"])
        )
        if disk_path is None:
            continue

        content = await anyio.to_thread.run_sync(disk_path.read_text)

        if d["has_versions"]:
            # Compare against the raw stored version, not get_version()'s
            # response-facing (possibly redacted) content -- otherwise an
            # unchanged agent file in demo mode never matches its own
            # redacted-placeholder comparison and gets re-snapshotted every call.
            latest = await _read_version_row(d["kind"], d["name"], d["version"])
            if latest and latest["content"] == content:
                continue

        await save_definition(
            d["kind"], d["name"], content, message="snapshot from disk", validate=False
        )
        count += 1

    return count


_EXTENSIONS = (".md", ".playbook.yaml", ".yaml")


def _find_definition_file(base: Path, name: str) -> Path | None:
    """Locate the on-disk file for a definition via literal-path joins (not glob). Symlinks outside ``base`` are intentionally left unrestricted -- restricting them would break symlinked agent definitions."""
    # Fast path 1: direct child (base/<name><ext>)
    for ext in _EXTENSIONS:
        candidate = base / f"{name}{ext}"
        if candidate.exists():
            return candidate

    # Fast path 2: nested subdir (base/<name>/<name><ext>)
    subdir = base / name
    for ext in _EXTENSIONS:
        candidate = subdir / f"{name}{ext}"
        if candidate.exists():
            return candidate

    # Fast path 3: nested subdir whose definition file doesn't share the
    # directory's name. list_definitions() names such a definition after its
    # containing directory, so fetching must resolve the same way: fall back
    # to the file that listing would have picked (same extension priority,
    # alphabetical tiebreak).
    if subdir.is_dir():
        for ext in _EXTENSIONS:
            matches = sorted(p for p in subdir.iterdir() if p.is_file() and p.name.endswith(ext))
            if matches:
                return matches[0]

    # Slow path: scan one level of subdirectories with literal candidates —
    # NOT Path.glob() with untrusted input so no metacharacter expansion occurs.
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


class SaveBody(BaseModel):
    content: str
    message: str | None = None


@studio_route("/definitions/", method="GET", area="definitions", name="list_definitions")
async def list_definitions_route(
    # "skill" is not a KIND_DIRS entry, so it never appears in this generic
    # multi-kind listing (its shape -- <name>/SKILL.md, not <name>.<ext> --
    # doesn't fit KIND_DIRS/_find_definition_file's shared scan). It is still
    # editable: GET/POST /definitions/skill/{name} route through the dedicated
    # _get_skill_definition/_save_skill_definition path below. Listing itself
    # comes from GET /skills/, which Studio's Library page already calls.
    kind: str | None = Query(default=None, description="Filter by kind: agent, playbook"),
) -> dict[str, Any]:
    return {"definitions": await list_definitions(kind)}


@studio_route("/definitions/{kind}/{name}", method="GET", area="definitions", name="get_definition")
async def get_definition_route(kind: str, name: str) -> dict[str, Any]:
    defn = await get_definition(kind, name)
    if defn is None:
        raise HTTPException(status_code=404, detail=f"Definition '{kind}/{name}' not found")
    return defn


@studio_route(
    "/definitions/{kind}/{name}/versions/{version}",
    method="GET",
    area="definitions",
    name="get_version",
)
async def get_version_route(kind: str, name: str, version: int) -> dict[str, Any]:
    # 503, not 501: every store this deployment can be configured for is one
    # StateDB can read, so failing to read it is an operational condition a
    # retry can outlive. 501 would tell the caller to stop asking.
    #
    # The driver's own message is not repeated back over HTTP. It routinely
    # quotes the connection string it failed on, which carries the store
    # password, and a caller can do nothing with it either way. The diagnosis
    # goes to the log, where it is masked.
    try:
        v = await get_version(kind, name, version)
    except HistoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=_HISTORY_UNAVAILABLE_DETAIL) from exc
    if v is None:
        raise HTTPException(
            status_code=404, detail=f"Version {version} not found for {kind}/{name}"
        )
    return v


# POST /api/definitions/{kind}/{name}
@studio_route(
    "/definitions/{kind}/{name}", method="POST", area="definitions", name="save_definition"
)
async def save_definition_route(kind: str, name: str, body: SaveBody) -> dict[str, Any]:
    # ADR-0077: an unknown kind, or content a kind's validator rejects (e.g.
    # unparseable skill frontmatter), raises ValueError in the service layer;
    # catch it and return 422 instead of propagating a 500.
    try:
        return await save_definition(kind, name, body.content, body.message)
    except (PermissionError, RedactedPayloadError) as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# version as query param, not path segment
@studio_route(
    "/definitions/{kind}/{name}/rollback",
    method="POST",
    area="definitions",
    name="rollback_definition",
)
async def rollback_definition_route(
    kind: str,
    name: str,
    version: int = Query(..., description="Target version to restore"),
) -> dict[str, Any]:
    try:
        result = await rollback_definition(kind, name, version)
    except HistoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=_HISTORY_UNAVAILABLE_DETAIL) from exc
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"Version {version} not found for {kind}/{name}"
        )
    return result


@studio_route("/definitions/snapshot", method="POST", area="definitions", name="snapshot_current")
async def snapshot_current_route(
    kind: str | None = Query(default=None),
) -> dict[str, Any]:
    count = await snapshot_current(kind)
    return {"snapshots_created": count}
