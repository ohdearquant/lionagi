"""The run's rows in khive (ADR-0025): a session and a run row when it starts, a message row for each
entry before the next call, a call row around each request to the model, the outcome at its end. All
through the `lion` pack's verbs over `Khive.exec`, so the actor's identity signs every row. `history`
reads a session's messages back as the entries a resumed run replays (`RunRequest.history`): a run
that continues another holds only its own entries, so the session and the sessions its `continues`
chain names are the whole of what a resume needs (C3). `append_messages` and `read_messages` are the
one way message rows are written and read, here and for a harness session's take: a field too large
for one argument travels as pieces in the blob store (D8), and a session is read a page at a time
(D9)."""

from __future__ import annotations

import base64
import json
import math
import os
import time
import tomllib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from .checkpoint import entry_of, entry_row, row_json
from .khive import ARGUMENT_BYTES, Khive, KhiveError, lit
from .notes import Note
from .record import Entry, Kind, ViewEntry

__all__ = (
    "SOURCE",
    "CallStatus",
    "INLINE_BYTES",
    "PIECE_BYTES",
    "REFS_INLINE",
    "PIECES_PER_READ",
    "lion_database",
    "append_messages",
    "read_messages",
    "Store",
    "KhiveNoteStore",
)

if TYPE_CHECKING:
    from .actor import Run

SOURCE = "lion1"
# how a model call closed: a reply taken, a reply the backend refused to hand on, a call an interrupt
# cut, a call that raised or that the run's end left open
CallStatus = Literal["ok", "refused", "cut", "failed"]
_CALL_STATUSES = get_args(CallStatus)
_ROLE = {Kind.INPUT: "user", Kind.SYSTEM: "system", Kind.TEXT: "assistant", Kind.RESULT: "tool"}
_BATCH = 100  # ops per `kkernel exec`: the DSL's parallel form takes that many
# A message's `text`, `raw` or `directive` travels inline while its DSL literal fits INLINE_BYTES; past it,
# as pieces in the blob store, which the row names (D8). The three fields at that size and the row's
# other arguments stay inside one argument.
_CARRIABLE = ("text", "raw", "directive")
INLINE_BYTES = 40 * 1024
PIECE_BYTES = 90 * 1024  # a piece's base64 (122880 characters) and its call stay inside one argument
REFS_INLINE = 256  # past this many pieces, a field's refs are themselves carried, as an index
PIECES_PER_READ = 30  # their base64 stays under the daemon's 4 MiB budget for one call's answers


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


