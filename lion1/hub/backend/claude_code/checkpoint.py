"""A Claude Code session's checkpoint (ADR-0019/D2): what it reads, the take and the restore prompt,
on the session's own vocabulary over the run's (`SESSION_KINDS`, `SESSION_WHYS`). Each source is a
small function that returns `Var`s: the transcript's own prompts, final texts and Monitor, TaskStop
and Agent calls; the hook tracker's directive ring; the sessions registry's topic; the statusline's
per-session figure. The sources no harness owns (the actor's lanes, its loop cursor) and the restore's
body are `hub.backend.checkpoint`'s, named here too. A source that is absent is a value, never a
failed take.

A take reads the transcript once, as one `Snapshot` for all of them: its size, times and inode
captured once, its bytes read to that size and no further, a last line cut mid-write left unread, the
file opened checked against that state before a byte is read and after the last. A rollout is not
read: a first row that is a `session_meta` is refused. A prompt is a `user` row whose content is a
string or text blocks and which no hook or harness wrote, as typed: never a tool result, a compaction
summary, a system-sourced or meta row, a slash command or its caveat and output, a task notification,
a system reminder, or an interrupt marker. The assistant's tool calls and thinking are never read into
a turn; only the last text block before the next prompt is the turn's text. The person's row with no
words, an image alone, ends the turn before it and opens none.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lion1.checkpoint import KINDS, WHYS, Checkpoint, CheckpointError, Var, listed, take

from ..checkpoint import (
    DIRECTIVES,
    MAIL_PAGE,
    SPAWN_HOURS,
    Snapshot,
    _at,
    _fenced,
    _lines,
    _now,
    _one,
    _open,
    actor_id,
    cursor,
    fold_turns,
    lanes,
    opening,
    posture_of,
    pulls,
    restored,
    snapshot_of,
)

__all__ = (
    "SESSION_KINDS",
    "SESSION_WHYS",
    "DIRECTIVES",
    "PROJECTS",
    "SESSIONS",
    "TYPED",
    "BY_SESSION",
    "INBOX_MONITOR",
    "MONITOR_DEFAULT_MS",
    "MONITOR_CAP_MS",
    "SPAWN_HOURS",
    "MAIL_PAGE",
    "actor_id",
    "slug",
    "newest_transcript",
    "snapshot",
    "transcript_source",
    "transcript_turns",
    "directives",
    "watches",
    "spawns",
    "topic",
    "lanes",
    "pulls",
    "context_figure",
    "cursor",
    "posture",
    "fold_turns",
    "take_session",
    "render_prompt",
)

# the run's kinds and whys and the session's own: a watch to re-arm, a spawn, a lane, the person's
# words, the topic; a take when the harness rotates the session or compacts it
SESSION_KINDS = (*KINDS, "watch", "spawn", "lane", "directive", "topic")
SESSION_WHYS = (*WHYS, "rotate", "precompact")

PROJECTS = Path.home() / ".claude" / "projects"
SESSIONS = Path.home() / ".khive" / "sessions"  # what the prompt hook persists per session
TYPED = (
    Path.home() / ".claude" / "usage" / "typed.jsonl"
)  # the typing log: what a program typed, not the person
BY_SESSION = Path.home() / ".claude" / "usage" / "by-session"  # the statusline's figure per session
# the inbox script a restore re-arms (`LION_INBOX_MONITOR`, a path) and the actors it names after the
# directory's own (`LION_INBOX_PRIORITY`, space-separated), so on another host both are the host's
INBOX_MONITOR = os.environ.get("LION_INBOX_MONITOR", "inbox-monitor.sh")
INBOX_PRIORITY = tuple(os.environ.get("LION_INBOX_PRIORITY", "").split())
MONITOR_DEFAULT_MS = 300_000  # the Monitor tool's default timeout
MONITOR_CAP_MS = 1_800_000  # and its cap: every monitor, persistent or not, is killed at its deadline

_FRAMES = (
    "<command-name>",
    "<command-message>",
    "<local-command-",
    "<task-notification>",
    "<system-reminder>",
)
_INTERRUPT = "[Request interrupted by user"  # the harness's mark on a turn cut short, not a prompt
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_SLASH = re.compile(r"/[A-Za-z][\w:-]*")  # `/compact` alone on a row; a typed path has a second slash
_TERMINAL = ("completed", "killed", "failed", "stopped")  # a task notification that ends its task
_EVENT_MAX = 200  # an event turn's label: the notification's summary, this long at most


@dataclass
class _Scan:
    prompts: list[dict] = field(default_factory=list)  # {turn, at, input, text, kind: person | event}
    last: tuple[str, str] | None = None  # the last assistant text block and its row's time
    model: str | None = None
    monitors: list[dict] = field(default_factory=list)  # {use, i, at, input, task}
    agents: list[dict] = field(default_factory=list)  # {use, i, at, input, id, status}
    stopped: dict[str, int] = field(default_factory=dict)  # task id -> the row of its last TaskStop
    ended: dict[str, int] = field(default_factory=dict)  # task or tool-use id -> its terminal notification
    unread: int = 0  # lines not read: not JSON, or the snapshot's last line, cut mid-write


def _clean(text: str) -> str | None:
    """A prompt's own words, as typed: None when a hook or the harness wrote it, else the text with any
    system reminder blocks cut out. Whether it holds any words is its caller's to ask."""
    head = text.lstrip()
    if head.startswith(_FRAMES) or head.startswith(_INTERRUPT):
        return None
    if head and _SLASH.fullmatch(head.split(maxsplit=1)[0]):
        return None
    return _REMINDER.sub("", text)


