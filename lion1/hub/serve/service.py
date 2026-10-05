"""The served actor's service (ADR-0021): one actor's offers and the runs it started.

An offer is a profile, a backend and a request template the owner lets a client run. A client starts a
run of an offer with data only, reads it as rows and bus events, speaks to it and ends it; the run is
`Actor.run`'s own. A row is a record entry in the chat log's form, appended to the run's log before any
subscriber has it. The service knows no HTTP; `hub.serve.transport` carries it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import math
import os
import re
import secrets
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic_core import to_jsonable_python

from lion1.actor import _ACTORS, Actor, Backend, Handle, Inbound, Profile, Run, RunRequest, Success
from lion1.checkpoint import entry_row, load_log
from lion1.spec import spec_name

__all__ = (
    "Code",
    "Phase",
    "ServeError",
    "Offer",
    "Service",
)

_PROTOCOL = 1  # the card's protocol version: a change that breaks a client raises it (ADR-0021/D9)
_TEMPLATE = RunRequest()
_ID = re.compile(r"[0-9a-f]{16}")
_ONCE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_FIELDS = ("offer", "inputs", "bounds", "resume", "once")
_COUNTS = ("max_turns", "max_idle", "max_refusals", "context_budget")
_AMOUNTS = ("time_budget", "cost_budget")
# where a served run is (ADR-0021/D9): starting until `run.started`, then working or waiting, and last
# ended or aborted; cut_short for a run whose log stops before its END row
Phase = Literal["starting", "working", "waiting", "ended", "aborted", "cut_short"]
_LIVE: tuple[Phase, ...] = ("starting", "working", "waiting")
# a refusal's code, ADR-0021/D9's table by status; the transport answers each with its own
Code = Literal[
    "bad_request",
    "forbidden",
    "unknown_offer",
    "unknown_run",
    "not_found",
    "method_not_allowed",
    "timeout",
    "ended",
    "starting",
    "not_conversation",
    "no_resume",
    "unreadable_log",
    "conflict",
    "gone",
    "too_large",
    "unsupported_type",
    "refused",
    "too_many",
    "log_failed",
    "busy",
]
_CODES = get_args(Code)
_MIB = 1 << 20  # an input, a body
_INPUTS = 16  # inputs one start carries
_UNTAKEN = 64  # inputs a run holds not yet taken
_KEYS = 100  # `once` keys held per operation
_GONE = 10_000  # ids of dropped runs remembered, the oldest forgotten first
_FRAMES = 1_000  # frames queued for one subscriber
_PAGE = 500  # rows one record read returns
_SUBSCRIBERS = 32
_DEPTH = 32  # how deep a value pydantic cannot project as a whole is projected part by part
_COMMENT = (None, None)  # a stream's keep-alive, written as a comment line


class ServeError(Exception):
    """A refusal a client reads: a code of ADR-0021/D9's table and a sentence saying why."""

    def __init__(self, code: Code, reason: str):
        if code not in _CODES:
            raise ValueError(f"serve error code {code!r} is not one of {', '.join(_CODES)}")
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Offer:
    """What the owner lets a client run, named by its profile's name (ADR-0021/D1). A served run starts
    from an offer and from nothing else."""

    profile: Profile
    backend: Backend
    request: RunRequest
    converse: bool = False

    @property
    def name(self) -> str:
        return self.profile.name


class _Sub:
    """One stream's frames, bounded in count: one that would overflow is closed and never waited for
    (ADR-0021/C3). Its owners are the sets that hold it while it is open."""

    def __init__(self, *owners: set[_Sub]):
        self._owners = owners
        for owner in owners:
            owner.add(self)
        self._frames: deque[tuple[str | None, dict | None]] = deque()
        self._open = True
        self._ready = asyncio.Event()

    def put(self, frame: tuple[str, dict]) -> None:
        """Only an open stream is put to: closing removes it from the sets its frames are sent through."""
        if len(self._frames) >= _FRAMES:
            self.close()
            return
        self._frames.append(frame)
        self._ready.set()

    def ping(self) -> None:
        if self._open and len(self._frames) < _FRAMES:
            self._frames.append(_COMMENT)
            self._ready.set()

    def close(self) -> None:
        """No frame after this one; what is queued is still read."""
        self._open = False
        for owner in self._owners:
            owner.discard(self)
        self._ready.set()

    def __aiter__(self) -> _Sub:
        return self

    async def __anext__(self) -> tuple[str | None, dict | None]:
        while not self._frames:
            if not self._open:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()
        return self._frames.popleft()


