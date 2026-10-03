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
import math
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
        # `seq` is held as given: every caller passes the record's integer sequence, and only the file store
        # renders it, so its `put` is where a non-integer is refused, before it changes anything
        prior = self._notes.get(key)
        note = Note(key=key, value=value, run=run, seq=seq, version=(prior.version + 1) if prior else 1)
        self._notes[key] = note
        self._version += 1
        return note

    def list(self, prefix: str = "") -> list[Note]:
        return [n for k, n in sorted(self._notes.items()) if k.startswith(prefix)]

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


def _utf8(text: str) -> None:
    """The one rule for a string the file cannot hold: UTF-8 cannot encode it. A lone surrogate passes
    `json.dumps` raw (`ensure_ascii=False`) and would fail at the file's encode, so `put` refuses a key, a
    run or a value by it before the store changes, and the loader a document by it before the next write
    (`_lone_surrogate`)."""
    text.encode("utf-8")


def _lone_surrogate(document: Any) -> str | None:
    """Where the first string `_utf8` refuses sits in a parsed file, as the subscripts that reach it
    (`document['notes']['k']['value'][2]`), else None. A dict's key is read before its value."""
    stack: list[tuple[Any, Any]] = [(document, None)]  # a path is a chain of (parent, part)
    while stack:
        node, path = stack.pop()
        if isinstance(node, str):
            try:
                _utf8(node)
            except UnicodeEncodeError:
                parts = []
                while path is not None:
                    path, part = path
                    parts.append(f"[{part!r}]")
                return "document" + "".join(reversed(parts))
        elif isinstance(node, dict):
            for key, value in reversed(node.items()):
                stack += [(value, (path, key)), (key, (path, key))]
        elif isinstance(node, list):
            for i, value in reversed(list(enumerate(node))):
                stack.append((value, (path, i)))
    return None


class FileNoteStore(MemoryNoteStore):
    """One JSON file per profile: the file is the store. Every read loads it and every write is a
    locked read-modify-write, so two consoles over one directory keep each other's notes (a store
    that read once and rewrote whole erased the other console's notes)."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            # the file is the store: with none, nothing is kept (a first write that failed left its
            # note in memory, readable at version 1, and the next write carried it to the file)
            self._version, self._notes = 0, {}
            return

        # every file this store writes is standard JSON: one that holds NaN or Infinity, or a number a
        # float cannot hold (`1e999` reads as infinity), is refused here, not loaded and written back by
        # the next put
        def refuse(constant: str):
            raise ValueError(
                f"{self.path}: the file holds {constant}, which is not standard JSON; repair it by hand"
            )

        def finite(number: str) -> float:
            value = float(number)
            if not math.isfinite(value):
                raise ValueError(
                    f"{self.path}: the file holds {number}, which is not a finite number; repair it by hand"
                )
            return value

        def integer(digits: str) -> int:
            try:
                return int(digits)
            except ValueError:  # past the interpreter's digit limit: int() names no file
                n = len(digits.lstrip("-"))
                raise ValueError(
                    f"{self.path}: the file holds an integer of {n} digits, past the limit int() converts; "
                    "repair it by hand"
                ) from None

        # a file another locale wrote cannot be told from a damaged UTF-8 one by its bytes, so it is
        # refused whole, never converted by a guess
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(
                f"{self.path}: the file is not UTF-8 (the byte at offset {e.start} cannot be decoded); "
                "re-encode it as UTF-8 by hand"
            ) from None
        data = json.loads(text, parse_constant=refuse, parse_float=finite, parse_int=integer)
        # valid UTF-8 can still spell a lone surrogate (`\ud800`), which parses to a string the flush
        # cannot encode: every later write would fail there, so the file is refused here
        where = _lone_surrogate(data)
        if where is not None:
            raise ValueError(
                f"{self.path}: the file holds a lone surrogate in {where}, which UTF-8 cannot carry; "
                "repair it by hand"
            )
        # a row's seq is an int, not a bool, as `put` writes it: a file holding another kind is refused
        # whole, not loaded and carried by the next write
        for k, v in data.get("notes", {}).items():
            seq = v.get("seq") if isinstance(v, dict) else None
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise ValueError(
                    f"{self.path}: the seq of note {k!r} is not an integer: {seq!r}; repair it by hand"
                )
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
            # a value the file cannot carry, or would carry changed, is refused before the write, never
            # coerced: standard JSON (no NaN or Infinity), UTF-8 (a lone surrogate passes `json.dumps` raw
            # and would fail at the file's encode), and read back equal (a tuple comes back a list, a key
            # that is not a string comes back as text, and two keys can come back as one)
            text = json.dumps(value, ensure_ascii=False, allow_nan=False)
            # every string the row persists, the key and the run beside the value's text: the flush fails
            # on any of them, and by then the store has changed
            for what, string in (("key", key), ("run", run), ("value", text)):
                try:
                    _utf8(string)
                except UnicodeEncodeError as e:
                    raise ValueError(f"the {what} is not UTF-8 text: {e}") from None
            # the sequence is written with them: NaN fails the flush, and a bool or a float that gets
            # through is written as `true` or `2.0` and read back as the sequence
            if not isinstance(seq, int) or isinstance(seq, bool):
                raise ValueError(f"the seq is not an integer: {seq!r}")
            # an integer the serializer refuses (past the interpreter's digit limit) fails the flush
            # as well, so it is rendered here as the flush renders it
            json.dumps(seq, ensure_ascii=False, allow_nan=False)
            kept = json.loads(text)  # the copy this store keeps and writes, never the caller's object
            if kept != value:
                raise ValueError("JSON would read it back changed")
        except (TypeError, ValueError, RecursionError) as e:  # RecursionError: nested past what JSON walks
            raise ValueError(f"note {key!r}: {e}; the file store carries JSON values only") from None
        with self._locked():
            note = super().put(key, kept, run=run, seq=seq)
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
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, allow_nan=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
