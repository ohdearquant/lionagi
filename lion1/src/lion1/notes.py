"""Notes: per profile, persistent, named (ADR-0007/C5).

A name with the `note.` prefix is written to the agent's note store instead of the run's record.
The store outlives any run. The value carries provenance: which run and which sequence wrote it.
In memory for the actor's lifetime, or in the store when the actor has one (`lion1.store`, ADR-0025
D6); the carrier is replaceable by anything with the same five methods and `version`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

__all__ = (
    "PREFIX",
    "Note",
    "NoteStore",
    "key_of",
    "MemoryNoteStore",
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
        # `seq` is held as given: every caller passes the record's integer sequence, and only the file store
        # renders it, so its `put` is where a non-integer is refused, before it changes anything
        prior = self._notes.get(key)
        note = Note(key=key, value=value, run=run, seq=seq, version=(prior.version + 1) if prior else 1)
        self._notes[key] = note
        self._version += 1
        return note

    def list(self, prefix: str = "") -> list[Note]:
        return [self._notes[k] for k in sorted(k for k in self._notes if k.startswith(prefix))]

    def find(self, query: str) -> list[Note]:
        """Case-insensitive substring over key and content, casefolded (`STRASSE` finds `Straße`); a blank
        query is refused, `list` is for that."""
        if not query.strip():
            raise ValueError("note.find: a blank query would match every note; use note.list(prefix=...)")
        q = query.casefold()
        return [
            n for k, n in sorted(self._notes.items()) if q in k.casefold() or q in _text(n.value).casefold()
        ]

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