class _Served:
    """A run the service started, or one its log describes."""

    def __init__(self, sid: str, offer: str, resumed: str | None, converse: bool = False):
        self.id = sid
        self.offer = offer
        self.resumed = resumed
        self.converse = converse
        self.phase: Phase = "starting"
        self.run: Run | None = None
        self.task: asyncio.Task | None = None
        self.begun: asyncio.Future | None = None  # None once it began or was cancelled; a refusal else
        self.subs: set[_Sub] = set()
        self.rows: list[dict] | None = []  # the rows published; None for a run read from its log
        self.path: Path | None = None
        self.log: Any = None  # the open log, until its END row or its latch
        self.latched = False
        self.error: str | None = None
        self.closed = False  # `end` was accepted: input is refused, and the next wait answers False
        self.stopped = False  # what the run's `stop` callback reads
        self.wake = asyncio.Event()
        self.inputs: OrderedDict[str, tuple[str, str]] = OrderedDict()  # once key -> (text, handle)
        self.final: dict | None = None  # the state once the run ended, or as its log has it


class Service:
    """One actor's offers and the runs it started (ADR-0021/D1). With `dir`, each run's log is
    `<dir>/<id>.jsonl`; `max_live` counts runs not ended, `keep` the ended runs held in memory."""

    def __init__(self, actor: Actor, *, dir: str | Path | None = None, max_live: int = 8, keep: int = 100):
        for name, count, least in (("max_live", max_live, 1), ("keep", keep, 0)):
            if type(count) is not int or count < least:
                raise ValueError(f"{name} is a count of at least {least}, got {count!r}")
        self.actor = actor
        self.dir = Path(dir) if dir is not None else None
        self.max_live = max_live
        self.keep = keep
        self.offers: dict[str, Offer] = {}
        self.runs: dict[str, _Served] = {}  # live and kept, in the order they were held
        self._ended: OrderedDict[str, None] = OrderedDict()  # the kept runs, the first to end first
        # the last `_GONE` ids no longer held, each with whether its log failed while it was: read from
        # the log, or gone; one forgotten reads as its log has it, or as an unknown run
        self._dropped: OrderedDict[str, bool] = OrderedDict()
        self._starts: OrderedDict[str, tuple[dict, _Served]] = OrderedDict()  # once key -> first start
        self._by_run: dict[str, _Served] = {}  # live runs by the runtime's id, which routes events (S11)
        self._all: set[_Sub] = set()  # the streams of every run
        self._subs: set[_Sub] = set()  # every open stream, against the cap
        self._off: Callable[[], None] | None = None  # the bus subscription, while the service holds one
        self._closing = False
        if self.dir is not None:
            self._list()

    # offers

    def offer(
        self, profile: Profile, backend: Backend, request: RunRequest = _TEMPLATE, *, converse: bool = False
    ) -> Offer:
        """Offer `profile` on `backend` with `request` as the template. The profile is copied with its own
        `defaults` and the template is copied; the stores a profile names and a request's callbacks stay
        the caller's, live. `converse` gives each run the service's `wait`."""
        if request.wait is not None:
            raise ValueError(
                f"offer {profile.name!r}: the template sets wait; offer a conversation with converse"
            )
        if request.reply_on_commands:
            raise ValueError(f"offer {profile.name!r}: the template sets reply_on_commands")
        if profile.name in self.offers:
            raise ValueError(f"offer {profile.name!r} is already offered")
        self.actor.check(profile)
        offer = Offer(
            dataclasses.replace(profile, defaults=dict(profile.defaults)),
            backend,
            dataclasses.replace(request),
            converse,
        )
        self._card(offer)  # a command no JSON schema describes fails the offer, never a client's read
        self.offers[offer.name] = offer
        return offer

    def card(self) -> dict:
        """What a client is told (ADR-0021/C13): each offer's commands as the model is told them, never
        the role prompt."""
        return _jsonable(
            {
                "actor": self.actor.name,
                "protocol": _PROTOCOL,
                "offers": [self._card(o) for o in self.offers.values()],
            }
        )

    def _card(self, offer: Offer) -> dict:
        subset = self.actor.subset(offer.profile)
        commands = []
        for name in subset:
            spec, handler = subset.get(name), self.actor.handlers[name]
            doc = (spec.__doc__ or "").strip().splitlines()  # the line the model reads (`render_guidance`)
            commands.append(
                {
                    "name": name,
                    "doc": doc[0].strip() if doc else None,
                    "schema": spec.model_json_schema(),
                    "effect": handler.effect,
                    "privileges": sorted(handler.requires),
                }
            )
        return {
            "name": offer.name,
            "language": offer.profile.language,
            "commands": commands,
            "outputs": [{"name": spec_name(s), "schema": s.model_json_schema()} for s in offer.request.emits],
            "bounds": self._bounds(offer),
            "converse": offer.converse,
        }

    def _bounds(self, offer: Offer) -> dict[str, float | None]:
        """Each bound as a run of `offer` resolves it, by the run's own rule."""
        return dict(Run(self.actor, offer.profile, offer.request).bounds)

    # starting

    async def start(self, fields: dict[str, Any]) -> tuple[dict, bool]:
        """A run of an offer, from data only (ADR-0021/D2): its state once it began, and True; for a
        repeated `once` key, the first run's state and False. A refusal raises `ServeError`."""
        if self._closing:
            raise ServeError("busy", "the service is ending")
        name, inputs, bounds, resume, once = _start_fields(fields)
        if once is not None and once in self._starts:
            first, held = self._starts[once]
            if first != fields:
                raise ServeError("conflict", f"the once key {once!r} was given with other fields")
            refusal = await asyncio.shield(held.begun)
            if refusal is not None:
                raise refusal
            try:
                return self.state(held.id), False
            except ServeError as e:
                if e.code != "unknown_run":
                    raise
                # the key outlived the memory of its run's id (C14): a repeat is gone, never unknown
                raise ServeError("gone", f"run {held.id} is no longer held") from None
        offer = self.offers.get(name)
        if offer is None:
            raise ServeError("unknown_offer", f"no offer is named {name!r}")
        # a client's input names `client`, and a finished run answers that actor (S19)
        if "client" in _ACTORS:
            raise ServeError(
                "refused", "an actor named 'client' is registered; a client's input would answer it"
            )
        resolved = self._bounds(offer)
        for bound, value in bounds.items():
            if resolved[bound] is not None and value > resolved[bound]:
                raise ServeError("bad_request", f"{bound} {value} is above the offer's {resolved[bound]}")
        history = offer.request.history if resume is None else self._history(resume)
        if sum(s.phase in _LIVE for s in self.runs.values()) >= self.max_live:
            raise ServeError("too_many", f"{self.max_live} runs are live, the most this service holds")
        s = _Served(self._draw(), offer.name, resume, offer.converse)
        s.log = self._create(s, len(history))
        request = dataclasses.replace(
            offer.request,
            inputs=inputs,
            history=history,
            wait=functools.partial(self._wait, s) if offer.converse else None,
            **bounds,
        )
        loop = asyncio.get_running_loop()
        s.begun = loop.create_future()
        if self._off is None:
            self._off = self.actor.bus.subscribe("*", self._observe)
        self.runs[s.id] = s
        if once is not None:
            self._starts[once] = (dict(fields), s)
            if len(self._starts) > _KEYS:
                self._starts.popitem(last=False)
        # the log and the task in one step: from here the run is live
        s.task = loop.create_task(self._drive(s, offer, request))
        s.task.add_done_callback(functools.partial(self._finish, s))
        refusal = await asyncio.shield(s.begun)
        if refusal is not None:
            raise refusal
        return self.state(s.id), True

    def _history(self, resume: str) -> tuple:
        """The entries of a stored run's log, replayed as the new run's `history` (ADR-0021/C7)."""
        if self.dir is None:
            raise ServeError("no_resume", "this service has no directory, so it keeps no log to resume")
        held = self.runs.get(resume)
        if held is not None and held.phase in _LIVE:
            raise ServeError("no_resume", f"run {resume} is live")
        path = self.dir / f"{resume}.jsonl"
        if _ends(path) is None:
            raise ServeError("no_resume", f"no log of a run {resume} is stored here")
        try:
            return load_log(path, torn=[])
        except Exception as e:  # whatever the loader refuses, read or row
            raise ServeError(
                "unreadable_log", f"the log of {resume} cannot be read: {type(e).__name__}: {e}"
            ) from None

    def _draw(self) -> str:
        """A 16-hex id no live run, kept run, dropped run, log file or kept `once` answer has."""
        while True:
            sid = secrets.token_hex(8)
            taken = (
                sid in self.runs
                or sid in self._dropped
                or any(held.id == sid for _, held in self._starts.values())
                or (self.dir is not None and os.path.lexists(self.dir / f"{sid}.jsonl"))
            )
            if not taken:
                return sid

    def _create(self, s: _Served, replayed: int) -> Any:
        """The run's log, created exclusively with its SERVE row; None without a directory."""
        if self.dir is None:
            return None
        s.path = self.dir / f"{s.id}.jsonl"
        try:
            log = _opened(s.path)
        except OSError as e:
            raise ServeError("log_failed", f"the log {s.path.name} could not be created: {e}") from None
        row = {
            "kind": "SERVE",
            "version": 1,
            "id": s.id,
            "offer": s.offer,
            "resumed": s.resumed,
            "replayed": replayed,
        }
        try:
            _append(log, row)
        except Exception as e:
            with contextlib.suppress(OSError):
                log.close()
                s.path.unlink()
            raise ServeError("log_failed", f"the log {s.path.name} could not be written: {e}") from None
        return log

    async def _drive(self, s: _Served, offer: Offer, request: RunRequest) -> None:
        """`Actor.run`, its `run.started` read by a subscription made in the same step as the call: the
        loop emits it before its first await, so the first one is this run's (A1)."""

        def started(event: Any) -> None:
            off()
            self._began(s, self.actor.runs[event.payload["run"]], event)

        off = self.actor.bus.subscribe("run.started", started)
        try:
            await self.actor.run(offer.profile, request, offer.backend, stop=lambda: s.stopped)
        finally:
            off()

    def _began(self, s: _Served, run: Run, event: Any) -> None:
        s.run, s.phase = run, "working"
        self._by_run[run.id] = s
        self._publish(s)
        self._send(s, ("state", self._state(s)))
        self._send(s, ("bus", _bus(s, event)))
        s.begun.set_result(None)

    def _observe(self, event: Any) -> None:
        """Every bus event of a served run, forwarded as emitted; rows are published at the events
        where an entry is complete (A5) and at no other."""
        s = self._by_run.get(event.payload.get("run"))
        if s is None:
            return
        if event.kind in ("turn.started", "run.completed") or event.kind.startswith("command."):
            self._publish(s)
        self._send(s, ("bus", _bus(s, event)))

    def _publish(self, s: _Served) -> None:
        """The record's rows past the cursor (ADR-0021/D3): each appended and flushed, the cursor past
        its whole line, then handed to the streams. A write that raises latches the log."""
        if s.latched:
            return
        start, done = len(s.rows), []
        for seq in range(start, len(s.run.record)):
            row = _jsonable(entry_row(s.run.record.by_seq(seq)))
            if s.log is not None:
                try:
                    _append(s.log, row)
                except Exception as e:
                    self._latch(s, e)
                    break
            s.rows.append(row)
            done.append(row)
        if done:
            self._send(s, ("rows", {"id": s.id, "start": start, "rows": done, "more": False}))

    def _latch(self, s: _Served, error: Exception) -> None:
        """The log takes nothing more: a row or an END after the failed one would join or follow its
        torn bytes, and neither would be read back (ADR-0021/C8). The run is stopped."""
        s.latched = True
        s.error = f"the log failed: {type(error).__name__}: {error}"
        with contextlib.suppress(Exception):
            s.log.close()
        s.log = None
        self._halt(s)

    def _halt(self, s: _Served) -> None:
        s.stopped = True
        s.wake.set()
        if s.run is not None:
            s.run.interrupt()

    def _send(self, s: _Served, frame: tuple[str, dict]) -> None:
        for sub in [*s.subs, *self._all]:
            sub.put(frame)

    async def _wait(self, s: _Served, run: Run) -> bool:
        """A conversation's wait (ADR-0021/D5): False once it is ended or stopped, read first; True when
        its inbox holds input; else asleep on the wake event. A wake left from a `say` the last turn
        already took is cleared before the checks, so it finds nothing and the run sleeps."""
        self._publish(s)
        s.wake.clear()
        if not (s.closed or s.stopped or run.inbox):
            s.phase = "waiting"
            self._send(s, ("state", self._state(s)))
            self.actor.bus.emit("run.waiting", run=run.id)
            try:
                while not (s.closed or s.stopped or run.inbox):
                    await s.wake.wait()
                    s.wake.clear()
            finally:
                s.phase = "working"
                self._send(s, ("state", self._state(s)))
        return not (s.closed or s.stopped)

    def _finish(self, s: _Served, task: asyncio.Task) -> None:
        """The task's end, however it ended; a done callback, since a task cancelled before its first
        step runs no `finally`. The last rows, the END row and the final state, then the close."""
        error = None if task.cancelled() else task.exception()
        if s.run is None and error is not None:
            self._refused(s, error)
            return
        if s.run is not None:
            self._by_run.pop(s.run.id, None)
            self._publish(s)
        if s.run is not None and s.run.outcome is not None:  # the terminal commit was made
            s.phase = "ended"
        else:
            s.phase = "aborted"
            if error is not None and s.error is None:
                s.error = f"{type(error).__name__}: {error}"
        final = self._state(s)
        if s.log is not None:
            try:
                _append(s.log, {"kind": "END", "state": final})
            except Exception as e:
                self._latch(s, e)
                final = self._state(s)
            else:
                s.log.close()
                s.log = None
        s.final = final
        self._send(s, ("state", final))
        for sub in list(s.subs):
            sub.close()
        self._ended[s.id] = None
        while len(self._ended) > self.keep:
            sid, _ = self._ended.popitem(last=False)
            self._dropped[sid] = self.runs.pop(sid).latched
            if len(self._dropped) > _GONE:
                self._dropped.popitem(last=False)
        if not s.begun.done():  # cancelled before it began: the start answers with the aborted run
            s.begun.set_result(None)

    def _refused(self, s: _Served, error: BaseException) -> None:
        """`Actor.run` refused before `run.started`: no run, so no log and no held key (ADR-0021/C2). On a
        resume that is the history not replaying."""
        del self.runs[s.id]
        for key, (_, held) in list(self._starts.items()):
            if held is s:
                del self._starts[key]
        why = f"{type(error).__name__}: {error}"
        if s.resumed is None:
            s.begun.set_result(ServeError("refused", why))
        else:
            s.begun.set_result(
                ServeError("unreadable_log", f"the history of {s.resumed} does not replay: {why}")
            )
        if s.log is not None:
            with contextlib.suppress(OSError):
                s.log.close()
                s.path.unlink()

    # reading

    def state(self, id: str) -> dict:
        """The run's state (ADR-0021/D9): a held run's, else as its log has it."""
        return self._state(self._find(id))

    def runs_state(self) -> dict:
        """The states of the live and kept runs, in the order they were held."""
        return {"runs": [self._state(s) for s in self.runs.values()]}

    def record(self, id: str, start: int = 0) -> dict:
        """At most 500 rows from sequence `start`, and whether more follow (ADR-0021/D4): the rows
        confirmed while the service holds the run, else the whole rows of its log."""
        s = self._find(id)
        rows = s.rows if s.rows is not None else _log_rows(s.path)
        if type(start) is not int or not 0 <= start <= len(rows):
            raise ServeError("bad_request", f"start {start!r} is outside 0 to {len(rows)}")
        page = rows[start : start + _PAGE]
        return {"id": s.id, "start": start, "rows": page, "more": start + len(page) < len(rows)}

    def events(self, id: str | None = None) -> _Sub:
        """A stream (ADR-0021/D4, D12): of one run, opening with its state, or of every run, opening
        with the `runs` frame. Registered in the step that takes its first frame, so no later row
        misses it; for a run that ended the state is the whole stream."""
        if id is None:
            sub = self._subscribe(self._all)
            sub.put(("runs", self.runs_state()))
            return sub
        s = self._find(id)
        if s.phase not in _LIVE:
            sub = _Sub()
            sub.put(("state", self._state(s)))
            sub.close()
            return sub
        sub = self._subscribe(s.subs)
        sub.put(("state", self._state(s)))
        return sub

    def _subscribe(self, owner: set[_Sub]) -> _Sub:
        if len(self._subs) >= _SUBSCRIBERS:
            raise ServeError("busy", f"{_SUBSCRIBERS} streams are open, the most this service holds")
        return _Sub(owner, self._subs)

    def _find(self, sid: str) -> _Served:
        held = self.runs.get(sid)
        if held is not None:
            return held
        if not _ID.fullmatch(sid):  # never a path: only an id reaches the directory
            raise ServeError("unknown_run", f"no run {sid!r}")
        if self._dropped.get(sid):
            raise ServeError("gone", f"run {sid} is no longer held, and its log failed while it was")
        if self.dir is not None:
            ends = _ends(self.dir / f"{sid}.jsonl")
            if ends is not None:
                return _logged(self.dir / f"{sid}.jsonl", *ends)
        if sid in self._dropped:
            raise ServeError("gone", f"run {sid} is no longer held, and no log of it can be read")
        raise ServeError("unknown_run", f"no run {sid!r}")

    def _state(self, s: _Served) -> dict:
        if s.final is not None:
            return dict(s.final)
        run = s.run
        ended = s.phase == "ended"
        return _jsonable(
            {
                "id": s.id,
                "offer": s.offer,
                "resumed": s.resumed,
                "run": run.id if run is not None else None,
                "phase": s.phase,
                "turn": run.turn if run is not None else None,
                "cost": run.cost if run is not None else None,
                "rows": len(s.rows),
                "pending": [i.handle.id for i in run.inbox] if run is not None else [],
                "closed": s.closed,
                "outcome": _outcome(run.outcome) if ended else None,
                "output": run.output if ended and isinstance(run.outcome, Success) else None,
                "error": s.error,
            }
        )

    def _list(self) -> None:
        """After a restart: the logs in the directory, held as ended or cut short, the newest `keep`
        by modification time (ADR-0021/C7). A file with no whole SERVE row is not listed."""
        found = []
        for path in self.dir.glob("*.jsonl"):
            if not _ID.fullmatch(path.stem):
                continue
            try:
                ends, mtime = _ends(path), path.stat().st_mtime
            except OSError:
                continue
            if ends is not None:
                found.append((mtime, path.stem, path, ends))
        found.sort(key=lambda f: f[:2])
        for _, sid, path, ends in found[max(0, len(found) - self.keep) :]:
            self.runs[sid] = _logged(path, *ends)
            self._ended[sid] = None

    # speaking to a run

    def say(self, id: str, text: str, once: str | None = None) -> str:
        """Queue `text` on the run `id` names, waking it if it waits (ADR-0021/D5); the handle. The run
        takes it at its next turn as INPUT from `client`."""
        if type(text) is not str or not text:
            raise ServeError("bad_request", "text is a string of at least one character")
        if _utf8(text, "text") > _MIB:
            raise ServeError("too_large", f"text is past {_MIB} bytes in UTF-8")
        if once is not None and (type(once) is not str or not _ONCE.fullmatch(once)):
            raise ServeError("bad_request", "once is 1 to 64 letters, digits, '.', '_' or '-'")
        s = self._find(id)
        if once is not None and once in s.inputs:  # a repeat is answered before the phase check
            first, handle = s.inputs[once]
            if first != text:
                raise ServeError("conflict", f"the once key {once!r} was given with other text")
            return handle
        self._live(s, "input")
        if s.closed:
            raise ServeError("ended", f"run {s.id} was ended; input is refused")
        if len(s.run.inbox) >= _UNTAKEN:
            raise ServeError("too_many", f"run {s.id} holds {_UNTAKEN} inputs not yet taken")
        handle = uuid.uuid4().hex[:16]
        s.run.inbox.append(
            Inbound(
                handle=Handle(handle), content=text, origin_actor="client", origin_run=s.id, origin_alias=None
            )
        )
        s.wake.set()
        if once is not None:
            s.inputs[once] = (text, handle)
            if len(s.inputs) > _KEYS:
                s.inputs.popitem(last=False)
        return handle

    def end(self, id: str) -> dict:
        """Close a conversation (ADR-0021/C15): later input is refused, a waiting run wakes, and the run
        ends Stopped at its next wait unless something ended it first."""
        s = self._find(id)
        self._live(s, "end")
        if not s.converse:
            raise ServeError("not_conversation", f"run {s.id} is no conversation; it has nothing to end")
        if not s.closed:
            s.closed = True
            s.wake.set()
            self._send(s, ("state", self._state(s)))
        return self._state(s)

    def interrupt(self, id: str) -> dict:
        """`Run.interrupt` (ADR-0021/D6): `cut` says whether a pending call was found and its
        cancellation requested, which a backend may absorb."""
        s = self._find(id)
        self._live(s, "an interrupt")
        cut = s.run.interrupt()
        return {**self._state(s), "cut": cut}

    def stop(self, id: str) -> dict:
        """The flag the run's `stop` reads and the wake event, then `Run.interrupt`: the run ends Stopped
        at its next turn unless it ended first (ADR-0021/C6)."""
        s = self._find(id)
        self._live(s, "a stop")
        self._halt(s)
        return self._state(s)

    async def cancel(self, id: str) -> dict:
        """Cancel the run's task and await it (ADR-0021/D6): an abort with no outcome; on a starting run,
        the reserved task."""
        s = self._find(id)
        if s.phase not in _LIVE:
            raise ServeError("ended", f"run {s.id} is {s.phase}; a cancel is refused")
        s.task.cancel()
        await asyncio.wait({s.task})
        return self._state(s)

    def _live(self, s: _Served, what: str) -> None:
        if s.phase == "starting":
            raise ServeError("starting", f"run {s.id} is still starting; {what} is refused until it began")
        if s.phase not in _LIVE:
            raise ServeError("ended", f"run {s.id} is {s.phase}; {what} is refused")

    # ending

    @property
    def closing(self) -> bool:
        """Whether the service began ending: a start is refused busy, and so is a request."""
        return self._closing

    def halt(self) -> None:
        """Begin the end (ADR-0021/D10): no start is admitted from here, every open stream closes with
        what is queued still read, and then every live run is stopped, so no stream takes a run's end.
        The transport calls it before it closes its connections; a second call changes nothing."""
        if self._closing:
            return
        self._closing = True
        for sub in list(self._subs):
            sub.close()
        for s in self.runs.values():
            if s.phase in _LIVE:
                self._halt(s)

    async def close(self, grace: float = 10.0) -> None:
        """End what the service started (ADR-0021/C11): `halt`, then every live run awaited under
        `grace`, what remains cancelled and awaited, the bus subscription last. A run's END row is
        written as its task ends."""
        self.halt()
        live = [s for s in self.runs.values() if s.phase in _LIVE]
        if live:
            _, late = await asyncio.wait([s.task for s in live], timeout=grace)
            for task in late:
                task.cancel()
            await asyncio.gather(*late, return_exceptions=True)
        if self._off is not None:
            self._off()
            self._off = None


