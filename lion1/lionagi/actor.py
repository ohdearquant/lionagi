"""The actor, its profiles, its handlers, and the round loop (ADR-0001, ADR-0002, ADR-0005,
ADR-0006, ADR-0010).

An actor is one addressable principal: its handlers are its Operable, and it has the inbox, the
privileges and the notes. A profile is how the model is configured when the actor runs. A run is
rounds: one SYSTEM notification, one model call, extract, gate, dispatch, settle.
Nothing a handler does is awaited by the loop inline; the next round starts when what the model
asked for has settled.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import functools
import inspect
import json
import re
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from . import lndl
from .bus import Bus
from .lndl import Lact, Lvar, Pointer, Program
from .notes import FileNoteStore, MemoryNoteStore, NoteStore, key_of
from .program import Branch, Command, EvalError, Output, Script, Value, evaluate, pointers
from .program import check as check_script
from .program import guidance as program_guidance
from .program import read as read_script
from .record import Directive, Entry, Kind, Record, ViewEntry, fold
from .spec import Operable, Spec, render_guidance, spec_name

__all__ = (
    "Message",
    "Reply",
    "Backend",
    "Profile",
    "HandlerFn",
    "BeforeFn",
    "AfterFn",
    "Handler",
    "ProfileError",
    "Result",
    "Success",
    "Continue",
    "Retry",
    "Exhausted",
    "Refused",
    "Stopped",
    "Outcome",
    "RunRequest",
    "Handle",
    "Inbound",
    "Run",
    "Context",
    "ContextHide",
    "ContextShow",
    "ContextSummarize",
    "NoteList",
    "NoteFind",
    "NoteGet",
    "NoteDelete",
    "DEFAULT_HANDLERS",
    "Actor",
)

# --- what a backend sees ---------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant
    content: str
    name: str | None = None  # the record entry this message renders, when it renders one
    spec: str | None = None  # for a RESULT: the command that produced it
    run: str | None = None  # the run whose record this renders: a backend keeping sessions keys on it


class Reply(str):
    """A backend's text with the provider's prompt count for the call (fresh input, cache read and cache
    write), None when the provider did not say; the actor anchors its `context:` figure on it."""

    prompt_tokens: int | None

    def __new__(cls, text: str, prompt_tokens: int | None = None) -> Reply:
        reply = super().__new__(cls, text)
        reply.prompt_tokens = prompt_tokens
        return reply


Backend = Callable[[list[Message]], "Awaitable[str] | str"]
"""A chat completion backend: the view in, the model's text out, a `Reply` when it knows the prompt's
size (ADR-0009/C1, ADR-0009/C2)."""


# --- agents and handlers -----------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """How the model is configured when the actor runs: a hat, not a principal.

    A profile has no inbox, no address and no credentials of its own; the actor has those. It names
    which of the actor's Operable the model is told about, the privileges the run acts under (a
    subset of the actor's), and the role's prose."""

    name: str
    specs: frozenset[str] | None = None  # None: the actor's whole Operable
    privileges: frozenset[str] = frozenset()
    system: str = ""  # the role's own prose, ahead of the generated guidance
    notes: NoteStore | None = None  # None: the actor keeps one per profile name
    language: str = "core"  # core: the three constructs; program: one fenced program (ADR-0008/C6)

    def __post_init__(self) -> None:
        # the name is the notes file's name under `notes_dir`: one file per profile, none outside it
        if not _PROFILE_NAME.match(self.name) or ".." in self.name:
            raise ProfileError(f"profile name {self.name!r}: letters, digits, _ . - only, it names a file")
        if self.language not in ("core", "program"):
            raise ProfileError(f"profile language {self.language!r}: core or program")


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


HandlerFn = Callable[[BaseModel, "Context"], "Awaitable[Any] | Any"]
# a hook before a command runs: None lets it run, a str refuses it with that reason, a request of the
# same spec replaces its arguments; a hook that raises refuses it (a gate that fails admits nothing)
BeforeFn = Callable[["Run", Lact, BaseModel], "Awaitable[str | BaseModel | None] | str | BaseModel | None"]
# a hook after a command settled, ok or not: None keeps the result, a Result replaces it; a hook that
# raises is told in the next notice and the result stands (the command already ran)
AfterFn = Callable[["Run", Lact, BaseModel, "Result"], "Awaitable[Result | None] | Result | None"]


@dataclass(frozen=True)
class Handler:
    spec: Spec
    fn: HandlerFn
    requires: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        return spec_name(self.spec)


class ProfileError(Exception):
    """A profile asks for what its actor does not have."""


# --- results and outcomes ----------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    """What a settled command put on the record."""

    spec: str
    ok: bool
    value: Any = None
    error: str | None = None
    reason: str | None = None  # unknown | invalid | privilege | refused | failed | cancelled

    def preview(self, width: int = 120) -> str:
        text = self.error if not self.ok else _text(self.value)
        text = " ".join(text.split())
        return text if len(text) <= width else text[: width - 1] + "…"


@dataclass(frozen=True)
class Success:
    output: Any


@dataclass(frozen=True)
class Continue:
    pass


@dataclass(frozen=True)
class Retry:
    error: str


@dataclass(frozen=True)
class Exhausted:
    """A budget ran out: `rounds` of them (the request's max_rounds) or the wall clock (time_budget)."""

    rounds: int
    reason: str = "rounds"


@dataclass(frozen=True)
class Refused:
    """OUT{} was refused by the request's accept gate `times` times in a row; the model was not converging."""

    why: str
    times: int


@dataclass(frozen=True)
class Stopped:
    round: int


Outcome = Success | Continue | Retry | Exhausted | Refused | Stopped

_CALL_SHAPED = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*\(.*\)\s*$", re.DOTALL)  # `name(...)` as a whole value
_SYSTEM_FRAME = re.compile(r"<system round=")  # the notification's own opening tag


@dataclass(frozen=True)
class RunRequest:
    inputs: tuple[str, ...] = ()
    emits: tuple[Spec, ...] = ()  # what OUT{} fills; empty: OUT{} ends the run with its dereferenced fields
    max_rounds: int = 10
    time_budget: float | None = None  # seconds of wall clock for the whole run; None: unbounded
    context_budget: int = 200_000  # tokens, estimated
    # the system's gate on OUT{} beyond validation: returns why the output is not acceptable, or None;
    # a refusal is told to the model and the run continues
    accept: Callable[[Run, Any], Awaitable[str | None] | str | None] | None = None
    max_refusals: int = 3  # consecutive accept refusals before the run ends as Refused
    # the view folds once, to about half, when it measures past this (tokens, estimated); until then
    # and after, nothing leaves it but by the model's own hides
    view_budget: int = 60_000
    # a conversation: called at the start of a round after a turn that ran nothing, so the model's
    # plain text was its reply and the next round takes what the other side says. It queues the next
    # input on the actor and returns True, or returns False and the run ends Stopped. With it set, a
    # turn without LNDL is a reply, not idleness.
    wait: Callable[[Run], Awaitable[bool]] | None = None
    # with `wait`: a turn whose commands all landed is the reply too (the work was done on the model's own
    # tools; the turn carried what goes through the runtime). Off, only a turn without commands replies:
    # a console reads a command's result back before it answers the person.
    reply_on_commands: bool = False
    # under a long conversation the fold may hide the inputs and replies of earlier exchanges too (the
    # latest input stays); off, INPUT is never hidden and only results fold
    fold_inputs: bool = False
    # a resumed conversation: entries of an earlier run, replayed onto this run's record before its
    # inputs, their aliases and values in reach again (a pointer to an old result or value resolves)
    history: tuple[Entry, ...] = ()


@dataclass(frozen=True)
class Handle:
    id: str


@dataclass(frozen=True)
class Inbound:
    """Something another actor queued here, or an answer coming back (ADR-0010/C1, ADR-0010/C3)."""

    handle: Handle
    content: Any
    origin_actor: str
    origin_run: str
    origin_alias: str | None
    reply: bool = False


class Run:
    def __init__(self, actor: Actor, profile: Profile, request: RunRequest):
        self.id = uuid.uuid4().hex[:8]
        self.actor = actor
        self.profile = profile
        self.request = request
        self.record = Record(self.id)
        self.started = time.monotonic()
        self.round = 0
        self.values: dict[str, tuple[int, Any]] = {}  # alias -> (seq of the TEXT that declared it, value)
        self.fields: dict[str, str] = {}  # alias -> the output field it was declared for, for the whole run
        self.pending: dict[str, asyncio.Task] = {}
        self.entered: set[str] = set()  # pending aliases whose handler has been called
        self.pointing: dict[str, set[str]] = {}  # command alias -> the names its arguments point at
        self.settled: list[str] = []  # aliases settled since the last notification
        self.refused: list[tuple[str, str]] = []  # (alias, why) since the last notification
        self.failed: list[tuple[str, str]] = []  # (alias, why) whose handler started, not succeeding
        self.unknown: list[tuple[str, str]] = []  # (alias, why) cancelled while their handler ran
        self.notes_written: list[str] = []
        self.hook_errors: list[tuple[str, str]] = []  # (alias, what) an after hook raised, this round
        self.consumed: list[Inbound] = []  # inbound items this run took as INPUT
        self.inbox: list[Inbound] = []  # answers addressed to this run, taken at its next round
        self.asked: dict[str, tuple[str, asyncio.Future]] = {}  # handle id -> (alias, the answer)
        self.retry_error: str | None = None
        self.idle_note: str | None = None  # the last turn carried no LNDL; told once
        self.idle_rounds = 0  # consecutive turns without LNDL
        self.replied = (
            False  # under a conversation (request.wait), the last turn was the reply: plain text, or
            # commands that all landed (the work was done on the model's own tools; the turn carried
            # what goes through the runtime: sends, notes, context operations)
        )
        self.pause = False  # hand the turn back at the next round boundary (a person's interrupt)
        self.same_turns = 0  # consecutive turns byte-identical to the one before
        self.same_errors = 0  # consecutive rounds ending in the same retry error
        self.viewed: set[int] = set()  # RESULT seqs in view at the last render
        self.view_est = 0  # the view the last notification measured, post-fold (tokens, estimated)
        self.anchor: tuple[int, int] | None = None  # (prompt tokens a backend reported, the view_est sent)
        self.grown = (0, 0)  # (reported, estimated) growth summed over counted calls whose view grew
        # the least a reported count stood above the view sent: the backend's own context beyond the
        # view (system prompt, tools), which no fold takes off; what it later stands above that is
        # history the record never saw (a session-keeping CLI's tool turns), which a fold does
        self.floor: int | None = None
        self.refusals = 0  # consecutive accept refusals
        self.repairs: Counter[str] = Counter()  # repairs the reading applied to this run's turns, by name
        # each unknown command name, the nearest name the profile has and the edit distance between them:
        # what a name repair would have done, measured while it is off (ADR-0008/D2)
        self.misnamed: list[tuple[str, str | None, int | None]] = []
        self.thunks: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}  # a program command's arguments
        # results a program consumed, each with the seq it was consumed at, hidden by the next notification
        self.intermediates: dict[str, int] = {}
        # a program's values not computed yet, for the whole run: the seq of the TEXT that bound it and
        # its expression; a value nothing has read stays bound (ADR-0008/C6)
        self.unread: dict[str, tuple[int, ast.expr]] = {}
        self.computed: list[str] = []  # program values computed since the last notification, which keeps them
        self.bound: list[str] = []  # program values bound since the last notification, which carries them
        self.outcome: Outcome | None = None
        self.output: Any = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def notes(self) -> NoteStore:
        return self.actor.notes_for(self.profile)

    def resolve(self, name: str) -> Any:
        """Dereference a pointer: an alias, a runtime name, or a note. KeyError when nothing has that name."""
        if name in self.values:
            return self.values[name][1]
        entry = self.record.get(name)
        if entry is not None:
            if entry.kind is Kind.RESULT:
                return entry.content.value if entry.content.ok else entry.content
            return entry.content
        key = key_of(name)
        if key is not None:
            note = self.notes.get(key)
            if note is not None:
                return note.value
        raise KeyError(name)

    def known(self, name: str) -> bool:
        try:
            self.resolve(name)
        except KeyError:
            return name in self.pending or name in self.unread
        return True


