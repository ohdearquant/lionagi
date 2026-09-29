"""The checkpoint and the restore (ADR-0019): named state plus a folded view, never prose about the
session. A take makes no model call. A Claude Code session's view is its turns, each the input and the
final text, the oldest folded first to half the budget; a resident's view is its record after `fold`,
by name, the record itself staying on disk where `source.record` points. Restore is the first prompt of
a fresh session, its first line re-arming the watches, or a resident's record replayed as history.
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
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .actor import Result
from .record import Directive, Entry, Kind, Record, fold

__all__ = (
    "KINDS",
    "WHYS",
    "DIRECTIVES",
    "LAZY",
    "CheckpointError",
    "NotIgnored",
    "Var",
    "Checkpoint",
    "entry_of",
    "fold_turns",
    "take",
    "render_prompt",
    "assert_ignored",
    "save",
    "load",
    "to_history",
)

KINDS = ("fact", "watch", "spawn", "lane", "directive", "topic", "posture")
WHYS = ("threshold", "end", "rotate", "precompact", "manual")
DIRECTIVES = 8  # the prompt hook's ring length: the person's words a restore carries, never more
# characters: a longer var is carried by name, size and where it loads from, never printed whole in a
# restore; a directive and a watch's command are always inline, a watch being re-armed as written
LAZY = 400

# what a hook or the harness injects into a user turn; a kept input or a directive holding one is refused
_FRAMES = (
    "<command-name>",
    "<command-message>",
    "<local-command-",
    "<task-notification>",
    "<system-reminder>",
)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_KEEP = ("inbound", "origin", "reply", "execution", "unread", "refused")


class CheckpointError(Exception):
    """A take or a restore that cannot stand; the message says why and what was not written."""


class NotIgnored(CheckpointError):
    """`<dir>/.khive/context/` sits in a git work tree that does not ignore it."""


@dataclass(frozen=True)
class Var:
    name: str
    kind: str  # fact | watch | spawn | lane | directive | topic | posture
    # watch {command, description, timeout_ms} · spawn {id, task, state} · lane {kind, id, title, awaiting}
    value: Any
    source: str  # declared | hook | registry | notes | transcript | khive | gh
    at: str  # ISO time, or the record seq for a resident
    size: int | None = None  # set when the value is over LAZY: the restore names it, never prints it
    # where the value lives when not in the checkpoint (`note.get(key="posture")`); None: the file keeps it
    ref: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    id: str  # ckpt-<YYYYMMDD-HHMMSS>-<actor slug>
    actor: str
    taken_at: str
    why: str  # threshold | end | rotate | precompact | manual
    # {kind: claude-code|resident, session_id, transcript|record, cwd, model, context: {used, window, pct}}
    source: dict
    vars: tuple[Var, ...]
    view: dict  # {budget, kept: [{turn, at, input, text}], hidden: [names], summaries: [{before, text}]}


def entry_of(row: dict) -> Entry | None:
    """A log row (the chat log's `entry_row`, ADR-0018/D1) back as a record entry: a result as Result,
    the inbound marks, each result's execution fact, a refused turn's reason, the context directives
    and a program's computed values kept, the seq as it was; None for a row of another kind (a USAGE
    row, or another app's: the log, not the record)."""
    if row["kind"] not in Kind.__members__:
        return None
    kind = Kind[row["kind"]]
    content: Any = row["content"]
    if kind is Kind.RESULT and isinstance(content, dict) and "spec" in content:
        content = Result(**content)
    # `execution`: whether a result's handler was called; an older log has none, which says nothing
    kept = row.get("meta") or {}
    meta = {k: v for k, v in kept.items() if k in _KEEP}

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


def fold_turns(turns: Sequence[dict], budget: int) -> dict:
    """A Claude Code session's view: its turns oldest-first, folded from the oldest until the rest fits
    half the budget (the rule `_notify` folds by, on the same estimate); the latest turn always stays. A
    folded turn is named `<turn>@<at>` (from a transcript: its user row's uuid and timestamp)."""
    sizes = [(len(t.get("input") or "") + len(t.get("text") or "")) // 4 for t in turns]
    total, cut = sum(sizes), 0
    while cut < len(turns) - 1 and total > budget // 2:
        total -= sizes[cut]
        cut += 1
    return {
        "budget": budget,
        "kept": [dict(t) for t in turns[cut:]],
        "hidden": [f"{t.get('turn')}@{t.get('at')}" for t in turns[:cut]],
        "summaries": [],
    }


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


def _hooked(text: Any) -> bool:
    s = str(text)
    return s.lstrip().startswith(_FRAMES) or _REMINDER.search(s) is not None


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
) -> Checkpoint:
    """The checkpoint of a Claude Code session (`view` from its turns, `fold_turns`) or of a resident
    (`record`, folded here, its entries by name). Pure: no model call, no file. Vars keep their order and
    a later one of the same name stands (a declared var over a harvested one). A kept input or a
    directive carrying a hook's frame is refused, and so is a kind or a why outside `KINDS` and `WHYS`
    (ADR-0019/C1)."""
    if why not in WHYS:
        raise CheckpointError(f"why {why!r}: one of {', '.join(WHYS)}")
    if not actor:
        raise CheckpointError("no actor: a checkpoint is the actor's, and nothing names it")
    named: dict[str, Var] = {}
    for v in vars:
        v = replace(v, value=copy.deepcopy(v.value))  # the checkpoint's own copy: a later edit is not its
        if v.kind not in KINDS:
            raise CheckpointError(f"var {v.name!r}: kind {v.kind!r} is not one of {', '.join(KINDS)}")
        if v.kind == "directive" and _hooked(v.value):
            raise CheckpointError(
                f"directive {v.name!r} carries a hook's frame; whatever read it let it through"
            )
        if v.kind not in ("directive", "watch") and v.size is None and len(_one(v.value)) > LAZY:
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
        view = copy.deepcopy(view) if view is not None else fold_turns((), budget)
        for t in view.get("kept", []):
            if _hooked(t.get("input") or ""):
                raise CheckpointError(f"turn {t.get('turn')}: its input carries a hook's frame")
        source = {"kind": "claude-code", **source}
    return Checkpoint(
        id=f"ckpt-{now:%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9]+', '-', actor).strip('-')}",
        actor=actor,
        taken_at=now.isoformat(),
        why=why,
        source=source,
        vars=tuple(named.values()),
        view=view,
    )


def _fenced(text: str) -> str:
    """The text between fences longer than any backtick run inside it, so it reads back byte for byte."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _one(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _shown(c: Checkpoint, v: Var) -> str:
    """A var's value, or for a lazy one its size and how to load it."""
    if v.size is None:
        return _one(v.value)
    how = v.ref or f"lion context show --id {c.id} --var {v.name}"
    return f"{v.size} chars, not shown here; load it with `{how}`"


def _listed(c: Checkpoint, then: str) -> Checkpoint:
    """The checkpoint when its why and every var's kind are on the lists (ADR-0019/C1); otherwise
    refused, the value named, never left out."""
    if c.why not in WHYS:
        raise CheckpointError(f"{c.id}: why {c.why!r} is not one of {', '.join(WHYS)}; {then}")
    for v in c.vars:
        if v.kind not in KINDS:
            raise CheckpointError(
                f"{c.id}: var {v.name!r} has kind {v.kind!r}, not one of {', '.join(KINDS)}; {then}"
            )
    return c


def render_prompt(checkpoint: Checkpoint) -> str:
    """The first prompt of the fresh session. Its first line names the checkpoint, the actor and the
    context figure at the take, and asks for the watches to be re-armed before any other work; each
    watch's command follows verbatim. Then the person's words verbatim (the newest DIRECTIVES), the
    lanes, the running spawns, the topic, what was declared, the kept view, the posture, and what to do
    first. A lazy var is its name, its size and how to load it, never its body."""
    c = _listed(checkpoint, "nothing was restored")
    of = {k: [v for v in c.vars if v.kind == k] for k in KINDS}
    watches, facts = of["watch"], {v.name: v for v in of["fact"]}
    ctx = c.source.get("context") or {}
    figure = (
        f"~{ctx.get('used')} of {ctx.get('window')} tokens ({ctx.get('pct')}%)"
        if ctx.get("used") is not None or ctx.get("pct") is not None
        else "not known"
    )
    head = f"Checkpoint {c.id} of {c.actor}, taken {c.taken_at} ({c.why}); context at the take {figure}. "
    if watches:
        head += (
            f"Before any other work, re-arm these {len(watches)} watch(es) with the Monitor tool, "
            "each command exactly as written:"
        )
    else:
        head += "There is no watch to re-arm."
    lines = [head]
    for i, w in enumerate(watches, 1):
        v = w.value if isinstance(w.value, dict) else {"command": _one(w.value)}
        opts = ", ".join(f"{k} {v[k]}" for k in ("timeout_ms", "persistent") if v.get(k) is not None)
        lines.append(f"{i}. {v.get('description') or w.name}" + (f" ({opts})" if opts else "") + ":")
        lines.append(_fenced(str(v.get("command") or "")))
    if "mcp" in facts:
        lines.append(f"MCP servers expected: {_shown(c, facts['mcp'])}")
    if "loop" in facts:
        lines.append(f"Loop file: {_shown(c, facts['loop'])}")
    directives = of["directive"][-DIRECTIVES:]
    if directives:
        lines += ["", f"The person's words, verbatim, oldest first ({len(directives)}):"]
        for d in directives:
            lines += [f"[{d.at}]", _fenced(str(d.value))]
    lines += ["", f"Lanes ({c.source.get('lanes', 'not read')}):"]
    lines += [f"- {_shown(c, v)}" for v in of["lane"]] or ["- none"]
    if of["spawn"]:
        lines += ["", "Spawned agents the transcript shows running at the take:"]
        lines += [f"- {_shown(c, v)}" for v in of["spawn"]]
    for v in of["topic"]:
        lines += ["", f"Topic: {_shown(c, v)}"]
    rest = [v for v in of["fact"] if v.name not in ("mcp", "loop", "next")]
    if rest:
        lines += ["", "Declared:"] + [f"- {v.name}: {_shown(c, v)}" for v in rest]
    view = c.view or {}
    kept, hidden = view.get("kept") or [], view.get("hidden") or []
    lines.append("")
    if c.source.get("kind") == "resident":
        lines.append(
            f"The record {c.source.get('record')} to seq {c.source.get('seq')}: {len(kept)} entries in "
            f"view, {len(hidden)} out of it, {len(view.get('summaries') or [])} summaries; the run's "
            "history replays it."
        )
    else:
        lines.append(
            f"The last {len(kept)} exchange(s) as they stood; {len(hidden)} earlier folded, the "
            f"transcript keeps them: {c.source.get('transcript')}"
        )
        for t in kept:
            opener = "wake" if t.get("kind") == "event" else "in"
            lines += [f"[{t.get('at')}] {opener}:", _fenced(str(t.get("input") or ""))]
            if t.get("text"):
                lines += ["out:", _fenced(str(t["text"]))]
    for v in of["posture"]:
        label = (
            "Your posture at the take:" if v.name == "posture" else f"Your loop file's posture ({v.name}):"
        )
        lines += ["", label, str(v.value) if v.size is None else _shown(c, v)]
    first = facts.get("next")
    lines += [
        "",
        "What to do first: "
        + (
            _shown(c, first)
            if first is not None
            else "re-arm the watches above, then take up the newest of the person's words and the lanes "
            "from where the posture leaves them."
        ),
    ]
    return "\n".join(lines)


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


def save(checkpoint: Checkpoint, directory: str | Path) -> Path:
    """`<dir>/.khive/context/<id>.json`, then `latest.json` pointing at it; the path written. An id
    already on disk (two takes in one second) gets `-02`, `-03`: nothing is overwritten. An id that is
    not a name (`load` takes only a name; `latest` is the pointer's), a why or a kind off the lists and
    a value the file cannot carry are refused before anything is written."""
    if not _ID.fullmatch(checkpoint.id) or checkpoint.id == "latest":
        raise CheckpointError(f"checkpoint id {checkpoint.id!r} is not a name; nothing was written")
    doc = asdict(_listed(checkpoint, "nothing was written"))
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
            data = _bytes({**doc, "id": cid})
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _replace_text(ctx / "latest.json", json.dumps({"id": cid}) + "\n")
    return path


def _bytes(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, indent=1, default=str).encode("utf-8")


def load(directory: str | Path, id: str | None = None) -> Checkpoint:
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
    return _listed(Checkpoint(**{**doc, "vars": tuple(Var(**v) for v in doc["vars"])}), "nothing was loaded")


def to_history(checkpoint: Checkpoint) -> tuple[Entry, ...]:
    """A resident's checkpoint as a run's history (`RunRequest.history`): the record it points at, to
    the seq it was taken at, each row back as its entry (`entry_of`), the directives with them, so
    `fold` renders the view the ended run last showed. The record replayed is folded here and must
    name the checkpoint's view; a record that no longer does is refused, never replayed."""
    src = checkpoint.source
    if src.get("kind") != "resident":
        raise CheckpointError(
            f"{checkpoint.id} is a {src.get('kind')} checkpoint: its restore is the first prompt"
        )
    path, n = Path(str(src.get("record"))), int(src.get("seq", 0))
    try:
        # a row ends at a line feed: a JSON string may carry U+2028 or U+0085 unescaped, where
        # str.splitlines would break the row in two
        rows = path.read_text().split("\n")
    except OSError as e:
        raise CheckpointError(f"{checkpoint.id}: its record {path} cannot be read: {e}") from None
    entries: list[Entry] = []
    for i, raw in enumerate(rows, 1):
        if len(entries) == n:
            break  # the record as it stood at the take; what the run appended after is not this checkpoint
        if not raw.strip():
            continue
        try:
            e = entry_of(json.loads(raw))
        except (ValueError, KeyError, TypeError) as err:
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