# the log


def _opened(path: Path) -> Any:
    """A log file created exclusively and written unbuffered: a write lands whole or short, and
    nothing is left in a buffer to land after the latch."""
    return open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb", buffering=0)


def _append(log: Any, row: dict) -> None:
    data = (json.dumps(row) + "\n").encode()
    written = log.write(data)
    if written != len(data):
        raise OSError(f"a short write: {written} of {len(data)} bytes")
    log.flush()


def _line(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode())
    except ValueError:  # UnicodeDecodeError is a ValueError: a cut write
        return None


def _ends(path: Path) -> tuple[dict, dict | None] | None:
    """A log's SERVE row and its END row's state, None for the state when its last line is no whole
    END row; None when its first line is no whole SERVE row, or the file cannot be read."""
    try:
        lines = path.read_bytes().split(b"\n")
    except OSError:
        return None
    serve = _line(lines[0])
    if not (
        isinstance(serve, dict)
        and serve.get("kind") == "SERVE"
        and serve.get("version") == 1
        and serve.get("id") == path.stem
        and isinstance(serve.get("offer"), str)
        and (serve.get("resumed") is None or isinstance(serve.get("resumed"), str))
    ):
        return None
    last = _line(next(raw for raw in reversed(lines) if raw.strip()))
    if (
        isinstance(last, dict)
        and last.get("kind") == "END"
        and isinstance(last.get("state"), dict)
        and last["state"].get("phase") in ("ended", "aborted")
    ):
        return serve, last["state"]
    return serve, None