class Context:
    """What a handler needs and the model does not see (ADR-0005/C3)."""

    def __init__(self, run: Run, alias: str, handler: Handler):
        self.run = run
        self.actor = run.actor
        self.profile = run.profile
        self.alias = alias
        self.handler = handler
        self.directive: Directive | None = None

    @property
    def notes(self) -> NoteStore:
        return self.run.notes

    def get(self, name: str) -> Any:
        return self.run.resolve(name)

    def emit(self, kind: str, **payload: Any) -> None:
        self.actor.bus.emit(kind, run=self.run.id, alias=self.alias, **payload)

    def direct(self, op: str, refs: Iterable[str], text: str = "") -> None:
        """A context command's effect on the view; lands on this command's RESULT."""
        self.directive = Directive(op=op, refs=tuple(refs), text=text)


# --- the default Specs every profile has -------------------------------------------------------


class ContextHide(BaseModel):
    """Remove entries from your view; they stay on the record and their names still resolve. Hiding
    makes the provider re-read everything after the hidden entry, so hide in one batch when a phase
    ends, not one entry at a time."""

    __spec_name__ = "context.hide"
    refs: list[str]


class ContextShow(BaseModel):
    """Bring hidden entries back into view."""

    __spec_name__ = "context.show"
    refs: list[str]


class ContextSummarize(BaseModel):
    """Replace a span of entries with your summary; the summary keeps their names so you can reopen them."""

    __spec_name__ = "context.summarize"
    refs: list[str]
    text: str


class NoteList(BaseModel):
    """List your notes: key, size, version, which run wrote it. The prefix is an exact, case-sensitive
    start of the key, as a key is written."""

    __spec_name__ = "note.list"
    prefix: str = ""


class NoteFind(BaseModel):
    """Find notes whose key or content contains the query, case-insensitive; each hit carries a 120-char
    preview, size and version. A blank query is refused: list is for everything. To read a note whole,
    note.get(key), or point at it with *note.key."""

    __spec_name__ = "note.find"
    query: str


class NoteGet(BaseModel):
    """Read one note whole: its value, version and the run that wrote it. The view budget is the only
    size control."""

    __spec_name__ = "note.get"
    key: str


class NoteDelete(BaseModel):
    """Delete one note by its exact key. Overwriting with an empty value keeps the key; this removes it."""

    __spec_name__ = "note.delete"
    key: str


async def _hide(req: ContextHide, ctx: Context) -> str:
    ctx.direct("hide", req.refs)
    return f"hidden: {', '.join(req.refs)}"


async def _show(req: ContextShow, ctx: Context) -> str:
    ctx.direct("show", req.refs)
    return f"shown: {', '.join(req.refs)}"


async def _summarize(req: ContextSummarize, ctx: Context) -> str:
    ctx.direct("summarize", req.refs, req.text)
    return f"summarized: {', '.join(req.refs)}"


async def _note_list(req: NoteList, ctx: Context) -> list[dict]:
    return [
        {"key": n.key, "size": len(_text(n.value)), "run": n.run, "version": n.version}
        for n in ctx.notes.list(req.prefix)
    ]


async def _note_find(req: NoteFind, ctx: Context) -> list[dict]:
    return [
        {
            "key": n.key,
            "preview": _text(n.value)[:120],
            "size": len(_text(n.value)),
            "run": n.run,
            "version": n.version,
        }
        for n in ctx.notes.find(req.query)
    ]


async def _note_get(req: NoteGet, ctx: Context) -> dict:
    n = ctx.notes.get(req.key)
    if n is None:
        raise KeyError(f"no note {req.key!r}; note.list(prefix=...) shows what there is")
    return {"key": n.key, "value": n.value, "run": n.run, "version": n.version}


async def _note_delete(req: NoteDelete, ctx: Context) -> str:
    n = ctx.notes.delete(req.key)
    if n is None:
        raise KeyError(f"no note {req.key!r}; nothing deleted")
    return f"deleted note {n.key!r} (version {n.version}, written by run {n.run})"


DEFAULT_HANDLERS: tuple[Handler, ...] = (
    Handler(ContextHide, _hide),
    Handler(ContextShow, _show),
    Handler(ContextSummarize, _summarize),
    Handler(NoteList, _note_list),
    Handler(NoteFind, _note_find),
    Handler(NoteGet, _note_get),
    Handler(NoteDelete, _note_delete),
)


# --- the actor ---------------------------------------------------------------------------------

_ACTORS: dict[str, Actor] = {}
"""Actors by name, so an answer can go back to whoever queued (one process, one registry)."""


