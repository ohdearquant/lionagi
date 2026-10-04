"""What a harness session's checkpoint reads and renders whatever the harness (ADR-0019): the
directory's actor, its lanes (active gtd rows, the threads its unread mail awaits a reply on, its open
pull requests), its loop cursor, the fold of a session's exchanges, the restore prompt's body, and the
one file state a take reads its session's file in (`Snapshot`). Each harness's own scan and its first
line are its module's: `claude_code.checkpoint` for a Claude Code session, `codex_checkpoint` for a
Codex one. A source that is absent is a value, never a failed take.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lion1.checkpoint import Checkpoint, CheckpointError, Var, jsonable
from lion1.lndl import plain

from ..agent.instruments import GH, PR_PAGE, Runner, sh
from ..khive import Khive, lit

__all__ = (
    "DIRECTIVES",
    "SPAWN_HOURS",
    "MAIL_PAGE",
    "RETAKES",
    "Snapshot",
    "snapshot_of",
    "actor_id",
    "lanes",
    "pulls",
    "cursor",
    "posture_of",
    "fold_turns",
    "opening",
    "restored",
)

DIRECTIVES = 8  # the person's words a restore carries, never more: a prompt hook's ring length
SPAWN_HOURS = 12  # a spawn older than this with nothing ending it is taken as dead
MAIL_PAGE = 50  # unread messages one read returns; a full page is not every thread awaiting a reply
RETAKES = 2  # a session's file that changed under its read is stat and read again this many times


@dataclass(frozen=True)
class Snapshot:
    """One file state of a session's file, a transcript or a rollout, captured once: every read of a
    take reads exactly its `size` bytes, and a rollout's scan keeps the view `budget` folds to, none
    when it is None."""

    path: str
    size: int
    mtime_ns: int
    dev: int  # an inode names a file on its own device only
    ino: int  # a file put in its place
    ctime_ns: int  # a rewrite in place keeps the size and can restore the mtime, never this
    budget: int | None = None

    def __fspath__(self) -> str:
        return self.path


class _Moved(CheckpointError):
    """The file a snapshot names is not in the snapshot's state: put in its place, cut, or rewritten."""


