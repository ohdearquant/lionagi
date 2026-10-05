"""The checkpoint (ADR-0019): the named values and the folded view of a session at a turn, computed from
what the store holds, never prose about the run and never a file. A run of this runtime needs none: its
rows are the store's and its history replays them (ADR-0025 C3). A harness that is not a run of this
runtime (a Claude Code or Codex session) is written into the store as a session of its own source by its
adapter, and its checkpoint at any turn is read back from there: `take` folds what it is handed and
calls no model. The adapter hands its own vocabulary of kinds and whys; a run's is the default.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel

from .actor import Result
from .record import Directive, Entry, Kind, Op

__all__ = (
    "KINDS",
    "WHYS",
    "LAZY",
    "CheckpointError",
    "Var",
    "Checkpoint",
    "entry_row",
    "entry_of",
    "load_log",
    "jsonable",
    "row_json",
    "take",
    "listed",
)

# the default vocabulary, a run's own: a take and a restore check against the lists they are given
KINDS = ("fact", "posture")
WHYS = ("threshold", "end", "manual")
# characters: a longer var is carried by name, size and where it loads from, never printed whole in a
# restore; a kind the taker names eager is always inline
LAZY = 400

_OPS = get_args(Op)
_KEEP = ("inbound", "origin", "reply", "execution", "request", "returned", "unread", "refused")


class CheckpointError(Exception):
    """A take or a restore that cannot stand; the message says why and what was not written."""


@dataclass(frozen=True)
class Var:
    name: str
    kind: str  # one of the taker's kinds; a run's are fact | posture
    value: Any
    source: str  # where it was read: declared | notes | registry | khive | gh | what the harness names
    at: str  # ISO time, or the record seq for a resident
    size: int | None = None  # set when the value is over LAZY: the restore names it, never prints it
    # where the value lives when not in the checkpoint (`note.get(key="posture")`); None: the store keeps it
    ref: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    id: str  # <session>@<turn>: the session the store holds and the turn it was read to
    actor: str
    taken_at: str
    why: str  # one of the taker's whys; a run's are threshold | end | manual
    # the harness's own: `claude-code` or `codex` with session_id and transcript, the store's turn count
    source: dict
    vars: tuple[Var, ...]
    view: dict  # {budget, kept: [{turn, at, input, text}], hidden: [names], summaries: [{before, text}]}


def entry_row(e: Entry) -> dict:
    """One entry as the log writes it: a result as its five fields, everything else as it is."""
    content: Any = e.content
    if e.kind is Kind.RESULT and hasattr(content, "spec"):
        content = {
            "spec": content.spec,
            "ok": content.ok,
            "value": content.value,
            "error": content.error,
            "reason": content.reason,
        }
    meta = dict(e.meta)

    def row(d: Directive) -> dict:
        return {"op": d.op, "refs": list(d.refs), "text": d.text, "at": d.at, "seqs": d.seqs}

    # what this entry did to the view, as data: a directive, and the hides of what a program consumed
    if isinstance(d := meta.get("directive"), Directive):
        meta["directive"] = row(d)
    if isinstance(ds := meta.get("consumed"), tuple):
        meta["consumed"] = [row(d) for d in ds]
    if isinstance(vs := meta.get("values"), tuple):  # a value that failed to compute: its error, fourth
        meta["values"] = [
            [a, seq, None, v.error] if isinstance(v, Result) else [a, seq, v] for a, seq, v in vs
        ]
    return {
        "seq": e.seq,
        "kind": e.kind.name,
        "name": e.name,
        "turn": e.turn,
        "alias": e.alias,
        "meta": meta,
        "content": content,
    }


def entry_of(row: dict) -> Entry | None:
    """A log row (the chat log's `entry_row`, ADR-0018/D1) back as a record entry: a result as Result,
    the inbound marks, each result's execution fact and its two captures (the request the handler
    entered, its return before the after hooks), the note versions it read, a refused turn's reason,
    the context directives and a program's computed values kept, the seq as it was; None for a row of
    another kind (a USAGE row, or another app's: the log, not the record)."""
    if row["kind"] not in Kind.__members__:
        return None
    kind = Kind[row["kind"]]
    content: Any = row["content"]
    if kind is Kind.RESULT and isinstance(content, dict) and "spec" in content:
        content = Result(**content)
    # `execution`: whether a result's handler was called; an older log has none, which says nothing
    kept = row.get("meta") or {}
    meta = {k: v for k, v in kept.items() if k in _KEEP}
    # `notes`: the version each note was read at, kept when it is that shape; anything else says nothing
    notes = kept.get("notes")
    if (
        isinstance(notes, dict)
        and notes
        and all(isinstance(k, str) and type(v) is int for k, v in notes.items())
    ):
        meta["notes"] = dict(notes)

    def directive(d: dict) -> Directive:
        return Directive(
            op=d["op"],
            refs=tuple(d["refs"]),
            text=d.get("text", ""),
            at=int(d.get("at", 0)),
            seqs=tuple(d["seqs"]) if d.get("seqs") is not None else None,
        )

    # an op this runtime has not got (a row a later one wrote) is left out, as the fold would leave it
    if isinstance(d := kept.get("directive"), dict) and d.get("op") in _OPS:
        meta["directive"] = directive(d)
    if isinstance(ds := kept.get("consumed"), list):  # a program's consumed results ride a notification too
        meta["consumed"] = tuple(directive(d) for d in ds if d.get("op") in _OPS)
    if isinstance(bs := kept.get("bound"), list):  # the values a program bound and nothing read yet
        meta["bound"] = tuple((str(b[0]), int(b[1]), int(b[2]), str(b[3])) for b in bs)
    if isinstance(vs := kept.get("values"), list):  # and the values it computed: alias, seq, value,
        # or alias, seq, None, error for one that failed to compute, bound to its failure
        meta["values"] = tuple(
            (str(v[0]), int(v[1]), Result("value", False, error=str(v[3]), reason="invalid"))
            if len(v) == 4
            else (str(v[0]), int(v[1]), v[2])
            for v in vs
        )
    return Entry(
        seq=row["seq"],
        kind=kind,
        content=content,
        alias=row.get("alias"),
        turn=row.get("turn", 0),
        meta=meta,
    )


def load_log(
    path: Path, torn: list[str] | None = None, ran: dict | None = None, usage: list[dict] | None = None
) -> tuple[Entry, ...]:
    """The entries a log holds, as a run's history: results back as Result, the inbound marks, each
    result's execution fact and the context directives kept, the seqs as they were (a log is written
    in order, from 0); rows of other kinds are skipped, and with `ran` given the BOX row, where the
    chat ran, is copied there, with `usage` given each USAGE row, what each call cost, is appended
    there. A last line that is not JSON is a write that was cut: with `torn` given it is dropped and
    described there, else it raises; a bad line with lines after it always raises."""
    entries: list[Entry] = []
    # a row ends at a line feed: a row may carry U+2028 or NEL unescaped, where str.splitlines would
    # break it, and a write cut inside a multi-byte character is a torn last row, not an unreadable file
    rows = [(i, raw) for i, raw in enumerate(Path(path).read_bytes().split(b"\n"), 1) if raw.strip()]
    for n, (i, raw) in enumerate(rows):
        try:
            r = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            if n == len(rows) - 1 and torn is not None:
                why = e.reason if isinstance(e, UnicodeDecodeError) else e.msg
                torn.append(f"line {i} of {path} is not JSON ({why}), a write that was cut; dropped")
                break
            where = "the last line" if n == len(rows) - 1 else f"line {i}, with {len(rows) - n - 1} after it"
            why = e.reason if isinstance(e, UnicodeDecodeError) else e.msg
            raise ValueError(
                f"{path}: {where} is not JSON ({why}): {raw.decode(errors='replace')[:80]!r}"
            ) from None
        if ran is not None and r.get("kind") == "BOX":
            ran.update(r)
        if usage is not None and r.get("kind") == "USAGE":
            usage.append(r)
        entry = entry_of(r)  # the row back as its entry; the one reading a checkpoint's record uses too
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def jsonable(value: Any) -> Any:
    """The `default=` of every JSON write of a log row and of a checkpoint: a pydantic model as its
    `model_dump(mode="json")`, whatever that is (an object of its fields, computed ones included, a
    list or a scalar), so a replay reads that JSON; anything else, or a model with a field JSON cannot
    hold, as its str."""
    if isinstance(value, BaseModel):
        try:
            return value.model_dump(mode="json")
        except ValueError:  # pydantic's serialization error: a field of a type it cannot write
            pass
    return str(value)


def row_json(value: Any, **kw: Any) -> str:
    """`value` as a row's JSON, `kw` passed to `json.dumps`: what JSON cannot hold, `jsonable` writes as
    its str (ADR-0018 D16), and a key JSON cannot hold (one that is not a string or a number) and a value
    holds itself are written the same way, each as its str, so the row is written and the run or the
    chat goes on. A replay reads that part back as the str, as it reads anything else `jsonable` wrote."""
    try:
        return json.dumps(value, default=jsonable, **kw)
    except (TypeError, ValueError):  # ValueError: a value that holds itself
        return json.dumps(_carried(value, frozenset()), default=jsonable, **kw)


def _carried(value: Any, path: frozenset[int]) -> Any:
    if not isinstance(value, dict | list | tuple):
        return value
    if id(value) in path:  # the value holds itself here
        return str(value)
    inner = path | {id(value)}
    if isinstance(value, dict):
        return {(k if _key(k) else str(k)): _carried(v, inner) for k, v in value.items()}
    return [_carried(v, inner) for v in value]


def _key(k: Any) -> bool:
    """Whether JSON writes `k` as an object's key."""
    return k is None or isinstance(k, str | int | float | bool)


def take(
    source: dict,
    vars: Iterable[Var],
    *,
    actor: str,
    why: str,
    budget: int,
    view: dict | None = None,
    now: datetime | None = None,
    kinds: Iterable[str] = KINDS,
    whys: Iterable[str] = WHYS,
    eager: Iterable[str] = (),
) -> Checkpoint:
    """The checkpoint of a session at a turn: `source` names the session and the turn, `view` is its
    exchanges folded (`hub.backend.checkpoint.fold_turns`). Pure: no model call, nothing written. Vars keep
    their order and a later one of the same name stands (a declared var over a harvested one). A kind or
    a why outside `kinds` and `whys` is refused (ADR-0019/C1); a var of a kind in `eager` is never made
    lazy. The id is `<session_id>@<turns>` from `source`, the turns the view's when `source` names
    none; the time `now`."""
    kinds, whys, eager = tuple(kinds), tuple(whys), frozenset(eager)
    if why not in whys:
        raise CheckpointError(f"why {why!r}: one of {', '.join(whys)}")
    if not actor:
        raise CheckpointError("no actor: a checkpoint is the actor's, and nothing names it")
    named: dict[str, Var] = {}
    for v in vars:
        v = replace(v, value=copy.deepcopy(v.value))  # the checkpoint's own copy: a later edit is not its
        if v.kind not in kinds:
            raise CheckpointError(f"var {v.name!r}: kind {v.kind!r} is not one of {', '.join(kinds)}")
        if v.kind not in eager and v.size is None and (size := len(_one(v.value))) > LAZY:
            # lazy: a var with a ref leaves its value where the ref points; without one it is kept here
            v = replace(v, size=size, value=None if v.ref else v.value)
        named[v.name] = v
    now = now or datetime.now().astimezone()
    source = copy.deepcopy(source)
    view = (
        copy.deepcopy(view)
        if view is not None
        else {"budget": budget, "kept": [], "hidden": [], "summaries": []}
    )
    turns = source.get("turns", len(view.get("kept") or ()) + len(view.get("hidden") or ()))
    return Checkpoint(
        id=f"{source.get('session_id')}@{turns}",
        actor=actor,
        taken_at=now.isoformat(),
        why=why,
        source=source,
        vars=tuple(named.values()),
        view=view,
    )


def _one(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=jsonable)


def listed(
    c: Checkpoint, then: str, *, kinds: Iterable[str] = KINDS, whys: Iterable[str] = WHYS
) -> Checkpoint:
    """The checkpoint when its why and every var's kind are on the lists (ADR-0019/C1); otherwise
    refused, the value named, never left out; `then` says what was not done."""
    kinds, whys = tuple(kinds), tuple(whys)
    if c.why not in whys:
        raise CheckpointError(f"{c.id}: why {c.why!r} is not one of {', '.join(whys)}; {then}")
    for v in c.vars:
        if v.kind not in kinds:
            raise CheckpointError(
                f"{c.id}: var {v.name!r} has kind {v.kind!r}, not one of {', '.join(kinds)}; {then}"
            )
    return c