class Actor:
    def __init__(
        self,
        name: str,
        *,
        privileges: Iterable[str] = (),
        bus: Bus | None = None,
        notes_dir: str | Path | None = None,
    ):
        self.name = name
        self.privileges = frozenset(privileges)
        self.bus = bus or Bus()
        self.notes_dir = Path(notes_dir) if notes_dir is not None else None
        self.handlers: dict[str, Handler] = {}
        self.operable = Operable()
        self.inbox: list[Inbound] = []  # unaddressed inbound, taken by the next run that starts a round
        self.runs: dict[str, Run] = {}  # live runs by id, so an answer reaches the run that asked
        self.sections: list[Callable[[Run], Awaitable[str | None] | str | None]] = []
        self.befores: list[BeforeFn] = []
        self.afters: list[AfterFn] = []
        self._notes: dict[str, NoteStore] = {}
        for h in DEFAULT_HANDLERS:
            self.register(h)
        _ACTORS[name] = self

    # registration

    def register(self, handler: Handler | Any) -> Handler:
        """A `Handler`, or any object with a `spec` attribute that is callable as `(request, context)`."""
        if not isinstance(handler, Handler):
            spec = getattr(handler, "spec", None)
            if not (isinstance(spec, type) and issubclass(spec, BaseModel)) or not callable(handler):
                raise TypeError(
                    "register() takes a Handler or an object with a .spec and a __call__(request, context)"
                )
            handler = Handler(spec, handler, frozenset(getattr(handler, "requires", ())))
        name = self.operable.add(handler.spec)
        self.handlers[name] = handler
        return handler

    def handler(self, spec: Spec, *, requires: Iterable[str] = ()) -> Callable[[HandlerFn], HandlerFn]:
        def deco(fn: HandlerFn) -> HandlerFn:
            self.register(Handler(spec, fn, frozenset(requires)))
            return fn

        return deco

    def section(
        self, fn: Callable[[Run], Awaitable[str | None] | str | None]
    ) -> Callable[[Run], Awaitable[str | None] | str | None]:
        """A contributor to the notification: called every round, awaited if it must look somewhere,
        its text (if any) is appended. This is where the system says what the model cannot hold: the
        state of the thing being worked on, not only the state of the conversation."""
        self.sections.append(fn)
        return fn

    def before(self, fn: BeforeFn) -> BeforeFn:
        """A gate on every command, called with the validated request after the privilege check and
        before the handler, in registration order: None lets it run, a str refuses it with that
        reason (the model reads `refused: ...`), a request of the same spec replaces its arguments.
        A hook that raises refuses the command: a gate that fails admits nothing."""
        self.befores.append(fn)
        return fn

    def after(self, fn: AfterFn) -> AfterFn:
        """An observer of every command that ran, ok or not, called with the request and the result
        in registration order: None keeps the result, a Result replaces it. A hook that raises is
        told in the next notice and the result stands: the command already ran."""
        self.afters.append(fn)
        return fn

    def check(self, profile: Profile) -> Profile:
        """A profile may only name Specs in this actor's Operable and privileges this actor holds."""
        if profile.specs is not None:
            unknown = sorted(profile.specs - self.operable.names())
            if unknown:
                raise ProfileError(f"{profile.name} names specs this actor has no handler for: {unknown}")
        if not profile.privileges <= self.privileges:
            raise ProfileError(
                f"{profile.name} asks privileges beyond this actor: "
                f"{sorted(profile.privileges - self.privileges)}"
            )
        return profile

    def subset(self, profile: Profile) -> Operable:
        names = profile.specs if profile.specs is not None else self.operable.names()
        return self.operable.subset(sorted(names, key=list(self.operable).index))

    def notes_for(self, profile: Profile) -> NoteStore:
        if profile.notes is not None:
            return profile.notes
        store = self._notes.get(profile.name)
        if store is None:
            if self.notes_dir is not None:
                store = FileNoteStore(self.notes_dir / f"{profile.name}.json")
            else:
                store = MemoryNoteStore()
            self._notes[profile.name] = store
        return store

    def queue(self, content: Any, context: Context) -> Handle:
        """Hand something to this actor from inside a handler (ADR-0010/C1, ADR-0010/C2).

        It lands as INPUT in this actor's next run, or in a run already going. A handler that returns
        the Handle is settled by the answer: its alias resolves to what the other actor emitted, and
        the asking run's next round starts when the answer is in. Running the other actor is not this
        method's business; something has to run it."""
        handle = Handle(uuid.uuid4().hex[:8])
        self.inbox.append(
            Inbound(
                handle=handle,
                content=content,
                origin_actor=context.actor.name,
                origin_run=context.run.id,
                origin_alias=context.alias,
            )
        )
        context.run.asked[handle.id] = (context.alias, asyncio.get_running_loop().create_future())
        self.bus.emit("inbox.queued", actor=self.name, handle=handle.id, origin=context.actor.name)
        return handle

    # the loop

    async def run(
        self,
        profile: Profile,
        request: RunRequest,
        backend: Backend,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> Run:
        self.check(profile)
        run = Run(self, profile, request)
        subset = self.subset(profile)
        for e in request.history:
            replayed = run.record.append(e.kind, e.content, alias=e.alias, round=e.round, **e.meta)
            if e.kind is Kind.TEXT and profile.language == "core":  # the values that turn declared, admitted
                # against the record so far; a program's prose is scratch and its turn is folded, not rerun
                try:
                    program = _read(str(e.content))
                except lndl.LNDLError:
                    continue
                _admit(run, program, replayed.seq)
            if profile.language == "program":  # the values a program computed, bound again as they were
                for alias, seq, line, source in e.meta.get("bound", ()):  # bound, and read by nothing yet
                    expr = ast.parse(source, mode="eval").body
                    ast.increment_lineno(expr, line - 1)
                    run.unread[alias] = (seq, expr)
                for alias, seq, value in e.meta.get("values", ()):
                    run.values[alias] = (seq, value)
                    run.unread.pop(alias, None)
        for text in request.inputs:
            run.record.append(Kind.INPUT, text)
        self.runs[run.id] = run  # live from here: a history that does not replay leaves no run behind
        self.bus.emit("run.started", run=run.id, actor=self.name, profile=profile.name)

        try:
            for round_no in range(1, request.max_rounds + 1):
                if request.time_budget is not None and run.elapsed >= request.time_budget:
                    run.outcome = Exhausted(run.round, "time")
                    break
                run.round = round_no
                if request.wait is not None and (run.replied or run.pause) and not run.pending:
                    run.replied = run.pause = False
                    if not await request.wait(run):
                        run.round = round_no - 1
                        run.outcome = Stopped(round_no)
                        break
                self._take_inbox(run)
                text, folded = await self._notify(run)
                notice = run.record.append(Kind.SYSTEM, text, round=round_no)
                if folded is not None:  # the fold event rides its own notification, at its seq
                    notice.meta["directive"] = Directive(
                        folded.op, folded.refs, folded.text, at=notice.seq, seqs=folded.seqs
                    )
                if run.intermediates:  # the hide of what a program consumed, dated at each consumption
                    # (ADR-0008/D3), so a show the program made after it stands over it
                    by_at: dict[int, list[str]] = {}
                    for alias, at in run.intermediates.items():
                        by_at.setdefault(at, []).append(alias)
                    notice.meta["consumed"] = tuple(
                        Directive("hide", tuple(refs), at=at, seqs=tuple(run.record.get(a).seq for a in refs))
                        for at, refs in sorted(by_at.items())
                    )
                    run.intermediates.clear()
                if run.computed:  # the values a program computed, so a replayed record binds them again
                    notice.meta["values"] = tuple((a, *run.values[a]) for a in run.computed)
                    run.computed.clear()
                if run.bound:  # the values it bound and nothing read yet: their expressions, for a replay
                    notice.meta["bound"] = tuple(
                        (a, seq, expr.lineno, ast.unparse(expr))
                        for a in run.bound
                        if a in run.unread
                        for seq, expr in (run.unread[a],)
                    )
                    run.bound.clear()
                if stop is not None and stop():
                    # asked after the notification, not before it: the sections have just looked at
                    # the world, and what they saw (acceptance checks passing, a fix that has stood
                    # still) is what a stop decides on. Asked before, the model got one more turn
                    # after every notice that said the run was over.
                    run.round = round_no - 1
                    run.outcome = Stopped(round_no)
                    break
                self.bus.emit("round.started", run=run.id, round=round_no)
                messages = self._render(run, subset)
                text = backend(messages)
                if inspect.isawaitable(text):
                    text = await text
                reported = getattr(text, "prompt_tokens", None)
                if isinstance(text, str):
                    text = str(text)  # a Reply's count is read; the record keeps a plain str
                if isinstance(reported, int) and not isinstance(reported, bool) and reported > 0:
                    if run.anchor is not None and run.view_est > run.anchor[1]:
                        run.grown = (
                            run.grown[0] + reported - run.anchor[0],
                            run.grown[1] + run.view_est - run.anchor[1],
                        )
                    run.anchor = (reported, run.view_est)
                    beyond = reported - run.view_est
                    run.floor = beyond if run.floor is None else min(run.floor, beyond)
                previous = run.record.latest(Kind.TEXT)
                m = run.record.append(Kind.TEXT, text, round=round_no)
                run.same_turns = (
                    run.same_turns + 1 if previous is not None and previous.content == text else 0
                )
                try:
                    program = _read(text, run.profile.language)
                    run.repairs.update(program.repairs)  # applied by the reading, whatever the check says
                    if isinstance(program, Script):  # whole, before anything runs (ADR-0008/C1)
                        check_script(program, subset.names(), run.known)
                except lndl.LNDLError as e:
                    run.misnamed += [_nearest(name, subset.names()) for name in getattr(e, "names", ())]
                    self._retry(run, str(e))
                    self.bus.emit("round.retry", run=run.id, round=round_no, error=str(e))
                    continue
                run.idle_note = None
                if isinstance(program, Script):
                    acted = spoke = bool(program.body)
                else:
                    acted = bool(program.lacts) or program.out is not None
                    spoke = acted or bool(program.lvars)
                if request.wait is not None and not acted:
                    run.replied = True  # the reply; the other side speaks next
                elif not spoke:
                    run.idle_rounds += 1
                    stray = lndl.stray_tags(text, subset.names())
                    run.idle_note = "your last turn contained no LNDL, so nothing was executed" + (
                        f"; <{stray[0]}> is not a command call, write <lact a1>{stray[0]}(...)</lact>"
                        if stray
                        else ""
                    )
                    if run.idle_rounds >= 2:
                        run.idle_note += (
                            "; there is no tool-call API in this conversation, the tags are literal text "
                            'in your reply, for example <lact a1>list_dir(path=".")</lact>'
                        )
                else:
                    run.idle_rounds = 0
                for var in () if isinstance(program, Script) else program.lvars:
                    # a call written in a value tag runs nothing; say so by name
                    call = _CALL_SHAPED.match(var.value)
                    if call and call.group(1) in subset.names():
                        run.refused.append(
                            (
                                var.alias,
                                f"a value tag runs nothing; to run {call.group(1)}(...) write "
                                f"<lact {var.alias}>{call.group(1)}(...)</lact>",
                            )
                        )
                for problem in program.problems:
                    run.refused.append(("turn", problem))
                if isinstance(program, Script):
                    walk = _Walk(self, run, subset, m.seq)
                    try:
                        found = await walk.execute(program)
                    except lndl.LNDLError as e:  # the output could not be computed: a retry (ADR-0008/C7)
                        self._retry(run, str(e))
                        self.bus.emit("round.retry", run=run.id, round=round_no, error=str(e))
                        continue
                    program = Program(lacts=walk.lacts, out=None if found is None else lndl.Out(found))
                    referenced: set[str] = set()
                    tasks = walk.tasks
                else:
                    self._declare(run, program, m.seq)
                    referenced = _referenced(program)
                    tasks = self._dispatch(run, program, subset)
                if program.out is None:
                    await _settle(tasks)
                    if (
                        request.reply_on_commands
                        and request.wait is not None
                        and program.lacts
                        and not run.refused
                    ):
                        # a turn whose commands all landed is the reply: the other side speaks next and
                        # the results arrive with its next word. A refused or failed command earns one
                        # more turn, so the model sees it now.
                        landed = [e.content for e in run.record if e.seq > m.seq and e.kind is Kind.RESULT]
                        if all(isinstance(r, Result) and r.ok for r in landed):
                            run.replied = True
                    run.refusals = 0  # a turn that did not declare done breaks the row
                    run.retry_error, run.same_errors = None, 0  # a turn that read whole ends the error
                    run.outcome = Continue()
                    continue
                # OUT{} waits for this turn's commands and for anything it points at still running
                await _settle(list({*tasks, *(run.pending[a] for a in referenced if a in run.pending)}))
                try:
                    run.output = self._assemble(run, program)
                except lndl.LNDLError as e:
                    # the error clears only once the whole turn, assembly included, read: a finish
                    # that fails the same way every round is counted as the same error (ADR-0002/C3)
                    self._retry(run, str(e))
                    self.bus.emit("round.retry", run=run.id, round=round_no, error=str(e))
                    continue
                run.retry_error, run.same_errors = None, 0
                if request.accept is not None:
                    try:
                        why = request.accept(run, run.output)
                        if inspect.isawaitable(why):
                            why = await why
                    except Exception as e:  # a gate that cannot judge admits nothing, and the run goes on
                        why = f"the gate could not judge it ({type(e).__name__}: {e}); fix that or try again"
                    if why:
                        run.output = None
                        run.refusals += 1
                        if run.refusals >= request.max_refusals:
                            run.outcome = Refused(why, run.refusals)
                            break
                        run.retry_error = f"OUT{{}} not accepted: {why}"
                        self.bus.emit("round.retry", run=run.id, round=round_no, error=why)
                        continue
                run.outcome = Success(run.output)
                break
            else:
                run.outcome = Exhausted(request.max_rounds)
        except BaseException as e:
            # a backend that raised ends this run; whoever queued work here and is waiting on the
            # answer hears that, with the error, or its own command waits forever. The run has no
            # outcome (ADR-0002/C4): the Continue a settled turn left is not its end
            run.outcome = None
            self.runs.pop(run.id, None)
            self._deliver(run, error=e)
            raise
        finally:
            for _alias, answer in run.asked.values():
                if not answer.done():
                    answer.cancel()

        self.runs.pop(run.id, None)
        self._deliver(run)
        self.bus.emit("run.completed", run=run.id, outcome=type(run.outcome).__name__, rounds=run.round)
        return run

    # pieces of a round

    def _retry(self, run: Run, error: str) -> None:
        """The error the next notification carries; repeats are counted so the next turn differs."""
        run.same_errors = run.same_errors + 1 if run.retry_error == error else 0
        run.retry_error = error

    def _take_inbox(self, run: Run) -> None:
        items, self.inbox, run.inbox = self.inbox + run.inbox, [], []
        for item in items:
            run.consumed.append(item)
            run.record.append(
                Kind.INPUT,
                item.content,
                round=run.round,
                inbound=item.handle.id,
                origin=item.origin_actor,
                reply=item.reply,
            )

    def _declare(self, run: Run, program: Program, seq: int) -> None:
        for lv, why in _admit(run, program, seq):
            if why is not None:
                run.refused.append((lv.alias, why))
                continue
            if lv.spec is not None and lv.spec not in {spec_name(sp) for sp in run.request.emits}:
                # `<lvar qa.smoke>` reads as spec `qa`, field `smoke`; with no such output it is almost
                # always a note missing its prefix
                run.refused.append(
                    (
                        lv.alias,
                        f"<lvar {lv.spec}.{lv.field}> names an output `{lv.spec}` this run does not emit, "
                        f"so it is a plain value `{lv.alias}`; a note is written as "
                        f"<lvar note.{lv.spec}.{lv.field}>",
                    )
                )
            key = key_of(lv.alias)
            if key is not None:
                run.notes.put(key, lv.value, run=run.id, seq=seq)
                run.notes_written.append(lv.alias)

    def _dispatch(self, run: Run, program: Program, subset: Operable) -> list[asyncio.Task]:
        # Every command in the turn runs, OUT{} or not. Commands beside OUT{} used to be scratch
        # (declared, not run, named as such): the model writes "edit, then done" in one turn and
        # meant both, and told three times that the edit had not run it wrote the same turn again,
        # so the fix it had written never reached the tree and the run ended refused.
        # every command of the turn is admitted before any of them runs: under an eager task factory
        # the coroutine would otherwise run inside create_task, settle, and only then be registered as
        # pending (a second RESULT under its alias from `_never_started`), and a pointer at a later
        # command of the same turn would find nothing to wait for
        admitted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tasks = []
        try:
            for la in program.lacts:
                if la.alias.startswith("auto_") and (
                    run.record.has_alias(la.alias) or la.alias in run.pending
                ):
                    # a generated alias (native tool-call syntax, no name given) may recur verbatim in a
                    # later round; the model never chose it, so it is not held to uniqueness
                    la = dataclasses.replace(la, alias=f"{la.alias}_{run.record.seq}")
                if la.alias.startswith("_"):
                    run.refused.append((la.alias, _OWN.format(alias=la.alias)))
                    continue
                if run.record.has_alias(la.alias) or la.alias in run.pending or la.alias in run.values:
                    prior = run.record.get(la.alias)
                    where = (
                        prior.name
                        if prior is not None
                        else ("a value" if la.alias in run.values else "a call still running")
                    )
                    run.refused.append((la.alias, _TAKEN.format(alias=la.alias, where=where)))
                    continue
                if la.field is not None:
                    run.fields[la.alias] = la.field
                run.pointing[la.alias] = {p.name for p in la.pointers()}
                task = asyncio.create_task(self._perform(run, la, subset, admitted))
                task.add_done_callback(functools.partial(self._never_started, run, la))
                run.pending[la.alias] = task
                tasks.append(task)
        finally:
            admitted.set_result(None)
        return tasks

    def _never_started(self, run: Run, la: Lact, task: asyncio.Task) -> None:
        """A command cancelled before its coroutine ran: none of `_perform`'s exits happened, so the
        alias would stay in `pending` and no RESULT would land. One cancelled before it entered is
        recorded by the loop (ADR-0002/C1) and `pending` drops the alias on every exit (ADR-0005/C1)."""
        if run.pending.get(la.alias) is not task:
            return  # `_perform` ran and settled the command itself
        run.pending.pop(la.alias)
        result = Result(la.name, False, error="cancelled before it started", reason="cancelled")
        run.record.append(Kind.RESULT, result, alias=la.alias, round=run.round, execution="not executed")
        run.refused.append((la.alias, result.error))
        self.bus.emit(
            "command.settled",
            run=run.id,
            alias=la.alias,
            name=la.name,
            ok=False,
            reason="cancelled",
            execution="not executed",
        )

    async def _perform(self, run: Run, la: Lact, subset: Operable, admitted: asyncio.Future[None]) -> None:
        """One command, from request to RESULT. Every exit takes the alias out of `pending` and leaves
        a RESULT: a handler's error, a validator's own exception and a cancellation (recorded, then
        raised on) all settle the command; nothing is advertised as running after it stopped."""
        await admitted  # set once the whole turn is in `pending`; already set under the default factory
        self.bus.emit("command.requested", run=run.id, alias=la.alias, name=la.name)
        cancelled = False
        try:
            try:
                result, directive = await self._execute(run, la, subset)
            except asyncio.CancelledError:
                result = Result(la.name, False, error="cancelled before it finished", reason="cancelled")
                directive, cancelled = None, True
            # whether the handler was called, never read off a reason a handler or a hook can write
            if la.alias not in run.entered:
                execution, told = "not executed", run.refused
            elif cancelled:
                execution, told = "completion unknown", run.unknown
            else:
                execution, told = "started", run.failed
            if directive is not None:
                missing = [ref for ref in directive.refs if run.record.get(ref) is None]
                if missing:  # a result of this same turn not landed yet, or a name that is nobody's:
                    # stored as sequences it would hide nothing forever and report success
                    result = Result(
                        la.name,
                        False,
                        error=f"{directive.op}: nothing on the record is named {', '.join(missing)}; "
                        "a result of this turn is addressable next round",
                        reason="failed",
                    )
                    directive = None
            try:
                entry = run.record.append(
                    Kind.RESULT, result, alias=la.alias, round=run.round, execution=execution
                )
            except ValueError as e:  # the alias was taken meanwhile; the call ran, its result has no name
                told.append((la.alias, str(e)))
                return
            if directive is not None:
                seqs = tuple(run.record.get(ref).seq for ref in directive.refs)
                entry.meta["directive"] = Directive(
                    directive.op, directive.refs, directive.text, at=entry.seq, seqs=seqs
                )
            if result.ok:
                run.settled.append(la.alias)
            else:
                told.append((la.alias, result.error or result.reason or "failed"))
            self.bus.emit(
                "command.settled",
                run=run.id,
                alias=la.alias,
                name=la.name,
                ok=result.ok,
                reason=result.reason,
                execution=execution,
            )
        finally:
            run.pending.pop(la.alias, None)
            run.entered.discard(la.alias)
        if cancelled:
            raise asyncio.CancelledError

    async def _execute(self, run: Run, la: Lact, subset: Operable) -> tuple[Result, Directive | None]:
        if la.name not in subset:
            run.misnamed.append(_nearest(la.name, subset.names()))
            return Result(la.name, False, error=f"unknown command {la.name!r}", reason="unknown"), None
        handler = self.handlers[la.name]
        try:
            args = await self._deref(run, la)
            request = handler.spec.model_validate(args)
        except KeyError as e:
            return Result(la.name, False, error=f"pointer *{e.args[0]} names nothing", reason="invalid"), None
        except lndl.LNDLError as e:
            return Result(la.name, False, error=str(e), reason="invalid"), None
        except ValidationError as e:
            error = f"invalid arguments: {_validation_text(e)}"
            return Result(la.name, False, error=error, reason="invalid"), None
        except Exception as e:  # a validator's own exception: the arguments were not accepted
            error = f"invalid arguments: {type(e).__name__}: {e}"
            return Result(la.name, False, error=error, reason="invalid"), None
        if not handler.requires <= run.profile.privileges:
            missing = sorted(handler.requires - run.profile.privileges)
            return Result(la.name, False, error=f"{la.name} requires {missing}", reason="privilege"), None
        for hook in self.befores:
            try:
                verdict = hook(run, la, request)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
            except Exception as e:
                why = f"refused: {_hook_name(hook)} failed, {type(e).__name__}: {e}"
                return Result(la.name, False, error=why, reason="refused"), None
            if isinstance(verdict, str):
                return Result(la.name, False, error=f"refused: {verdict}", reason="refused"), None
            if isinstance(verdict, BaseModel):
                request = verdict
        ctx = Context(run, la.alias, handler)
        run.entered.add(la.alias)  # from here the handler may have acted on the world
        try:
            value = handler.fn(request, ctx)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, Handle) and value.id in run.asked:
                # the answer, under the same alias; the ask is dropped once consumed, so an answer
                # that lands before the handler returns is still found here
                handle_id = value.id
                value = await run.asked[handle_id][1]
                run.asked.pop(handle_id, None)
            result = Result(la.name, True, value=value)
        except Exception as e:
            result = Result(la.name, False, error=f"{type(e).__name__}: {e}", reason="failed")
        for hook in self.afters:
            try:
                seen = hook(run, la, request, result)
                if inspect.isawaitable(seen):
                    seen = await seen
            except Exception as e:
                run.hook_errors.append((la.alias, f"{_hook_name(hook)}: {type(e).__name__}: {e}"))
                continue
            if isinstance(seen, Result):
                result = seen
        return result, (ctx.directive if result.ok else None)

    async def _deref(self, run: Run, la: Lact) -> dict[str, Any]:
        """Wait for any pointer still pending, then substitute values for pointers."""
        thunk = run.thunks.pop(la.alias, None)
        if thunk is not None:  # a program's command: its expressions computed now, waiting on what they read
            return await thunk()
        for p in la.pointers():
            task = run.pending.get(p.name)
            if task is not None and task is not asyncio.current_task():
                if _reaches(run, p.name, la.alias):
                    raise lndl.LNDLError(
                        f"*{p.name} points back at {la.alias}: a circle of pointers never settles"
                    )
                await asyncio.shield(task)

        def sub(v: Any) -> Any:
            if isinstance(v, Pointer):
                return run.resolve(v.name)
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [sub(x) for x in v]
            if isinstance(v, tuple):
                return tuple(sub(x) for x in v)
            return v

        return sub(la.args)

    def _assemble(self, run: Run, program: Program) -> Any:
        assert program.out is not None
        emits = {spec_name(s): s for s in run.request.emits}
        out: dict[str, Any] = {}
        for key, value in program.out.fields.items():
            if emits and key not in emits and len(emits) == 1 and len(program.out.fields) == 1:
                (only,) = emits
                run.refused.append(
                    ("OUT", f"OUT{{{key}: ...}} read as OUT{{{only}: ...}}, the run's one output")
                )
                key = only
            if emits and key not in emits:
                first = sorted(emits)[0]
                raise lndl.LNDLError(
                    f"OUT{{{key}: ...}}: this run's outputs are {sorted(emits)}; write "
                    f"OUT{{{first}: [aliases]}} or OUT{{{first}: {{field: value, ...}}}}"
                )
            spec = emits.get(key)
            if not isinstance(value, lndl.Aliases):  # a literal, with pointers resolved
                value = _deref(run, key, value)
                if spec is None:
                    out[key] = value
                    continue
                required = [n for n, f in spec.model_fields.items() if f.is_required()]
                try:
                    if isinstance(value, dict):  # a pointed-at lvar is text; the field decides
                        out[key] = spec.model_validate({k: _coerce(spec, k, v) for k, v in value.items()})
                    elif len(spec.model_fields) == 1 or len(required) == 1:
                        out[key] = spec.model_validate({(required or list(spec.model_fields))[0]: value})
                    else:
                        raise lndl.LNDLError(
                            f"OUT{{{key}: ...}}: {key} has several fields; give a dict literal or aliases"
                        )
                except ValidationError as e:
                    raise lndl.LNDLError(f"OUT{{{key}: ...}}: {_validation_text(e)}") from None
                continue
            fields: dict[str, Any] = {}
            whole: list[Any] = []
            for alias in value:
                if alias not in run.values and run.record.get(alias) is None:
                    raise lndl.LNDLError(f"OUT{{{key}: ...}}: {alias!r} was never declared")
                fld = run.fields.get(alias)
                resolved = run.resolve(alias)
                if fld is not None:
                    fields[fld] = _coerce(spec, fld, resolved) if spec is not None else resolved
                else:
                    whole.append(resolved)
            if spec is None:
                out[key] = fields if fields else (whole[0] if len(whole) == 1 else whole)
                continue
            try:
                if fields:
                    out[key] = spec.model_validate(fields)
                elif len(whole) == 1:
                    out[key] = _validate_whole(spec, whole[0])
                elif len(whole) == len(spec.model_fields):
                    names = list(spec.model_fields)
                    out[key] = spec.model_validate(
                        {n: _coerce(spec, n, v) for n, v in zip(names, whole, strict=True)}
                    )
                else:
                    raise lndl.LNDLError(
                        f"OUT{{{key}: ...}}: give one alias holding the whole value, or one per field as "
                        f"<lvar {key}.field alias>"
                    )
            except ValidationError as e:
                raise lndl.LNDLError(f"OUT{{{key}: ...}}: {_validation_text(e)}") from None
        missing = [
            n
            for n, s in emits.items()
            if n not in out and any(f.is_required() for f in s.model_fields.values())
        ]
        if missing:
            raise lndl.LNDLError(f"OUT{{...}} must fill: {missing}")
        return out

    def _deliver(self, run: Run, error: BaseException | None = None) -> None:
        """An answered inbound goes back to its origin under the same handle (ADR-0010/C3)."""
        done = error is None and isinstance(run.outcome, Success)
        how = (
            f"in error ({type(error).__name__}: {error})" if error is not None else type(run.outcome).__name__
        )
        for item in run.consumed:
            if item.reply:
                continue
            origin = _ACTORS.get(item.origin_actor)
            if origin is None:
                continue
            asker = origin.runs.get(item.origin_run)
            if asker is not None and item.handle.id in asker.asked:
                _alias, answer = asker.asked[item.handle.id]  # the asking command pops it when it reads it
                if answer.done():
                    continue
                if done:
                    answer.set_result(_plain(run.output))
                else:
                    answer.set_exception(RuntimeError(f"{self.name} ended {how} without answering"))
                self.bus.emit("inbox.answered", actor=origin.name, handle=item.handle.id, by=self.name)
                continue
            if not done:
                continue
            origin.inbox.append(
                Inbound(
                    handle=item.handle,
                    content=run.output,
                    origin_actor=self.name,
                    origin_run=run.id,
                    origin_alias=None,
                    reply=True,
                )
            )
            self.bus.emit("inbox.answered", actor=origin.name, handle=item.handle.id, by=self.name)

    # what the model sees

    async def _notify(self, run: Run) -> tuple[str, Directive | None]:
        """The round's notification, and the fold event when the view has outgrown its budget."""
        r = run.record
        clock = _clock(run.elapsed)
        if run.request.time_budget is not None:
            clock += f"/{_clock(run.request.time_budget)}"
        lines = [f"<system round={run.round}/{run.request.max_rounds} time={clock} seq={r.seq}>"]
        view = self._fold(run)
        if run.intermediates:  # consumed since the last notification: hidden before the model sees them
            unseen = {r.get(a).seq for a in run.intermediates}
            view = [dataclasses.replace(v, hidden=True) if v.entry.seq in unseen else v for v in view]
        used = _tokens(_messages(view))
        context_line = len(lines)
        lines.append("")  # the context figure, written once the fold below has settled the view
        folded: Directive | None = None
        # the figure the fold decides on: the view's estimate plus what the backend's last reported
        # count stood above the view beyond its floor for this run, history the record never saw (a
        # session-keeping CLI's tool turns: one call read 14.8M tokens while the estimate never
        # moved, ADR-0009/A3). The floor itself, the backend's fixed context, is never counted:
        # a fold cannot take it off, and counting it would fold every round past a budget it exceeds.
        excess = 0
        if run.anchor is not None and run.floor is not None:
            excess = max(0, run.anchor[0] - run.anchor[1] - run.floor)
        measured = used + excess
        keep = used
        if measured > run.request.view_budget:
            # one fold event, to about half the budget, oldest entries first (results; under
            # fold_inputs the inputs and replies of earlier exchanges too, the latest input stays); the
            # results of the round just run are new to the model and stay. It rides this notification
            # as a directive, so it happens once and the view is append-only again until the next one,
            # and a session-keeping backend starts its session over from the folded view on it.
            latest = r.latest(Kind.SYSTEM)
            newest_input = r.latest(Kind.INPUT)
            old = [
                v.entry
                for v in view
                if not v.hidden
                and v.summary is None
                and (latest is None or v.entry.seq < latest.seq)
                and (
                    v.entry.kind is Kind.RESULT
                    or (
                        run.request.fold_inputs
                        and v.entry.kind in (Kind.INPUT, Kind.TEXT)
                        and (newest_input is None or v.entry.seq < newest_input.seq)
                    )
                )
            ]
            remaining, names, freed = measured, [], 0  # `keep` stays on the estimate's scale
            for e in old:
                if remaining <= run.request.view_budget // 2:
                    break
                size = _tokens([_message(ViewEntry(e))])
                remaining, keep, freed = remaining - size, keep - size, freed + size
                names.append(e.name)
            folded = Directive("hide", tuple(names), seqs=tuple(e.seq for e in old[: len(names)]))
            beyond = f" plus ~{excess} of session history beyond it" if excess else ""
            if names:
                lines.append(
                    f"folded: the view was ~{used} of {run.request.view_budget} tokens{beyond}; "
                    f"{len(names)} older entries ({names[0]} to {names[-1]}, ~{freed} tokens) are "
                    "placeholders now; *name or context.show reopens any"
                )
            else:
                lines.append(
                    f"folded: the view was ~{used} of {run.request.view_budget} tokens{beyond} with "
                    "nothing older to fold; the conversation starts over from this view"
                )
        # context: measures the view it will stand in (ADR-0006/D1); a fold just decided
        # changed that view, so the post-fold size is the one measured; folded: keeps the pre-fold
        # figure, where it reads as past tense. With a count the backend reported, the figure is that
        # count plus the view added since; a shrink takes off what left at the rate this run's reported
        # growth has measured per estimated token, so a backend's fixed context is never scaled with
        # the view (ADR-0006/C4, ADR-0009/C3).
        run.view_est = keep
        shown = keep
        if run.anchor is not None:
            reported, sent = run.anchor
            if keep >= sent:
                shown = reported + keep - sent
            else:
                rate = run.grown[0] / run.grown[1] if run.grown[1] > 0 else 1.0
                shown = max(round(reported - rate * (sent - keep)), keep)
        lines[context_line] = f"context: ~{shown} / {run.request.context_budget} tokens"
        if run.retry_error:
            again = f" (the same error {run.same_errors + 1} rounds running)" if run.same_errors else ""
            lines.append(f"your last turn could not be read{again}: {run.retry_error}")
        if run.idle_note:
            lines.append(run.idle_note)
        if run.same_turns:
            lines.append(
                f"your last turn was identical to the one before it ({run.same_turns + 1} times now); "
                "it reads the same way every time, so change it"
            )
        if run.settled:
            lines.append("settled:")
            for alias in run.settled:
                e = r.get(alias)
                assert e is not None
                res: Result = e.content
                if alias in run.intermediates:  # its value stays out of the context, preview included
                    lines.append(f"  {alias} ({e.name}, {res.spec}): {_typename(res.value)}, consumed")
                    continue
                lines.append(f"  {alias} ({e.name}, {res.spec}): {_typename(res.value)} {res.preview()}")
        for header, told in (("failed after starting:", run.failed), ("completion unknown:", run.unknown)):
            if told:  # the handler was called: what it did before it failed or was stopped stands
                lines.append(header)
                lines.extend(f"  {alias}: {why}" for alias, why in told)
        visible = _shown(view)
        gone = [r.by_seq(seq) for seq in sorted(run.viewed - visible)]
        if gone:
            names = ", ".join(f"{e.alias} ({e.name})" for e in gone)
            lines.append(f"out of view now, still addressable by name or context.show: {names}")
        if run.refused:
            lines.append("not executed:")
            for alias, why in run.refused:
                lines.append(f"  {alias}: {why}")
        if run.notes_written:
            lines.append("notes written: " + ", ".join(run.notes_written))
        if run.hook_errors:
            lines.append("hooks failed (the commands ran; their results stand):")
            for alias, what in run.hook_errors:
                lines.append(f"  {alias}: {what}")
        inbound = [e for e in r.of_kind(Kind.INPUT) if e.round == run.round and e.meta.get("inbound")]
        if inbound:
            lines.append("inbound:")
            for e in inbound:
                kind = "answer" if e.meta.get("reply") else "request"
                lines.append(f"  {e.name}: {kind} from {e.meta['origin']} (handle {e.meta['inbound']})")
        for section in self.sections:
            try:
                text = section(run)
                if inspect.isawaitable(text):
                    text = await text
            except Exception as e:  # a section reads the world; its failure is a line, not the run's end
                text = f"section failed: {_hook_name(section)}: {type(e).__name__}: {e}"
            if text:
                lines.append(text)
        lines.append("</system>")
        run.settled.clear()
        run.refused.clear()
        run.failed.clear()
        run.unknown.clear()
        run.notes_written.clear()
        run.hook_errors.clear()
        return "\n".join(lines), folded

    def _render(self, run: Run, subset: Operable) -> list[Message]:
        outputs = tuple(spec_name(s) for s in run.request.emits)
        language = lndl.guidance if run.profile.language == "core" else program_guidance
        parts = [
            run.profile.system.strip(),
            language(outputs, commands=subset),
            "COMMANDS\n" + subset.guidance(),
        ]
        if run.request.emits:
            parts.append(_output_guidance(run.request.emits))
        messages = [Message("system", "\n\n".join(p for p in parts if p))]
        view = self._fold(run)
        messages.extend(_messages(view))
        run.viewed = _shown(view)
        return [dataclasses.replace(m, run=run.id) for m in messages]

    def _fold(self, run: Run) -> list[ViewEntry]:
        return fold(run.record, protect_input=not run.request.fold_inputs)