def _logged(path: Path, serve: dict, end: dict | None) -> _Served:
    """A run as its log has it: ended with its END row's state, else cut short."""
    s = _Served(serve["id"], serve["offer"], serve["resumed"])
    s.path, s.rows = path, None
    s.final = (
        end
        if end is not None
        else {
            "id": serve["id"],
            "offer": serve["offer"],
            "resumed": serve["resumed"],
            "run": None,
            "phase": "cut_short",
            "turn": None,
            "cost": None,
            "rows": None,
            "pending": None,
            "closed": None,
            "outcome": None,
            "output": None,
            "error": None,
        }
    )
    s.phase = s.final.get("phase")
    return s


def _log_rows(path: Path) -> list[dict]:
    """The rows of a log as it was written: what the loader refuses, this read refuses too."""
    try:
        load_log(path, torn=[])
        lines = path.read_bytes().split(b"\n")
    except Exception as e:  # whatever the loader refuses, read or row
        raise ServeError(
            "unreadable_log", f"the log {path.name} cannot be read: {type(e).__name__}: {e}"
        ) from None
    return [r for r in map(_line, lines) if isinstance(r, dict) and r.get("kind") not in ("SERVE", "END")]


# shapes


def _start_fields(fields: Any) -> tuple[str, tuple[str, ...], dict, str | None, str | None]:
    """A start's fields as D2's table admits them: (offer, inputs, the bounds given, resume, once)."""
    if not isinstance(fields, dict):
        raise ServeError("bad_request", "a start is an object")
    unknown = [k for k in fields if k not in _FIELDS]
    if unknown:
        raise ServeError("bad_request", f"a start takes {', '.join(_FIELDS)}; not {unknown}")
    offer = fields.get("offer")
    if type(offer) is not str:
        raise ServeError("bad_request", "offer is required, a string")
    inputs = fields.get("inputs", [])
    if type(inputs) is not list or len(inputs) > _INPUTS or not all(type(t) is str and t for t in inputs):
        raise ServeError("bad_request", f"inputs is a list of at most {_INPUTS} strings, none empty")
    if any(_utf8(t, "an input") > _MIB for t in inputs):
        raise ServeError("too_large", f"an input is past {_MIB} bytes in UTF-8")
    bounds = fields.get("bounds", {})
    if type(bounds) is not dict:
        raise ServeError("bad_request", "bounds is an object")
    given = {}
    for name, value in bounds.items():
        if name not in _COUNTS and name not in _AMOUNTS:
            raise ServeError("bad_request", f"bounds has no {name!r}")
        if value is None:  # absent or null: the offer's
            continue
        if name in _COUNTS:
            ok = type(value) is int and value >= 1
        else:
            try:
                ok = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:  # an integer past what a float holds, which the run's clock reads as one
                ok = False
        if not ok:
            want = "an integer, 1 or more" if name in _COUNTS else "a finite number above 0"
            raise ServeError("bad_request", f"{name} is {want}, not {value!r}")
        given[name] = value
    resume = fields.get("resume")
    if "resume" in fields and (type(resume) is not str or not _ID.fullmatch(resume)):
        raise ServeError("bad_request", "resume is the 16 lowercase hex digits of a stored run's id")
    once = fields.get("once")
    if "once" in fields and (type(once) is not str or not _ONCE.fullmatch(once)):
        raise ServeError("bad_request", "once is 1 to 64 letters, digits, '.', '_' or '-'")
    return offer, tuple(inputs), given, resume, once


