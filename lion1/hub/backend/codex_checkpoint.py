"""A Codex session's checkpoint (ADR-0019/D5): the scan of its rollout file, the take and the restore
prompt, on the Codex session's vocabulary (`CODEX_KINDS`, `CODEX_WHYS`). The rollout is
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl`: one JSON row per line, each a `type`
and a `payload`, the first the thread's own `session_meta`. A take reads it once, as one `Snapshot`:
the file's size, times and inode captured once, its bytes read to that size and no further, a last
line cut mid-write left unread. The read is of that state or is not kept: the file opened is checked
against it before a byte is read and after the last. The scan keeps what the take's readers read and
nothing else.

A prompt is the text items of a user `message` that the row's own `content_item_kinds` marks
`user.text`, as typed; the CLI marks what it writes there itself (the environment, AGENTS.md, the
plugin list) by other kinds, so none of it is read as the person's. A turn's text is the last assistant
text before the next message the person sent or nothing marked: tool calls and their output, reasoning
and compactions are never read into a turn. The `SubAgentActivity` rows name the agents the thread
spawned, and the last `token_count` is the figure. A forked thread's rows before its own history starts
are its parent's and are not read. The sources no harness owns are `hub.backend.checkpoint`'s.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lion1.checkpoint import KINDS, WHYS, Checkpoint, CheckpointError, Var, listed, take

from .checkpoint import DIRECTIVES, SPAWN_HOURS, _at, _open, fold_turns, opening, posture_of, restored
from .codex import _THREAD

__all__ = (
    "CODEX_KINDS",
    "CODEX_WHYS",
    "OWNERS",
    "Snapshot",
    "snapshot",
    "is_rollout",
    "newest_rollout",
    "rollout_of",
    "rollout_source",
    "rollout_turns",
    "directives",
    "spawns",
    "context_figure",
    "posture",
    "take_codex",
    "render_prompt",
)

# the run's kinds and whys and the Codex session's own: a spawn, a lane, the person's words, the topic,
# a take when the session is rotated. No `watch`: the CLI has no Monitor to re-arm one with
CODEX_KINDS = (*KINDS, "spawn", "lane", "directive", "topic")
CODEX_WHYS = (*WHYS, "rotate")

# the openings of the text the CLI writes into a user message itself (measured on 0.156.1 to 0.160.0,
# each under a kind other than `user.text`): a directive or kept input opening so was let through
_INJECTED = (
    "<environment_context>",
    "<recommended_plugins>",
    "# AGENTS.md instructions",
    "<skill>",
    "<codex_internal_context",
    "<external_codex_apps_open_page",
)
# a sub-agent's last activity while it holds work: spawned, or handed more
_RUNNING = ("started", "interacted")
_NONE = {"used": None, "window": None, "pct": None}
# the keys of the thread's own session_meta its readers read; the rest (the base instructions among
# them) is never kept
_META = (
    "id",
    "cwd",
    "model_provider",
    "cli_version",
    "originator",
    "thread_source",
    "parent_thread_id",
    "agent_path",
)
# the thread_source of a thread whose owner is known: a person's, or a sub-agent's
OWNERS = ("user", "subagent")
RETAKES = 2  # a rollout that changed under its read is stat and read again this many times


@dataclass(frozen=True)
class Snapshot:
    """One file state of a rollout, captured once: every read of a take reads exactly its `size`
    bytes, and its scan keeps the view `budget` folds to, none when it is None."""

    path: str
    size: int
    mtime_ns: int
    ino: int  # a file put in its place
    ctime_ns: int  # a rewrite in place keeps the size and can restore the mtime, never this
    budget: int | None = None

    def __fspath__(self) -> str:
        return self.path


class _Moved(CheckpointError):
    """The file a snapshot names is not in the snapshot's state: put in its place, cut, or rewritten."""


@dataclass
class _Scan:
    meta: dict = field(default_factory=dict)  # the thread's own session_meta, its `_META` keys
    model: str | None = None  # the newest turn_context's
    words: list[tuple[str, str]] = field(default_factory=list)  # the newest DIRECTIVES prompts, at
    view: dict | None = None  # `fold_turns` of every prompt to the snapshot's budget
    last: tuple[str, str] | None = None  # the last assistant text and its row's time
    agents: dict[str, dict] = field(default_factory=dict)  # thread id -> {id, path, at, last}
    figure: dict = field(default_factory=lambda: dict(_NONE))
    unread: int = 0  # lines that were not JSON (the live file's last line, mid-write)
    unmarked: int = 0  # user messages whose row does not say what wrote each item: never read