def _prompt(r: dict) -> str | None:
    if r.get("type") != "user" or "toolUseResult" in r:
        return None
    if r.get("isCompactSummary") or r.get("isMeta") or r.get("promptSource") == "system":
        return None
    content = (r.get("message") or {}).get("content")
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, list) and content:
        # text blocks, beside a pasted image at most, or an image alone (no words); a tool_result
        # block is never a prompt
        if all(isinstance(b, dict) and b.get("type") in ("text", "image") for b in content):
            return _clean("\n".join(b.get("text") or "" for b in content if b.get("type") == "text"))
    return None


def _notice(text: str) -> dict | None:
    """A `<task-notification>` as the harness writes it: the ids it names, whether it ends the task, and
    a label for the wake it opened. None for any other text."""
    if not text.lstrip().startswith("<task-notification>"):
        return None
    status = re.search(r"<status>([^<]*)</status>", text)
    expired = re.search(r"<event>\s*\[Monitor expired", text)
    summary = re.search(r"<summary>(.*?)</summary>", text, re.S)
    event = re.search(r"<event>(.*?)</event>", text, re.S)
    said = (summary.group(1) if summary else "") + " " + (event.group(1) if event else "")
    label = " ".join(said.split())
    return {
        # the call it names when it names one: a resumed agent keeps its task id, and a late duplicate
        # of the old call's notice must not end the new call
        "keys": [k.strip() for k in re.findall(r"<tool-use-id>([^<]*)</", text)]
        or [k.strip() for k in re.findall(r"<task-id>([^<]*)</", text)],
        "ends": bool(expired) or bool(status and status.group(1).strip() in _TERMINAL),
        "label": ("[event] " + label)[:_EVENT_MAX] if label else "[event]",
    }


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text") or "" for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


