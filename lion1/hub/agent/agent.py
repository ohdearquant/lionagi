"""A long-running actor: it wakes on mail, runs once per wake, answers on the thread, and leaves a
cursor for the next wake.

An actor has an inbox and a run has bounds; nothing in the runtime runs an actor when mail lands
(`Actor.queue`: "something has to run it"). The Agent is that something, outside the loop. It
long-polls the actor's khive inbox, takes what arrived as one batch, marks it read before anything
acts on it (a wake that dies halfway must not answer twice), lets a policy choose the profile for
the wake by who wrote, and runs. A reply is a command the model gives, `send`, which requires the
`comm.send` privilege: a profile without it reads mail and never answers it. The wake ends with
OUT{posture: ...}, the model's own line on what it did and what it awaits.

The Agent keeps its own state in one note store beside the profiles' notes. `cursor` holds the
facts of the last wake, written as "running" with the batch's ids before the run so a wake that
dies still names what it took, then overwritten with the outcome. `threads` counts what this agent
has sent on each thread; past the hop limit `send` refuses, which is what stops two agents answering
each other forever. `spend` holds the wakes of the last hour and the cost of the day; at either cap
the agent does not wake, and mail waits. The model's line goes under `posture` in the profile's own
notes. Cursor and posture head the next wake's input, and the record crosses wakes through the store:
each run continues the last run the cursor names, its history that run's rows (ADR-0026 C6). One
process holds an identity: `serve`, and every one-shot command of the CLI, locks a pid file in the
agent's directory and refuses to start while a live process holds it.
"""

from __future__ import annotations

import asyncio
import fcntl
import fnmatch
import json
import math
import os
import re
import sys
import time
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from lion1.actor import (
    Actor,
    Backend,
    Context,
    Handle,
    Inbound,
    Profile,
    Run,
    RunRequest,
    Success,
    reported_cost,
)
from lion1.khive import Khive, KhiveError, lit
from lion1.lndl import plain
from lion1.notes import NoteStore
from lion1.record import Kind

from ..areas import partial
from ..backend.claude_code import _prompt_tokens

__all__ = (
    "AgentError",
    "Send",
    "Posture",
    "Policy",
    "Budget",
    "WakeRule",
    "Wake",
    "Gtd",
    "CLAUDE_CODE",
    "claude_code_options",
    "codex_options",
    "Agent",
)


class AgentError(Exception):
    pass


class LoopLock:
    """An asyncio.Lock for whichever loop is running. A plain one binds to the first loop that waits
    on it, and the next `asyncio.run` driving the same agent then raises at its first wait."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop, self._lock = loop, asyncio.Lock()
        await self._lock.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


class Send(BaseModel):
    """Answer on a thread. One message per thread per wake; say it once, briefly."""

    to: str = Field(description="the sender's actor id, from the mail header")
    thread: str = Field(description="the thread id, from the mail header")
    text: str


class Posture(BaseModel):
    """How this wake ended, for the next wake to read first."""

    done: str = Field(description="what this wake did, one or two lines")
    awaiting: str = Field(default="", description="what a later wake should look for, or empty")


Policy = Callable[[list[dict]], Profile]
"""The mail of a wake in, the profile the wake runs under out."""


@dataclass(frozen=True)
class Budget:
    wakes_per_hour: int = 60
    cost_per_day: float = 1.0  # USD from the backend's own cost figures; a backend without them spends 0
    hops_per_thread: int = 12  # messages this agent may send on one thread, ever

    @classmethod
    def load(cls, table: dict | None) -> Budget:
        """`[budget]` of chores.toml: the caps, each defaulting to this class's own."""
        b = table or {}
        return cls(
            wakes_per_hour=int(b.get("wakes_per_hour", cls.wakes_per_hour)),
            cost_per_day=float(b.get("cost_per_day", cls.cost_per_day)),
            hops_per_thread=int(b.get("hops_per_thread", cls.hops_per_thread)),
        )


_THEN = ("now", "batch", "drop")


@dataclass(frozen=True)
class WakeRule:
    """One `[[wake.rule]]`: what to do with mail it matches. Every named condition must hold."""

    then: str  # now | batch | drop
    senders: tuple[str, ...] = ()  # globs on the sender's actor id (fnmatch); none = any sender
    subject: str | None = None  # a regex searched in the subject; None = any
    spent_over: float | None = None  # holds only once today's cost is past this fraction of the day's cap

    def matches(self, m: dict, spent: float) -> bool:
        if self.senders and not any(fnmatch.fnmatchcase(_sender(m), g) for g in self.senders):
            return False
        if self.subject is not None and re.search(self.subject, m.get("subject") or "") is None:
            return False
        return self.spent_over is None or spent > self.spent_over


@dataclass(frozen=True)
class Wake:
    """`[wake]`: which mail wakes the model at once, which waits for a batch, which is dropped (a wake
    per mail is too dear; the trigger is a rule). Rules read top-down, the first
    that matches decides, none matching is `default`. Mail that waits is read and kept under the
    `deferred` note until the window opens: `batch_size` waiting, or the oldest `batch_after` old,
    outside `quiet` hours. `now` mail wakes at once, quiet hours included, and takes what waits along;
    so does the `wake-now` file a person drops in the directory."""

    default: str = "now"
    batch_after: float = 1800.0  # seconds
    batch_size: int = 5
    quiet: tuple[tuple[int, int], ...] = ()  # (from, to) in minutes of the local day; to < from wraps
    rules: tuple[WakeRule, ...] = ()

    @classmethod
    def load(cls, table: dict | None) -> Wake | None:
        """`[wake]` of chores.toml; no table is None (every admitted mail wakes at once). A rule's
        `then`, `default`, a span like `30m` and a quiet range like `23:00-07:00` are checked here, so
        a bad file fails the start (or the reload, in the log), never a wake."""
        if not table:
            return None
        default = str(table.get("default", "now"))
        if default not in _THEN:
            raise ValueError(f"[wake] default {default!r}: one of {_THEN}")
        rules = []
        for i, r in enumerate(table.get("rule") or []):
            then = str(r.get("then", ""))
            if then not in _THEN:
                raise ValueError(f"[[wake.rule]] {i + 1}: then {then!r}: one of {_THEN}")
            senders = r.get("from", ())
            senders = (senders,) if isinstance(senders, str) else tuple(str(s) for s in senders)
            subject = r.get("subject")
            if subject is not None:
                re.compile(str(subject))
            over = r.get("spent_over")
            rules.append(
                WakeRule(
                    then,
                    senders,
                    None if subject is None else str(subject),
                    None if over is None else float(over),
                )
            )
        return cls(
            default=default,
            batch_after=_span(table.get("batch_after", 1800.0)),
            batch_size=int(table.get("batch_size", 5)),
            quiet=tuple(_quiet(q) for q in (table.get("quiet") or ())),
            rules=tuple(rules),
        )

    def decide(self, m: dict, spent: float) -> str:
        for r in self.rules:
            if r.matches(m, spent):
                return r.then
        return self.default

    def is_quiet(self, now: float) -> bool:
        t = time.localtime(now)
        minute = t.tm_hour * 60 + t.tm_min
        return any((a <= minute < b) if a <= b else (minute >= a or minute < b) for a, b in self.quiet)

    def window_open(self, waiting: list[dict], now: float) -> bool:
        """Whether what waits wakes the model now: enough of it, or old enough, outside quiet hours."""
        if not waiting or self.is_quiet(now):
            return False
        oldest = min(float(m.get("deferred_ts") or now) for m in waiting)
        return len(waiting) >= self.batch_size or now - oldest >= self.batch_after

    def describe(self) -> str:
        quiet = ", ".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in self.quiet)
        return (
            f"default {self.default}, {len(self.rules)} rule(s), window {self.batch_after:.0f}s or "
            f"{self.batch_size} mail" + (f", quiet {quiet}" if quiet else "")
        )