def _sessions() -> Path:
    """Where the CLI keeps its rollouts: under `CODEX_HOME`, else `~/.codex`, read at each call."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def _meta(path: Path) -> dict | None:
    """The rollout's own `session_meta` payload, its first row; None when the first row is not one."""
    try:
        with path.open("rb") as f:
            row = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    payload = row.get("payload") if isinstance(row, dict) and row.get("type") == "session_meta" else None
    return payload if isinstance(payload, dict) else None


def _typed(p: dict) -> tuple[bool, str] | None:
    """A user message's sender and words: whether the row marks any item the person's (`user.*`), and
    its `input_text` items marked `user.text`, joined as typed; None when the row does not mark each
    item or marks none, so what wrote it is not known."""
    content = p.get("content")
    marks = p.get("internal_chat_message_metadata_passthrough")
    kinds = marks.get("content_item_kinds") if isinstance(marks, dict) else None
    if not isinstance(content, list) or not isinstance(kinds, list) or len(kinds) != len(content):
        return None
    if not kinds:  # nothing marked: not the CLI's by any mark either
        return None
    said = [
        str(b.get("text") or "")
        for b, k in zip(content, kinds, strict=True)
        if k == "user.text" and isinstance(b, dict) and b.get("type") == "input_text"
    ]
    return any(isinstance(k, str) and k.startswith("user.") for k in kinds), "\n".join(said)


def _count(n: Any) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n >= 0


def _figure(info: dict) -> dict:
    """A `token_count`'s figure: the last request's input, cache read included, against the model's
    window, and the percentage that is of it."""
    usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
    used = usage.get("input_tokens") if _count(usage.get("input_tokens")) else None
    window = info.get("model_context_window") if _count(info.get("model_context_window")) else None
    pct = round(used * 100 / window) if used is not None and window else None
    return {"used": used, "window": window, "pct": pct}


