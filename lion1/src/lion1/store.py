"""The run's rows in khive (ADR-0025): a session and a run row when it starts, a message row for each
entry before the next call, a call row around each request to the model, the outcome at its end. All
through the `lion` pack's verbs over `Khive.exec`, so the actor's identity signs every row. `history`
reads a session's messages back as the entries a resumed run replays (`RunRequest.history`): a run
that continues another holds only its own entries, so the session and the sessions its `continues`
chain names are the whole of what a resume needs (C3)."""

from __future__ import annotations

import json
import os
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .checkpoint import entry_of, entry_row, row_json
from .khive import Khive, KhiveError, lit
from .notes import Note
from .record import Entry, Kind, ViewEntry

__all__ = ("SOURCE", "lion_database", "Store", "KhiveNoteStore")

if TYPE_CHECKING:
    from .actor import Run

SOURCE = "lion1"
_ROLE = {Kind.INPUT: "user", Kind.SYSTEM: "system", Kind.TEXT: "assistant", Kind.RESULT: "tool"}
_BATCH = 100  # ops per `kkernel exec`: the DSL's parallel form takes that many


def lion_database(config: Path, cwd: Path) -> Path:
    """The file the config routes the `lion` pack's tables to (ADR-0026 C8): a `[[backends]]` entry
    named `lion` with a `path`, the `main` entry the store requires once any backend is declared, and
    `[packs.lion] backend = "lion"`. Anything less would put the pack's tables in the main store, and so
    would a `lion` path that reaches main's own file: the binary, run in `cwd`, takes a relative path
    from there and opens one database for two names that reach one file. A `file:` path SQLite reads as
    a URI naming any file, so it is refused rather than compared. Two names of files not made yet that
    differ only in case or in Unicode form are refused too: a file system that does not tell them apart
    (this machine's) makes one file of them."""
    try:
        cfg = tomllib.loads(config.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise KhiveError(f"{config}: {e}") from None
    declared = [b for b in cfg.get("backends") or () if isinstance(b, dict)]
    backends = {b.get("name"): b for b in declared}
    lion, main = backends.get("lion"), backends.get("main")
    routed = ((cfg.get("packs") or {}).get("lion") or {}).get("backend")
    a_file = lion is not None and lion.get("path") and lion.get("kind", "sqlite") == "sqlite"
    if not a_file or main is None or routed != "lion":
        raise KhiveError(
            f"{config}: the lion pack's tables would resolve to the main store; declare "
            '[[backends]] name = "main" (the store itself) and name = "lion" with a path, and '
            '[packs.lion] backend = "lion" (ADR-0026 C8)'
        )
    if len(backends) != len(declared):
        raise KhiveError(f"{config}: two backends share a name, and the binary refuses such a config")
    for name, b in (("lion", lion), ("main", main)):
        given = str(b.get("path") or "")
        if given.startswith("file:") and b.get("kind", "sqlite") == "sqlite":
            raise KhiveError(
                f"{config}: the {name} backend's path {given} is one SQLite reads as a URI, which can name "
                "any file, the main store's included; give it a plain path (ADR-0026 C8)"
            )
    path = cwd / _home(str(lion["path"]))
    if main.get("path") and main.get("kind", "sqlite") == "sqlite":
        other = cwd / _home(str(main["path"]))
        real = [unicodedata.normalize("NFC", os.path.realpath(p)) for p in (path, other)]
        same = real[0] == real[1] or real[0].casefold() == real[1].casefold()
        if same or _identity(path) == _identity(other):
            raise KhiveError(
                f"{config}: the lion backend's path {path} is the main store's own file {other}, so the "
                "lion pack's tables would be written there (ADR-0026 C8)"
            )
    return path


def _identity(path: Path) -> tuple[int, int, str]:
    """The file `path` names as the file system tells it: the deepest part of it that exists, by device
    and inode, whatever spelling reached it (a link of either kind, `..`, a firmlink), and the names
    below it not made yet, folded to one Unicode form and one letter case."""
    real, rest = Path(os.path.realpath(path)), []
    while not real.exists() and real != real.parent:
        rest.append(real.name)
        real = real.parent
    st = real.stat()
    names = unicodedata.normalize("NFC", "/".join(reversed(rest)))
    return st.st_dev, st.st_ino, unicodedata.normalize("NFC", names.casefold())


def _home(path: str) -> str:
    """`path` with a leading `~` as the binary reads it: the home directory, and no other user's."""
    home = os.environ.get("HOME", ".")
    return home + path[1:] if path == "~" or path.startswith("~/") else path


def _text(e: Entry) -> str:
    return e.content if isinstance(e.content, str) else row_json(entry_row(e)["content"])


@dataclass
class Store:
    khive: Khive
    database: Path | None = field(init=False, default=None)  # where the pack's tables go; a double has none

    def __post_init__(self) -> None:
        if isinstance(self.khive, Khive):  # a test double carries no config and no file: nothing routes
            config = self.khive.config or self.khive.cwd / ".khive" / "config.toml"
            self.database = lion_database(config, self.khive.cwd)

    async def open(self, run: Run, *, trigger: list[str] | None = None, continues: str | None = None) -> None:
        """The session and the run, both under the run's id; the run row names what started it. A run that
        continues another writes none of the history it replays: those entries are rows of the chain."""
        run.store_flushed = len(run.request.history) if continues is not None else 0
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
            f"kind={lit(e.kind.value)}, text={lit(_text(e))}, raw={lit(row_json(row))}, "
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
        output = row_json(run.output) if run.output is not None else None
        await self.khive.exec(
            f"lion.run_close(id={lit(run.id)}, outcome={lit(outcome)}, cost={lit(run.cost)}, "
            f"turns={run.turn}, output={lit(output)}) | lion.session_close(id={lit(run.id)})"
        )

    def notes(self, profile: str) -> KhiveNoteStore:
        """The profile's notes, in the store (D6)."""
        return KhiveNoteStore(self.khive, profile)

    async def history(self, run_id: str) -> tuple[Entry, ...]:
        """The entries a run that continues `run_id` replays, in seq order: the messages of that run's
        session after those of every run its `continues` chain names, oldest first (C3). A run of the
        chain the store does not hold raises its `not_found`; a chain that comes back to a run raises."""
        sessions: list[list[dict]] = []
        named: list[str] = []
        at: str | None = run_id
        while at is not None:
            if at in named:
                raise KhiveError(f"run {at} continues itself through {' <- '.join(named)}", "invalid")
            named.append(at)
            row, got = await self.khive.exec(
                f'[lion.get(table="runs", id={lit(at)}), lion.get(table="messages", id={lit(at)})]'
            )
            sessions.append(got["rows"])
            at = row.get("continues")
        entries = []
        for rows in reversed(sessions):
            for r in rows:
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
