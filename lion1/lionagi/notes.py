"""Notes: per profile, persistent, named (ADR-0007/C5).

A name with the `note.` prefix is written to the agent's note store instead of the run's record.
The store outlives any run. The value carries provenance: which run and which sequence wrote it.
In memory for the actor's lifetime unless the actor is given `notes_dir`, then one JSON file per
profile; the carrier is replaceable by anything with the same five methods and `version`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

__all__ = (
    "PREFIX",
    "Note",
    "NoteStore",
    "key_of",
    "MemoryNoteStore",
    "FileNoteStore",
)

PREFIX = "note."


@dataclass(frozen=True)
class Note:
    key: str
    value: Any
    run: str  # the run that wrote it
    seq: int  # the record sequence of the write
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


class NoteStore(Protocol):
    def get(self, key: str) -> Note | None: ...
    def put(self, key: str, value: Any, *, run: str, seq: int) -> Note: ...
    def list(self, prefix: str = "") -> list[Note]: ...
    def find(self, query: str) -> list[Note]: ...
    def delete(self, key: str) -> Note | None: ...  # the note removed, or None when there was none
    @property
    def version(self) -> int: ...


def key_of(name: str) -> str | None:
    """`note.wake17` -> `wake17`; None when the name is not a note."""
    if name.startswith(PREFIX) and len(name) > len(PREFIX):
        return name[len(PREFIX) :]
    return None


class MemoryNoteStore:
    """For tests and for callers that bring their own persistence."""

    def __init__(self) -> None:
        self._notes: dict[str, Note] = {}
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def get(self, key: str) -> Note | None:
        return self._notes.get(key)

    def put(self, key: str, value: Any, *, run: str, seq: int) -> Note:
        prior = self._notes.get(key)
        note = Note(key=key, value=value, run=run, seq=seq, version=(prior.version + 1) if prior else 1)
        self._notes[key] = note
        self._version += 1
        return note

    def list(self, prefix: str = "") -> list[Note]:
        return [n for k, n in sorted(self._notes.items()) if k.startswith(prefix)]

    def find(self, query: str) -> list[Note]:
        """Case-insensitive substring over key and content; a blank query is refused, `list` is for that."""
        if not query.strip():
            raise ValueError("note.find: a blank query would match every note; use note.list(prefix=...)")
        q = query.lower()
        return [n for k, n in sorted(self._notes.items()) if q in k.lower() or q in _text(n.value).lower()]

    def delete(self, key: str) -> Note | None:
        note = self._notes.pop(key, None)
        if note is not None:
            self._version += 1
        return note


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # ValueError: a value that refers to itself
        return str(value)


class FileNoteStore(MemoryNoteStore):
    """One JSON file per profile: the file is the store. Every read loads it and every write is a
    locked read-modify-write, so two consoles over one directory keep each other's notes (a store
    that read once and rewrote whole erased the other console's notes)."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self._version = int(data.get("version", 0))
            self._notes = {k: Note(**v) for k, v in data.get("notes", {}).items()}

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / f".{self.path.name}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self._load()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @property
    def version(self) -> int:
        self._load()
        return self._version

    def get(self, key: str) -> Note | None:
        self._load()
        return super().get(key)

    def list(self, prefix: str = "") -> list[Note]:
        self._load()
        return super().list(prefix)

    def find(self, query: str) -> list[Note]:
        self._load()
        return super().find(query)

    def put(self, key: str, value: Any, *, run: str, seq: int) -> Note:
        try:
            # a value the file cannot carry is refused before the write, never coerced: JSON, and UTF-8
            # (a lone surrogate passes `json.dumps` escaped and fails at the file)
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise ValueError(f"note {key!r}: {e}; the file store carries JSON values only") from None
        with self._locked():
            note = super().put(key, value, run=run, seq=seq)
            self._flush()
        return note

    def delete(self, key: str) -> Note | None:
        with self._locked():
            note = super().delete(key)
            if note is not None:
                self._flush()
        return note

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self._version, "notes": {k: n.to_dict() for k, n in self._notes.items()}}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
