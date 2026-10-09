"""What a harness session's checkpoint reads and renders whatever the harness (ADR-0019): the
directory's actor, its lanes (active gtd rows, the threads its unread mail awaits a reply on, its open
pull requests), its loop cursor, the fold of a session's exchanges, the restore prompt's body, and the
one file state a take reads its session's file in (`Snapshot`). Each harness's own scan and its first
line are its module's: `claude_code.checkpoint` for a Claude Code session, `codex.checkpoint` for a
Codex one. A source that is absent is a value, never a failed take.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lion1.checkpoint import Checkpoint, CheckpointError, Var, jsonable, row_json
from lion1.khive import Khive, KhiveError, lit
from lion1.lndl import plain
from lion1.store import append_messages, read_messages

from ..agent.instruments import GH, PR_PAGE, Runner, sh

__all__ = (
    "DIRECTIVES",
    "SPAWN_HOURS",
    "MAIL_PAGE",
    "RETAKES",
    "SOURCES",
    "DECLARED",
    "Snapshot",
    "snapshot_of",
    "actor_id",
    "session_of",
    "latest_session",
    "held_session",
    "held_view",
    "exchanges_of",
    "record_exchanges",
    "declare",
    "declared",
    "lanes",
    "pulls",
    "cursor",
    "posture_of",
    "fold_turns",
    "opening",
    "restored",
)

SOURCES = {
    "claude-code": "claude_code",
    "codex": "codex",
}  # a checkpoint's `source.kind` -> the store's source
DECLARED = "context."  # a declared value is the actor's note under this prefix, its name after it

DIRECTIVES = 8  # the person's words a restore carries, never more: a prompt hook's ring length
SPAWN_HOURS = 12  # a spawn older than this with nothing ending it is taken as dead
MAIL_PAGE = 50  # unread messages one read returns; a full page is not every thread awaiting a reply
RETAKES = 2  # a session's file that changed under its read is stat and read again this many times
SUPERSEDED = 3  # a take superseded by a newer one begins again this many times, then is refused


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


# --- the session in the store: a harness session is a session of its own source (ADR-0025 D1) ---


async def session_of(khive: Khive, session_id: str) -> dict | None:
    """The store's row for the session, None when it holds none."""
    try:
        (row,) = await khive.exec(f'lion.get(table="sessions", id={lit(session_id)})')
    except KhiveError as e:
        if e.kind == "not_found":
            return None
        raise
    return row


async def latest_session(khive: Khive, actor: str) -> dict | None:
    """The actor's newest harness session in the store, of either source; None when there is none."""
    ops = ", ".join(f"lion.sessions(limit=1, actor={lit(actor)}, source={lit(s)})" for s in SOURCES.values())
    found = [r for got in await khive.exec(f"[{ops}]") for r in got["rows"]]
    return max(found, key=lambda r: r["started"], default=None)


async def held_session(khive: Khive, actor: str, name: str | None = None) -> dict | None:
    """The session a restore, a show or a status reads (ADR-0025 D12): the tip the `takes` row of the
    CLI session `name` names, else the session `name` itself, so a branch's own id names that branch;
    with no name, the tip of the CLI session of the actor's newest harness session. A session no take
    has named a tip for is read as it is. None when the store holds no such session; a tip it lacks
    is refused by name."""
    row = None
    if name is None:
        row = await latest_session(khive, actor)
        if row is None:
            return None
        name = str(row.get("provider_session_id") or row["id"])
    try:
        (take,) = await khive.exec(f'lion.get(table="takes", id={lit(name)})')
    except KhiveError as e:
        if e.kind != "not_found":
            raise
        return row if row is not None else await session_of(khive, name)
    tip = await session_of(khive, take["tip"]) if take["tip"] is not None else None
    if tip is None:
        raise KhiveError(
            f"the takes of {name} name the tip {take['tip']}, which the store does not hold", "not_found"
        )
    return tip


async def held_view(khive: Khive, session_id: str, *, carried: bool = True) -> list[dict]:
    """The session's held view (ADR-0025 D12), as message rows in seq order: a branch's parent's held
    view before the branch's seq and then its own rows; any other session's own rows. The chain is
    followed one session at a time, each read only to its cutoff, so pieces are fetched for the rows
    kept alone. A `branch_of` the store does not hold, or a chain that comes back to a session, is
    refused by name."""
    rows: list[dict] = []
    walked: list[str] = []
    at, end = session_id, None
    while at is not None:
        if at in walked:
            raise KhiveError(f"session {at} branches from itself through {' <- '.join(walked)}", "invalid")
        row = await session_of(khive, at)
        if row is None:
            held = f"session {walked[-1]} branches from {at}" if walked else f"session {at}"
            raise KhiveError(f"{held}, which the store does not hold", "not_found")
        walked.append(at)
        start = int(row.get("branch_seq") or 0) if row.get("branch_of") is not None else 0
        rows[:0] = await read_messages(khive, at, carried=carried, start=start, end=end)
        end = start if end is None else min(end, start)
        at = row.get("branch_of")
    return rows