def _span(value: Any) -> float:
    """Seconds from a number or `90s` / `30m` / `2h` / `1d`."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(value))
    if m is None:
        raise ValueError(f"[wake] span {value!r}: a number of seconds or like 30m")
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


@dataclass(frozen=True)
class Gtd:
    """`[gtd]`: the agent's own task queue as a wake source and a line in every notification. The
    queue is `gtd.next` for the actor, read every `poll` seconds while serving; nothing here moves a
    task. A task whose `at` has passed wakes the model like `now` mail, once per `at` (`due`); with
    `work = "idle"` a quiet agent (no mail, nothing waiting, `idle_after` since its last wake) takes
    the top task of its queue. An agent built from a config reads its queue and cannot move a task,
    so it would take the same task at every idle pass: `load` refuses `work = "idle"`
    (ADR-0013/C9), and the pass is left for an agent whose tools can close what it takes."""

    poll: float = 60.0  # seconds between queue reads
    due: str = "now"  # now | off: a task past its `at`
    work: str = "off"  # off | idle: self-driven work on the queue's top when quiet
    idle_after: float = 900.0  # seconds since the last wake before a quiet chair works its queue
    notice: int = 5  # tasks named in each turn's notification
    limit: int = 20  # tasks read per poll

    @classmethod
    def load(cls, table: dict | None) -> Gtd | None:
        if not table:
            return None
        due, work = str(table.get("due", "now")), str(table.get("work", "off"))
        if due not in ("now", "off"):
            raise ValueError(f"[gtd] due {due!r}: now or off")
        if work == "idle":
            raise ValueError(
                '[gtd] work "idle": the queue is read-only to the agent, so it would take the same task at '
                "every idle pass (ADR-0013/C9); due tasks still wake it"
            )
        if work != "off":
            raise ValueError(f"[gtd] work {work!r}: off")
        return cls(
            poll=_span(table.get("poll", 60.0)),
            due=due,
            work=work,
            idle_after=_span(table.get("idle_after", 900.0)),
            notice=int(table.get("notice", 5)),
            limit=int(table.get("limit", 20)),
        )

    def describe(self) -> str:
        return f"poll {self.poll:.0f}s, due {self.due}, work {self.work}" + (
            f" after {self.idle_after:.0f}s quiet" if self.work == "idle" else ""
        )


CLAUDE_CODE = ("timeout", "effort", "cli")  # the `[claude_code]` keys: none of them gives the model a tool


def claude_code_options(table: dict | None) -> dict:
    """`[claude_code]` as an agent takes it: `timeout`, `effort` and `cli`. A claude_code model has no
    tools of its own (ADR-0016/D3), so any other key refuses by name, whatever the model, at load and at
    a reload, never ignored."""
    if table is not None and not isinstance(table, dict):
        raise ValueError(f"[claude_code] is a table of timeout, effort and cli, not {table!r}")
    options = dict(table or {})
    if extra := sorted(set(options) - set(CLAUDE_CODE)):
        raise ValueError(
            f"[claude_code] {', '.join(extra)}: a claude_code model has no tools of its own "
            f"(ADR-0016/D3); the keys are {', '.join(CLAUDE_CODE)}"
        )
    timeout = options.get("timeout")
    if "timeout" in options and (  # `true` is an int to Python: one second, read off a typo
        isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout < float("inf")
    ):
        raise ValueError(f"[claude_code] timeout is a number of seconds above zero, not {timeout!r}")
    for key in ("effort", "cli"):
        if key in options and not (isinstance(options[key], str) and options[key]):
            raise ValueError(f"[claude_code] {key} is a string that is not empty, not {options[key]!r}")
    return options


CODEX = ("timeout", "effort", "cli", "home")  # the `[codex]` keys: none of them gives the model a tool


def codex_options(table: dict | None) -> dict:
    """`[codex]` as an agent takes it: `timeout`, `effort`, `cli` and `home`. A codex model has no
    tools of its own and runs only on a CLI release its tools-off claim was measured on
    (ADR-0016/D3), so any other key, `unmeasured` among them, refuses by name, whatever the model,
    at load and at a reload, never ignored."""
    if table is not None and not isinstance(table, dict):
        raise ValueError(f"[codex] is a table of timeout, effort, cli and home, not {table!r}")
    options = dict(table or {})
    if extra := sorted(set(options) - set(CODEX)):
        raise ValueError(
            f"[codex] {', '.join(extra)}: a codex model has no tools of its own "
            f"(ADR-0016/D3); the keys are {', '.join(CODEX)}"
        )
    timeout = options.get("timeout")
    if "timeout" in options and (  # `true` is an int to Python: one second, read off a typo
        isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout < float("inf")
    ):
        raise ValueError(f"[codex] timeout is a number of seconds above zero, not {timeout!r}")
    for key in ("effort", "cli", "home"):
        if key in options and not (isinstance(options[key], str) and options[key]):
            raise ValueError(f"[codex] {key} is a string that is not empty, not {options[key]!r}")
    return options


def _at(task: dict) -> float | None:
    """A task's `at` as unix seconds: a number or an ISO time (naive = local); None if unset or unreadable."""
    value = task.get("at")
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _task_notice(task: dict, why: str) -> dict:
    """A task as a mail-shaped notice for the batch: never marked read, named apart on the cursor."""
    p = task.get("properties") or {}
    return {
        "id": task.get("id"),
        "task": why,  # due | idle | hand
        "subject": task.get("title") or "",
        "content": str(p.get("description") or ""),
        "priority": task.get("priority"),
        "status": task.get("status"),
        "at": task.get("at"),
        "properties": {"from_actor": "gtd", "thread_id": None},
    }


def _quiet(value: Any) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d\d):(\d\d)\s*-\s*(\d\d):(\d\d)\s*", str(value))
    if m is None:
        raise ValueError(f"[wake] quiet {value!r}: like 23:00-07:00")
    hours, minutes = (int(m[1]), int(m[3])), (int(m[2]), int(m[4]))
    if any(h > 23 for h in hours) or any(n > 59 for n in minutes):  # 07:75 is not 08:15
        raise ValueError(f"[wake] quiet {value!r}: hours 00-23, minutes 00-59")
    return hours[0] * 60 + minutes[0], hours[1] * 60 + minutes[1]


