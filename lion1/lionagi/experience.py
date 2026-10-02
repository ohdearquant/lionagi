"""Experience across runs (ADR-0020): an immutable record of each run of an opted-in profile, kept in a
store a later run of the same experience scope recalls from before its first call.

A record holds the ask, how the run ended, the spend the runtime measured and, when the model left
one through `experience.lesson`, the lesson: the model's own claim, never a fact the runtime checked.
A store adds records and never updates or deletes one. The stores here are in memory and one JSON
file; anything with the same two async methods is a store.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import json
import math
import os
import re
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

__all__ = (
    "ExperienceRecord",
    "ExperienceStore",
    "Experience",
    "ExperienceLesson",
    "MemoryExperienceStore",
    "FileExperienceStore",
)

_Scope = tuple[str, str, str | None]  # the actor's name, the profile's name, the definition version


@dataclass(frozen=True)
class ExperienceRecord:
    """One run, after its reap. `cost` sums the finite non-negative costs the calls reported, `costed`
    counts those calls and `uncosted` the calls that reported none; `complete` holds when none did not."""

    id: str
    actor: str
    profile: str
    version: str | None
    run: str
    ask: str | None
    outcome: str
    lesson: str | None = None
    used_ids: tuple[str, ...] = ()
    model: str | None = None
    cost: float = 0.0
    costed: int = 0
    uncosted: int = 0
    complete: bool = True

    def __post_init__(self) -> None:
        try:
            full = str(uuid.UUID(self.id))
        except (TypeError, ValueError, AttributeError):
            full = None
        if full != self.id:
            raise ValueError(f"experience record id {self.id!r} is not a full UUID")
        for name in ("actor", "profile", "run", "outcome"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"experience record {name} must be a str")
        for name in ("version", "ask", "lesson", "model"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), str):
                raise TypeError(f"experience record {name} must be a str or None")
        used = self.used_ids
        if not isinstance(used, list | tuple) or not all(isinstance(i, str) for i in used):
            raise TypeError("experience record used_ids must be a tuple of str")
        object.__setattr__(self, "used_ids", tuple(used))
        cost = self.cost
        if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost) or cost < 0:
            raise ValueError(f"experience record cost {cost!r} is not a finite non-negative number")
        object.__setattr__(self, "cost", float(cost))
        for name in ("costed", "uncosted"):
            n = getattr(self, name)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ValueError(f"experience record {name} {n!r} is not a count")
        if self.complete is not (self.uncosted == 0):
            raise ValueError("experience record complete must say whether every call reported its cost")
        self.canonical().encode("utf-8")  # a value the file cannot carry is refused here, not at the file

    @property
    def scope(self) -> _Scope:
        return (self.actor, self.profile, self.version)

    def canonical(self) -> str:
        """The one serialization: same record, same bytes."""
        fields = dataclasses.asdict(self)
        return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_dict(cls, data: object) -> ExperienceRecord:
        names = {f.name for f in dataclasses.fields(cls)}
        if not isinstance(data, dict) or set(data) != names:
            raise ValueError(f"an experience record holds exactly the fields {sorted(names)}")
        return cls(**data)


class ExperienceStore(Protocol):
    async def recall(self, text: str, k: int, *, scope: _Scope) -> list[ExperienceRecord]: ...
    async def persist(self, record: ExperienceRecord) -> None: ...


@dataclass(frozen=True)
class Experience:
    """A profile's opt-in: the store, the declared model and definition version the records name, how
    many hits a recall shows (`k`), how many characters of hits the first notification holds
    (`max_chars`), and the bound on each store call in seconds (`io_timeout`)."""

    store: ExperienceStore
    model: str | None = None
    version: str | None = None
    k: int = 3
    max_chars: int = 4096
    io_timeout: float = 1.0

    def __post_init__(self) -> None:
        for name in ("k", "max_chars"):
            n = getattr(self, name)
            if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                raise ValueError(f"experience {name} {n!r} is not a positive count")
        t = self.io_timeout
        if isinstance(t, bool) or not isinstance(t, int | float) or not math.isfinite(t) or t <= 0:
            raise ValueError(f"experience io_timeout {t!r} is not a positive number of seconds")


class ExperienceLesson(BaseModel):
    """Leave a lesson for a later run of this same ask: what to do or avoid, in your own words, 1 to
    1,024 characters. used_ids names the experience records your first notification showed that you
    used; an id it did not show refuses the lesson. When the run ends, the last lesson that settled ok
    is kept; one still running when OUT{} ends the run is cancelled, so let it settle in an earlier turn."""

    __spec_name__ = "experience.lesson"
    text: str
    used_ids: list[str] = Field(default_factory=list)


_WORD = re.compile(r"\w+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def _ranked(records: Iterable[ExperienceRecord], text: str, k: int, scope: _Scope) -> list[ExperienceRecord]:
    """The scope's records with a lesson, ranked by how many distinct query words their ask and lesson
    hold, ties by id; zero overlap is no hit and a blank query recalls nothing. Scope comes before k."""
    query = _words(text)
    if not query or k < 1:
        return []
    scored = []
    for r in records:
        if r.scope != tuple(scope) or r.lesson is None:
            continue
        overlap = len(query & (_words(r.ask or "") | _words(r.lesson)))
        if overlap:
            scored.append((-overlap, r.id, r))
    return [r for _, _, r in sorted(scored, key=lambda t: t[:2])[:k]]


def _refuse_other_bytes(held: str | None, record: ExperienceRecord) -> bool:
    """True when the record is new; False for a replay of the same bytes; another record under its id
    is refused, since a record is never replaced."""
    if held is None:
        return True
    if held == record.canonical():
        return False
    raise ValueError(f"experience record {record.id} is already kept with other content; none is replaced")


class MemoryExperienceStore:
    """For tests and for callers that bring their own persistence; holds each record's canonical bytes,
    so every hit is a new copy."""

    def __init__(self) -> None:
        self._records: dict[str, str] = {}

    async def recall(self, text: str, k: int, *, scope: _Scope) -> list[ExperienceRecord]:
        records = (ExperienceRecord.from_dict(json.loads(s)) for s in self._records.values())
        return _ranked(records, text, k, scope)

    async def persist(self, record: ExperienceRecord) -> None:
        if _refuse_other_bytes(self._records.get(record.id), record):
            self._records[record.id] = record.canonical()


class FileExperienceStore:
    """One JSON file: the file is the store. Every read loads it, a read or parse error is raised (a
    store that cannot be read is not empty), and a persist is a locked read, check, add and atomic
    replace, so two stores over one file keep each other's additions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def recall(self, text: str, k: int, *, scope: _Scope) -> list[ExperienceRecord]:
        return await asyncio.to_thread(self._recall, text, k, scope)

    async def persist(self, record: ExperienceRecord) -> None:
        await asyncio.to_thread(self._persist, record)

    def _recall(self, text: str, k: int, scope: _Scope) -> list[ExperienceRecord]:
        records = (ExperienceRecord.from_dict(json.loads(s)) for s in self._read().values())
        return _ranked(records, text, k, scope)

    def _persist(self, record: ExperienceRecord) -> None:
        with self._locked():
            records = self._read()
            if _refuse_other_bytes(records.get(record.id), record):
                records[record.id] = record.canonical()
                self._write(records)

    def _read(self) -> dict[str, str]:
        """Each record's canonical bytes by id, in the order they were added; no file is no record."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise ValueError(f"{self.path}: an experience store is {{'records': [...]}}")
        out: dict[str, str] = {}
        for row in data["records"]:
            record = ExperienceRecord.from_dict(row)
            if record.id in out:
                raise ValueError(f"{self.path}: experience record {record.id} is kept twice")
            out[record.id] = record.canonical()
        return out

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / f".{self.path.name}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write(self, records: dict[str, str]) -> None:
        payload = {"records": [json.loads(s) for s in records.values()]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
