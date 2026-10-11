# Copyright (c) 2026, Reuven Cohen
# SPDX-License-Identifier: Apache-2.0
"""Atomic native RVF checkpoints with a disposable retrieval index."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import ruvector

from lionagi.protocols.graph.node import Node

from .store import RuVectorStore, _text

__all__ = ("PersistentRuVectorStore",)

_OPTIONS = (
    "dimensions",
    "capacity",
    "max_content_bytes",
    "max_metadata_bytes",
    "max_metadata_depth",
    "max_batch_size",
    "max_k",
    "ef_search",
    "max_vector_bytes",
    "max_total_payload_bytes",
    "max_tool_output_bytes",
)
_METADATA_KEY = "lionagi_state_v1"
_MAX_MANIFEST = 80 * 1024 * 1024


class PersistentRuVectorStore(RuVectorStore):
    """Commit a complete RVF checkpoint per mutation; reopen rebuilds Rust HNSW.

    Checkpoints cost O(N) I/O. One open instance holds a stable sidecar lease.
    Use a private application directory. Files are not encrypted or authenticated.
    A checkpoint failure poisons this instance; reopen the last complete snapshot.
    """

    def __init__(
        self, path: str | Path, dimensions: int, *, embedding_id: str, **store_options: Any
    ):
        self._rvf: Any = getattr(ruvector, "RvfStore", None)
        if self._rvf is None or any(
            not hasattr(self._rvf, name)
            for name in ("set_metadata", "get_metadata", "export_vectors", "metadata_recovery")
        ):
            raise RuntimeError(
                "RVF persistence requires a RuVector SDK with metadata and vector export APIs"
            )
        if type(dimensions) is not int or not 1 <= dimensions <= 4096:
            raise ValueError("RVF dimensions must be between 1 and 4096")
        super().__init__(dimensions, **store_options)
        self._embedding_id = _text(embedding_id, 256, "embedding_id")
        if not embedding_id.strip():
            raise ValueError("embedding_id must not be empty")
        self.path = Path(path).absolute()
        self._closed = False
        self._events: dict[str, tuple[str, str, float]] = {}
        self._aggregates: dict[str, dict[str, tuple[float, int]]] = {}
        self._validate_path(self.path, missing=True)
        lock_path = self.path.with_name(self.path.name + ".lionagi.lock")
        self._validate_path(lock_path, missing=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self._lease = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == "nt":
                import msvcrt

                if lock_path.stat().st_size == 0:
                    self._lease.write(b"\0")
                self._lease.seek(0)
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise RuntimeError("RVF checkpoint leasing requires POSIX or Windows")
            self._validate_path(self.path, missing=True)
            if self.path.exists():
                self._restore()
            else:
                self._checkpoint()
        except BaseException:
            self._lease.close()
            self._closed = True
            raise

    @property
    def embedding_id(self) -> str:
        return self._embedding_id

    def _configuration(self) -> dict[str, Any]:
        return {
            "version": 1,
            "embedding_id": self.embedding_id,
            **{name: getattr(self, name) for name in _OPTIONS},
        }

    def _validate_path(self, path: Path, *, missing: bool = False) -> None:
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError("symlink paths are not supported")
        parent = path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode):
            raise ValueError("RVF parent must be a directory")
        if os.name == "posix" and (parent.st_mode & 0o077 or parent.st_uid != os.getuid()):
            raise ValueError("RVF parent must be private and owned by the current user")
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                return
            raise
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("RVF must be a regular file with one link")
        if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise ValueError("RVF must be private and owned by the current user")
        if info.st_size > 2 * self.max_vector_bytes + 4 * _MAX_MANIFEST:
            raise ValueError("RVF exceeds byte budget")

    def _manifest(self) -> str:
        value = json.dumps(
            {
                "configuration": self._configuration(),
                "records": [
                    [key, record[0], json.loads(record[1])] for key, record in self._records.items()
                ],
                "events": [[event, *data] for event, data in self._events.items()],
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(value.encode("utf-8")) > _MAX_MANIFEST:
            raise ValueError("RVF metadata exceeds byte budget")
        return value

    def _checkpoint(self) -> None:
        manifest = self._manifest()
        fd, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".rvf", dir=self.path.parent
        )
        os.close(fd)
        temporary = Path(name)
        temporary.unlink()  # Native create requires a nonexistent path in this private directory.
        native = None
        try:
            native = self._rvf.create(str(temporary), self.dimensions)
            os.chmod(temporary, 0o600)
            records = list(self._records.values())
            for offset in range(0, len(records), self.max_batch_size):
                batch = records[offset : offset + self.max_batch_size]
                if native.ingest(
                    [record[2].tolist() for record in batch],
                    list(range(offset, offset + len(batch))),
                ) != len(batch):
                    raise RuntimeError("RVF checkpoint ingestion mismatch")
            self._write_manifest(native, manifest)
            native.close()
            native = None
            self._validate_path(temporary)
            self._replace_checkpoint(temporary)
        finally:
            if native is not None:
                native.close()
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_manifest(native: Any, manifest: str) -> None:
        data = manifest.encode("utf-8")
        if len(data) > _MAX_MANIFEST:
            raise ValueError("RVF metadata exceeds byte budget")
        chunks = [manifest[i : i + 1048576] for i in range(0, len(manifest), 1048576)]
        for index, chunk in enumerate(chunks):
            native.set_metadata(f"{_METADATA_KEY}.{index}", chunk)
        native.set_metadata(
            _METADATA_KEY,
            json.dumps(
                {
                    "chunks": len(chunks),
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ),
        )

    @staticmethod
    def _read_manifest(native: Any) -> str:
        header = native.get_metadata(_METADATA_KEY)
        if type(header) is not str or len(header) > 256:
            raise ValueError("missing or invalid RVF manifest header")
        info = json.loads(header)
        if type(info) is not dict or set(info) != {"chunks", "bytes", "sha256"}:
            raise ValueError("invalid RVF manifest header")
        count, size = info["chunks"], info["bytes"]
        if (
            type(count) is not int
            or not 1 <= count <= 80
            or type(size) is not int
            or not 1 <= size <= _MAX_MANIFEST
        ):
            raise ValueError("RVF manifest exceeds byte budget")
        chunks = []
        used = 0
        for index in range(count):
            chunk = native.get_metadata(f"{_METADATA_KEY}.{index}")
            if type(chunk) is not str or len(chunk) > 1048576:
                raise ValueError("missing or oversized RVF manifest chunk")
            used += len(chunk.encode("utf-8"))
            if used > size:
                raise ValueError("RVF manifest size mismatch")
            chunks.append(chunk)
        value = "".join(chunks)
        if used != size or hashlib.sha256(value.encode("utf-8")).hexdigest() != info["sha256"]:
            raise ValueError("RVF manifest checksum mismatch")
        return value

    def _replace_checkpoint(self, temporary: Path) -> None:
        # Native close fsyncs the complete file; rename exposes only complete checkpoints.
        os.replace(temporary, self.path)
        if os.name == "posix":
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _restore(self) -> None:
        native = self._rvf.open(str(self.path))
        try:
            if (
                native.dimension != self.dimensions
                or native.status()["total_vectors"] > self.capacity
            ):
                raise ValueError("RVF dimension or vector count mismatch")
            recovery = native.metadata_recovery()
            if recovery["dropped_generations"] or recovery["dropped_records"]:
                raise ValueError("RVF metadata recovery reported corruption")
            manifest = self._read_manifest(native)
            state = json.loads(manifest)
            if (
                type(state) is not dict
                or set(state) != {"configuration", "records", "events"}
                or state["configuration"] != self._configuration()
            ):
                raise ValueError("RVF schema, configuration or embedding_id mismatch")
            rows, events = state["records"], state["events"]
            if (
                type(rows) is not list
                or len(rows) > self.capacity
                or type(events) is not list
                or len(events) > 10000
            ):
                raise ValueError("RVF record or event count exceeds bounds")
            vectors = native.export_vectors(
                max_vectors=self.capacity, max_bytes=self.max_vector_bytes
            )
        finally:
            native.close()
        if len(vectors) != len(rows):
            raise ValueError("RVF manifest and vector count differ")
        vector_map = dict(vectors)
        if len(vector_map) != len(rows) or set(vector_map) != set(range(len(rows))):
            raise ValueError("RVF vector IDs must match manifest order")
        for index, row in enumerate(rows):
            if type(row) is not list or len(row) != 3:
                raise ValueError("invalid RVF record")
            key, content, metadata = row
            if type(key) is not str or str(UUID(key)) != key or key in self._records:
                raise ValueError("invalid or duplicate stored node ID")
            _text(content, self.max_content_bytes, "content")
            serialized = self._metadata(metadata)
            vector = np.array(vector_map[index], dtype=np.float32)
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
                raise ValueError("stored vector must be finite, normalized and correct dimension")
            self._payload_bytes += len(content.encode()) + len(serialized.encode())
            if self._payload_bytes > self.max_total_payload_bytes:
                raise ValueError("RVF payload exceeds byte budget")
            self._records[key] = (content, serialized, vector)
        for event in events:
            if type(event) is not list or len(event) != 4:
                raise ValueError("invalid stored feedback")
            event_id, context, key, reward = event
            self._validate_feedback(context, key, reward, event_id)
            if event_id in self._events:
                raise ValueError("duplicate stored feedback event ID")
            self._events[event_id] = (context, key, reward)
        self._reindex_feedback()
        items = list(self._records.items())
        for offset in range(0, len(items), self.max_batch_size):
            batch = items[offset : offset + self.max_batch_size]
            ids = [key for key, _ in batch]
            if self._index.insert_batch(ids, np.stack([record[2] for _, record in batch])) != ids:
                raise RuntimeError("native rebuild returned unexpected IDs")

    def _check(self) -> None:
        if self._closed:
            raise RuntimeError("persistent store is closed")
        super()._check()

    def _commit(self) -> None:
        try:
            self._checkpoint()
        except BaseException:
            self._poisoned = True
            raise

    def add_nodes(self, nodes: list[Node]) -> list[str]:
        with self._lock:
            self._check()
            ids = super().add_nodes(nodes)
            self._commit()
            return ids

    def delete(self, ids: list[str]) -> int:
        with self._lock:
            self._check()
            removed = super().delete(ids)
            if removed:
                self._events = {
                    event: data for event, data in self._events.items() if data[1] in self._records
                }
                self._reindex_feedback()
                self._commit()
            return removed

    def _validate_feedback(self, context: str, node_id: str, reward: float, event_id: str) -> None:
        for value, limit, name in (
            (context, 256, "context"),
            (node_id, 128, "node ID"),
            (event_id, 128, "event ID"),
        ):
            _text(value, limit, name)
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if node_id not in self._records:
            raise ValueError("feedback requires an existing node")
        if type(reward) not in (float, int) or not -1 <= reward <= 1 or not math.isfinite(reward):
            raise ValueError("reward must be a finite number between -1 and 1")

    def _reindex_feedback(self) -> None:
        self._aggregates = {}
        for context, key, reward in self._events.values():
            target = self._aggregates.setdefault(context, {})
            total, count = target.get(key, (0.0, 0))
            target[key] = (total + reward, count + 1)

    def record_feedback(self, context: str, node_id: str, reward: float, event_id: str) -> bool:
        """Commit one trusted reward; retries of identical evidence are idempotent."""
        with self._lock:
            self._check()
            self._validate_feedback(context, node_id, reward, event_id)
            previous = self._events.get(event_id)
            if previous is not None:
                if previous != (context, node_id, reward):
                    raise ValueError("event ID already has different feedback")
                return False
            if len(self._events) >= 10000:
                raise ValueError("feedback event capacity exceeded")
            self._events[event_id] = (context, node_id, float(reward))
            target = self._aggregates.setdefault(context, {})
            total, count = target.get(node_id, (0.0, 0))
            target[node_id] = (total + reward, count + 1)
            self._commit()
            return True

    def feedback(self, context: str, node_ids: list[str]) -> dict[str, tuple[float, int]]:
        """Read indexed reward sum and observation count for up to 1000 candidates."""
        _text(context, 256, "context")
        if type(node_ids) is not list or len(node_ids) > 1000:
            raise ValueError("node_ids must be a list with at most 1000 entries")
        for key in node_ids:
            _text(key, 128, "node ID")
        with self._lock:
            self._check()
            target = self._aggregates.get(context, {})
            return {key: target[key] for key in node_ids if key in target}

    def has_feedback(self, context: str) -> bool:
        _text(context, 256, "context")
        with self._lock:
            self._check()
            return bool(self._aggregates.get(context))

    def reset_feedback(self, context: str) -> int:
        """Forget a context's evidence and checkpoint the resulting state."""
        _text(context, 256, "context")
        with self._lock:
            self._check()
            previous = len(self._events)
            self._events = {
                event: data for event, data in self._events.items() if data[0] != context
            }
            removed = previous - len(self._events)
            if removed:
                self._reindex_feedback()
                self._commit()
            return removed

    def close(self) -> None:
        """Release the lease; every successful mutation was already checkpointed."""
        with self._lock:
            if not self._closed:
                self._lease.close()
                self._closed = True

    def __enter__(self) -> PersistentRuVectorStore:
        self._check()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