def _state(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns


def _same(f: Any, snap: Snapshot) -> bool:
    """Whether the open file is the snapshot's: in its very state, or the same file grown since. One
    grown is taken as appended to, its bytes to the snapshot's size the snapshot's own."""
    st = os.fstat(f.fileno())
    return _state(st) == (snap.size, snap.mtime_ns, snap.ino, snap.ctime_ns) or (
        st.st_ino == snap.ino and st.st_size > snap.size
    )


@functools.lru_cache(maxsize=4)
def _read(snap: Snapshot) -> _Scan:
    s = _Scan()
    lines = 0  # the first is the thread's own session_meta; a later one is a fork's parent's
    start: Any = None  # a forked thread's first own ordinal: the rows before it are its parent's
    prompts: list[dict] = []  # {turn, at, input, text, kind: person}
    turn: dict | None = None
    with open(snap.path, "rb") as f:
        if not _same(f, snap):  # another file put in its place, or this one cut or rewritten
            raise _Moved(f"{snap.path} is not the file its snapshot was taken of")
        left = snap.size  # a row appended after the snapshot is after the take
        while left > 0:
            raw = f.readline(left)
            if not raw:  # cut while it was read: the check after the last byte refuses it
                break
            left -= len(raw)
            lines += 1
            if not raw.endswith(b"\n"):  # the snapshot's last line, cut mid-write: never read
                s.unread += 1
                break
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                s.unread += 1
                continue
            if not isinstance(r, dict):
                continue
            kind = r.get("type")
            p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
            if kind == "session_meta":
                if lines == 1:
                    start = p.get("subagent_history_start_ordinal")
                    s.meta = {k: p[k] for k in _META if k in p}
                continue
            ordinal = r.get("ordinal")
            if isinstance(start, int) and isinstance(ordinal, int) and ordinal < start:
                continue
            at = r.get("timestamp") if isinstance(r.get("timestamp"), str) else ""
            if kind == "turn_context":
                s.model = p.get("model") or s.model
            elif kind == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                typed = _typed(p)
                if typed is None:  # what wrote it is not known: the exchange before it ends there
                    s.unmarked += 1
                    turn = None
                elif typed[0]:  # the person's, an image alone included: it ends the one before it
                    words = typed[1]
                    turn = _open(prompts, p.get("id"), at, words, "person") if words.strip() else None
            elif kind == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
                for b in p.get("content") or []:
                    said = (
                        str(b.get("text") or "")
                        if isinstance(b, dict) and b.get("type") == "output_text"
                        else ""
                    )
                    if said.strip():
                        s.last = (said, at)
                        if turn is not None:
                            turn["text"] = said
            elif kind == "event_msg" and p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                s.figure = _figure(p["info"])  # a count with no info (a rate-limit notice) tells no figure
            elif kind == "event_msg" and p.get("type") == "item_completed":
                item = p.get("item") if isinstance(p.get("item"), dict) else {}
                thread = item.get("agent_thread_id")
                if item.get("type") != "SubAgentActivity" or not isinstance(thread, str):
                    continue
                if item.get("kind") == "started":
                    s.agents[thread] = {
                        "id": thread,
                        "path": item.get("agent_path"),
                        "at": at,
                        "last": "started",
                    }
                elif thread in s.agents:  # an agent another thread spawned is not this one's
                    s.agents[thread]["last"] = item.get("kind")
        if not _same(f, snap):
            raise _Moved(f"{snap.path} was rewritten while it was read")
    s.words = [(p["input"], p["at"]) for p in prompts[-DIRECTIVES:]]
    s.view = fold_turns(prompts, snap.budget) if snap.budget is not None else None
    return s


def snapshot(path: str | Path, budget: int | None = None) -> Snapshot:
    """The rollout's file state now, for a take whose view folds to `budget`, read at once: the scan
    every reader of the snapshot gets is of that state. A file that changes between its stat and its
    read is taken again, RETAKES times at most."""
    p = Path(path)
    tries = 0
    while True:
        snap = Snapshot(str(p), *_state(p.stat()), budget)
        try:
            _read(snap)
        except _Moved:
            tries += 1
            if tries > RETAKES:
                raise
        else:
            return snap


def _scan(path: str | Path | Snapshot, budget: int | None = None) -> _Scan:
    if not isinstance(path, Snapshot):
        return _read(snapshot(path, budget))
    return _read(path if budget is None else dataclasses.replace(path, budget=budget))


def is_rollout(path: str | Path) -> bool:
    """Whether the file is a Codex rollout: its first row is a `session_meta`, which no Claude Code
    transcript row is."""
    return _meta(Path(path)) is not None


def newest_rollout(directory: str | Path, sessions: Path | None = None) -> tuple[Path, int]:
    """The most recently written rollout of a person's thread that ran in `directory`, and how many of
    the directory's rollouts written after it were skipped because their first row names no owner. A
    sub-agent runs in its parent's directory and writes as often. Of two written in the same
    nanosecond the later name is the newer: a name holds the thread's start time, then its id."""
    root, want = sessions or _sessions(), Path(directory).resolve()
    found = root.glob("*/*/*/rollout-*.jsonl")
    skipped = 0
    for p in sorted(found, key=lambda f: (f.stat().st_mtime_ns, f.name), reverse=True):
        meta = _meta(p)
        if meta is None or not isinstance(meta.get("cwd"), str) or Path(meta["cwd"]).resolve() != want:
            continue
        if meta.get("thread_source") == "user":
            return p, skipped
        skipped += meta.get("thread_source") not in OWNERS
    raise CheckpointError(
        f"no Codex rollout under {root} whose cwd is {want} and whose first row says a person's "
        f"thread; {skipped} whose first row names no owner skipped"
    )


def rollout_of(thread: str, sessions: Path | None = None) -> Path:
    """The one rollout of the Codex thread `thread`."""
    root = sessions or _sessions()
    if not _THREAD.fullmatch(thread):
        raise CheckpointError(f"{thread!r} is not a Codex thread id")
    found = list(root.glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    if len(found) != 1:
        raise CheckpointError(f"{len(found)} Codex rollouts name thread {thread} under {root}, not one")
    return found[0]


def rollout_source(path: str | Path) -> dict:
    """What the checkpoint's `source` says of the rollout: its thread, path and directory, the model
    that last answered, the CLI, who started the thread and, for a sub-agent's, its parent and path,
    how many of its lines were not JSON when read, and how many user messages did not say what wrote
    each item, so none of them was read."""
    s = _scan(path)
    m = s.meta
    return {
        "session_id": m.get("id"),
        "transcript": os.fspath(path),
        "session_cwd": m.get("cwd"),
        "model": s.model,
        "model_provider": m.get("model_provider"),
        "cli_version": m.get("cli_version"),
        "originator": m.get("originator"),
        "thread_source": m.get("thread_source"),
        "parent_thread_id": m.get("parent_thread_id"),
        "agent_path": m.get("agent_path"),
        "unread_lines": s.unread,
        "unmarked_messages": s.unmarked,
    }


def rollout_turns(path: str | Path, budget: int) -> dict:
    """The session's view: each prompt with its turn's final text, folded oldest-first to half the
    budget, the latest always kept (`fold_turns`)."""
    return copy.deepcopy(_scan(path, budget).view)


def directives(path: str | Path) -> list[Var]:
    """The person's words, oldest first, never more than DIRECTIVES: the newest prompts of a thread
    whose first row says a person's. Any other has none: a sub-agent's prompts are its parent's."""
    s = _scan(path)
    if s.meta.get("thread_source") != "user":
        return []
    return [
        Var(f"directive.{i}", "directive", text, "rollout", at) for i, (text, at) in enumerate(s.words, 1)
    ]


def spawns(path: str | Path, now: datetime | None = None) -> list[Var]:
    """The sub-agents the thread started whose last activity is their start or more work handed to
    them, never their completion or interruption, started within SPAWN_HOURS of `now`: running is what
    the rollout shows at the take, and the value says so."""
    s = _scan(path)
    now = now or datetime.now(UTC)
    out = []
    for a in s.agents.values():
        started = _at(a)
        if a["last"] not in _RUNNING or started is None or started + timedelta(hours=SPAWN_HOURS) <= now:
            continue
        value = {"id": a["id"], "path": a["path"], "state": "running-at-take"}
        out.append(Var(f"spawn.{a['id']}", "spawn", value, "rollout", a["at"]))
    return out


def context_figure(path: str | Path | None) -> dict:
    """The figure of the rollout's last `token_count`: `{used, window, pct}`, each None when not known,
    a rollout absent or unreadable included."""
    if path is None:
        return dict(_NONE)
    try:
        return dict(_scan(path).figure)
    except (OSError, CheckpointError):  # absent, or changing under every read of it
        return dict(_NONE)


def posture(path: str | Path) -> list[Var]:
    """The last assistant text, its LNDL taken out and its lines kept."""
    return posture_of(_scan(path).last, "rollout")


def _injected(text: Any) -> bool:
    return str(text).lstrip().startswith(_INJECTED)


def take_codex(
    source: dict,
    vars: Iterable[Var],
    *,
    actor: str,
    why: str,
    budget: int,
    view: dict | None = None,
    now: datetime | None = None,
) -> Checkpoint:
    """The checkpoint of a Codex session: `take` on its kinds and whys, its view the session's turns
    (`fold_turns`), a directive never lazy. A directive or a kept input opening with text the CLI
    writes itself is refused: whatever read it let it through."""
    c = take(
        {"kind": "codex", **source},
        vars,
        actor=actor,
        why=why,
        budget=budget,
        view=view if view is not None else fold_turns((), budget),
        now=now,
        kinds=CODEX_KINDS,
        whys=CODEX_WHYS,
        eager=("directive",),
    )
    for v in c.vars:
        if v.kind == "directive" and _injected(v.value):
            raise CheckpointError(
                f"directive {v.name!r} opens with text the Codex CLI writes; whatever read it let it through"
            )
    for t in c.view.get("kept", []):
        if _injected(t.get("input") or ""):
            raise CheckpointError(f"turn {t.get('turn')}: its input opens with text the Codex CLI writes")
    return c


def render_prompt(checkpoint: Checkpoint) -> str:
    """The first prompt of the fresh session. Its first line names the checkpoint, the actor, the time,
    the reason and the context figure at the take, and the thread it was taken from; the rest is
    `restored`: the person's words verbatim, the lanes, the running spawns, the declared values, the
    kept view, the posture and what to do first. A lazy var is its name, size and loader, never its body."""
    c = listed(checkpoint, "nothing was restored", kinds=CODEX_KINDS, whys=CODEX_WHYS)
    s = c.source
    head = opening(c) + (
        f"It was taken from Codex thread {s.get('session_id')} (model {s.get('model')}, "
        f"CLI {s.get('cli_version')})."
    )
    first = "take up the newest of the person's words and the lanes from where the posture leaves them."
    return "\n".join([head, *restored(c, first)])