def _utf8(text: str, what: str) -> int:
    try:
        return len(text.encode())
    except UnicodeEncodeError:  # a lone surrogate, which JSON's escapes can carry
        raise ServeError("bad_request", f"{what} is not text UTF-8 can hold") from None


def _outcome(outcome: Any) -> dict:
    """An outcome as its kind and its own fields, its output aside (the state carries it)."""
    fields = {f.name: getattr(outcome, f.name) for f in dataclasses.fields(outcome) if f.name != "output"}
    return {"kind": type(outcome).__name__, **fields}


def _bus(s: _Served, event: Any) -> dict:
    return {"id": s.id, "kind": event.kind, "payload": _jsonable(event.payload)}


def _jsonable(value: Any, path: frozenset[int] = frozenset()) -> Any:
    """`value` as JSON data (ADR-0021/D9): pydantic's projection, a value it cannot project carried as
    `{"repr": ...}`. A container that fails whole (bytes that are not UTF-8, a cycle) is projected
    part by part, to a depth."""
    try:
        return to_jsonable_python(value, fallback=_repr, inf_nan_mode="null")
    except Exception:
        if not isinstance(value, dict | list | tuple) or id(value) in path or len(path) >= _DEPTH:
            return _repr(value)
    inner = path | {id(value)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v, inner) for k, v in value.items()}
    return [_jsonable(v, inner) for v in value]


def _repr(value: Any) -> dict:
    try:
        return {"repr": repr(value)}
    except Exception:  # a repr that raises, or one that recurses past the limit
        return {"repr": object.__repr__(value)}