# --- helpers -----------------------------------------------------------------------------------


def _clock(seconds: float) -> str:
    """Wall clock the way a person says it: 42s, 3m10s, 1h05m."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


_OWN = "alias {alias!r} starts with _, which the record keeps for its own names (_r41, _m12); pick another"
_TAKEN = "alias {alias!r} already names {where}; aliases are unique for the whole run"


def _read(text: str, language: str = "core") -> Program | Script:
    """A turn as the loop reads it, live or resumed; LNDLError is what the model is told."""
    if _SYSTEM_FRAME.search(text):
        # the model wrote the loop's own frame and went on as if the results in it were real (9 of
        # 50 thinking-off runs, half their output tokens); none of it runs
        raise lndl.LNDLError(
            "the turn carries a <system round=...> frame; only the loop writes those and the results "
            "inside it were never observed, so nothing in the turn was executed: write one program, "
            "end the turn, and read the real results in the next notification"
        )
    return lndl.parse(text) if language == "core" else read_script(text)


def _admit(run: Run, program: Program, seq: int) -> list[tuple[Lvar, str | None]]:
    """The turn's values admitted into `run.values` and `run.fields` against the record so far, each
    with why it was refused or None: the live loop and a resumed history both fold turns through it."""
    out: list[tuple[Lvar, str | None]] = []
    for lv in program.lvars:
        if lv.alias.startswith("_"):
            out.append((lv, _OWN.format(alias=lv.alias)))
            continue
        if run.record.has_alias(lv.alias) or lv.alias in run.pending:
            where = (
                run.record.get(lv.alias).name if run.record.has_alias(lv.alias) else "a call still running"
            )
            out.append((lv, _TAKEN.format(alias=lv.alias, where=where)))
            continue
        run.values[lv.alias] = (seq, lv.value)
        if lv.field is not None:
            run.fields[lv.alias] = lv.field
        else:  # a plain declaration of a bound alias: the binding went with the value it named
            run.fields.pop(lv.alias, None)
        out.append((lv, None))
    return out


class _Failed(Exception):
    """A block finished Failed: nothing after it runs, in it or around it (ADR-0008/C7)."""


class _Walk:
    """One program over the dispatcher (ADR-0008/D1, D3): a command runs as its own task when its block
    reaches it, a value is computed when first read, a note is written when its assignment is reached,
    and a branch's control result gates the rest of its block. The results a reader that settled ok
    read, and those a note took, are consumed: the next notification hides them."""

    def __init__(self, actor: Actor, run: Run, subset: Operable, seq: int):
        self.actor, self.run, self.subset, self.seq = actor, run, subset, seq
        self.tasks: list[asyncio.Task] = []
        self.lacts: list[Lact] = []
        # an earlier turn's values nothing read yet are in reach, computed when first read
        self.values: dict[str, tuple[int, ast.expr]] = dict(run.unread)
        self.computed: dict[str, asyncio.Future] = {}
        self.value_reads: dict[str, set[str]] = {}  # a value's reads, charged to whoever reads the value
        self.writes: dict[str, asyncio.Task] = {}  # note key -> the latest assignment reached
        # a write's task -> the values it reads (through the values they read) and the write before it
        self.write_reads: dict[asyncio.Task, tuple[frozenset[str], asyncio.Task | None]] = {}
        self.reads: dict[str, set[str]] = {}  # this program's command -> the results its arguments read
        # results read by a reader that settled ok, or taken by a note: the seq each was first consumed at
        self.used: dict[str, int] = {}
        self.named: set[str] = set()  # what OUT{} points at: never consumed

    async def execute(self, script: Script) -> dict[str, Any] | None:
        try:
            return await self.block(script.body)
        except _Failed:
            return None
        except (
            asyncio.CancelledError
        ):  # the run is cancelled: its commands with it, each settling as cancelled
            for task in self.tasks:
                task.cancel()
            raise
        finally:
            await _settle(self.tasks)
            for alias, reads in self.reads.items():
                entry = self.run.record.get(alias)
                if entry.content.ok:
                    self.consume(reads, entry.seq)  # at the reader's settle
            consumed = sorted(self.used.keys() - self.named, key=lambda a: self.run.record.get(a).seq)
            self.run.intermediates.update((a, self.used[a]) for a in consumed)

    def consume(self, reads: set[str], at: int) -> None:
        for alias in reads:
            self.used[alias] = min(at, self.used.get(alias, at))

    async def block(self, nodes: tuple) -> dict[str, Any] | None:
        for node in nodes:
            if isinstance(node, Branch):
                reader = _Reader(self)
                try:
                    taken = await evaluate(node.test, reader)
                except lndl.LNDLError as e:
                    self.run.refused.append((f"line {node.line}", f"if: {e}; nothing after it ran"))
                    raise _Failed from None
                self.consume(reader.reads, len(self.run.record))
                ended = await self.block(node.body if taken else node.orelse)
                if ended is not None:
                    return ended
            elif isinstance(node, Output):
                reader = _Reader(self)
                self.named |= node.named
                fields = {key: await evaluate(v, reader) for key, v in node.fields}
                self.consume(reader.reads, len(self.run.record))
                return fields
            elif isinstance(node, Command):
                self.command(node)
            elif isinstance(node, Value):
                self.values[node.alias] = self.run.unread[node.alias] = (self.seq, node.expr)
                self.run.bound.append(node.alias)
            else:
                self.write(node)
        return None

    def auto(self) -> str:
        n, taken = len(self.lacts), self.run.pending
        while self.run.record.has_alias(f"r{self.seq}.{n}") or f"r{self.seq}.{n}" in taken:
            n += 1  # a resumed record may hold the name already (its rows are renumbered on load)
        return f"r{self.seq}.{n}"

    def launch(self, la: Lact, args: Callable[[], Awaitable[dict[str, Any]]]) -> asyncio.Task:
        run = self.run
        admitted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        run.thunks[la.alias] = args
        task = asyncio.create_task(self.actor._perform(run, la, self.subset, admitted))
        task.add_done_callback(functools.partial(self.actor._never_started, run, la))
        run.pending[la.alias] = task
        admitted.set_result(None)  # registered before it runs, under any task factory
        self.reads.setdefault(la.alias, set())
        self.tasks.append(task)
        self.lacts.append(la)
        return task

    def command(self, node: Command) -> str:
        alias = node.alias or self.auto()
        reader = _Reader(self)

        async def args() -> dict[str, Any]:
            return {key: await evaluate(v, reader) for key, v in node.args}

        self.reads[alias] = reader.reads
        self.launch(Lact(alias, node.name, {}), args)
        return alias

    async def value(self, name: str, writes: dict[str, asyncio.Task] | None = None) -> Any:
        if name not in self.computed:
            seq, expr = self.values[name]
            # the reader's line decides: the value reads after the note writes before its first reader,
            # whichever turn bound it (ADR-0008/D4)
            reader = _Reader(self, self._before(name, self.writes if writes is None else writes))
            self.computed[name] = asyncio.ensure_future(self._compute(name, seq, expr, reader))
        return await asyncio.shield(self.computed[name])

    def _before(self, name: str, writes: dict[str, asyncio.Task]) -> dict[str, asyncio.Task]:
        """The writes a value reads after. A write that reads the value comes after the value, not
        before it (waiting for it would wait for itself): the value reads after the write before that."""
        out: dict[str, asyncio.Task] = {}
        for key, task in writes.items():
            while task is not None and name in self.write_reads[task][0]:
                task = self.write_reads[task][1]
            if task is not None:
                out[key] = task
        return out

    def _reads(self, *exprs: ast.expr) -> frozenset[str]:
        """The values the expressions read, and the values those read, as far as it goes."""
        names, todo = set(), list(pointers(*exprs))
        while todo:
            n = todo.pop()
            if n not in names and n in self.values:
                names.add(n)
                todo += pointers(self.values[n][1])
        return frozenset(names)

    async def _compute(self, name: str, seq: int, expr: ast.expr, reader: _Reader) -> Any:
        try:
            value = await evaluate(expr, reader)
        except lndl.LNDLError as e:
            # a failed computation is the value's failure for the run: whatever it ran, ran once; a
            # later read finds the failure, never the expression again
            value = Result("value", False, error=str(e), reason="invalid")
        self.run.values[name] = (seq, value)
        self.run.unread.pop(name, None)
        self.run.computed.append(name)
        if isinstance(value, Result):
            raise EvalError(f"*{name}: {value.error}")
        self.value_reads[name] = reader.reads  # consumed only once a reader of the value settles ok
        return value

    def write(self, node: Any) -> None:
        prior = self.writes.get(node.key)
        reader = _Reader(self)
        target = ".".join(("note", node.key, *node.path))
        producer = self.command(node.value) if isinstance(node.value, Command) else None

        async def put() -> None:
            if prior is not None:  # two assignments to one note are two writes, in the order written
                await asyncio.shield(prior)
            try:
                value = await (
                    reader.point(producer) if producer is not None else evaluate(node.value, reader)
                )
            except lndl.LNDLError as e:  # what it writes failed: the note keeps its value
                self.run.refused.append((target, f"not written: {e}"))
                return
            try:
                old = self.run.notes.get(node.key)
                whole = _put_path(old.value if old is not None else {}, node.path, value)
                self.run.notes.put(node.key, whole, run=self.run.id, seq=self.seq)
            except Exception as e:  # the store refused: the result stands and no write is claimed
                self.run.refused.append((target, f"not written: {type(e).__name__}: {e}"))
                return
            self.run.notes_written.append(target)
            self.consume(reader.reads, len(self.run.record))

        task = asyncio.create_task(put())
        self.write_reads[task] = (
            self._reads(*(v for _, v in node.value.args))
            if producer is not None
            else self._reads(node.value),
            prior,
        )
        self.writes[node.key] = task
        self.tasks.append(task)


class _Reader:
    """One node's reads while its expressions are computed: each note read once, so one expression
    sees one version (ADR-0008/C3), and the program's results it read."""

    def __init__(self, walk: _Walk, writes: dict[str, asyncio.Task] | None = None):
        self.walk = walk
        self.writes = dict(walk.writes if writes is None else writes)
        self.reads: set[str] = set()
        self.notes: dict[str, Any] = {}

    async def point(self, name: str) -> Any:
        walk = self.walk
        if name in walk.values:
            value = await walk.value(name, self.writes)
            self.reads |= walk.value_reads.get(name, set())
            return value
        task = walk.run.pending.get(name)
        if task is not None:
            await asyncio.shield(task)
        value = walk.run.resolve(name)
        if isinstance(value, Result):  # a failed command resolves to its Result
            raise EvalError(f"*{name} did not settle ok: {value.error}")
        if name in walk.reads:
            self.reads.add(name)
        return value

    async def note(self, key: str) -> Any:
        if key not in self.notes:
            write = self.writes.get(key)
            if write is not None:
                await asyncio.shield(write)
            note = self.walk.run.notes.get(key)
            if note is None:
                raise EvalError(f"note[{key!r}] is not in the store")
            self.notes[key] = note.value
        return self.notes[key]

    async def do(self, name: str, args: dict[str, Any], line: int) -> Any:
        alias = self.walk.auto()

        async def given() -> dict[str, Any]:
            return args

        await asyncio.shield(self.walk.launch(Lact(alias, name, {}), given))
        return await self.point(alias)


