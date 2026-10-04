"""The run's rows in khive (ADR-0025): a session and a run row when it starts, a message row for each
entry before the next call, a call row around each request to the model, the outcome at its end. All
through the `lion` pack's verbs over `Khive.exec`, so the actor's identity signs every row. `history`
reads a session's messages back as the entries a resumed run replays (`RunRequest.history`); the
session is the whole of what a resume needs (C3)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .checkpoint import entry_of, entry_row, jsonable
from .khive import Khive, KhiveError, lit
from .notes import Note
from .record import Entry, Kind, ViewEntry

__all__ = ("SOURCE", "Store", "KhiveNoteStore")

if TYPE_CHECKING:
    from .actor import Run

SOURCE = "lion1"
_ROLE = {Kind.INPUT: "user", Kind.SYSTEM: "system", Kind.TEXT: "assistant", Kind.RESULT: "tool"}
_BATCH = 100  # ops per `kkernel exec`: the DSL's parallel form takes that many


def _text(e: Entry) -> str:
    return e.content if isinstance(e.content, str) else json.dumps(entry_row(e)["content"], default=jsonable)


@dataclass
class Store:
    khive: Khive

    async def open(self, run: Run, *, trigger: list[str] | None = None, continues: str | None = None) -> None:
        """The session and the run, both under the run's id; the run row names what started it."""
        run.store_flushed = 0
        await self.khive.exec(
            f"lion.session_open(id={lit(run.id)}, source={lit(SOURCE)}, actor={lit(run.actor.name)}, "
            f"profile={lit(run.profile.name)}) | "
            f"lion.run_open(id={lit(run.id)}, profile={lit(run.profile.name)}, session_id={lit(run.id)}, "
            f"actor={lit(run.actor.name)}, bounds={lit(json.dumps(run.bounds))}, "
            f"trigger={lit(json.dumps(trigger) if trigger is not None else None)}, "
            f"continues={lit(continues)})"
        )

    async def flush(self, run: Run) -> int:
        """Every entry appended since the last flush, as message rows; how many went."""
        entries = run.record.entries[run.store_flushed :]
        for i in range(0, len(entries), _BATCH):
            ops = ", ".join(self._append(run.id, e) for e in entries[i : i + _BATCH])
            await self.khive.exec(f"[{ops}]" if "," in ops else ops)
        run.store_flushed = len(run.record)
        return len(entries)

    def _append(self, session: str, e: Entry) -> str:
        row = entry_row(e)
        d = row["meta"].get("directive")
        return (
            f"lion.message_append(session_id={lit(session)}, seq={e.seq}, role={lit(_ROLE[e.kind])}, "
            f"kind={lit(e.kind.value)}, text={lit(_text(e))}, raw={lit(json.dumps(row, default=jsonable))}, "
            f"turn={e.turn}, alias={lit(e.alias)}, directive={lit(json.dumps(d) if d is not None else None)})"
        )

    async def call_open(self, run: Run, turn: int, model: str, view: list[ViewEntry]) -> str:
        """A call row naming the messages rendered into the request; the row's id, closed by `call_close`."""
        # a hidden entry and one standing as a summary are both rendered as a placeholder, not their value
        shown = [v.entry.seq for v in view if not v.hidden and v.summary is None]
        folded = [v.entry.seq for v in view if v.hidden or v.summary is not None]
        (got,) = await self.khive.exec(
            f"lion.call_open(session_id={lit(run.id)}, turn={turn}, model={lit(model)}, "
            f"seq_from={min(shown, default=0)}, seq_to={max(shown, default=0)}, "
            f"folded={lit(json.dumps(folded))})"
        )
        run.store_call_started = time.monotonic()
        return got["id"]

    async def call_close(
        self,
        run: Run,
        call: str,
        *,
        status: str,
        reply_seq: int | None = None,
        tokens_in: int | None = None,
        cost: float | None = None,
        error: str | None = None,
    ) -> None:
        ms = int((time.monotonic() - run.store_call_started) * 1000)
        await self.khive.exec(
            f"lion.call_close(id={lit(call)}, status={lit(status)}, reply_seq={lit(reply_seq)}, "
            f"tokens_in={lit(tokens_in)}, cost={lit(cost)}, duration_ms={ms}, error={lit(error)})"
        )

    async def close(self, run: Run) -> None:
        """What is left to write, the outcome, and the session's end."""
        await self.flush(run)
        outcome = type(run.outcome).__name__.lower() if run.outcome is not None else "stopped"
        output = json.dumps(run.output, default=jsonable) if run.output is not None else None
        await self.khive.exec(
            f"lion.run_close(id={lit(run.id)}, outcome={lit(outcome)}, cost={lit(run.cost)}, "
            f"turns={run.turn}, output={lit(output)}) | lion.session_close(id={lit(run.id)})"
        )

    def notes(self, profile: str) -> KhiveNoteStore:
        """The profile's notes, in the store (D6)."""
        return KhiveNoteStore(self.khive, profile)

    async def history(self, run_id: str) -> tuple[Entry, ...]:
        """A session's messages as the entries a resumed run replays, in seq order."""
        (got,) = await self.khive.exec(f'lion.get(table="messages", id={lit(run_id)})')
        entries = []
        for r in got["rows"]:
            e = entry_of(json.loads(r["raw"]))
            if e is not None:
                entries.append(e)
        return tuple(entries)