def _state(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, st.st_ctime_ns


def _same(f: Any, snap: Snapshot) -> bool:
    """Whether the open file is the snapshot's: in its very state, or the same file grown since. One
    grown is taken as appended to, its bytes to the snapshot's size the snapshot's own."""
    st = os.fstat(f.fileno())
    return _state(st) == (snap.size, snap.mtime_ns, snap.dev, snap.ino, snap.ctime_ns) or (
        (st.st_dev, st.st_ino) == (snap.dev, snap.ino) and st.st_size > snap.size
    )


def _lines(f: Any, snap: Snapshot) -> Iterator[bytes]:
    """The lines of the open file `f` to the snapshot's size and no further, the last one without its
    end when the snapshot cut it mid-write. The file is checked against the snapshot before the first
    byte is read and after the last."""
    if not _same(f, snap):  # another file put in its place, or this one cut or rewritten
        raise _Moved(f"{snap.path} is not the file its snapshot was taken of")
    left = snap.size  # a row appended after the snapshot is after the take
    while left > 0:
        raw = f.readline(left)
        if not raw:  # cut while it was read: the check after the last byte refuses it
            break
        left -= len(raw)
        yield raw
    if not _same(f, snap):
        raise _Moved(f"{snap.path} was rewritten while it was read")


def snapshot_of(path: str | Path, read: Callable[[Snapshot], Any], budget: int | None = None) -> Snapshot:
    """The file's state now, for a take whose view folds to `budget`, read at once by the harness's
    `read`: the scan every reader of the snapshot gets is of that state. A file that changes between
    its stat and its read is taken again, RETAKES times at most."""
    p = Path(path)
    tries = 0
    while True:
        snap = Snapshot(str(p), *_state(p.stat()), budget)
        try:
            read(snap)
        except _Moved:
            tries += 1
            if tries > RETAKES:
                raise
        else:
            return snap


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _at(m: dict) -> datetime | None:
    """`m["at"]` as a time; one with no zone is UTC, one that does not parse is None."""
    try:
        started = datetime.fromisoformat(str(m["at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return started if started.tzinfo is not None else started.replace(tzinfo=UTC)


def _open(prompts: list[dict], turn: Any, at: str, text: str, kind: str) -> dict:
    """An exchange opened by a row named `turn`: the person's words, or the label of the event that
    woke the actor. A harness re-queues a prompt across a restart, and an event can fire again before
    the actor answers: the same words again with no answer between are one exchange (measured: one
    prompt five times in twelve minutes)."""
    opened = {"turn": turn, "at": at, "input": text, "text": "", "kind": kind}
    if prompts and prompts[-1]["input"] == text and not prompts[-1]["text"]:
        prompts[-1] = opened
    else:
        prompts.append(opened)
    return opened


def actor_id(directory: str | Path) -> str:
    """The actor: `[actor] id` of `<dir>/.khive/config.toml`."""
    cfg = Path(directory) / ".khive" / "config.toml"
    try:
        actor = (tomllib.loads(cfg.read_text()).get("actor") or {}).get("id")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CheckpointError(f"{cfg}: {e}; the actor that takes the checkpoint is named there") from None
    if not actor:
        raise CheckpointError(f"{cfg} has no [actor] id; the actor that takes the checkpoint is named there")
    return str(actor)


def _valid(rows: list, kind: str) -> tuple[list[dict], str]:
    """The rows of one read that make lanes of `kind` (task, thread or pr), and what `source.lanes`
    adds for the rest: how many were invalid and what the first one was. A row is an object; a pull
    request's number is a positive int, never a bool; any other row's `properties` is an object when
    given, and the id its lane is named by is a non-empty string."""
    good, bad, first = [], 0, ""  # the count and the first reason: only the first is ever spelled out
    for r in rows:
        if not isinstance(r, dict):
            bad, first = bad + 1, first or f"{type(r).__name__}, not an object"
            continue
        if kind == "pr":
            n = r.get("number")
            if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                bad, first = bad + 1, first or f"number {n!r:.40}, not a positive int"
                continue
        else:
            props = r.get("properties")
            if props is not None and not isinstance(props, dict):
                bad, first = bad + 1, first or f"properties {type(props).__name__}, not an object"
                continue
            key = (props or {}).get("thread_id") or r.get("id") if kind == "thread" else r.get("id")
            if not isinstance(key, str) or not key:
                bad, first = bad + 1, first or f"id {key!r:.40}, not a non-empty string"
                continue
        good.append(r)
    return good, f" ({bad} invalid; the first: {first})" if bad else ""


async def lanes(khive: Khive) -> tuple[list[Var], str]:
    """The actor's active gtd rows and the threads its unread mail awaits a reply on, as lanes, and what
    `source.lanes` says of each read: done, or why it could not be. A thread is named by its newest
    unread message. The open pull requests are `pulls`'."""
    now = _now()
    out: list[Var] = []
    said: list[str] = []
    try:
        (found,) = await khive.exec(f"gtd.tasks(status={lit('active')}, assignee={lit(khive.actor)})")
    except Exception as e:  # khive down is a value in the checkpoint, never a failed take
        found = e
    rows = found.get("tasks") if isinstance(found, dict) else found
    if isinstance(found, Exception):
        said.append(f"gtd.tasks unavailable: {type(found).__name__}: {found}")
    elif not isinstance(rows, list):
        said.append(f"gtd.tasks unavailable: answered {type(found).__name__}, not a list of tasks")
    else:
        good, invalid = _valid(rows, "task")
        for t in good:
            value = {
                "kind": "task",
                "id": t.get("id"),
                "title": t.get("title"),
                "awaiting": (t.get("properties") or {}).get("awaiting"),
                "priority": t.get("priority"),
            }
            out.append(Var(f"lane.{t.get('id')}", "lane", value, "khive", now))
        said.append(f"gtd.tasks active{invalid}")
    try:
        mail = await khive.inbox(limit=MAIL_PAGE)  # unread, no wait; nothing is marked read
    except Exception as e:
        said.append(f"comm.inbox unavailable: {type(e).__name__}: {e}")
        return out, "; ".join(said)
    if not isinstance(mail, list):
        said.append(f"comm.inbox unavailable: answered {type(mail).__name__}, not a list of messages")
        return out, "; ".join(said)
    good, invalid = _valid(mail, "thread")
    threads: dict[str, dict] = {}
    for m in good:  # newest first
        props = m.get("properties") or {}
        thread = str(props.get("thread_id") or m.get("id"))
        awaiting = f"a reply to {props.get('from_actor')}"
        threads.setdefault(
            thread, {"kind": "thread", "id": thread, "title": m.get("subject"), "awaiting": awaiting}
        )
    out += [Var(f"lane.thread.{k}", "lane", v, "khive", now) for k, v in threads.items()]
    full = f" (a full page of {MAIL_PAGE}: more may be unread)" if len(mail) >= MAIL_PAGE else ""
    said.append(f"comm.inbox unread{full}{invalid}")
    return out, "; ".join(said)


async def pulls(directory: str | Path, run: Runner | None = None) -> tuple[list[Var], str]:
    """The open pull requests the gh account authored in the directory's repository, as lanes, and what
    `source.lanes` says of the read: done, or why it could not be."""
    argv = [GH, "pr", "list", "--author", "@me", "--state", "open", "--limit", str(PR_PAGE)]
    argv += ["--json", "number,title,url,reviewDecision"]
    try:
        rc, out, err = await (run or sh)(argv, cwd=str(directory))
    except OSError as e:  # no gh to run is a value in the checkpoint, never a failed take
        return [], f"gh pr list unavailable: {type(e).__name__}: {e}"
    try:
        rows = json.loads(out) if rc == 0 else None
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        why = (err.strip()[:200] or f"rc {rc}") if rc != 0 else f"no list of pull requests in {out[:120]!r}"
        return [], f"gh pr list unavailable: {why}"
    now = _now()
    good, invalid = _valid(rows, "pr")
    found = []
    for p in good:
        value = {
            "kind": "pr",
            "id": p.get("number"),
            "title": p.get("title"),
            "awaiting": p.get("reviewDecision") or None,
            "url": p.get("url"),
        }
        found.append(Var(f"lane.pr.{p.get('number')}", "lane", value, "gh", now))
    full = f" (a full page of {PR_PAGE}: more may be open)" if len(rows) >= PR_PAGE else ""
    return found, f"gh pr list open, authored by @me{full}{invalid}"


def cursor(directory: str | Path) -> list[Var]:
    """The actor's loop cursor, `<dir>/.khive/loop/*loop*.md` (the newest, archives aside): its HEADER,
    the text before the first `## ` heading, and its `## Posture` section when it has one. The apps's
    rule is that the header is the authoritative read and the LOG tail is history."""
    loop = Path(directory) / ".khive" / "loop"
    files = [p for p in loop.glob("*loop*.md") if p.is_file()]
    if not files:
        return []
    p = max(files, key=lambda f: f.stat().st_mtime)
    try:
        text = p.read_text()
    except OSError:
        return []
    at = datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    header, posture_text = [], []
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if section is None:
            header.append(line)
        elif section == "posture":
            posture_text.append(line)
    out = []
    if "\n".join(header).strip():
        out.append(Var("cursor.header", "fact", "\n".join(header).strip(), str(p), at))
    if "\n".join(posture_text).strip():
        out.append(Var("cursor.posture", "posture", "\n".join(posture_text).strip(), str(p), at))
    return out


def posture_of(last: tuple[str, str] | None, source: str) -> list[Var]:
    """The session's last text and its row's time as the `posture`, its LNDL taken out and its lines
    kept; nothing when there is no text."""
    text = plain(last[0]) if last is not None else ""
    return [Var("posture", "posture", text, source, last[1])] if last is not None and text else []


def fold_turns(turns: Sequence[dict], budget: int) -> dict:
    """A session's view: its turns oldest-first, folded from the oldest until the rest fits half the
    budget (the rule `_notify` folds by, on the same estimate); the latest turn always stays. A folded
    turn is named `<turn>@<at>` (the id and time of the row that opened it)."""
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


def _fenced(text: str) -> str:
    """The text between fences longer than any backtick run inside it, so it reads back byte for byte."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _one(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=jsonable)


def _shown(c: Checkpoint, v: Var) -> str:
    """A var's value, or for a lazy one its size and how to load it."""
    if v.size is None:
        return _one(v.value)
    how = v.ref or f"lion context show --id {c.id} --var {v.name}"
    return f"{v.size} chars, not shown here; load it with `{how}`"


def opening(c: Checkpoint) -> str:
    """The restore's first words: the checkpoint, the actor, the time, the reason and the context
    figure at the take, or "not known"."""
    ctx = c.source.get("context") or {}
    figure = (
        f"~{ctx.get('used')} of {ctx.get('window')} tokens ({ctx.get('pct')}%)"
        if ctx.get("used") is not None or ctx.get("pct") is not None
        else "not known"
    )
    return f"Checkpoint {c.id} of {c.actor}, taken {c.taken_at} ({c.why}); context at the take {figure}. "


def restored(c: Checkpoint, first: str) -> list[str]:
    """The restore's lines after its first: a declared `mcp` and `loop`, the person's words verbatim
    (the newest DIRECTIVES), the lanes, the running spawns, the topic, what else was declared, the
    kept view, the posture, and what to do first: a declared `next`, else `first`. A lazy var is its
    name, its size and how to load it, never its body. `c` is already checked against its lists."""
    kinds = ("fact", "directive", "lane", "spawn", "topic", "posture")
    of = {k: [v for v in c.vars if v.kind == k] for k in kinds}
    facts = {v.name: v for v in of["fact"]}
    lines = []
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
    nxt = facts.get("next")
    lines += ["", "What to do first: " + (_shown(c, nxt) if nxt is not None else first)]
    return lines
