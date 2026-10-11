# Copyright (c) 2026, Reuven Cohen
# SPDX-License-Identifier: Apache-2.0
"""Immutable native RVF transactions published by an atomic control pointer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np

from lionagi.protocols.graph.node import Node

from .persistent import PersistentRuVectorStore
from .store import _text

__all__ = ("JournaledRuVectorStore",)

_NAME = re.compile(r"segment-[0-9a-f]{32}\.rvf\Z")
_POINTER_BYTES = 65536


class JournaledRuVectorStore(PersistentRuVectorStore):
    """Single owner RVF delta journal with bounded replay and explicit compaction.

    CURRENT is a small control pointer, never document storage. Each acknowledged
    mutation publishes a complete immutable native RVF transaction. Authentication
    and encryption remain application responsibilities.
    """

    def __init__(
        self,
        directory: str | Path,
        dimensions: int,
        *,
        embedding_id: str,
        max_segments: int = 64,
        max_journal_bytes: int = 1073741824,
        **store_options: Any,
    ) -> None:
        if type(max_segments) is not int or not 2 <= max_segments <= 256:
            raise ValueError("max_segments must be between 2 and 256")
        if type(max_journal_bytes) is not int or not 1048576 <= max_journal_bytes <= 2147483648:
            raise ValueError("max_journal_bytes must be between 1 MiB and 2 GiB")
        self.max_journal_bytes = max_journal_bytes
        self._journal_bytes = 0
        directory = Path(directory).absolute()
        for part in (directory, *directory.parents):
            if part.is_symlink():
                raise ValueError("symlink paths are not supported")
        if not directory.exists():
            parent = directory.parent.stat()
            if not stat.S_ISDIR(parent.st_mode):
                raise ValueError("journal parent must be a directory")
            if os.name == "posix" and (parent.st_mode & 0o077 or parent.st_uid != os.getuid()):
                raise ValueError("journal creation requires a private owned parent")
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        self.max_segments = max_segments
        self._segments: list[dict[str, str]] = []
        self._generation = 0
        super().__init__(
            directory / "CURRENT", dimensions, embedding_id=embedding_id, **store_options
        )

    def _configuration(self) -> dict[str, Any]:
        return {
            **super()._configuration(),
            "journal_version": 1,
            "max_segments": self.max_segments,
            "max_journal_bytes": self.max_journal_bytes,
        }

    def _commit(self) -> None:
        # Parent mutations validate and change memory; the wrapper publishes their delta.
        pass

    def _sync_directory(self) -> None:
        if os.name == "posix":
            fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1048576), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _publish_pointer(self, segments: list[dict[str, str]], generation: int) -> None:
        data = json.dumps(
            {"version": 1, "generation": generation, "segments": segments},
            separators=(",", ":"),
        ).encode()
        if len(data) > _POINTER_BYTES:
            raise ValueError("journal pointer exceeds budget")
        fd, name = tempfile.mkstemp(prefix=".CURRENT.", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._replace_checkpoint(temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_transaction(self, operation: str, ids: list[str], payload: Any) -> None:
        generation = self._generation + 1
        segment = self.path.parent / f"segment-{uuid4().hex}.rvf"
        rows = [[key, self._records[key][0], json.loads(self._records[key][1])] for key in ids]
        value = {
            "configuration": self._configuration(),
            "generation": generation,
            "operation": operation,
            "records": rows,
            "payload": payload,
        }
        manifest = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        native = None
        try:
            native = self._rvf.create(str(segment), self.dimensions)
            os.chmod(segment, 0o600)
            for offset in range(0, len(ids), self.max_batch_size):
                batch = ids[offset : offset + self.max_batch_size]
                if native.ingest(
                    [self._records[key][2].tolist() for key in batch],
                    list(range(offset, offset + len(batch))),
                ) != len(batch):
                    raise RuntimeError("RVF transaction ingestion mismatch")
            self._write_manifest(native, manifest)
            native.close()
            native = None
            self._validate_path(segment)
            size = segment.stat().st_size
            if operation != "base" and self._journal_bytes + size > self.max_journal_bytes:
                segment.unlink()
                self._checkpoint()
                self._collect()
                return
            if size > self.max_journal_bytes:
                raise ValueError("RVF base exceeds active journal byte budget")
            self._sync_directory()
            entry = {"name": segment.name, "sha256": self._digest(segment)}
            segments = [entry] if operation == "base" else [*self._segments, entry]
            self._publish_pointer(segments, generation)
            self._segments = segments
            self._generation = generation
            self._journal_bytes = size if operation == "base" else self._journal_bytes + size
        finally:
            if native is not None:
                native.close()
        # Unreferenced files are harmless after failure and are reclaimed by compaction.

    def _checkpoint(self) -> None:
        self._write_transaction(
            "base", list(self._records), [[event, *data] for event, data in self._events.items()]
        )

    def _delta(self, operation: str, ids: list[str], payload: Any) -> None:
        try:
            if len(self._segments) >= self.max_segments:
                self._checkpoint()
                self._collect()
            else:
                self._write_transaction(operation, ids, payload)
        except BaseException:
            self._poisoned = True
            raise

    def _collect(self) -> None:
        retained = {entry["name"] for entry in self._segments}
        for path in self.path.parent.iterdir():
            if _NAME.fullmatch(path.name) and path.name not in retained:
                try:
                    self._validate_path(path)
                except (ValueError, FileNotFoundError):
                    continue
                path.unlink()
        self._sync_directory()

    def compact(self) -> None:
        """Publish a complete current base, then reclaim unreferenced managed RVFs."""
        with self._lock:
            self._assert_writable()
            try:
                self._checkpoint()
                self._collect()
            except BaseException:
                self._poisoned = True
                raise

    async def acompact(self) -> None:
        await asyncio.to_thread(self.compact)

    @property
    def segment_count(self) -> int:
        with self._lock:
            self._check()
            return len(self._segments)

    def add_nodes(self, nodes: list[Node]) -> list[str]:
        with self._lock:
            ids = super().add_nodes(nodes)
            if ids:
                self._delta("add", ids, None)
            return ids

    def delete(self, ids: list[str]) -> int:
        with self._lock:
            removed = super().delete(ids)
            if removed:
                self._delta("delete", [], ids)
            return removed

    def _delete_many(self, ids: list[str]) -> int:
        """Delete a trusted lifecycle batch in one atomic journal transaction."""
        if type(ids) is not list or not 1 <= len(ids) <= self.capacity:
            raise ValueError("bulk deletion IDs must be a bounded nonempty list")
        validated = {_text(key, 128, "node ID") for key in ids}
        with self._lock:
            self._assert_writable()
            remaining = {key: value for key, value in self._records.items() if key not in validated}
            removed = len(self._records) - len(remaining)
            if not removed:
                return 0
            index = self._new_index()
            items = list(remaining.items())
            for offset in range(0, len(items), self.max_batch_size):
                batch = items[offset : offset + self.max_batch_size]
                keys = [key for key, _ in batch]
                if index.insert_batch(keys, np.stack([value[2] for _, value in batch])) != keys:
                    raise RuntimeError("native rebuild returned unexpected IDs")
            self._index, self._records = index, remaining
            self._payload_bytes = sum(
                len(content.encode()) + len(metadata.encode())
                for content, metadata, _ in remaining.values()
            )
            self._events = {
                event: data for event, data in self._events.items() if data[1] in remaining
            }
            self._reindex_feedback()
            self._delta("delete", [], ids)
            return removed

    def record_feedback(self, context: str, node_id: str, reward: float, event_id: str) -> bool:
        with self._lock:
            changed = super().record_feedback(context, node_id, reward, event_id)
            if changed:
                self._delta("feedback", [], [event_id, context, node_id, float(reward)])
            return changed

    def reset_feedback(self, context: str) -> int:
        with self._lock:
            removed = super().reset_feedback(context)
            if removed:
                self._delta("reset", [], context)
            return removed

    def _restore(self) -> None:
        if self.path.stat().st_size > _POINTER_BYTES:
            raise ValueError("journal pointer exceeds budget")
        pointer = json.loads(self.path.read_text())
        if (
            type(pointer) is not dict
            or set(pointer) != {"version", "generation", "segments"}
            or type(pointer["version"]) is not int
            or pointer["version"] != 1
            or type(pointer["generation"]) is not int
            or pointer["generation"] < 1
        ):
            raise ValueError("invalid journal pointer")
        entries = pointer["segments"]
        if type(entries) is not list or not 1 <= len(entries) <= self.max_segments:
            raise ValueError("journal segment count exceeds bounds")
        active_bytes = 0
        for entry in entries:
            if (
                type(entry) is not dict
                or type(entry.get("name")) is not str
                or not _NAME.fullmatch(entry["name"])
            ):
                raise ValueError("invalid journal segment reference")
            path = self.path.parent / entry["name"]
            self._validate_path(path)
            active_bytes += path.stat().st_size
            if active_bytes > self.max_journal_bytes:
                raise ValueError("active RVF journal exceeds byte budget")
        self._journal_bytes = active_bytes
        previous = None
        names = set()
        for index, entry in enumerate(entries):
            if (
                type(entry) is not dict
                or set(entry) != {"name", "sha256"}
                or type(entry["name"]) is not str
                or not _NAME.fullmatch(entry["name"])
                or entry["name"] in names
                or type(entry["sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            ):
                raise ValueError("invalid journal segment reference")
            names.add(entry["name"])
            path = self.path.parent / entry["name"]
            self._validate_path(path)
            if self._digest(path) != entry["sha256"]:
                raise ValueError("journal segment checksum mismatch")
            native = self._rvf.open(str(path))
            try:
                recovery = native.metadata_recovery()
                if (
                    native.dimension != self.dimensions
                    or recovery["dropped_generations"]
                    or recovery["dropped_records"]
                ):
                    raise ValueError("invalid or recovered RVF segment")
                state = json.loads(self._read_manifest(native))
                vectors = native.export_vectors(
                    max_vectors=self.capacity, max_bytes=self.max_vector_bytes
                )
            finally:
                native.close()
            if (
                type(state) is not dict
                or set(state) != {"configuration", "generation", "operation", "records", "payload"}
                or state["configuration"] != self._configuration()
                or type(state["generation"]) is not int
                or state["generation"] < 1
                or (previous is not None and state["generation"] != previous + 1)
                or (index == 0 and state["operation"] != "base")
                or (index > 0 and state["operation"] == "base")
            ):
                raise ValueError("invalid journal transaction chain")
            previous = state["generation"]
            self._replay(state, vectors)
        if previous is None or previous != pointer["generation"]:
            raise ValueError("journal generation mismatch")
        self._segments = entries
        self._generation = previous
        self._reindex_feedback()
        items = list(self._records.items())
        for offset in range(0, len(items), self.max_batch_size):
            batch = items[offset : offset + self.max_batch_size]
            ids = [key for key, _ in batch]
            if self._index.insert_batch(ids, np.stack([record[2] for _, record in batch])) != ids:
                raise RuntimeError("native rebuild returned unexpected IDs")

    def _replay(self, state: dict[str, Any], vectors: list[Any]) -> None:
        operation, rows, payload = state["operation"], state["records"], state["payload"]
        if operation not in ("base", "add", "delete", "feedback", "reset"):
            raise ValueError("unknown journal operation")
        if type(rows) is not list or len(rows) > self.capacity or len(vectors) != len(rows):
            raise ValueError("invalid transaction record count")
        if operation not in ("base", "add") and rows:
            raise ValueError("unexpected transaction records")
        vector_map = dict(vectors)
        if len(vector_map) != len(rows) or set(vector_map) != set(range(len(rows))):
            raise ValueError("invalid transaction vector IDs")
        for index, row in enumerate(rows):
            if type(row) is not list or len(row) != 3:
                raise ValueError("invalid stored record")
            key, content, metadata = row
            if type(key) is not str or str(UUID(key)) != key or key in self._records:
                raise ValueError("invalid or duplicate node ID")
            _text(content, self.max_content_bytes, "content")
            serialized = self._metadata(metadata)
            vector = np.asarray(vector_map[index], dtype=np.float32)
            if (
                vector.shape != (self.dimensions,)
                or not np.isfinite(vector).all()
                or not math.isclose(
                    float(np.linalg.norm(vector.astype(np.float64))),
                    1.0,
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                )
            ):
                raise ValueError("invalid stored vector")
            self._records[key] = (content, serialized, vector)
            self._payload_bytes += len(content.encode()) + len(serialized.encode())
        if len(self._records) > self.capacity or self._payload_bytes > self.max_total_payload_bytes:
            raise ValueError("journal state exceeds budget")
        if operation == "add":
            if payload is not None:
                raise ValueError("unexpected add payload")
        elif operation == "delete":
            if type(payload) is not list or len(payload) > self.capacity:
                raise ValueError("invalid deletion payload")
            for key in payload:
                _text(key, 128, "node ID")
                record = self._records.pop(key, None)
                if record is not None:
                    self._payload_bytes -= len(record[0].encode()) + len(record[1].encode())
            self._events = {
                event: data for event, data in self._events.items() if data[1] in self._records
            }
        elif operation == "reset":
            _text(payload, 256, "context")
            self._events = {
                event: data for event, data in self._events.items() if data[0] != payload
            }
        else:
            events = payload if operation == "base" else [payload]
            if type(events) is not list or len(events) > 10000:
                raise ValueError("invalid feedback payload")
            for event in events:
                if type(event) is not list or len(event) != 4:
                    raise ValueError("invalid stored feedback")
                event_id, context, key, reward = event
                self._validate_feedback(context, key, reward, event_id)
                if event_id in self._events:
                    raise ValueError("duplicate feedback event ID")
                self._events[event_id] = (context, key, float(reward))
            if len(self._events) > 10000:
                raise ValueError("feedback capacity exceeded")
