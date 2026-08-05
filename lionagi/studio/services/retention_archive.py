# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Self-verifying ZIP64 archival of rows a prune chunk is about to delete.

Each call publishes one self-contained ``.zip`` file containing a
``manifest.json`` (format version, per-table row counts, per-member SHA-256
digests) plus one ``rows/<table>.jsonl`` member per non-empty table. Rows are
canonical UTF-8 JSON (``sort_keys=True``, compact separators), one per line.

Publication is crash-safe: the archive is built in a temp file in the
destination directory, fsynced, digest-verified by reopening it, atomically
renamed into place, the destination directory fsynced, then verified again by
reopening the *final* path. A partially written or unverifiable archive never
appears under its final name, so a crash or corruption mid-write leaves
nothing a caller could mistake for a completed, trustworthy archive. Filenames
are unique per chunk and never reused, so a rerun after interruption cannot
overwrite an already-published archive.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_FORMAT_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_BYTES_MARKER = "__bytes_b64__"
_ESCAPE_MARKER = "__archive_escaped__"


class ArchiveWriteError(Exception):
    """Raised when a prune chunk's archive could not be durably written.

    Callers must treat this as a hard refusal: no delete for the chunk that
    failed to archive may proceed.
    """


class ArchiveVerificationError(ArchiveWriteError):
    """Raised when a published archive fails digest/row-count re-verification.

    A subclass of :class:`ArchiveWriteError` so existing callers that only
    catch the base class still refuse deletion on a verification failure.
    """


def archive_chunk_id(*, cutoff: float, chunk_index: int, kind: str = "prune") -> str:
    """Build a unique, append-only archive id for one prune chunk."""
    return f"{kind}-{int(cutoff)}-{chunk_index:06d}-{uuid.uuid4().hex[:8]}"