@functools.lru_cache(maxsize=4)
def _read(snap: Snapshot) -> _Scan:
    s = _Scan()
    uses: dict[str, dict] = {}  # tool_use id -> the Monitor or Agent call it opened
    turn: dict | None = None
    with open(snap.path, "rb") as f:
        for i, raw in enumerate(_lines(f, snap)):  # the snapshot's bytes, the file checked before and after
            if not raw.endswith(b"\n"):  # the snapshot's last line, cut mid-write: never read
                s.unread += 1
                continue
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                s.unread += 1
                continue
            if not isinstance(r, dict):
                continue
            if i == 0 and r.get("type") == "session_meta":  # its first row says a Codex rollout
                raise CheckpointError(
                    f"{snap.path}: its first row is a session_meta, so it is a Codex rollout, which the "
                    "Claude Code reader does not read"
                )
            if r.get("type") == "queue-operation":  # queue bookkeeping: ends tasks, opens nothing
                n = _notice(str(r.get("content") or ""))
                if n and n["ends"]:
                    for key in n["keys"]:
                        s.ended[key] = i
                continue
            if r.get("type") == "attachment":
                att = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
                n = _notice(str(att.get("prompt") or "")) if att.get("type") == "queued_command" else None
                if n:
                    if n["ends"]:
                        for key in n["keys"]:
                            s.ended[key] = i
                    turn = _open(s.prompts, r.get("uuid"), r.get("timestamp") or "", n["label"], "event")
                continue
            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            content = msg.get("content")
            if r.get("type") == "assistant" and isinstance(content, list):
                s.model = msg.get("model") or s.model
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and (b.get("text") or "").strip():
                        s.last = (b["text"], r.get("timestamp") or "")
                        if turn is not None:
                            turn["text"] = b["text"]
                    elif b.get("type") == "tool_use":
                        name, given = b.get("name"), b.get("input") or {}
                        call = {"use": b.get("id"), "i": i, "at": r.get("timestamp") or "", "input": given}
                        if name == "Monitor":
                            uses[b.get("id")] = {**call, "task": None}
                            s.monitors.append(uses[b.get("id")])
                        elif name == "Agent":
                            uses[b.get("id")] = {**call, "id": None, "status": None}
                            s.agents.append(uses[b.get("id")])
                        elif name == "TaskStop" and given.get("task_id"):
                            s.stopped[str(given["task_id"])] = i
                continue
            if r.get("type") != "user":
                continue
            if isinstance(content, list):
                for b in content:
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "tool_result"
                        and b.get("tool_use_id") in uses
                    ):
                        call, text = uses[b["tool_use_id"]], _result_text(b)
                        told = r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else {}
                        call["error"] = bool(b.get("is_error"))
                        if "task" in call:
                            m = re.search(r"\(task ([A-Za-z0-9_-]+)", text)
                            call["task"] = m.group(1) if m else told.get("taskId")
                        else:
                            m = re.search(r"agentId: ([A-Za-z0-9_-]+)", text)
                            call["id"], call["status"] = (
                                (m.group(1) if m else told.get("agentId")),
                                told.get("status"),
                            )
            if r.get("promptSource") == "system" and isinstance(content, str):
                n = _notice(content)
                if n:
                    if n["ends"]:
                        for key in n["keys"]:
                            s.ended[key] = i
                    # a wake the harness opened: the actor worked after it without a word from the owner
                    turn = _open(s.prompts, r.get("uuid"), r.get("timestamp") or "", n["label"], "event")
                continue
            text = _prompt(r)
            if text is not None:  # the person's, an image alone included: it ends the one before it
                at = r.get("timestamp") or ""
                turn = _open(s.prompts, r.get("uuid"), at, text, "person") if text.strip() else None
    return s


def snapshot(path: str | Path) -> Snapshot:
    """The transcript's file state now, read at once: the scan every reader of the snapshot gets is of
    that state. A file that changes between its stat and its read is taken again, RETAKES times at
    most."""
    return snapshot_of(path, _read)


def _scan(path: str | Path | Snapshot) -> _Scan:
    return _read(path if isinstance(path, Snapshot) else snapshot(path))


def _later(s: _Scan, i: int, *keys: str | None) -> bool:
    """A TaskStop, or a notification that ends the task, after row `i` naming any of the keys."""
    return any(k and (s.stopped.get(k, -1) > i or s.ended.get(k, -1) > i) for k in keys)