def _shape(size: int) -> dict:
    """The manifest a field of `size` bytes is carried under, with stand-in refs of the real length: what
    the row's call will weigh, known before any piece is written."""
    k = -(-size // PIECE_BYTES)
    if k <= REFS_INLINE:
        return {"size": size, "pieces": ["0" * 64] * k}
    return {"size": size, "index": _shape(64 * k)}


async def _carry(khive: Khive, data: bytes) -> dict:
    """`data` written to the blob store as pieces, one `blob.put` each, which keeps no upload open and so
    needs no daemon; its manifest, the refs listed, or past REFS_INLINE carried in turn as an index."""
    refs = []
    for i in range(0, len(data), PIECE_BYTES):
        (put,) = await khive.exec(
            f"blob.put(bytes={lit(base64.b64encode(data[i : i + PIECE_BYTES]).decode())})"
        )
        refs.append(put["content_ref"])
    if len(refs) <= REFS_INLINE:
        return {"size": len(data), "pieces": refs}
    return {"size": len(data), "index": await _carry(khive, "".join(refs).encode())}


def _op(verb: str, session_id: str, args: dict) -> str:
    named = ", ".join(f"{k}={lit(v)}" for k, v in args.items())
    return f"lion.{verb}(session_id={lit(session_id)}, {named})"


async def _message_op(khive: Khive, session_id: str, row: dict) -> str:
    """The row's append. A field past INLINE_BYTES is carried, and the row goes by the verb that takes
    pieces; the call is weighed first, by either verb, so a row still past one argument is refused
    before any piece and on every platform alike."""
    big = {
        name: value.encode()
        for name in _CARRIABLE
        if isinstance(value := row.get(name), str) and len(lit(value)) > INLINE_BYTES
    }
    args = {**row, **{name: "" if name == "text" else None for name in big}}
    verb = "message_append_carried" if big else "message_append"
    shape = {"carried": {n: _shape(len(b)) for n, b in big.items()}} if big else {}
    weighed = _op(verb, session_id, {**args, **shape})
    if len(weighed.encode()) > ARGUMENT_BYTES:
        raise KhiveError(
            f"session {session_id} seq {row['seq']}: its call is {len(weighed.encode())} bytes"
            + (" with its large fields carried" if big else "")
            + f", past the {ARGUMENT_BYTES} of one argument; nothing of it was written",
            "invalid",
        )
    if not big:
        return weighed
    carried = {name: await _carry(khive, data) for name, data in big.items()}
    return _op(verb, session_id, {**args, "carried": carried})


async def append_messages(
    khive: Khive, session_id: str, rows: Sequence[dict], *, per_call: int = _BATCH
) -> None:
    """`rows` as the session's message rows, in order, each a dict of `lion.message_append`'s arguments
    but the session. The rows go in calls of at most `per_call`, each string within `ARGUMENT_BYTES`, so
    the rows before one that is refused are written."""
    batch: list[str] = []
    size = 0  # the batch's string as sent: its ops, ", " between them, and the brackets

    async def send() -> None:
        if batch:
            await khive.exec(f"[{', '.join(batch)}]" if len(batch) > 1 else batch[0])

    for row in rows:
        try:
            op = await _message_op(khive, session_id, row)
        except KhiveError:
            await send()
            raise
        n = len(op.encode())
        if batch and len(batch) < per_call and size + 2 + n <= ARGUMENT_BYTES:
            batch.append(op)
            size += 2 + n
        else:
            await send()
            batch, size = [op], 2 + n
    await send()


async def read_messages(
    khive: Khive, session_id: str, *, carried: bool = True, start: int = 0, end: int | None = None
) -> list[dict]:
    """The session's message rows in seq order, a page at a time (D9), those from seq `start` and, when
    `end` is given, before it: the paging stops at the first page reaching `end`, whose rows past it
    are dropped unfetched, and an empty interval reads nothing. With `carried`, each carried field of a
    row read is fetched from its pieces and checked against its size, so a row reads as it was written;
    without, a carried field stays empty and only the rows are read, as a count needs."""
    if end is not None and end <= start:
        return []
    rows: list[dict] = []
    after: int | None = start - 1 if start > 0 else None
    while True:
        args = f"session_id={lit(session_id)}" + (f", after={after}" if after is not None else "")
        (page,) = await khive.exec(f"lion.messages({args})")
        rows.extend(page["rows"])
        nxt = page.get("next_after")
        if nxt is None:
            break
        if after is not None and nxt <= after:
            raise KhiveError(f"lion.messages of {session_id} did not move past seq {after}", "invalid")
        if end is not None and nxt >= end - 1:  # every row before `end` is read
            break
        after = nxt
    if end is not None:
        rows = [r for r in rows if r["seq"] < end]
    if carried:
        await _fetch(khive, session_id, rows)
    return rows


async def _pieces(khive: Khive, refs: list[str]) -> dict[str, bytes]:
    """Each distinct ref's bytes, PIECES_PER_READ to a call."""
    distinct = list(dict.fromkeys(refs))
    got: dict[str, bytes] = {}
    for i in range(0, len(distinct), PIECES_PER_READ):
        group = distinct[i : i + PIECES_PER_READ]
        ops = ", ".join(f"blob.get(content_ref={lit(ref)})" for ref in group)
        for ref, answer in zip(group, await khive.exec(f"[{ops}]" if len(group) > 1 else ops), strict=True):
            got[ref] = base64.b64decode(answer["bytes"])
    return got


def _whole(where: str, manifest: dict, refs: list[str], got: dict[str, bytes]) -> bytes:
    data = b"".join(got[ref] for ref in refs)
    if len(data) != manifest["size"]:
        raise KhiveError(f"{where} reads {len(data)} bytes, not its {manifest['size']}", "invalid")
    return data


async def _refs(khive: Khive, where: str, manifest: dict) -> list[str]:
    """A manifest's refs in order: listed, or read from its index."""
    if "pieces" in manifest:
        return manifest["pieces"]
    index = manifest["index"]
    refs = await _refs(khive, f"{where} index", index)
    text = _whole(f"{where} index", index, refs, await _pieces(khive, refs)).decode()
    return [text[i : i + 64] for i in range(0, len(text), 64)]


async def _fetch(khive: Khive, session_id: str, rows: list[dict]) -> None:
    fields = []
    for r in rows:
        for name, manifest in json.loads(r.get("carried") or "{}").items():
            where = f"session {session_id} seq {r['seq']}: carried {name}"
            fields.append((r, name, where, manifest, await _refs(khive, where, manifest)))
    got = await _pieces(khive, [ref for *_, refs in fields for ref in refs])
    for r, name, where, manifest, refs in fields:
        r[name] = _whole(where, manifest, refs, got).decode()


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

    async def open(
        self,
        run: Run,
        *,
        trigger: list[str] | None = None,
        continues: str | None = None,
        step: dict[str, Any] | None = None,
    ) -> None:
        """The session and the run, both under the run's id; the run row names what started it. A run that
        continues another writes none of the history it replays: those entries are rows of the chain. A
        run that is a step of a flow writes its step row in the same op (ADR-0026 D4), and `close` closes
        it, so no step row stands without its run."""
        run.store_flushed = len(run.request.history) if continues is not None else 0
        await self.khive.exec(
            f"lion.session_open(id={lit(run.id)}, source={lit(SOURCE)}, actor={lit(run.actor.name)}, "
            f"profile={lit(run.profile.name)}) | "
            f"lion.run_open(id={lit(run.id)}, profile={lit(run.profile.name)}, session_id={lit(run.id)}, "
            f"actor={lit(run.actor.name)}, bounds={lit(json.dumps(run.bounds))}, "
            f"trigger={lit(json.dumps(trigger) if trigger is not None else None)}, "
            f"continues={lit(continues)}" + (f", step={lit(step)})" if step is not None else ")")
        )

    async def open_flow(
        self, flow_id: str, name: str, actor: str, *, trigger: list[str] | None = None
    ) -> None:
        """A flow's own run row (ADR-0026 D3), under the profile `flow:<name>`, and its session, which holds
        no message: the flow makes no call of its own, and its steps' runs name it as their trigger."""
        profile = f"flow:{name}"
        await self.khive.exec(
            f"lion.session_open(id={lit(flow_id)}, source={lit(SOURCE)}, actor={lit(actor)}, "
            f"profile={lit(profile)}) | "
            f"lion.run_open(id={lit(flow_id)}, profile={lit(profile)}, session_id={lit(flow_id)}, "
            f"actor={lit(actor)}, trigger={lit(json.dumps(trigger) if trigger is not None else None)})"
        )

    async def close_flow(self, flow_id: str, outcome: str, output: Any) -> None:
        """The flow's outcome and output, no cost and no turn of its own: those are its steps' rows."""
        await self.khive.exec(
            f"lion.run_close(id={lit(flow_id)}, outcome={lit(outcome)}, turns=0, "
            f"output={lit(row_json(output) if output is not None else None)}) | "
            f"lion.session_close(id={lit(flow_id)})"
        )

    async def steps(self, flow_run_id: str) -> list[dict]:
        """A flow run's step rows, in order."""
        (got,) = await self.khive.exec(f'lion.get(table="steps", id={lit(flow_run_id)})')
        return list(got["rows"])

    async def messages(self, session_id: str) -> list[dict]:
        """A session's message rows, in order, each carried field read back whole."""
        return await read_messages(self.khive, session_id)

    async def result(self, run_id: str) -> dict:
        """A run's row with its output; one the store does not hold raises its `not_found`."""
        (row,) = await self.khive.exec(f'lion.get(table="runs", id={lit(run_id)})')
        return row

    async def flush(self, run: Run) -> int:
        """Every entry appended since the last flush, as message rows; how many went. After a flush that
        failed part way, the rows it sent may stand: each row of this flush the store holds is read back
        whole and must be the row this flush would send, and only the others are sent (D10)."""
        rows = [self._row(e) for e in run.record.entries[run.store_flushed :]]
        if run.store_unsure:
            mine = {r["seq"]: r for r in rows}
            held = [r for r in await read_messages(self.khive, run.id, carried=False) if r["seq"] in mine]
            await _fetch(self.khive, run.id, held)
            for r in held:
                # the row as the pack keeps it: a null turn stored as 0, and no parent, which this
                # writer never sends
                want = mine.pop(r["seq"])
                if any(
                    r.get(k) != v for k, v in {**want, "turn": want["turn"] or 0, "parent_seq": None}.items()
                ):
                    raise KhiveError(
                        f"session {run.id} seq {r['seq']} holds a row this run did not write", "invalid"
                    )
            rows = list(mine.values())
        run.store_unsure = True
        await append_messages(self.khive, run.id, rows)
        run.store_unsure = False
        run.store_flushed = len(run.record)
        return len(rows)

    def _row(self, e: Entry) -> dict:
        row = entry_row(e)
        d = row["meta"].get("directive")
        return {
            "seq": e.seq,
            "role": _ROLE[e.kind],
            "kind": e.kind.value,
            "text": _text(e),
            "raw": row_json(row),
            "turn": e.turn,
            "alias": e.alias,
            "directive": json.dumps(d) if d is not None else None,
        }

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
        status: CallStatus,
        reply_seq: int | None = None,
        tokens_in: int | None = None,
        cost: float | None = None,
        error: str | None = None,
    ) -> None:
        if status not in _CALL_STATUSES:
            raise ValueError(f"call status {status!r} is not one of {', '.join(_CALL_STATUSES)}")
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
        # a cost the run read as spent (NaN, ADR-0003/S7) has no figure, and the store takes no NaN
        cost = run.cost if math.isfinite(run.cost) else None
        await self.khive.exec(
            f"lion.run_close(id={lit(run.id)}, outcome={lit(outcome)}, cost={lit(cost)}, "
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
            (row,) = await self.khive.exec(f'lion.get(table="runs", id={lit(at)})')
            sessions.append(await read_messages(self.khive, at))
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