async def exchanges_of(khive: Khive, session_id: str) -> list[dict]:
    """The session's exchanges as the store holds them, in order: each message row's `raw` in its held
    view, the exchange as it was recorded (`{turn, at, input, text, kind, why}`), read whole however it
    was carried."""
    return [json.loads(r["raw"]) for r in await held_view(khive, session_id)]


async def record_exchanges(
    khive: Khive,
    *,
    source: str,
    session_id: str,
    actor: str,
    transcript: str,
    exchanges: Sequence[dict],
    why: str,
) -> tuple[str, int, int]:
    """A take of the CLI session `session_id` (ADR-0025 D12): it begins with `lion.take_begin`, which
    names its generation and the tip, and compares each held `content_hash` of the tip's view with the
    SHA-256 of the transcript's prompt at that seq. All agreeing, each seq the view lacks is appended to
    the tip, holes included, and a held tail past a shorter transcript stays. The prompts parting first
    at seq d, a branch of the tip opens at the lesser of d and the view's first hole, and the
    transcript's exchanges from there are its rows. The first take opens a root under the provider
    session id. Each row carries the take's reason. A write refused for a newer take begins the take
    again, on the same exchanges, SUPERSEDED times at most; superseded at every try it is refused by
    name, and the transcript is taken again later. `(session, turns, appended)`: the tip the take
    left, how many its view holds, and how many this take wrote. The rows go through
    `append_messages`, in calls of at most 50; the view is read without fetching a carried field."""
    prompts = [hashlib.sha256(str(t.get("input") or "").encode()).hexdigest() for t in exchanges]
    for _ in range(SUPERSEDED + 1):
        (begun,) = await khive.exec(f"lion.take_begin(provider_session_id={lit(session_id)})")
        generation, tip = begun["generation"], begun["tip"]
        try:
            if tip is None:
                (opened,) = await khive.exec(
                    f"lion.session_open(id={lit(session_id)}, source={lit(SOURCES[source])}, "
                    f"provider_session_id={lit(session_id)}, actor={lit(actor)}, "
                    f"transcript={lit(transcript)}, generation={generation})"
                )
                tip = opened["id"]
            view = {r["seq"]: r for r in await held_view(khive, tip, carried=False)}
            parted = [s for s in sorted(view) if s < len(prompts) and view[s]["content_hash"] != prompts[s]]
            if parted:
                hole = next((s for s in range(max(view)) if s not in view), parted[0])
                at = min(parted[0], hole)
                row = await session_of(khive, tip)  # the view's read found it
                (opened,) = await khive.exec(
                    f"lion.session_open(source={lit(row['source'])}, provider_session_id={lit(session_id)}, "
                    f"actor={lit(row.get('actor'))}, transcript={lit(row.get('transcript'))}, "
                    f"branch_of={lit(tip)}, branch_seq={at}, generation={generation})"
                )
                tip, missing, turns = opened["id"], range(at, len(exchanges)), len(exchanges)
            else:
                missing = [s for s in range(len(exchanges)) if s not in view]
                turns = len(view) + len(missing)
            rows = [
                {
                    "seq": seq,
                    "role": "user",
                    "kind": "exchange",
                    "text": str(exchanges[seq].get("input") or ""),
                    "raw": row_json({**exchanges[seq], "why": why}, ensure_ascii=False),
                    "turn": seq,
                    "generation": generation,
                }
                for seq in missing
            ]
            await append_messages(khive, tip, rows, per_call=50)
            return tip, turns, len(rows)
        except KhiveError as e:
            if e.kind != "conflict" or "is superseded" not in str(e):
                raise
    raise KhiveError(
        f"the take of {session_id} was superseded {SUPERSEDED + 1} times in a row by newer takes", "conflict"
    )


def declare(khive: Khive, actor: str, var: Var) -> Var:
    """A declared value, written as the actor's note `context.<name>`; the var written. A later
    declaration of the name replaces the earlier."""
    doc = row_json(dataclasses.asdict(var), ensure_ascii=False)
    khive.exec_sync(
        f"lion.note_put(profile={lit(actor)}, key={lit(DECLARED + var.name)}, value={lit(doc)}, "
        f"run={lit('context')}, seq=0)"
    )
    return var


def declared(khive: Khive, actor: str) -> list[Var]:
    """Every value declared for the actor, oldest written first."""
    (got,) = khive.exec_sync(f"lion.note_list(profile={lit(actor)}, prefix={lit(DECLARED)})")
    rows = sorted(got["rows"], key=lambda r: r["written"])
    return [Var(**json.loads(r["value"])) for r in rows]


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
    lines.append(
        f"The last {len(kept)} exchange(s) as they stood; {len(hidden)} earlier folded, the store keeps "
        f"them under session {c.source.get('session_id')} (the transcript: {c.source.get('transcript')})"
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
