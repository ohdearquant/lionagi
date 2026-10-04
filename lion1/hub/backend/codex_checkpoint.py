"""A Codex session's checkpoint (ADR-0019/D5): the scan of its rollout file, the take and the restore
prompt, on the Codex session's vocabulary (`CODEX_KINDS`, `CODEX_WHYS`). The rollout is
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl`: one JSON row per line, each a `type`
and a `payload`, the first the thread's own `session_meta`. It is read once per file state (its size
and mtime) for every source.

A prompt is the text items of a user `message` that the row's own `content_item_kinds` marks
`user.text`; the CLI marks what it writes there itself (the environment, AGENTS.md, the plugin list)
by other kinds, so none of it is read as the person's. A turn's text is the last assistant text before
the next prompt: tool calls and their output, reasoning and compactions are never read into a turn. The
`SubAgentActivity` rows name the agents the thread spawned, and the last `token_count` is the figure. A
forked thread's rows before its own history starts are its parent's and are not read. The sources no
harness owns are `hub.backend.checkpoint`'s.
"""

from __future__ import annotations

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


@dataclass
class _Scan:
    meta: dict = field(default_factory=dict)  # the thread's own session_meta
    model: str | None = None  # the newest turn_context's
    prompts: list[dict] = field(default_factory=list)  # {turn, at, input, text, kind: person}
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


def _typed(p: dict) -> str | None:
    """A user message's own words: its `input_text` items the row marks `user.text`, joined; None when
    the row does not mark each item, so what wrote it is not known."""
    content = p.get("content")
    marks = p.get("internal_chat_message_metadata_passthrough")
    kinds = marks.get("content_item_kinds") if isinstance(marks, dict) else None
    if not isinstance(content, list) or not isinstance(kinds, list) or len(kinds) != len(content):
        return None
    said = [
        str(b.get("text") or "")
        for b, k in zip(content, kinds, strict=True)
        if k == "user.text" and isinstance(b, dict) and b.get("type") == "input_text"
    ]
    return "\n".join(said).strip()


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


@functools.lru_cache(maxsize=4)
def _read(path: str, size: int, mtime_ns: int) -> _Scan:
    s = _Scan()
    start: Any = None  # a forked thread's first own ordinal: the rows before it are its parent's
    turn: dict | None = None
    with open(path, "rb") as f:
        for raw in f:
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
                if not s.meta:  # the thread's own; a forked thread carries its parent's after it
                    s.meta, start = p, p.get("subagent_history_start_ordinal")
                continue
            ordinal = r.get("ordinal")
            if isinstance(start, int) and isinstance(ordinal, int) and ordinal < start:
                continue
            at = r.get("timestamp") if isinstance(r.get("timestamp"), str) else ""
            if kind == "turn_context":
                s.model = p.get("model") or s.model
            elif kind == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                text = _typed(p)
                if text is None:
                    s.unmarked += 1
                elif text:
                    turn = _open(s.prompts, p.get("id"), at, text, "person")
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
    return s


def _scan(path: str | Path) -> _Scan:
    p = Path(path)
    st = p.stat()
    return _read(str(p), st.st_size, st.st_mtime_ns)


def is_rollout(path: str | Path) -> bool:
    """Whether the file is a Codex rollout: its first row is a `session_meta`, which no Claude Code
    transcript row is."""
    return _meta(Path(path)) is not None


def newest_rollout(directory: str | Path, sessions: Path | None = None) -> Path:
    """The most recently written rollout whose thread ran in `directory` and is no sub-agent's (a
    sub-agent runs in its parent's directory and writes as often)."""
    root, want = sessions or _sessions(), Path(directory).resolve()
    for p in sorted(root.glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True):
        meta = _meta(p)
        if (
            meta is not None
            and meta.get("thread_source") != "subagent"
            and isinstance(meta.get("cwd"), str)
            and Path(meta["cwd"]).resolve() == want
        ):
            return p
    raise CheckpointError(f"no Codex rollout under {root} whose cwd is {want}, a sub-agent's aside")


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
        "transcript": str(path),
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
    return fold_turns(_scan(path).prompts, budget)


def directives(path: str | Path) -> list[Var]:
    """The person's words, oldest first, never more than DIRECTIVES: the newest prompts of a thread a
    person or a program started. A sub-agent's thread has none: its prompts are its parent's."""
    s = _scan(path)
    if s.meta.get("thread_source") == "subagent":
        return []
    return [
        Var(f"directive.{i}", "directive", p["input"], "rollout", p["at"])
        for i, p in enumerate(s.prompts[-DIRECTIVES:], 1)
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
    except OSError:
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