@dataclass
class Agent:
    actor: Actor
    khive: Khive
    backend: Backend
    profile_for: Policy
    admit: Callable[[dict], bool] | None = None  # None admits all mail; refused mail never reaches a run
    budget: Budget = Budget()
    owner: str | None = None  # the actor told when a batch is lost or a wake fails; None tells nobody
    state: NoteStore | None = None  # the agent's own notes: the CLI hands the store's; None: the actor's own
    max_turns: int = 6
    # the gate on OUT{} (RunRequest.accept): why the wake may not end yet, or None; a refusal is a retry
    accept: Callable[[Run, Any], Awaitable[str | None] | str | None] | None = None
    poll_ms: int = 5000  # how long one inbox call waits for mail before the loop asks again
    # a second box whose arrival wakes the agent: polled beside its own for the same wait, it returns
    # mail-shaped notices that join the wake's batch (never marked read: they are not the agent's
    # mail). Its failure is recorded and costs the agent nothing: its own mail still runs.
    watch: Callable[[int], Awaitable[list[dict]]] | None = None
    batch: int = 50
    clock: Callable[[], float] = time.time
    wakes: int = 0  # the wake number, continued from the stored cursor across processes
    held: str | None = None  # why the last wake did not happen, when a budget cap held it
    runs: list[Run] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _sending: LoopLock = field(default_factory=LoopLock, repr=False)
    _delivered: dict[str, int] = field(default_factory=dict, repr=False)  # run id -> messages sent
    _lock_fd: int | None = field(default=None, repr=False)  # the pid file, held locked while serving
    _dropped: list[dict] = field(default_factory=list, repr=False)  # this wake's refused mail
    admitted: list[dict] = field(default_factory=list)  # the running wake's batch: what an ask may answer
    # the chair: one run for the life of the process. The model's plain reply ends
    # a burst of mail; `_wait` books it as one wake, then brings the next mail into the same run, whose
    # view folds as it grows. A run that ends (OUT, a stop, the caps) is followed by a new one, which
    # continues it from the store's rows.
    continuous: bool = False
    _say: Callable[[str], None] = field(default=lambda line: None, repr=False)
    _until: Callable[[], bool] | None = field(default=None, repr=False)
    _burst: dict = field(default_factory=dict, repr=False)  # the running burst: mail, before, turn0, sent0
    _serving: bool = field(default=False, repr=False)  # inside serve(): the only place a run may go on
    _carry: tuple[list[dict], list[dict]] | None = field(default=None, repr=False)  # taken, for a fresh run
    idle: int = 0  # polls since the last wake that found no mail: each one cost no model call
    wake_rules: Wake | None = None  # `[wake]`; None: every admitted mail wakes at once
    gtd: Gtd | None = None  # `[gtd]`; None: the queue is neither read nor a wake source
    _queue: list[dict] = field(default_factory=list, repr=False)  # the last gtd.next read
    _queue_at: float = field(default=float("-inf"), repr=False)
    _last_wake: float = field(default=0.0, repr=False)  # the clock at the last wake booked
    config: Path | None = None  # chores.toml, whose [budget] and [wake] are re-read when it changes
    _config_mtime: float | None = field(default=None, repr=False)
    _waiting_said: int = field(default=-1, repr=False)  # the waiting count last said in the log

    def __post_init__(self) -> None:
        if self.state is None:  # the CLI hands the store's rows (ADR-0025 D6); else the actor's own
            self.state = self.actor.store.notes("agent")
        if self.config is not None and self.config.exists():
            self._config_mtime = self.config.stat().st_mtime  # what this process was built with
        # a quiet chair counts from its start, or its last wake booked, whichever is later
        self._last_wake = max([self.clock(), *_value(self.state, "spend", {}).get("wakes", [])])
        self.actor.section(self._queue_line)  # the queue in every notification, when [gtd] is on

        @self.actor.handler(Send, requires={"comm.send"})
        async def send(req: Send, ctx: Context) -> str:
            sent = await self.deliver(req.to, req.text, thread=req.thread, run=ctx.run)
            return f"sent {sent.get('id')} to {req.to} on thread {req.thread}"

    async def deliver(
        self,
        to: str,
        text: str,
        *,
        thread: str | None,
        run: Run | str,
        subject: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Every message this agent sends goes out through here: the thread's count is read, the
        message sent and the count written as one step under a lock (two sends in one turn run
        concurrently), and the send is counted for the wake's cursor. A handler of the agent's actor
        that sends on its own stands outside the hop limit; it calls this instead. The count is charged
        when the send is admitted, before the transport, and kept when the outcome is unknown: the
        message may have landed. A key is charged once (`hops_charged`, key -> thread): the same key
        again is its replay or a repeat settlement, neither capped nor charged, whatever the server's
        `replayed` says. A refusal before the send (`AgentError`) gives the charge back. `thread=None`
        opens a thread: the count starts at one under the id comm returns (`sent["thread_id"]`).
        `run` is the live run, or the id of the attempt a replay settles outside any run."""
        cap = self.budget.hops_per_thread
        rid, seq = (run, 0) if isinstance(run, str) else (run.id, run.record.seq)
        key = idempotency_key
        async with self._sending:
            threads = dict(_value(self.state, "threads", {}))
            charged = dict(_value(self.state, "hops_charged", {}))
            prior = dict(threads), dict(charged)
            fresh = key is None or key not in charged
            n = int(threads.get(thread, 0)) if thread is not None else 0
            if fresh and n >= cap:
                raise AgentError(f"hop limit: {n} messages already sent on thread {thread}; not again")

            def charge(tid: str) -> None:  # the count before the key: a death between them over-counts
                threads[tid] = int(threads.get(tid, 0)) + 1
                self.state.put("threads", threads, run=rid, seq=seq)
                if key is not None:
                    charged[key] = tid
                    self.state.put("hops_charged", charged, run=rid, seq=seq)
                self._delivered[rid] = self._delivered.get(rid, 0) + 1

            if fresh and thread is not None:
                charge(thread)
            try:
                sent = await self.khive.send(to, text, subject=subject, thread_id=thread, idempotency_key=key)
            except AgentError:  # refused before it was sent: the key goes back first, then the count
                if fresh and thread is not None:
                    if key is not None:
                        self.state.put("hops_charged", prior[1], run=rid, seq=seq)
                    self.state.put("threads", prior[0], run=rid, seq=seq)
                    self._delivered[rid] -= 1
                raise
            if fresh and thread is None:
                charge(str(sent.get("thread_id") or sent.get("id")))
        return sent

    # one wake

    def hold(self) -> str | None:
        """Why the agent must not wake now, or None: a `hold` file in the directory (a person's, its
        first line the reason; removing it resumes), the last hour's wakes or the day's cost at cap."""
        held = self._control("hold")
        if held is not None and held.exists():
            try:
                first = held.read_text(errors="replace").strip().splitlines()
            except OSError:
                first = []
            return "held by file: " + (first[0] if first else str(held))
        spend = _value(self.state, "spend", {})
        now = self.clock()
        recent = [t for t in spend.get("wakes", []) if now - t < 3600]
        if len(recent) >= self.budget.wakes_per_hour:
            if self.budget.wakes_per_hour < 1:  # no wake ages out to lift a cap below one: say so
                return f"wakes_per_hour is {self.budget.wakes_per_hour}: no wakes until it is raised"
            at = time.strftime("%H:%M:%S", time.localtime(min(recent) + 3600))
            return f"{len(recent)} wakes in the last hour (cap {self.budget.wakes_per_hour}); next at {at}"
        cost, unknown = _day_spend(spend, _day(now))
        if cost >= self.budget.cost_per_day:
            return (
                f"${cost:.4f} spent today"
                + partial(unknown)
                + f" (cap ${self.budget.cost_per_day}); next day"
            )
        return None

    def _control(self, name: str) -> Path | None:
        """A control file in the agent's directory (`hold`, `wake-now`): a person's lever, read each poll."""
        cwd = getattr(self.khive, "cwd", None)
        return None if cwd is None else Path(cwd) / name

    def _reload(self) -> None:
        """`[budget]` and `[wake]` follow the file: a cap or a rule edited while serving takes effect at
        the next poll, said once in the log; a file that does not parse keeps the values in force."""
        if self.config is None:
            return
        try:
            mtime = self.config.stat().st_mtime
        except OSError:
            return
        if self._config_mtime is not None and mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            cfg = tomllib.loads(self.config.read_text())
            budget, rules = Budget.load(cfg.get("budget")), Wake.load(cfg.get("wake"))
            gtd = Gtd.load(cfg.get("gtd"))
            claude_code_options(
                cfg.get("claude_code")
            )  # the backend stays until a restart: said, not ignored
            codex_options(cfg.get("codex"))
        except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError) as e:
            self.errors.append(f"reload failed: {type(e).__name__}: {e}")
            self._say(f"reload failed, the values in force stay: {type(e).__name__}: {e}")
            return
        self.budget, self.wake_rules, self.gtd = budget, rules, gtd
        self._say(
            f"reloaded: budget cost_per_day={budget.cost_per_day} wakes_per_hour={budget.wakes_per_hour}; "
            f"wake {rules.describe() if rules is not None else 'every mail now'}; "
            f"gtd {gtd.describe() if gtd is not None else 'off'}"
        )

    async def _read_queue(self, now: float, *, force: bool = False) -> None:
        """`gtd.next` for this actor (next and active, its order), at most every `poll` seconds."""
        if self.gtd is None or (not force and now - self._queue_at < self.gtd.poll):
            return
        (found,) = await self.khive.exec(
            f"gtd.next(assignee={lit(self.khive.actor)}, limit={int(self.gtd.limit)})"
        )
        self._queue, self._queue_at = [t for t in (found or []) if isinstance(t, dict)], now

    async def _tasks(self, now: float) -> list[dict]:
        """The tasks due now, as notices for this wake, once per `at`: a task the model leaves at the
        same `at` is not a wake again; one it moves is. A read that fails is a logged line, no tasks."""
        if self.gtd is None or (self.gtd.due == "off" and self.gtd.work != "idle"):
            return []
        try:
            await self._read_queue(now)  # with `due` off, `work = "idle"` still reads it: its top is the work
        except Exception as e:
            line = f"gtd read failed: {type(e).__name__}: {e}"
            if not self.errors or self.errors[-1] != line:
                self.errors.append(line)
                self._say(line)
            return []
        if self.gtd.due == "off":
            return []
        woken = dict(_value(self.state, "tasks", {}).get("woken") or {})
        out = []
        for t in self._queue:
            at = _at(t)
            if at is not None and at <= now and woken.get(str(t.get("id"))) != str(t.get("at")):
                out.append(_task_notice(t, "due"))
                woken[str(t.get("id"))] = str(t.get("at"))
        if out:
            self.state.put("tasks", {"woken": woken}, run="-", seq=0)
        return out

    def _idle_task(self, now: float) -> list[dict]:
        """Under `work = "idle"`: the queue's top as this wake's one notice, when the chair has been quiet
        for `idle_after`; the wake it causes moves the clock, so the next is `idle_after` later."""
        if self.gtd is None or self.gtd.work != "idle" or not self._queue:
            return []
        if now - self._last_wake < self.gtd.idle_after:
            return []
        return [_task_notice(self._queue[0], "idle")]

    async def _hand_wake(self, now: float) -> list[dict]:
        """A `wake-now` file with no mail waiting: the lever still pulls. The file is consumed first,
        then the queue is read as it is now, not the cache: its top runs as this wake's one notice,
        whatever `work` and `due` say; an empty queue, or one that could not be read, is said and
        spends no model call. A file that will not go is said once and does not pull."""
        forced = self._control("wake-now")
        if forced is None or not forced.is_file():
            return []
        try:
            forced.unlink()
        except OSError as e:
            line = f"wake-now: {type(e).__name__}: {e}; the file stays and does not pull"
            if not self.errors or self.errors[-1] != line:
                self.errors.append(line)
                self._say(line)
            return []
        if self.gtd is None:
            self._queue = []  # no [gtd]: no queue, whatever a reload left cached
        else:
            try:
                await self._read_queue(now, force=True)
            except Exception as e:
                self.errors.append(f"gtd read failed: {type(e).__name__}: {e}")
                self._say(f"wake-now: no mail waiting and the queue could not be read; no wake: {e}")
                return []
        if self._queue:
            self._say("wake-now: no mail waiting; the queue's top runs")
            return [_task_notice(self._queue[0], "hand")]
        self._say("wake-now: nothing waiting and no task; no wake")
        return []

    def _queue_line(self, run: Run) -> str | None:
        """The notification's line on the queue: counts and the top few, from the last read."""
        if self.gtd is None or not self._queue:
            return None
        active = sum(1 for t in self._queue if t.get("status") == "active")
        top = " · ".join(
            f"{t.get('id')} {t.get('priority')} {t.get('status')} {str(t.get('title') or '')[:80]}"
            for t in self._queue[: self.gtd.notice]
        )
        return f"queue: {active} active, {len(self._queue) - active} next; {top}"

    def _waiting(self) -> list[dict]:
        return list(_value(self.state, "deferred", {}).get("mail") or [])

    def _due(self) -> bool:
        """Whether what waits wakes the model with no new mail: the window opened, or a wake-now file."""
        waiting = self._waiting()
        if not waiting:
            return False
        forced = self._control("wake-now")
        if forced is not None and forced.is_file():
            return True
        return self.wake_rules is not None and self.wake_rules.window_open(waiting, self.clock())

    def _triage(self, mail: list[dict], *, woken: bool = False) -> list[dict]:
        """The mail that wakes the model now, in the order it came. The rules sort what arrived: dropped
        mail joins `_dropped` (read, recorded on the cursor, never run, like the gate's), mail that waits
        joins the `deferred` note (read, run when its window opens), `now` mail runs and takes what
        waits along; so does a `wake-now` file, consumed here, and an open window."""
        now = self.clock()
        before = self._waiting()
        waiting, take = list(before), []
        if self.wake_rules is None:
            take = list(mail)
        else:
            spend = _value(self.state, "spend", {})
            cost, _ = _day_spend(spend, _day(now))
            spent = cost / self.budget.cost_per_day if self.budget.cost_per_day > 0 else 0.0
            for m in mail:
                verdict = self.wake_rules.decide(m, spent)
                if verdict == "drop":
                    self._dropped.append({"id": m.get("id"), "from": _sender(m), "rule": "drop"})
                elif verdict == "batch":
                    waiting.append({**m, "deferred_at": _stamp(now), "deferred_ts": now})
                else:
                    take.append(m)
        forced = self._control("wake-now")
        if forced is not None and (waiting or take or woken) and forced.is_file():
            # one lever pull is one wake: spent by the wake this poll makes, whatever made it (mail,
            # or `woken`: a due task or a watch notice). With none (this poll's mail all dropped or
            # refused) the file stays and the next poll's `_hand_wake` answers it
            try:
                forced.unlink()
            except OSError as e:
                self.errors.append(f"wake-now: {type(e).__name__}: {e}")
            self._say(f"wake-now: taking {len(waiting)} waiting and {len(take)} new")
            take, waiting = waiting + take, []
        elif take or (self.wake_rules is not None and self.wake_rules.window_open(waiting, now)):
            take, waiting = waiting + take, []
        if [m.get("id") for m in waiting] != [m.get("id") for m in before]:
            kept = {m.get("id") for m in waiting}
            released = [m.get("id") for m in before if m.get("id") not in kept]
            if released:  # read mail reaches `taking` before `deferred` lets go of it (ADR-0013/C3)
                held = list(_value(self.state, "taking", {}).get("mail") or [])
                named = held + [i for i in released if i not in held]
                self.state.put("taking", {"mail": named}, run="-", seq=0)
            self.state.put("deferred", {"mail": waiting}, run="-", seq=0)
        if len(waiting) != self._waiting_said and (waiting or self._waiting_said > 0):
            self._waiting_said = len(waiting)
            if waiting:
                oldest = now - min(float(m.get("deferred_ts") or now) for m in waiting)
                rules = self.wake_rules
                window = f"{rules.batch_after:.0f}s or {rules.batch_size} mail" if rules is not None else "?"
                self._say(f"waiting: {len(waiting)} mail (oldest {oldest:.0f}s) for the window, {window}")
        return take

    async def wake(self, *, wait_ms: int | None = None) -> Run | None:
        """One wake: the mail that arrived, one run, the cursor. None when nothing arrived or a cap holds."""
        self._reload()
        self.held = self.hold()
        if self.held:
            return None
        if self._carry is not None:
            # mail `_wait` took (read, admitted, still under `taking`) whose profile is not the run's:
            # it runs fresh, under its own; nothing is recovered first, the taking row is this batch
            (mail, notices), self._carry = self._carry, None
            return await self._run_batch(mail, notices)
        self._dropped = []
        wait = self.poll_ms if wait_ms is None else wait_ms
        own = self.khive.inbox(wait_ms=wait, limit=self.batch)
        # the two boxes are polled side by side, each for the whole wait: one kkernel call apiece,
        # both back within the wait, neither cancelled (a cancelled call would leave its process), and
        # both joined when the own poll fails: a watch left running would write intake in a later wake
        if self.watch:
            mail, notices = await asyncio.gather(own, self._watched(wait), return_exceptions=True)
            for polled in (mail, notices):
                if isinstance(polled, BaseException):
                    raise polled
        else:
            mail, notices = await own, []
        now = self.clock()
        notices = list(notices) + await self._tasks(now)
        if not mail and not notices and not self._due():
            notices = await self._hand_wake(now) or self._idle_task(now)
            if not notices:
                self.idle += 1
                return None
        await self._recover()  # before this wake writes anything of its own
        if mail:
            # the batch's ids first, then the mark-read: a wake that dies between the two (or a
            # mark-read that raises after it committed) leaves ids behind for the next start to report
            self.state.put("taking", {"mail": [m.get("id") for m in mail]}, run="-", seq=0)
            ids = [m.get("full_id") or m["id"] for m in mail]
            await self.khive.exec(f"comm.read(ids={lit(ids)})")
        if self.admit is not None:  # refused mail is read, recorded on the cursor, and never run
            # one answer per message: a gate may do work per call, and a second call may answer otherwise
            verdicts = [(m, self.admit(m)) for m in mail]
            self._dropped = [{"id": m.get("id"), "from": _sender(m)} for m, ok in verdicts if not ok]
            mail = [m for m, ok in verdicts if ok]
        mail = self._triage(mail, woken=bool(notices))
        # `taking` names only what runs: deferred mail sits under its own note, dropped mail on the cursor
        self.state.put("taking", {"mail": [m.get("id") for m in mail]} if mail else {}, run="-", seq=0)
        if not mail and not notices:
            if self._dropped:
                prior = _value(self.state, "cursor", {})
                facts = {**prior, "dropped": self._dropped, "dropped_at": _stamp(self.clock())}
                self.state.put("cursor", facts, run="-", seq=0)
                self._dropped = []
            return None
        return await self._run_batch(mail, notices)

    async def _watched(self, wait_ms: int) -> list[dict]:
        """The watched box's notices for this wake; a failed watch is an error line and no notice."""
        assert self.watch is not None
        try:
            return list(await self.watch(wait_ms))
        except Exception as e:
            self.errors.append(f"watch failed: {type(e).__name__}: {e}")
            return []

    async def rerun(self, ids: list[str]) -> Run:
        """Take mail already marked read back through a wake: the batch a dead wake left, by its ids.
        A wake all the same: no ids is no wake, and a budget cap holds it as it holds `wake`."""
        if not ids:
            raise AgentError("rerun takes the ids of read mail; none were given")
        held = self.hold()
        if held is not None:
            raise AgentError(f"held: {held}")
        wanted, found, offset = set(ids), [], 0

        def names(m: dict) -> set[str]:
            # the cursor and the owner's notice carry the short id; the box answers with the full one
            keys = {str(k) for k in (m.get("full_id"), m.get("id")) if k}
            return {w for w in wanted if any(k == w or k.startswith(w) for k in keys)}

        while wanted:
            page = await self.khive.inbox(status="read", limit=self.batch, offset=offset)
            if not page:
                break
            found += [m for m in page if names(m)]
            wanted -= set().union(*(names(m) for m in page))
            offset += len(page)
        if wanted:
            raise AgentError(f"not in the read box: {sorted(wanted)}")
        self._dropped = []
        if self.admit is not None:  # the gate stands on this path as on `wake`: a rerun is a wake
            verdicts = [(m, self.admit(m)) for m in found]  # asked once per message, as on `wake`
            self._dropped = [{"id": m.get("id"), "from": _sender(m)} for m, ok in verdicts if not ok]
            found = [m for m, ok in verdicts if ok]
            if not found:
                prior = _value(self.state, "cursor", {})
                facts = {**prior, "dropped": self._dropped, "dropped_at": _stamp(self.clock())}
                self.state.put("cursor", facts, run="-", seq=0)
                dropped, self._dropped = self._dropped, []
                raise AgentError(f"the gate refuses all of that mail; no wake: {dropped}")
        # what an earlier wake left under `taking` or mid-run is owed before this run enters and lets go
        # of it; recovery leaves this batch's ids under `taking`, so a rerun that dies before its run
        # begins is reported in turn
        await self._recover(taken=[m.get("id") for m in found])
        return await self._run_batch(found)

    async def forget(self, run: str) -> None:
        """The owner's way past a refused wake: the cursor lets go of the run it names when the store
        cannot replay it, so the next wake continues nothing and starts a new chain. A run the store
        can replay is refused, and so is any run but the cursor's: neither is a break in the chain. What
        was forgotten, why and when stays under `forgotten` after later wakes move the cursor on."""
        prior = _value(self.state, "cursor", {})
        last = prior.get("run") or prior.get("continues")
        if run != last:
            raise AgentError(f"the cursor's last run is {last}, not {run}: nothing forgotten")
        try:
            await self.actor.store.history(run)
        except KhiveError as e:
            if e.kind not in ("not_found", "invalid"):
                raise
            gone = {"run": run, "why": str(e), "at": _stamp(self.clock())}
        else:
            raise AgentError(f"run {run} can be replayed: nothing forgotten")
        kept = _value(self.state, "forgotten", [])
        # a forget that died between its two writes is retried against a cursor that still names the
        # run: its record is already kept, and the retry lets the cursor go on that one
        gone = next((f for f in kept if f.get("run") == run), None) or gone
        if gone not in kept:
            self.state.put("forgotten", [*kept, gone], run="-", seq=0)
        self.state.put("cursor", {**prior, "run": None, "continues": None, "forgot": gone}, run="-", seq=0)

    async def _recover(self, taken: Collection[str] = ()) -> None:
        """A batch taken but never entered, or a cursor still at "running", is a wake that died after
        its mark-read: it is owed to the owner, recorded before `taking` and the cursor let go of it.
        Mail a rerun is taking back now (`taken`) is not owed: that rerun answers it. It goes under
        `taking` before any note naming it lets go, and stays there as the rerun's batch."""
        taken = list(taken)
        owed = []
        taking = _value(self.state, "taking", {})
        held = [i for i in taking.get("mail") or [] if i not in taken]
        if taken:  # one handoff: a death anywhere below leaves the rerun's mail under `taking`, owed in turn
            self.state.put("taking", {"mail": held + taken}, run="-", seq=0)
        if held:
            owed.append({"why": "a wake died after taking its mail and before its run began", "mail": held})
        prior = _value(self.state, "cursor", {})
        if prior.get("outcome") == "running":
            owed.append(
                {
                    "why": f"wake {prior.get('wake')} died mid-run",
                    # a wake that died entering its run left its batch under both notes: told once
                    "mail": [i for i in prior.get("mail") or [] if i not in taken and i not in held],
                    "notices": list(prior.get("notices") or []),
                }
            )
        self._owe(owed)
        gone = {i for o in owed for i in o["mail"]} | set(taken)
        left = [m for m in self._waiting() if m.get("id") not in gone]
        if len(left) != len(self._waiting()):  # told as lost or rerun now, so never also run from `deferred`
            self.state.put("deferred", {"mail": left}, run="-", seq=0)
        if taken or taking.get("mail"):
            self.state.put("taking", {"mail": taken} if taken else {}, run="-", seq=0)
        if prior.get("outcome") == "running":
            self.state.put("cursor", {**prior, "outcome": "lost"}, run="-", seq=0)
        await self._tell_owed()

    def _owe(self, entries: list[dict]) -> None:
        """Batches the owner must hear of go under `unreported` before any telling of them."""
        held = list(_value(self.state, "unreported", []))
        self.state.put("unreported", held + [e for e in entries if e not in held], run="-", seq=0)

    async def _tell_owed(self) -> None:
        """Tell the owner every batch under `unreported` in one message; the batches it named leave
        `unreported` once it landed, so a notice the transport dropped is told at the next start and a
        batch owed meanwhile stays. Watch notices are named apart: they are not mail and are not rerun."""
        owed = list(_value(self.state, "unreported", []))
        if not owed:
            return
        parts = []
        for o in owed:
            part = o["why"] + (f": its batch {o['mail']}" if o.get("mail") else "")
            parts.append(part + (f"; watch notices {o['notices']}" if o.get("notices") else ""))
        text = "; ".join(parts) + "."
        if any(o.get("mail") for o in owed):
            text += " That mail is marked read and unanswered. Agent.rerun(ids) takes it back through a wake"
            # a refused wake's mail cannot go back while the cursor still names the run the store lacks
            text += (
                ", once the run a refusal names is forgotten." if any(o.get("refused") for o in owed) else "."
            )
        if any(o.get("notices") for o in owed):
            text += " Watch notices are not mail: the watched box is read again at the next poll."
        if await self._tell_owner(text):
            left = [o for o in _value(self.state, "unreported", []) if o not in owed]
            self.state.put("unreported", left, run="-", seq=0)

    async def _run_batch(self, mail: list[dict], notices: list[dict] = ()) -> Run:
        """One run over the wake's own mail and the watch's notices; the cursor keeps them apart."""
        self.admitted = list(mail)
        notices = list(notices)
        batch = mail + notices
        profile = self.profile_for(batch)
        notes = self.actor.notes_for(profile)
        prior = _value(self.state, "cursor", {})
        self.wakes = int(prior.get("wake", 0)) + 1
        # the actor's last run, which this one continues from the store's rows (ADR-0026 C6): the cursor
        # names a run once it returns, and a wake that died or raised keeps the one it continued
        last = prior.get("run") or prior.get("continues")
        kept = {"continues": last} if last else {}
        try:
            history = await self.actor.store.history(last) if last else ()
        except KhiveError as e:
            if e.kind not in ("not_found", "invalid"):
                raise
            # fail closed: no run opens, so no run names one it never read; the batch is the owner's
            why = (
                f"wake {self.wakes} refused: the cursor's last run {last} cannot be replayed ({e}); every "
                f"wake refuses until `lion agent --forget {last}` (Agent.forget) lets the next start "
                "a new chain"
            )
            owed = {
                "why": why,
                "mail": [m.get("id") for m in mail],
                "notices": [n.get("id") for n in notices],
            }
            self._owe([{**owed, "refused": last}])
            self.state.put("taking", {}, run="-", seq=0)
            await self._tell_owed()
            raise AgentError(why) from None
        inputs = (self._input(batch, notes),)  # reads the last wake's cursor, before this wake overwrites it
        self._spend(wake=True)
        running = self._facts(None, mail, "running", profile, notices=notices)
        self.state.put("cursor", {**running, **kept}, run="-", seq=0)
        self.state.put("taking", {}, run="-", seq=0)  # entered: the cursor carries the batch from here
        trigger = tuple(str(m.get("full_id") or m["id"]) for m in batch if m.get("full_id") or m.get("id"))
        before = len(getattr(self.backend, "calls", ()))
        self._burst = {
            "mail": list(mail),
            "notices": notices,
            "before": before,
            "turn0": 0,
            "sent0": 0,
            "restarts0": int(getattr(self.backend, "restarts", 0) or 0),
        }
        if self.continuous and self._serving:
            # turns run on across bursts; `_stop` ends the run at a burst past max_turns or the day's
            # cap. No declared output: the runtime would ask for OUT{} at every turn, and a batch ends in
            # plain text; an OUT{} is the model's stop, taken as a literal that ends the run.
            request = RunRequest(
                inputs=inputs,
                emits=(),
                max_turns=1_000_000,
                accept=None,
                # the model has no tools of its own: its commands' results reach it next turn, and a turn
                # of plain text, the posture, ends the batch
                wait=self._wait,
                fold_inputs=True,  # a day of mail and replies folds too; the latest batch stays
                history=history,
                trigger=trigger,
                continues=last,
            )

            def stop() -> bool:  # this run, by its request: the actor's other runs are not asked
                run = next((r for r in reversed(self.actor.runs.values()) if r.request is request), None)
                return run is not None and self._stop(run)

        else:  # one run per wake, and `--once` or a rerun on a chair is one wake too
            request = RunRequest(
                inputs=inputs,
                emits=(Posture,),
                max_turns=self.max_turns,
                accept=self.accept,
                history=history,
                trigger=trigger,
                continues=last,
            )
            stop = None
        try:
            run = await self.actor.run(profile, request, self.backend, stop=stop)
        except Exception as e:
            b = self._burst
            if b.get("booked"):
                # the run died at a quiet point, inside `_wait`: its last burst is booked, and what
                # was being taken sits under `taking` for `_recover` to report at the next wake
                raise
            self._spend(mail=len(b["mail"]) + len(b["notices"]), **self._usage(b["before"]))
            outcome = f"error {type(e).__name__}: {e}"
            mail_ids, notice_ids = [m.get("id") for m in b["mail"]], [m.get("id") for m in b["notices"]]
            self._owe(
                [{"why": f"wake {self.wakes} failed: {outcome}", "mail": mail_ids, "notices": notice_ids}]
            )
            self.state.put("cursor", {**self._burst_facts(None, outcome, profile), **kept}, run="-", seq=0)
            await self._tell_owed()
            raise
        self.runs.append(run)
        seq = run.record.seq
        b = self._burst
        if not b.get("booked"):  # a burst `_wait` booked at its quiet point is not booked twice
            self._spend(mail=len(b["mail"]) + len(b["notices"]), **self._usage(b["before"]))
            facts = self._burst_facts(run, type(run.outcome).__name__, profile)
            self.state.put("cursor", facts, run=run.id, seq=seq)
        # a continuous run declares no output: its OUT{} is the model's stop, the literal what it wrote
        stopped = stop is not None and isinstance(run.outcome, Success)
        out = run.output if isinstance(run.outcome, Success) else {}
        posture = out.get("posture", out if stopped else None)
        if isinstance(posture, dict) and "done" in posture:
            posture = Posture(done=str(posture["done"]), awaiting=str(posture.get("awaiting") or ""))
        if isinstance(posture, Posture):
            line = posture.done + (f" Awaiting: {posture.awaiting}" if posture.awaiting else "")
            notes.put("posture", line, run=run.id, seq=seq)
        elif stopped:  # an earlier posture never stands for the stop's
            try:
                line = posture if isinstance(posture, str) else json.dumps(posture, default=str)
            except (TypeError, ValueError):  # a literal JSON cannot hold, a tuple key: as Python writes it
                line = repr(posture)
            notes.put("posture", line, run=run.id, seq=seq)
        return run

    def _burst_facts(self, run: Run | None, outcome: str, profile: Profile, done: int | None = None) -> dict:
        """The cursor for the running burst: its own mail, turns and sends, not the run's whole.
        `done` = turns completed so far when the run is still going (at the wait point `run.turn`
        is the turn about to start); an ended run's `run.turn` is its last."""
        b = self._burst
        facts = self._facts(run, b["mail"], outcome, profile, b["before"], notices=b["notices"])
        if run is not None:
            facts["turns"] = (run.turn if done is None else done) - b["turn0"]
            facts["sent"] = facts["sent"] - b["sent0"]
            if "tokens" in facts:  # sessions the backend started over from a folded view this burst
                facts["tokens"]["restarts"] = int(getattr(self.backend, "restarts", 0) or 0) - b.get(
                    "restarts0", 0
                )
        return facts

    def _stop(self, run: Run) -> bool:
        """Asked each turn of a continuous run: a burst past max_turns, or the day's cost at cap with
        this burst's calls counted, ends the run; serve() starts a new one on the next mail, which
        continues it."""
        b = self._burst
        spend = _value(self.state, "spend", {})
        today, _ = _day_spend(spend, _day(self.clock()))
        return run.turn - b["turn0"] > self.max_turns or (
            today + self._usage(b["before"])["cost"] >= self.budget.cost_per_day
        )

    async def _wait(self, run: Run) -> bool:
        """The chair's quiet point: the model answered with no commands, so this burst of mail is done.
        The burst is booked as one wake (spend, cursor, posture, the log line); then the box is polled,
        under the same caps as `wake`, until mail arrives, which is marked read, admitted and queued
        into this same run. False only when serve() says stop."""
        if not self._serving:
            return False
        b = self._burst
        done = run.turn - 1  # the wait comes at the start of a turn; the burst ended with the last
        self._spend(mail=len(b["mail"]) + len(b["notices"]), **self._usage(b["before"]))
        facts = self._burst_facts(run, "Quiet", run.profile, done=done)
        self.state.put("cursor", facts, run=run.id, seq=run.record.seq)
        notes = self.actor.notes_for(run.profile)
        last = run.record.latest(Kind.TEXT)
        # what the model said beside its commands, written even when it said nothing: an earlier burst's
        # posture never stands for this one
        posture = plain(str(last.content)) if last is not None else ""
        notes.put("posture", posture, run=run.id, seq=run.record.seq)
        b["booked"] = True
        self._say(_line(facts))
        was_held: str | None = None
        while True:
            if self._until is not None and self._until():
                return False
            self._reload()
            self.held = self.hold()
            if self.held and self.held != was_held:
                self._say(f"held: {self.held}")
            was_held = self.held
            if self.held:
                await asyncio.sleep(self.poll_ms / 1000)
                continue
            started = time.monotonic()
            try:  # the two boxes side by side, as `wake` polls them
                own = self.khive.inbox(wait_ms=self.poll_ms, limit=self.batch)
                if self.watch:
                    mail, notices = await asyncio.gather(
                        own, self._watched(self.poll_ms), return_exceptions=True
                    )
                    for polled in (mail, notices):
                        if isinstance(polled, BaseException):
                            raise polled
                else:
                    mail, notices = await own, []
            except Exception as e:
                self.errors.append(f"{type(e).__name__}: {e}")
                self._say(f"wake {self.wakes} failed: {self.errors[-1]}")
                mail, notices = [], []
            now = self.clock()
            notices = list(notices) + await self._tasks(now)
            if not mail and not notices and not self._due():
                notices = await self._hand_wake(now) or self._idle_task(now)
            if not mail and not notices and not self._due():
                self.idle += 1
                # a box that answers before the wait is up (a fake, a fast refusal) must not spin
                await asyncio.sleep(max(0.0, self.poll_ms / 1000 - (time.monotonic() - started)))
                continue
            try:
                await self._recover()  # a take that died last time is owed before this one writes
                self._dropped = []
                if mail:
                    self.state.put("taking", {"mail": [m.get("id") for m in mail]}, run="-", seq=0)
                    ids = [m.get("full_id") or m["id"] for m in mail]
                    await self.khive.exec(f"comm.read(ids={lit(ids)})")
                if self.admit is not None:
                    verdicts = [(m, self.admit(m)) for m in mail]  # asked once per message, as on `wake`
                    self._dropped = [{"id": m.get("id"), "from": _sender(m)} for m, ok in verdicts if not ok]
                    mail = [m for m, ok in verdicts if ok]
                mail = self._triage(mail, woken=bool(notices))
                self.state.put(
                    "taking", {"mail": [m.get("id") for m in mail]} if mail else {}, run="-", seq=0
                )
            except Exception as e:
                # the run outlives a failed take: the ids stay under `taking` for `_recover`; mail the
                # mark-read never reached is unread still, and the next poll brings it back
                self.errors.append(f"{type(e).__name__}: {e}")
                self._say(f"wake {self.wakes} failed: {self.errors[-1]}")
                await asyncio.sleep(self.poll_ms / 1000)
                continue
            if not mail and not notices:
                prior = _value(self.state, "cursor", {})
                dropped = {**prior, "dropped": self._dropped, "dropped_at": _stamp(self.clock())}
                self.state.put("cursor", dropped, run="-", seq=0)
                self.state.put("taking", {}, run="-", seq=0)
                self._dropped = []
                continue
            want = self.profile_for(mail + notices)
            if (want.name, want.specs, want.privileges, want.system) != (
                run.profile.name,
                run.profile.specs,
                run.profile.privileges,
                run.profile.system,
            ):
                # this batch runs under another profile, or its profile changed under the same name
                # (fewer specs, other privileges): the run ends here and `wake` runs it fresh
                self._carry = (mail, notices)
                return False
            self.admitted = list(mail)
            self.wakes = int(_value(self.state, "cursor", {}).get("wake", 0)) + 1
            content = self._input(mail + notices, notes)  # reads the cursor of the burst just booked
            self._burst = {
                "mail": list(mail),
                "notices": notices,
                "before": len(getattr(self.backend, "calls", ())),
                "turn0": run.turn - 1,
                "sent0": self._delivered.get(run.id, 0),
                "restarts0": int(getattr(self.backend, "restarts", 0) or 0),
            }
            self._spend(wake=True)
            running = {**self._facts(None, mail, "running", run.profile, notices=notices), "run": run.id}
            self.state.put("cursor", running, run="-", seq=0)
            self.state.put("taking", {}, run="-", seq=0)
            run.inbox.append(
                Inbound(
                    handle=Handle(uuid.uuid4().hex[:8]),
                    content=content,
                    origin_actor="the inbox",
                    origin_run="-",
                    origin_alias=None,
                )
            )
            return True

    async def serve(
        self, *, until: Callable[[], bool] | None = None, log: Callable[[str], None] | None = None
    ) -> None:
        """Wake on mail until `until()` says stop. One process per identity; a failed wake is recorded
        and the loop waits one poll; a budget cap waits too, and the mail waits with it. `log` gets one
        line per wake, per failure, and per change of hold: a served agent is read through its log and
        its cursor, nothing else. An agent `unservable` names a reason for is refused before it claims."""
        why = self.unservable()
        if why is not None:
            raise AgentError(why)
        pid = self._claim()
        say = log or (lambda line: None)
        self._say, self._until, self._serving = say, until, True
        was_held: str | None = None
        try:
            await self._recover()  # a process restart is where a died wake is found
            while until is None or not until():
                try:
                    run = await self.wake()
                except Exception as e:
                    self.errors.append(f"{type(e).__name__}: {e}")
                    say(f"wake {self.wakes} failed: {self.errors[-1]}")
                    await asyncio.sleep(self.poll_ms / 1000)
                    continue
                if run is not None and not self._burst.get("booked"):  # `_wait` said its own bursts
                    say(_line(_value(self.state, "cursor", {})))
                if self.held and self.held != was_held:
                    say(f"held: {self.held}")
                was_held = self.held
                if run is None and self.held:
                    await asyncio.sleep(self.poll_ms / 1000)
        finally:
            self._serving, self._carry = False, None
            self._release(pid)

    def unservable(self) -> str | None:
        """Why this agent may not be served, or None. ADR-0026 C6, still owed, has each wake continue the
        last run and replay its chain whole, and no fold point bounds that chain yet, so a served agent's
        history would grow without bound: a store that routes the lion tables is refused until one
        exists. A test double routes nothing."""
        if self.actor.store.database is not None:
            return (
                f"{self.khive.actor} is not served: its directory routes the lion tables, and no fold "
                "point yet bounds the chain ADR-0026 C6 has each wake continue"
            )
        return None

    # bookkeeping

    async def _tell_owner(self, text: str) -> bool:
        """Whether the notice landed. No owner counts as told: there is nobody to tell."""
        if self.owner is None:
            return True
        try:
            await self.khive.send(self.owner, text, subject=f"{self.khive.actor}: unanswered batch")
        except Exception as e:  # the notice must never mask what it reports
            self.errors.append(f"owner notice failed: {type(e).__name__}: {e}")
            return False
        return True

    def _claim(self) -> Path | None:
        """Hold the identity's pid file for this process's lifetime: an exclusive lock on it, which
        the kernel drops when the process ends, so a stale file never holds and two starters never
        both pass (a read-then-write guard let two through). The pid inside is for people. A claim
        this Agent already holds is returned as it is: the CLI claims before its first act and `serve`
        keeps that lock."""
        cwd = getattr(self.khive, "cwd", None)
        if cwd is None:
            return None
        path = Path(cwd) / "agent.pid"
        if self._lock_fd is not None:
            return path
        while True:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                other = os.read(fd, 64).decode(errors="replace").strip() or "?"
                os.close(fd)
                raise AgentError(
                    f"{self.khive.actor} is held by live process {other} ({path}); one process per identity"
                ) from None
            # opened before a holder's release removed the file, the lock is on a file nobody else
            # will lock again while the path names a new one; locked on the path's own file it holds
            try:
                here = os.stat(path)
            except FileNotFoundError:
                here = None
            mine = os.fstat(fd)
            if here is not None and (here.st_dev, here.st_ino) == (mine.st_dev, mine.st_ino):
                break
            os.close(fd)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd
        return path

    def _release(self, path: Path | None) -> None:
        if self._lock_fd is None:
            return
        if path is not None and path.exists() and path.read_text().strip() == str(os.getpid()):
            path.unlink()  # before the lock goes: a starter that locks next gets a fresh file
        os.close(self._lock_fd)
        self._lock_fd = None

    def _usage(self, before: int) -> dict:
        """What the backend calls since `before` cost: their count, tokens in and out, dollars, and
        the calls the dollars miss (no figure, one the run would not sum (ADR-0003/S7), or the router's
        `cost_complete` false). Read off the envelopes the backend keeps (`calls`); a backend without
        them spent 0 of everything."""
        calls = [c for c in (getattr(self.backend, "calls", None) or [])[before:] if isinstance(c, dict)]
        tokens_in = tokens_out = 0
        for c in calls:
            u = c.get("usage") or {}
            tokens_in += int(
                u.get("prompt_tokens")
                or (
                    int(u.get("input_tokens") or 0)
                    + int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0)
                )
            )
            tokens_out += int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        cost, unknown = 0.0, 0
        for c in calls:
            # a malformed figure is no figure: never room against the day's cap (ADR-0013/C5)
            figure = math.nan if c.get("total_cost_usd") is None else reported_cost(c["total_cost_usd"])
            if not math.isnan(figure):
                cost = min(cost + figure, sys.float_info.max)  # past any cap, and a note takes no infinity
            if math.isnan(figure) or c.get("cost_complete") is False:
                unknown += 1
        # the context the last call's last turn stood in: what the next turn will pay for, and what
        # the fold decides on; the sum `in` above is the call's cost over all its turns
        context = _prompt_tokens(calls[-1]) if calls else None
        return {
            "calls": len(calls),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": cost,
            "unknown": unknown,
            "context": context,
        }

    def _spend(
        self,
        *,
        wake: bool = False,
        cost: float = 0.0,
        unknown: int = 0,
        calls: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        mail: int = 0,
        context: int | None = None,  # the last call's, read for the wake line; the day's row keeps none
    ) -> None:
        """The hour's wakes (the rate cap) and the day's spend: dollars, the calls those dollars miss
        (None when the day began before they were counted),
        model calls, tokens in and out, mail handled, wakes that ran, and polls that found nothing and
        cost no call. The day's row is the measurement behind "does a long-running agent save tokens":
        tokens per handled message beside idle polls at zero."""
        spend = dict(_value(self.state, "spend", {}))
        now = self.clock()
        wakes = [t for t in spend.get("wakes", []) if now - t < 3600]
        if wake:
            wakes.append(now)
            self._last_wake = now
        day = _day(now)
        same_day = spend.get("day") == day
        before, counted = _day_spend(spend, day)
        row = {
            "day": day,
            # a sum past a float's range is past any cap, and the note takes no infinity: the largest float
            "cost": min(before + cost, sys.float_info.max),
            "unknown": None if counted is None else counted + unknown,
            "wakes": wakes,
            "calls": (spend.get("calls", 0) if same_day else 0) + calls,
            "in": (spend.get("in", 0) if same_day else 0) + tokens_in,
            "out": (spend.get("out", 0) if same_day else 0) + tokens_out,
            "mail": (spend.get("mail", 0) if same_day else 0) + mail,
            "runs": (spend.get("runs", 0) if same_day else 0) + (1 if wake else 0),
            "idle": (spend.get("idle", 0) if same_day else 0) + (self.idle if wake else 0),
            "idle_last": self.idle if wake else spend.get("idle_last", 0),  # polls before this wake
        }
        if wake:
            self.idle = 0
        self.state.put("spend", row, run="-", seq=self.wakes)

    def _facts(
        self,
        run: Run | None,
        mail: list[dict],
        outcome: str,
        profile: Profile,
        before: int | None = None,
        notices: list[dict] = (),
    ) -> dict:
        sent = self._delivered.get(run.id, 0) if run is not None else 0
        facts = {
            "wake": self.wakes,
            "at": _stamp(self.clock()),
            "profile": profile.name,
            "run": run.id if run is not None else None,
            "outcome": outcome,
            "turns": run.turn if run is not None else 0,
            "mail": [m.get("id") for m in mail],
            "sent": sent,
        }
        if tasks := [m.get("id") for m in notices if m.get("task")]:
            facts["tasks"] = tasks  # the queue's, named apart: a task is not mail and is never rerun
        if notices:  # the watch's wake-ups: not mail of this agent's, never marked or rerun
            facts["notices"] = [m.get("id") for m in notices]
        if before is not None:
            u = self._usage(before)
            facts["tokens"] = {
                "calls": u["calls"],
                "in": u["tokens_in"],
                "out": u["tokens_out"],
                "cost": u["cost"],
                "unknown": u["unknown"],
                "context": u["context"],
            }
            facts["idle_polls"] = int(_value(self.state, "spend", {}).get("idle_last", 0))
        if self._dropped:
            facts["dropped"] = list(self._dropped)
        return facts

    def _input(self, mail: list[dict], notes: NoteStore) -> str:
        tasks = sum(1 for m in mail if m.get("task"))
        head = [
            f"{len(mail) - tasks} message(s) arrived since the last wake"
            + (f" and {tasks} task(s) of your queue are in front of you." if tasks else ".")
        ]
        cursor, posture = self.state.get("cursor"), notes.get("posture")
        if cursor is not None:
            head.append(f"Last wake: {cursor.value}")
        if posture is not None:
            head.append(f"Your posture then: {posture.value}")
        if self.continuous and self._serving:
            head.append(
                "Deal with the mail as your role says, with the commands: each one's result reaches you "
                "next turn. When the work is done, a turn of plain text, your posture, ends this batch, "
                f"and the next mail arrives in this same conversation. You have {self.max_turns} turns "
                "for this batch."
            )
        else:
            head.append("Deal with the mail as your role says, then finish with OUT{posture: ...}.")
        return "\n".join(head) + "\n\n" + "\n\n".join(_render(m) for m in mail)