def _put_path(base: Any, path: tuple[str, ...], value: Any) -> Any:
    """`value` written at `path` inside a note's JSON object, the rest of it kept."""
    if not path:
        return value
    if not isinstance(base, dict):
        raise EvalError(f"the note holds {type(base).__name__} where {path[0]!r} is written, not an object")
    return {**base, path[0]: _put_path(base.get(path[0], {}), path[1:], value)}


def _nearest(name: str, names: Iterable[str]) -> tuple[str, str | None, int | None]:
    """An unknown command name, the nearest name in the subset and their edit distance."""
    best = min(names, key=lambda n: (_distance(name, n), n), default=None)
    return name, best, None if best is None else _distance(name, best)


def _distance(a: str, b: str) -> int:
    """Levenshtein: the fewest insertions, deletions and substitutions from `a` to `b`."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def _referenced(program: Program) -> set[str]:
    """What OUT{} waits for: the aliases it names, and the pointers inside its literals. A literal
    list is data, never a list of names."""
    if program.out is None:
        return {a.alias for a in program.lacts}
    refs: set[str] = set()
    for v in program.out.fields.values():
        if isinstance(v, lndl.Aliases):
            refs.update(v)
        else:
            refs.update(p.name for p in lndl.pointers_in(v))
    return refs


def _reaches(run: Run, start: str, target: str) -> bool:
    """Whether following pointers from the pending command `start` comes back to `target`."""
    seen, stack = set(), [start]
    while stack:
        a = stack.pop()
        if a == target:
            return True
        if a in seen:
            continue
        seen.add(a)
        stack.extend(n for n in run.pointing.get(a, ()) if n in run.pending)
    return False


def _validate_whole(spec: Spec, value: Any) -> BaseModel:
    """One alias holding the whole output: a Spec instance, a JSON object, or, for a Spec with one
    field, the field's value itself (text, a number, a list: `OUT{count: [a]}` with `a` a bare 3)."""
    if isinstance(value, spec):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if not isinstance(value, dict):
        fields = list(spec.model_fields)
        if len(fields) != 1:
            name = spec_name(spec)
            raise lndl.LNDLError(
                f"{name} has fields {fields}: give one <lvar {name}.<field> alias> per field and "
                f"OUT{{{name}: [the aliases]}}, or one alias holding a JSON object"
            )
        value = {fields[0]: _coerce(spec, fields[0], value)}
    return spec.model_validate(value)