def _json_default(value: Any) -> Any:
    """Preserve BLOB columns exactly (base64) instead of stringifying their repr.

    ``json.dumps`` calls this only for values it cannot serialize natively;
    for SQLite BLOB columns that is ``bytes``/``bytearray``. Anything else
    falls back to ``str`` as before.
    """
    if isinstance(value, (bytes, bytearray)):
        return {_BYTES_MARKER: base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


def _encode_value(v: Any) -> Any:
    """Escape values that would collide with the codec markers, at any depth.

    On backends whose driver deserializes JSON columns (asyncpg returns
    ``dict``/``list`` for JSON/JSONB), a legitimate stored value of exactly
    ``{"__bytes_b64__": ...}`` (or the escape wrapper itself) would otherwise
    be misread on restore. Escaping must reach every depth because
    ``json.dumps(default=_json_default)`` converts ``bytes`` into marker
    dicts at every depth — a shallow escape paired with the deep bytes
    conversion would leave nested collisions ambiguous.
    """
    if isinstance(v, dict):
        encoded = {k: _encode_value(x) for k, x in v.items()}
        if set(v) == {_BYTES_MARKER} or set(v) == {_ESCAPE_MARKER}:
            return {_ESCAPE_MARKER: encoded}
        return encoded
    if isinstance(v, (list, tuple)):
        return [_encode_value(x) for x in v]
    return v


def _encode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Apply :func:`_encode_value` to every column of one row."""
    return {k: _encode_value(v) for k, v in row.items()}


def _decode_value(v: Any) -> Any:
    """Invert :func:`_encode_value` / :func:`_json_default`, at any depth."""
    if isinstance(v, dict):
        if set(v) == {_BYTES_MARKER} and isinstance(v[_BYTES_MARKER], str):
            return base64.b64decode(v[_BYTES_MARKER])
        if set(v) == {_ESCAPE_MARKER} and isinstance(v[_ESCAPE_MARKER], dict):
            # The wrapped dict IS the (marker-shaped) user value: decode its
            # children but never re-interpret the dict itself as a marker.
            return {k: _decode_value(x) for k, x in v[_ESCAPE_MARKER].items()}
        return {k: _decode_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_decode_value(x) for x in v]
    return v


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    """Apply :func:`_decode_value` to every column of one row."""
    return {k: _decode_value(v) for k, v in row.items()}


def _declare_zip64(zf: zipfile.ZipFile, member_name: str) -> None:
    """Raise *member_name*'s central-directory version fields to ZIP64 (4.5).

    ``force_zip64=True`` shapes only the member's LOCAL header; CPython
    computes the central-directory ``extract_version`` independently at close
    and leaves a small member at 2.0 — so an archive of small members would
    carry ZIP64 local headers that its own central directory disclaims. The
    close-time record writer takes ``max(min_version, zinfo.extract_version)``,
    so raising the attributes here is sufficient and survives close.
    """
    zinfo = zf.getinfo(member_name)
    zinfo.extract_version = max(zinfo.extract_version, zipfile.ZIP64_VERSION)
    zinfo.create_version = max(zinfo.create_version, zipfile.ZIP64_VERSION)


def _write_member_stream(
    zf: zipfile.ZipFile, member_name: str, rows: Sequence[Mapping[str, Any]]
) -> tuple[int, str]:
    """Stream *rows* into zip member *member_name* one line at a time.

    Never holds the member's full JSONL payload as a single ``bytes`` object
    -- each row is encoded, written, and hashed in turn -- so peak memory for
    a member is one row, not the whole table. Returns ``(row_count, sha256_hex)``.
    """
    hasher = hashlib.sha256()
    count = 0
    with zf.open(member_name, mode="w", force_zip64=True) as fh:
        for r in rows:
            line = (
                json.dumps(
                    _encode_row(r), default=_json_default, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            ).encode("utf-8")
            fh.write(line)
            hasher.update(line)
            count += 1
    _declare_zip64(zf, member_name)
    return count, hasher.hexdigest()


def _cleanup_path(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def write_archive_chunk(
    destination: Path,
    archive_id: str,
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    preimages: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> Path:
    """Durably publish *tables* (table name -> rows) as one ZIP64-capable archive.

    *preimages* (table name -> rows) captures the pre-mutation state of rows
    that a caller is about to NULLIFY (soft-FK columns) rather than delete --
    e.g. ``artifacts``/``plays``/``team_messages``/``dispatch_outbox`` rows
    whose ``session_id`` a session-prune chunk is about to null out. They are
    written as sibling ``preimages/<table>.jsonl`` members alongside the
    ``rows/<table>.jsonl`` members for deleted rows, so a restore can recover
    the original linkage instead of leaving those rows permanently orphaned.

    Writes to a temp file in *destination*, fsyncs it, verifies it can be
    reopened and its digests recomputed, renames it atomically to the final
    path, fsyncs the destination directory, then verifies the *published*
    file the same way. Raises :class:`ArchiveWriteError` (or the
    :class:`ArchiveVerificationError` subclass) on any failure and leaves no
    partial or unverifiable file under the final name -- including when the
    failure is detected only after the rename, in which case the published
    path is removed too.
    """
    preimages = preimages or {}
    final_path = destination / f"{archive_id}.zip"
    tmp_path = destination / f".{archive_id}.tmp"
    renamed = False

    try:
        members_meta: dict[str, dict[str, Any]] = {}
        with zipfile.ZipFile(
            tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as zf:
            # Stream each member's rows straight into the zip entry (see
            # _write_member_stream) instead of building the full JSONL bytes
            # up front -- peak memory is one row, not one table. Every entry
            # (including the manifest, written last since it depends on the
            # digests computed here) uses force_zip64=True so the archive is
            # genuinely ZIP64 (version-needed-to-extract 4.5), not merely
            # "ZIP64-capable" in name only via writestr's size-triggered default.
            for prefix, group in (("rows", tables), ("preimages", preimages)):
                for name, rows in group.items():
                    if not rows:
                        continue
                    member_name = f"{prefix}/{name}.jsonl"
                    count, digest = _write_member_stream(zf, member_name, rows)
                    members_meta[member_name] = {"rows": count, "sha256": digest}

            manifest = {
                "format_version": _FORMAT_VERSION,
                "archive_id": archive_id,
                "row_counts": {name: len(rows) for name, rows in tables.items()},
                "preimage_row_counts": {name: len(rows) for name, rows in preimages.items()},
                "members": members_meta,
            }
            manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            with zf.open(_MANIFEST_NAME, mode="w", force_zip64=True) as fh:
                fh.write(manifest_bytes)
            _declare_zip64(zf, _MANIFEST_NAME)

        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

        # Verify the staged archive before it is ever visible under its final
        # name: a mismatch here means the destination never had a publish to
        # roll back.
        verify_archive_chunk(tmp_path)

        os.replace(tmp_path, final_path)
        renamed = True
        dir_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        # Reopen-and-verify the *published* file per the archive contract:
        # a successful close/rename does not by itself prove a readable,
        # digest-correct artifact.
        verify_archive_chunk(final_path)
    except ArchiveVerificationError:
        _cleanup_path(final_path if renamed else tmp_path)
        raise
    except OSError as exc:
        _cleanup_path(final_path if renamed else tmp_path)
        raise ArchiveWriteError(
            f"failed to write archive {archive_id!r} to {destination}: {exc}"
        ) from exc
    except Exception as exc:
        # Any other failure (e.g. a bad row that can't be JSON-encoded) must
        # still leave no partial file behind under either name.
        _cleanup_path(final_path if renamed else tmp_path)
        raise ArchiveWriteError(
            f"failed to write archive {archive_id!r} to {destination}: {exc}"
        ) from exc

    return final_path


def verify_archive_chunk(path: Path) -> dict[str, Any]:
    """Reopen an archive and confirm its CRCs, digests, and row counts match its manifest.

    Returns the decoded manifest on success. Raises
    :class:`ArchiveVerificationError` on any CRC failure, unknown format
    version, digest mismatch, or row-count mismatch.
    """
    try:
        with zipfile.ZipFile(path, mode="r") as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                raise ArchiveVerificationError(
                    f"archive {path} failed CRC check on member {bad_member!r}"
                )
            manifest = json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))
            if manifest.get("format_version") != _FORMAT_VERSION:
                raise ArchiveVerificationError(
                    f"archive {path} has unsupported format_version "
                    f"{manifest.get('format_version')!r}"
                )
            for member_name, meta in manifest.get("members", {}).items():
                payload = zf.read(member_name)
                digest = hashlib.sha256(payload).hexdigest()
                if digest != meta.get("sha256"):
                    raise ArchiveVerificationError(
                        f"archive {path} member {member_name!r} digest mismatch"
                    )
                row_count = payload.count(b"\n") if payload else 0
                if row_count != meta.get("rows"):
                    raise ArchiveVerificationError(
                        f"archive {path} member {member_name!r} row-count mismatch"
                    )
    except ArchiveVerificationError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ArchiveVerificationError(f"archive {path} failed verification: {exc}") from exc
    return manifest


def _read_members(
    zf: zipfile.ZipFile, names: set[str], row_counts: Mapping[str, int], prefix: str
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for name in row_counts:
        member_name = f"{prefix}/{name}.jsonl"
        if member_name not in names:
            result[name] = []
            continue
        raw = zf.read(member_name).decode("utf-8")
        result[name] = [_decode_row(json.loads(line)) for line in raw.splitlines() if line]
    return result


def read_archive_chunk(path: Path) -> dict[str, Any]:
    """Decode a ``.zip`` archive written by :func:`write_archive_chunk`.

    Returns ``{"archive_id", "row_counts", "tables": {name: [row, ...]},
    "preimages": {name: [row, ...]}}``. ``preimages`` is ``{}`` for archives
    written before soft-FK preimage capture existed.
    """
    with zipfile.ZipFile(path, mode="r") as zf:
        manifest = json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))
        names = set(zf.namelist())
        tables = _read_members(zf, names, manifest.get("row_counts", {}), "rows")
        preimages = _read_members(zf, names, manifest.get("preimage_row_counts", {}), "preimages")
    return {
        "archive_id": manifest["archive_id"],
        "row_counts": manifest["row_counts"],
        "tables": tables,
        "preimages": preimages,
    }
