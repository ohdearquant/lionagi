"""Notes: per profile, persistent, named (ADR-0007/C5).

A name with the `note.` prefix is written to the agent's note store instead of the run's record.
The store outlives any run. The value carries provenance: which run and which sequence wrote it.
The notes are rows of the actor's store (`lion1.store`, ADR-0025 D6); the carrier is replaceable by
anything with the same five methods and `version`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

__all__ = (
    "PREFIX",
    "Note",
    "NoteStore",
    "key_of",
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