def slug(cwd: str | Path) -> str:
    """Claude Code's project directory name for a cwd: every character but a letter or digit is `-`
    (`/home/a/.khive` is `-home-a--khive`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def newest_transcript(directory: str | Path, projects: Path = PROJECTS) -> Path:
    """The most recently written session transcript of the actor whose cwd is `directory`, to the
    nanosecond, a `_precompact_` backup never. A transcript is named by its session id, a random UUID,
    so no name says which of two written in the same nanosecond is newer: that tie is refused."""
    d = projects / slug(Path(directory).resolve())
    found = {p: p.stat().st_mtime_ns for p in d.glob("*.jsonl") if "_precompact_" not in p.name}
    if not found:
        raise CheckpointError(f"no Claude Code transcript under {d}")
    newest = max(found.values())
    tied = sorted(p.name for p, ns in found.items() if ns == newest)
    if len(tied) > 1:
        raise CheckpointError(
            f"{len(tied)} Claude Code transcripts under {d} were last written in the same nanosecond "
            f"({', '.join(tied)}), and a random id names each: which is the session's is not known"
        )
    return d / tied[0]


def transcript_source(path: str | Path) -> dict:
    """What the checkpoint's `source` says of the transcript: its path, the model that last answered,
    and how many of its lines were not JSON when read, a last line cut mid-write included."""
    s = _scan(path)
    return {"transcript": os.fspath(path), "model": s.model, "unread_lines": s.unread}


def transcript_turns(path: str | Path, budget: int) -> dict:
    """The actor's view: each kept turn, the owner's prompt or the event that woke the actor, with the turn's
    final text, folded oldest-first to half the budget, the latest always kept (`fold_turns`)."""
    return fold_turns(_scan(path).prompts, budget)


def _ring(path: Path) -> list[Var]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    if isinstance(doc.get("directives"), list):
        said = [(str(t), "") for t in doc["directives"]]
    else:  # `persist` writes the messages; the ring is their `person` rows, as `from_dict` rebuilds it
        said = [
            (str(m.get("content") or ""), m.get("timestamp"))
            for m in doc.get("messages") or []
            if isinstance(m, dict) and m.get("role") == "person"
        ]
    out = []
    for text, ts in said:
        words = _clean(text)
        if words is None or not words.strip():
            continue
        stamped = isinstance(ts, int | float) and not isinstance(ts, bool) and ts > 0
        at = datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if stamped else ""
        out.append((words, at))
    return [
        Var(f"directive.{i}", "directive", t, "hook", at) for i, (t, at) in enumerate(out[-DIRECTIVES:], 1)
    ]


def directives(
    session_id: str | None, transcript_path: str | Path, typed_log: Path = TYPED, sessions: Path = SESSIONS
) -> list[Var]:
    """the person's words, oldest first, never more than DIRECTIVES: the hook tracker's ring for the session
    when it holds any, else the newest kept prompts whose text the actor tool did not type."""
    ring = _ring(sessions / f"{session_id}.json") if session_id else []
    if ring:
        return ring
    typed: set[str] = set()
    try:
        for line in typed_log.read_text().split("\n"):  # JSONL is framed by LF; U+2028 sits inside a string
            try:
                typed.add(str(json.loads(line).get("text", "")).strip())
            except (ValueError, AttributeError):
                continue
    except OSError:
        pass
    said = [p for p in _scan(transcript_path).prompts if p.get("kind", "person") == "person"]
    kept = [p for p in said if p["input"].strip() not in typed]
    return [
        Var(f"directive.{i}", "directive", p["input"], "transcript", p["at"])
        for i, p in enumerate(kept[-DIRECTIVES:], 1)
    ]


def _expired(m: dict, now: datetime) -> bool:
    """A Monitor ends at its timeout whatever `persistent` says (the tool kills every monitor at its
    deadline, 30 minutes at most; measured: 65 watches from 18 days of an actor's transcript,
    all persistent, all long dead). A missing timeout is the tool's default."""
    timeout = m["input"].get("timeout_ms")
    if not isinstance(timeout, int | float) or isinstance(timeout, bool):
        timeout = MONITOR_DEFAULT_MS
    started = _at(m)
    if started is None:
        return False
    return started + timedelta(milliseconds=min(timeout, MONITOR_CAP_MS)) <= now