def _coerce(spec: Spec, fld: str, value: Any) -> Any:
    """Text arriving for a field. Kept as written wherever the field takes text (`str`, `str | int`,
    a Literal of strings): a quoted value in a dict literal means the string. Read as a JSON or
    Python literal only for a field that cannot take text at all (a list, a number, a bool: what a
    namespaced lvar holds when its field is not text). The Spec stays the validator (ADR-0001/C1)."""
    info = spec.model_fields.get(fld)
    if info is None or not isinstance(value, str):
        return value
    if info.annotation is not Any:
        try:
            _adapter(spec, fld).validate_python(value, strict=True)
        except ValidationError:
            pass
        else:
            return value
    for reader in (json.loads, ast.literal_eval):
        try:
            return reader(value)
        except (ValueError, SyntaxError, TypeError):  # TypeError: a literal with an unhashable key
            continue
    return value


@functools.lru_cache(maxsize=512)
def _adapter(spec: Spec, fld: str) -> TypeAdapter:
    return TypeAdapter(spec.model_fields[fld].annotation)


def _output_guidance(emits: tuple[Spec, ...]) -> str:
    lines = [
        "OUTPUT",
        "Finish by filling these with OUT{}. For each output, one <lvar name.field alias> per "
        "field (a list or number field takes a JSON literal), or one <lact name.field alias> whose "
        "result fills the field, then OUT{name: [the aliases]}; or give the whole output as a dict "
        "literal, OUT{name: {field: value, ...}}. An output with one field also takes the value "
        'directly: OUT{name: 3} or OUT{name: ["a", "b"]}. A field is required unless it shows a '
        "default.",
    ]
    for spec in emits:
        lines.append(render_guidance(spec))
    return "\n".join(lines)


