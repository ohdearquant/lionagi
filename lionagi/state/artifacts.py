# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""High-level artifact persistence with SHA-256 integrity verification.

Wraps the existing StateDB artifact methods (insert_artifact,
list_artifacts_for_invocation, list_artifacts_for_session, get_artifact)
with content-hashing and an append-only surface.

The artifacts table schema does not carry a dedicated sha256 column;
integrity data is stored inside the content JSON under the reserved
key ``_sha256`` so it round-trips through the existing storage layer
without schema changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from lionagi.state.db import StateDB


class ArtifactRow(BaseModel):
    """Typed view of a persisted artifact, including its integrity hash.

    ``id`` is the 12-character hex primary key assigned by StateDB.
    ``sha256`` is computed over the *original* content dict (before
    the ``_sha256`` sentinel is injected) so callers can round-trip
    verify without stripping internal keys.
    """

    id: str
    invocation_id: str | None = None
    session_id: str | None = None
    kind: str
    name: str
    content: dict[str, Any]
    sha256: str
    file_path: str | None = None
    created_at: float
    updated_at: float

    # ------------------------------------------------------------------
    # Pydantic config

    model_config = {"frozen": False}


def _compute_sha256(content: dict[str, Any]) -> str:
    """Stable SHA-256 over a JSON-serialised dict (sorted keys)."""
    payload = json.dumps(content, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _row_to_artifact(row: dict[str, Any]) -> ArtifactRow:
    """Convert a raw StateDB artifact dict into an ``ArtifactRow``.

    The stored content has a ``_sha256`` sentinel embedded at write
    time.  The sentinel carries the hash of the *original* (clean)
    content so we can verify on read without recomputing from the
    enriched blob.
    """
    content: dict[str, Any] = dict(row.get("content") or {})
    sha256 = content.pop("_sha256", "")
    return ArtifactRow(
        id=row["id"],
        invocation_id=row.get("invocation_id"),
        session_id=row.get("session_id"),
        kind=row["kind"],
        name=row["name"],
        content=content,
        sha256=sha256,
        file_path=row.get("file_path"),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class ArtifactStore:
    """High-level artifact persistence with SHA-256 integrity.

    Wraps :class:`StateDB` artifact methods with content-hashing and
    an append-only surface.  ``update`` and ``delete`` are intentionally
    absent; callers who need a new version should write a new artifact
    with a distinct ``name`` or ``kind``.

    The SHA-256 is computed over ``content`` before storage and
    embedded under the reserved key ``_sha256`` inside the persisted
    content blob.  :meth:`verify` re-derives the hash from the
    in-memory ``ArtifactRow.content`` and compares it to the stored
    ``ArtifactRow.sha256``.

    Usage::

        store = ArtifactStore(db)
        row = await store.write(
            kind="ci_result",
            name="pytest-run-1",
            content={"passed": True, "summary": "all green"},
            invocation_id=inv_id,
        )
        assert store.verify(row)
    """

    def __init__(self, db: StateDB) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write

    async def write(
        self,
        *,
        kind: str,
        name: str,
        content: dict[str, Any],
        invocation_id: str | None = None,
        session_id: str | None = None,
        file_path: str | None = None,
    ) -> ArtifactRow:
        """Persist an artifact and return a typed row with SHA-256.

        SHA-256 is computed over ``content`` (sorted-key JSON encoding)
        before storage.  The hash is embedded in the persisted blob
        under ``_sha256`` so it survives a round-trip through StateDB.

        StateDB's ``insert_artifact`` is idempotent on the natural key
        ``(invocation_id, session_id, kind, name)`` — calling this
        method twice with the same key updates the content in place and
        preserves the original ``id``.
        """
        sha = _compute_sha256(content)
        enriched: dict[str, Any] = {**content, "_sha256": sha}

        artifact_id = await self._db.insert_artifact(
            kind=kind,
            name=name,
            content=enriched,
            invocation_id=invocation_id,
            session_id=session_id,
            file_path=file_path,
        )

        # Fetch the persisted row so created_at / updated_at come from
        # the database rather than being approximated in Python.
        row = await self._db.get_artifact(artifact_id)
        if row is None:  # pragma: no cover — insert always succeeds
            raise RuntimeError(f"Artifact {artifact_id!r} not found after insert")
        return _row_to_artifact(row)

    # ------------------------------------------------------------------
    # Read

    async def get(self, artifact_id: str) -> ArtifactRow | None:
        """Return a single artifact by its primary-key ``id``, or None."""
        row = await self._db.get_artifact(artifact_id)
        if row is None:
            return None
        return _row_to_artifact(row)

    async def query(
        self,
        *,
        invocation_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
    ) -> list[ArtifactRow]:
        """Return artifacts matching the supplied filters.

        At least one of ``invocation_id`` or ``session_id`` should be
        provided; if both are None the method returns all artifacts that
        match ``kind`` (or all artifacts when ``kind`` is also None).

        Filtering by ``kind`` is done in Python because StateDB's list
        methods do not expose a kind predicate.
        """
        if invocation_id is not None and session_id is not None:
            # Merge the two lists and deduplicate by id.
            inv_rows = await self._db.list_artifacts_for_invocation(invocation_id)
            ses_rows = await self._db.list_artifacts_for_session(session_id)
            seen: set[str] = set()
            merged: list[dict[str, Any]] = []
            for r in inv_rows + ses_rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    merged.append(r)
            rows = merged
        elif invocation_id is not None:
            rows = await self._db.list_artifacts_for_invocation(invocation_id)
        elif session_id is not None:
            rows = await self._db.list_artifacts_for_session(session_id)
        else:
            # No parent filter — fall back to a full scan via raw SQL.
            cur = await self._db.db.execute("SELECT * FROM artifacts ORDER BY created_at ASC")
            raw = await cur.fetchall()
            rows = [self._db._row_to_dict(r) for r in raw]

        if kind is not None:
            rows = [r for r in rows if r.get("kind") == kind]

        return [_row_to_artifact(r) for r in rows]

    # ------------------------------------------------------------------
    # Integrity

    def verify(self, row: ArtifactRow) -> bool:
        """Return True iff the artifact content matches its stored SHA-256.

        Recomputes the hash from ``row.content`` (the clean dict without
        the ``_sha256`` sentinel) and compares it to ``row.sha256``.
        Returns False when the hash is missing or the content has been
        tampered with after retrieval.
        """
        if not row.sha256:
            return False
        return _compute_sha256(row.content) == row.sha256