def watches(transcript_path: str | Path, actor: str, now: datetime | None = None) -> list[Var]:
    """Every Monitor whose task no later TaskStop, and no notification ending it, names, and that has
    not outlived its own timeout by `now`; one per command, the newest (the same command armed twice is
    a duplicate, never two watches). The actor's inbox monitor is added when none of them runs
    the inbox script."""
    s = _scan(transcript_path)
    now = now or datetime.now(UTC)
    latest: dict[str, dict] = {}  # command -> its newest arm, live or not: a re-arm retires the older
    for m in s.monitors:
        if m["task"] and not m.get("error"):
            latest.pop(str(m["input"].get("command")), None)
            latest[str(m["input"].get("command"))] = m
    out: list[Var] = []
    for m in latest.values():
        if _later(s, m["i"], m["task"], m["use"]) or _expired(m, now):
            continue
        given = m["input"]
        value = {k: given.get(k) for k in ("command", "description", "timeout_ms")}
        if "persistent" in given:
            value["persistent"] = given["persistent"]
        out.append(Var(f"watch.{m['task']}", "watch", value, "transcript", m["at"]))
    running = False  # a watch runs the inbox script, itself or handed to a shell; a mention is not a run
    for v in out:
        try:
            argv = shlex.split(str(v.value.get("command") or ""), comments=True)
        except ValueError:  # a command that does not parse is not known to run it
            continue
        running = running or _runs_inbox(argv)
    if not running:
        command = shlex.join(["bash", INBOX_MONITOR, actor, *(a for a in INBOX_PRIORITY if a != actor)])
        value = {"command": command, "description": f"{actor} inbox", "timeout_ms": None, "persistent": True}
        out.append(Var("watch.inbox", "watch", value, "declared", _now()))
    return out


# options after which the shell still runs its script (each measured on bash, sh and zsh); after
# any other (`-n`, `-s`, `--version`, `--rcfile FILE`) it is not known to
_SHELL_SHORT = frozenset("abefhklmpruvxBCEHPT")
_SHELL_SET = frozenset(("errexit", "nounset", "pipefail", "verbose", "xtrace"))  # `-o NAME`
_SHELL_LONG = {
    "zsh": frozenset(("--login", "--restricted", "--verbose")),
    "bash": frozenset(
        (
            "--debugger",
            "--login",
            "--noediting",
            "--noprofile",
            "--norc",
            "--posix",
            "--restricted",
            "--verbose",
        )
    ),
}


def _runs_inbox(argv: list[str]) -> bool:
    """Whether `argv` runs the inbox script: itself, after `env [NAME=value ...]`, or handed to a shell
    as its script or as the first word of its `-c` string. A command it cannot place is not known to."""
    if argv and Path(argv[0]).name == "env":
        argv = argv[1:]
        while argv and "=" in argv[0] and not argv[0].startswith("-"):
            argv = argv[1:]
    if argv and Path(argv[0]).name in ("bash", "sh", "zsh"):
        long = _SHELL_LONG["zsh" if Path(argv[0]).name == "zsh" else "bash"]
        argv, string = argv[1:], False
        while argv and argv[0][:1] in ("-", "+") and argv[0] not in ("-", "--"):
            flag, argv = argv[0], argv[1:]
            if flag.startswith("--"):
                if flag not in long:
                    return False
            elif flag[1:] == "o":  # `-o pipefail`: the option takes the next word
                if not argv or argv[0] not in _SHELL_SET:
                    return False
                argv = argv[1:]
            else:
                string = string or (flag[0] == "-" and "c" in flag)
                if not set(flag[1:].replace("c", "") if flag[0] == "-" else flag[1:]) <= _SHELL_SHORT:
                    return False
        if argv and argv[0] in ("-", "--"):
            argv = argv[1:]
        if string:
            try:
                return bool(argv) and _runs_inbox(shlex.split(argv[0], comments=True))
            except ValueError:
                return False
    return bool(argv) and Path(argv[0]).name == Path(INBOX_MONITOR).name