def _line(c: dict) -> str:
    """The serve log's line for a wake, from its cursor."""
    t = c.get("tokens") or {}
    return (
        f"wake {c.get('wake')}: {c.get('outcome')} after {c.get('turns')} turns, "
        f"{len(c.get('mail', []))} mail, {c.get('sent')} sent; {t.get('calls', 0)} model "
        f"call(s), {t.get('in', 0)} in, {t.get('out', 0)} out, ${t.get('cost', 0.0):.4f}"
        + partial(t.get("unknown", 0))
        + f"; {c.get('idle_polls', 0)} idle poll(s) before it at 0 tokens"
        + (f"; last turn stood in {t['context']} tokens" if t.get("context") is not None else "")
        + (f", {t['restarts']} session restart(s)" if t.get("restarts") else "")
        + (f"; {len(c['tasks'])} task(s) of the queue" if c.get("tasks") else "")
    )


def _sender(m: dict) -> str:
    return (m.get("properties") or {}).get("from_actor") or m.get("from") or "?"


def _stamp(now: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))


def _render(m: dict) -> str:
    p = m.get("properties") or {}
    if m.get("task"):
        why = {
            "due": "due now",
            "hand": "a person asked for a wake; the top of your queue",
        }.get(m["task"], "the top of your queue; no mail, you are working it")
        due = f" at {m['at']}" if m.get("at") else ""
        head = (
            f"[task {m.get('id')} {m.get('priority')} {m.get('status')}{due}: {why}] {m.get('subject') or ''}"
        )
        return f"{head}\n{m.get('content') or ''}"
    sender = _sender(m)
    subject = m.get("subject") or ""
    waited = f" waited since {m['deferred_at']}" if m.get("deferred_at") else ""
    head = f"[mail {m.get('id')} from {sender} thread {p.get('thread_id')} subject {subject!r}{waited}]"
    return f"{head}\n{m.get('content') or ''}"


def _value(store: NoteStore, key: str, default: Any) -> Any:
    note = store.get(key)
    return note.value if note is not None and isinstance(note.value, type(default)) else default


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _day_spend(spend: dict, day: str) -> tuple[float, int | None]:
    """The day's row as the cap reads it: dollars spent and calls of unknown cost, (0, 0) on another
    day's row. A same-day row with no count is from before the count: its calls went uncounted, and
    no later wake can count them, so the day reads as partial to its end (None, never 0). A row's
    dollars the run would not sum (ADR-0003/S7), a negative sum an earlier version kept among them,
    read as none spent and the day as partial: never room below zero."""
    if spend.get("day") != day:
        return 0.0, 0
    cost = reported_cost(spend.get("cost", 0.0))
    if math.isnan(cost):
        return 0.0, None
    return cost, spend.get("unknown")
