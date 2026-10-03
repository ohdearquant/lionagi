"""The checkpoint and the restore (ADR-0019): named state plus a folded view, never prose about the
run. A take makes no model call. A run's view is its record after `fold`, by name, the record itself
staying on disk where `source.record` points; the restore replays that record as the next run's
history, so any run can be resumed. A harness that is not a run of this runtime hands its own view and
its own vocabulary of kinds and whys; the Claude Code session's is in
`hub.backend.claude_code.checkpoint`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .actor import Result
from .record import Directive, Entry, Kind, Record, fold

__all__ = (
    "KINDS",
    "WHYS",
    "LAZY",
    "CheckpointError",
    "NotIgnored",
    "Var",
    "Checkpoint",
    "entry_of",
    "jsonable",
    "take",
    "listed",
    "assert_ignored",
    "save",
    "load",
    "to_history",
)

# the default vocabulary, a run's own: a take, a save and a load check against the lists they are given
KINDS = ("fact", "posture")
WHYS = ("threshold", "end", "manual")
# characters: a longer var is carried by name, size and where it loads from, never printed whole in a
# restore; a kind the taker names eager is always inline
LAZY = 400

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_KEEP = ("inbound", "origin", "reply", "execution", "request", "returned", "unread", "refused")


class CheckpointError(Exception):
    """A take or a restore that cannot stand; the message says why and what was not written."""


class NotIgnored(CheckpointError):
    """`<dir>/.khive/context/` sits in a git work tree that does not ignore it."""


@dataclass(frozen=True)
class Var:
    name: str
    kind: str  # one of the taker's kinds; a run's are fact | posture
    value: Any
    source: str  # where it was read: declared | notes | registry | khive | gh | what the harness names
    at: str  # ISO time, or the record seq for a resident
    size: int | None = None  # set when the value is over LAZY: the restore names it, never prints it
    # where the value lives when not in the checkpoint (`note.get(key="posture")`); None: the file keeps it
    ref: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    id: str  # ckpt-<YYYYMMDD-HHMMSS>-<actor slug>
    actor: str
    taken_at: str
    why: str  # one of the taker's whys; a run's are threshold | end | manual
    # {kind: resident, record, seq, digest, ...}, or the harness's own (`claude-code`: session_id, transcript)
    source: dict
    vars: tuple[Var, ...]
    view: dict  # {budget, kept: [{turn, at, input, text}], hidden: [names], summaries: [{before, text}]}


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

    if isinstance(d := kept.get("directive"), dict):
        meta["directive"] = directive(d)
    if isinstance(ds := kept.get("consumed"), list):  # a program's consumed results ride a notification too
        meta["consumed"] = tuple(directive(d) for d in ds)
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
        round=row.get("round", 0),
        meta=meta,
    )


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


def _record_view(record: Record | Iterable[Entry], budget: int, fold_inputs: bool) -> dict:
    if not isinstance(record, Record):
        replayed = Record("replay")
        for e in record:
            replayed.append(e.kind, e.content, alias=e.alias, round=e.round, **e.meta)
        record = replayed
    view = fold(record, protect_input=not fold_inputs)
    shown = [v.entry for v in view if v.summary is None and not v.hidden]
    in_view = {e.seq for e in shown}
    return {
        "budget": budget,
        "kept": [{"turn": e.name, "at": e.seq} for e in shown],
        "hidden": [e.name for e in record if e.seq not in in_view],
        "summaries": [{"before": v.entry.name, "text": v.summary} for v in view if v.summary is not None],
    }


def _digest(entries: Iterable[Entry]) -> str:
    """The rows as the replay checks them (ADR-0019/D1): each row's seq and kind, the text of an input,
    a turn or a notice, and a result's spec, ok, error and reason. A result's value reads back as data
    in the form the log chose, so it is not digested."""
    h = hashlib.sha256()
    for e in entries:
        c = e.content
        said = (
            [c.spec, c.ok, c.error, c.reason] if isinstance(c, Result) else c if isinstance(c, str) else None
        )
        h.update(json.dumps([e.seq, e.kind.name, said]).encode() + b"\n")
    return h.hexdigest()


def take(
    source: dict,
    vars: Iterable[Var],
    *,
    actor: str,
    why: str,
    budget: int,
    view: dict | None = None,
    record: Record | None = None,
    fold_inputs: bool = False,
    now: datetime | None = None,
    kinds: Iterable[str] = KINDS,
    whys: Iterable[str] = WHYS,
    eager: Iterable[str] = (),
) -> Checkpoint:
    """The checkpoint of a run (`record`, folded here, its entries by name) or of a harness that hands
    its own `view`. Pure: no model call, no file. Vars keep their order and a later one of the same name
    stands (a declared var over a harvested one). A kind or a why outside `kinds` and `whys` is refused
    (ADR-0019/C1); a var of a kind in `eager` is never made lazy."""
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
        if v.kind not in eager and v.size is None and len(_one(v.value)) > LAZY:
            # lazy: a var with a ref leaves its value where the ref points; without one the file keeps it
            v = replace(v, size=len(_one(v.value)), value=None if v.ref else v.value)
        named[v.name] = v
    now = now or datetime.now().astimezone()
    source = copy.deepcopy(source)
    if record is not None:
        view = _record_view(record, budget, fold_inputs)
        source = {
            **source,
            "kind": "resident",
            "seq": record.seq,
            "fold_inputs": fold_inputs,
            "digest": _digest(record),
        }
    else:
        view = (
            copy.deepcopy(view)
            if view is not None
            else {"budget": budget, "kept": [], "hidden": [], "summaries": []}
        )
    return Checkpoint(
        id=f"ckpt-{now:%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9]+', '-', actor).strip('-')}",
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


def assert_ignored(directory: str | Path) -> None:
    """A checkpoint lands under `<dir>/.khive/context/`; inside a git work tree that path must be
    ignored (`git check-ignore`) or the take refuses with NotIgnored. Outside a repository it passes;
    a git that cannot answer refuses too, never passes."""
    d = Path(directory)
    git = shutil.which("git") or "/usr/bin/git"
    try:
        inside = subprocess.run(
            [git, "-C", str(d), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckpointError(f"git could not be asked about {d}: {e}; nothing was written") from None
    if inside.returncode != 0:
        if "not a git repository" in inside.stderr:
            return
        said = inside.stderr.strip() or f"rc {inside.returncode}"
        raise CheckpointError(
            f"git could not say whether {d} is in a repository: {said}; nothing was written"
        )
    if inside.stdout.strip() != "true":
        return
    try:
        ignored = subprocess.run(
            [git, "-C", str(d), "check-ignore", "-q", ".khive/context/"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckpointError(
            f"git check-ignore could not be asked about {d}: {e}; nothing was written"
        ) from None
    if ignored.returncode == 0:
        return
    if ignored.returncode == 1:
        raise NotIgnored(
            f"{d / '.khive' / 'context'} is inside a git work tree that does not ignore it, so a "
            "checkpoint could be committed; add `.khive/context/` to the repository's .gitignore when "
            "the directory is the repository root, `**/.khive/context/` when it sits below it. "
            "Nothing was written"
        )
    said = ignored.stderr.strip() or f"rc {ignored.returncode}"
    raise CheckpointError(f"git check-ignore could not answer for {d}: {said}; nothing was written")


def _replace_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save(
    checkpoint: Checkpoint, directory: str | Path, *, kinds: Iterable[str] = KINDS, whys: Iterable[str] = WHYS
) -> Path:
    """`<dir>/.khive/context/<id>.json`, then `latest.json` pointing at it; the path written. An id
    already on disk (two takes in one second) gets `-02`, `-03`: nothing is overwritten. An id that is
    not a name (`load` takes only a name; `latest` is the pointer's), a why or a kind off the lists and
    a value the file cannot carry are refused before anything is written."""
    if not _ID.fullmatch(checkpoint.id) or checkpoint.id == "latest":
        raise CheckpointError(f"checkpoint id {checkpoint.id!r} is not a name; nothing was written")
    doc = asdict(listed(checkpoint, "nothing was written", kinds=kinds, whys=whys))
    try:
        data = _bytes(doc)
    except ValueError as e:  # a lone surrogate: JSON carries it escaped, UTF-8 refuses it
        raise CheckpointError(
            f"{checkpoint.id}: a value the file cannot carry ({e}); nothing was written"
        ) from None
    d = Path(directory)
    assert_ignored(d)
    ctx = d / ".khive" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    cid, n = checkpoint.id, 1
    while True:
        path = ctx / f"{cid}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
            cid = f"{checkpoint.id}-{n:02d}"
    if cid != checkpoint.id:  # encoded once: the id is the first field and a name JSON writes as it is,
        # so a taken name changes those bytes alone
        data = f'{{\n "id": "{cid}"'.encode() + data[len(f'{{\n "id": "{checkpoint.id}"') :]
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _replace_text(ctx / "latest.json", json.dumps({"id": cid}) + "\n")
    return path


def _bytes(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, indent=1, default=jsonable).encode("utf-8")


def load(
    directory: str | Path,
    id: str | None = None,
    *,
    kinds: Iterable[str] = KINDS,
    whys: Iterable[str] = WHYS,
) -> Checkpoint:
    """The checkpoint `id` under `<dir>/.khive/context/`, the latest when None; a file whose why or a
    var's kind is off the lists is refused, never read as another checkpoint."""
    ctx = Path(directory) / ".khive" / "context"
    if id is None:
        try:
            id = json.loads((ctx / "latest.json").read_text())["id"]
        except FileNotFoundError:
            raise CheckpointError(f"no checkpoint in {ctx}: latest.json is absent") from None
        except (ValueError, KeyError, TypeError) as e:
            raise CheckpointError(f"{ctx / 'latest.json'} does not name a checkpoint: {e}") from None
    if not isinstance(id, str) or not _ID.fullmatch(id):
        raise CheckpointError(f"checkpoint id {id!r} is not a name in {ctx}")
    path = ctx / f"{id}.json"
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        raise CheckpointError(f"no checkpoint {id!r} in {ctx}") from None
    c = Checkpoint(**{**doc, "vars": tuple(Var(**v) for v in doc["vars"])})
    return listed(c, "nothing was loaded", kinds=kinds, whys=whys)


def to_history(checkpoint: Checkpoint) -> tuple[Entry, ...]:
    """A resident's checkpoint as a run's history (`RunRequest.history`): the record it points at, to
    the seq it was taken at, each row back as its entry (`entry_of`), the directives with them, so
    `fold` renders the view the ended run last showed. The record replayed is folded here and must
    name the checkpoint's view; a record that no longer does is refused, never replayed."""
    src = checkpoint.source
    if src.get("kind") != "resident":
        raise CheckpointError(
            f"{checkpoint.id} holds no run's record (its source is {src.get('kind')!r}); nothing was replayed"
        )
    path, n = Path(str(src.get("record"))), int(src.get("seq", 0))
    try:
        # a row ends at a line feed: a JSON string may carry U+2028 or U+0085 unescaped, where
        # str.splitlines would break the row in two; a row is decoded when admitted, so a write cut
        # inside a multi-byte character past the take is never read
        rows = path.read_bytes().split(b"\n")
    except OSError as e:
        raise CheckpointError(f"{checkpoint.id}: its record {path} cannot be read: {e}") from None
    entries: list[Entry] = []
    for i, raw in enumerate(rows, 1):
        if len(entries) == n:
            break  # the record as it stood at the take; what the run appended after is not this checkpoint
        if not raw.strip():
            continue
        try:
            e = entry_of(json.loads(raw.decode()))
        except (ValueError, KeyError, TypeError) as err:  # UnicodeDecodeError is a ValueError
            raise CheckpointError(
                f"{path} line {i} is not a record row ({err}); nothing was replayed"
            ) from None
        if e is not None:
            entries.append(e)
    if [e.seq for e in entries] != list(range(n)):
        raise CheckpointError(f"{path} holds seqs {len(entries)} of {n} in order; nothing was replayed")
    try:
        again = _record_view(entries, int(checkpoint.view.get("budget", 0)), bool(src.get("fold_inputs")))
    except ValueError as e:
        raise CheckpointError(f"{path} does not replay: {e}") from None
    for key in ("kept", "hidden", "summaries"):
        if again[key] != checkpoint.view.get(key):
            raise CheckpointError(
                f"{path} no longer folds to the view of {checkpoint.id} ({key} differ); nothing was replayed"
            )
    # the view compares names; a row rewritten in place folds the same, so its content is checked too
    want = src.get("digest")
    if not isinstance(want, str):
        raise CheckpointError(f"{checkpoint.id} carries no digest of its record; nothing was replayed")
    if _digest(entries) != want:
        raise CheckpointError(
            f"{path} to seq {n} is not the record {checkpoint.id} was taken over (its rows were rewritten); "
            "nothing was replayed"
        )
    return tuple(entries)