def spawns(transcript_path: str | Path, now: datetime | None = None) -> list[Var]:
    """Agent spawns whose result named an agent id and that no later TaskStop, and no notification
    ending them, names, spawned within SPAWN_HOURS of `now`: running is what the transcript shows at the
    take, and the value says so. The age bound: a session restart loses its children without a notice
    (measured: 29 spawns read as running, the oldest two days old)."""
    s = _scan(transcript_path)
    now = now or datetime.now(UTC)
    out = []
    for a in s.agents:
        started = _at(a)
        if (
            not a["id"]
            or a.get("error")
            or a["status"] == "completed"
            or _later(s, a["i"], a["id"], a["use"])
            or started is None
            or started + timedelta(hours=SPAWN_HOURS) <= now
        ):
            continue
        value = {"id": a["id"], "task": a["input"].get("description"), "state": "running-at-take"}
        out.append(Var(f"spawn.{a['id']}", "spawn", value, "transcript", a["at"]))
    return out


def topic(registry_path: str | Path) -> list[Var]:
    """`active_topic` of the actor's sessions registry, when it names one."""
    p = Path(registry_path)
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    t = doc.get("active_topic") if isinstance(doc, dict) else None
    if not t:
        return []
    at = datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    return [Var("topic", "topic", t, "registry", at)]


def context_figure(session_id: str | None, by_session: Path = BY_SESSION) -> dict:
    """The statusline's figure for the session: `{used, window, pct}`, each None when not filed."""
    none = {"used": None, "window": None, "pct": None}
    if not session_id:
        return none
    try:
        doc = json.loads((by_session / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        return none
    if not isinstance(doc, dict):
        return none
    cw = doc.get("context_window") if isinstance(doc.get("context_window"), dict) else {}
    return {
        "used": cw.get("total_input_tokens", doc.get("total_input_tokens")),
        "window": cw.get("context_window_size", doc.get("context_window_size")),
        "pct": cw.get("used_percentage", doc.get("used_percentage")),
    }


def posture(transcript_path: str | Path) -> list[Var]:
    """The last assistant text block, its LNDL taken out and its lines kept."""
    return posture_of(_scan(transcript_path).last, "transcript")


def _hooked(text: Any) -> bool:
    s = str(text)
    return s.lstrip().startswith(_FRAMES) or _REMINDER.search(s) is not None


def take_session(
    source: dict,
    vars: Iterable[Var],
    *,
    actor: str,
    why: str,
    budget: int,
    view: dict | None = None,
    now: datetime | None = None,
) -> Checkpoint:
    """The checkpoint of a Claude Code session: `take` on the session's kinds and whys, its view the
    session's turns (`fold_turns`), a directive and a watch never lazy. A directive or a kept input
    carrying a hook's frame is refused: whatever read it let it through."""
    c = take(
        {"kind": "claude-code", **source},
        vars,
        actor=actor,
        why=why,
        budget=budget,
        view=view if view is not None else fold_turns((), budget),
        now=now,
        kinds=SESSION_KINDS,
        whys=SESSION_WHYS,
        eager=("directive", "watch"),
    )
    for v in c.vars:
        if v.kind == "directive" and _hooked(v.value):
            raise CheckpointError(
                f"directive {v.name!r} carries a hook's frame; whatever read it let it through"
            )
    for t in c.view.get("kept", []):
        if _hooked(t.get("input") or ""):
            raise CheckpointError(f"turn {t.get('turn')}: its input carries a hook's frame")
    return c


def render_prompt(checkpoint: Checkpoint) -> str:
    """The first prompt of the fresh session. Its first line names the checkpoint, the actor and the
    context figure at the take, and asks for the watches to be re-armed before any other work; each
    watch's command follows verbatim. Then the person's words verbatim (the newest DIRECTIVES), the
    lanes, the running spawns, the topic, what was declared, the kept view, the posture, and what to do
    first (`restored`). A lazy var is its name, its size and how to load it, never its body."""
    c = listed(checkpoint, "nothing was restored", kinds=SESSION_KINDS, whys=SESSION_WHYS)
    watches = [v for v in c.vars if v.kind == "watch"]
    head = opening(c)
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
    first = (
        "re-arm the watches above, then take up the newest of the person's words and the lanes from "
        "where the posture leaves them."
    )
    return "\n".join([*lines, *restored(c, first)])