async def _settle(tasks: list[asyncio.Task]) -> None:
    """Wait for the turn's commands. A cancelled command has settled itself as a RESULT; any other
    way out of a command task is the runtime's own defect and is raised, never dropped."""
    if tasks:
        for r in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                raise r


def _hook_name(fn: Any) -> str:
    return getattr(fn, "__name__", None) or type(fn).__name__


def _validation_text(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in e.errors()
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # ValueError: a value that refers to itself
        return str(value)


def _deref(run: Run, key: str, value: Any) -> Any:
    """Pointers inside an OUT literal resolve to what they name; one that names nothing is an error."""
    if isinstance(value, Pointer):
        try:
            return run.resolve(value.name)
        except KeyError:
            raise lndl.LNDLError(f"OUT{{{key}: ...}}: {value.name!r} was never declared") from None
    if isinstance(value, dict):
        return {k: _deref(run, key, v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_deref(run, key, v) for v in value]
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _typename(value: Any) -> str:
    if isinstance(value, str):
        return f"str[{len(value)}]"
    if isinstance(value, list | tuple | dict):
        return f"{type(value).__name__}[{len(value)}]"
    return type(value).__name__


def _tokens(messages: list[Message]) -> int:
    return sum(len(m.content) for m in messages) // 4


def _shown(view: list[ViewEntry]) -> set[int]:
    """The results the model can read in this view: not hidden, not standing under a summary."""
    return {v.entry.seq for v in view if v.entry.kind is Kind.RESULT and not v.hidden and v.summary is None}


def _messages(view: list[ViewEntry]) -> list[Message]:
    """The view as messages; a run of hidden results is one placeholder line."""
    messages: list[Message] = []
    i = 0
    while i < len(view):
        if view[i].hidden:
            j = i
            while j < len(view) and view[j].hidden:
                j += 1
            messages.append(_placeholder([v.entry for v in view[i:j]]))
            i = j
        else:
            messages.append(_message(view[i]))
            i += 1
    return messages


def _placeholder(entries: list[Entry]) -> Message:
    """Hidden entries, named with their size, so the model knows what it can reopen."""
    parts = []
    for e in entries:
        size = _tokens([_message(ViewEntry(e))])
        if e.kind is Kind.RESULT:
            res: Result = e.content
            parts.append(f"{e.name} {e.alias} {res.spec} ~{size} tokens")
        else:
            parts.append(f"{e.name} {e.kind.name.lower()} ~{size} tokens")
    return Message(
        "user", "[hidden; *name or context.show reopens: " + ", ".join(parts) + "]", name=entries[0].name
    )


def _message(v: ViewEntry) -> Message:
    e = v.entry
    if v.summary is not None:
        return Message("user", f"[summary before {e.name}] {v.summary}", name=e.name)
    if e.kind is Kind.SYSTEM:
        # the runtime's own fold rides a notification; a session-keeping backend reads the mark
        fold_mark = "fold" if isinstance(e.meta.get("directive"), Directive) else None
        return Message("system", e.content, name=e.name, spec=fold_mark)
    if e.kind is Kind.TEXT:
        return Message("assistant", e.content, name=e.name)
    if e.kind is Kind.RESULT:
        res: Result = e.content
        body = _text(res.value) if res.ok else f"error: {res.error}"
        return Message("user", f"[{e.name} {e.alias} {res.spec}] {body}", name=e.name, spec=res.spec)
    label = e.name
    if e.meta.get("inbound"):
        label += f" from {e.meta['origin']}"
    return Message("user", f"[{label}] {_text(e.content)}", name=e.name)