def _note(row: dict) -> Note:
    return Note(
        key=row["key"], value=json.loads(row["value"]), run=row["run"], seq=row["seq"], version=row["version"]
    )


@dataclass
class KhiveNoteStore:
    """A profile's notes in the store (ADR-0025 D6): the five methods of `NoteStore`, each one call of
    the binary. `find` folds ASCII case only, the store's `lower()`; `version` is the latest write's
    time, which rises with every put."""

    khive: Khive
    profile: str

    @property
    def version(self) -> int:
        return max((r["written"] for r in self._list("")), default=0)

    def _list(self, prefix: str) -> list[dict]:
        (got,) = self.khive.exec_sync(f"lion.note_list(profile={lit(self.profile)}, prefix={lit(prefix)})")
        return list(got["rows"])

    def get(self, key: str) -> Note | None:
        try:
            (got,) = self.khive.exec_sync(f"lion.note_get(profile={lit(self.profile)}, key={lit(key)})")
        except KhiveError as e:
            if e.kind == "not_found":
                return None
            raise
        return _note(got)

    def put(self, key: str, value: Any, *, run: str, seq: int) -> Note:
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise TypeError(f"note {key!r}: seq is the record's integer sequence, not {seq!r}")
        # the value JSON carries whole, or nothing is written (ADR-0007 C7): a tuple would come back a
        # list, an int key a string, NaN not at all
        try:
            text = json.dumps(value, allow_nan=False)
            copy = json.loads(text)
        except (TypeError, ValueError, RecursionError) as e:
            raise ValueError(f"note {key!r}: a value JSON cannot carry whole: {e}") from None
        if copy != value:
            raise ValueError(f"note {key!r}: a value JSON cannot carry whole (a tuple, or a non-string key)")
        (got,) = self.khive.exec_sync(
            f"lion.note_put(profile={lit(self.profile)}, key={lit(key)}, value={lit(text)}, "
            f"run={lit(run)}, seq={seq})"
        )
        return _note(got)

    def list(self, prefix: str = "") -> list[Note]:
        return [_note(r) for r in self._list(prefix)]

    def find(self, query: str) -> list[Note]:
        if not query.strip():
            raise ValueError("note.find: a blank query would match every note; use note.list(prefix=...)")
        (got,) = self.khive.exec_sync(f"lion.note_find(profile={lit(self.profile)}, query={lit(query)})")
        return [_note(r) for r in got["rows"]]

    def delete(self, key: str) -> Note | None:
        try:
            (got,) = self.khive.exec_sync(f"lion.note_delete(profile={lit(self.profile)}, key={lit(key)})")
        except KhiveError as e:
            if e.kind == "not_found":
                return None
            raise
        return _note(got)
