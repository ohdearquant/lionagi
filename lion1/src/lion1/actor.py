"""The actor, its profiles, its handlers, and the turn loop (ADR-0001, ADR-0002, ADR-0005,
ADR-0006, ADR-0010).

An actor is one addressable principal: its handlers are its Operable, and it has the inbox, the
privileges and the notes. A profile is how the model is configured when the actor runs. A run is
turns: one SYSTEM notification, one model call, extract, gate, dispatch, settle.
Nothing a handler does is awaited by the loop inline; the next turn starts when what the model
asked for has settled.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import functools
import inspect
import json
import math
import re
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from . import lndl
from .bus import Bus
from .lndl import (
    LIMIT,
    Branch,
    Command,
    EvalError,
    Lact,
    Lvar,
    Pointer,
    Program,
    Script,
    Value,
    _size,
    evaluate,
    note_keys,
    pointers,
    program_guidance,
)
from .lndl import (
    Output as ProgramOutput,  # OUT{} inside a fenced program; `Output` below is the out command's request
)
from .lndl import check as check_script
from .lndl import read as read_script
from .notes import FileNoteStore, MemoryNoteStore, Note, NoteStore, key_of
from .record import Directive, Entry, Kind, Record, ViewEntry, fold
from .spec import Operable, Spec, field_keys, render_guidance, spec_name

__all__ = (
    "Message",
    "Reply",
    "SessionPlan",
    "Backend",
    "Profile",
    "HandlerFn",
    "BeforeFn",
    "AfterFn",
    "Handler",
    "ProfileError",
    "JobError",
    "Result",
    "Success",
    "Continue",
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
    "NoteRead",
    "RunMore",
    "DEFAULT_HANDLERS",
    "Output",
    "Actor",
)

# --- what a backend sees ---------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant
    content: str
    name: str | None = None  # the record entry this message renders, when it renders one
    spec: str | None = None  # for a RESULT that landed: its command (a failed one changed nothing)
    run: str | None = None  # the run whose record this renders: a backend keeping sessions keys on it
    # a stand-in for entries the view no longer shows whole: a placeholder or a summary. A session that
    # still holds the entry under this name holds more than the view, which it may keep (ADR-0009/C4)
    folded: bool = False


class Reply(str):
    """A backend's text with the provider's prompt count for the call (fresh input, cache read and cache
    write), None when the provider did not say; the actor anchors its `context:` figure on it. `cost` is
    what the call cost when the provider says, summed by the run against its `cost_budget`. With
    `refused`, the backend would not hand the text on as a turn: none of it runs and the model is told why."""

    prompt_tokens: int | None
    cost: float | None
    refused: str | None

    def __new__(
        cls,
        text: str,
        prompt_tokens: int | None = None,
        cost: float | None = None,
        refused: str | None = None,
    ) -> Reply:
        reply = super().__new__(cls, text)
        reply.prompt_tokens = prompt_tokens
        reply.cost = cost
        reply.refused = refused
        return reply


@dataclass(frozen=True)
class SessionPlan:
    """What a backend keeping a session will do with the view it is about to get: its `plan(view,
    forced=..., run=...)` answers before the notification is written, so the notification says it
    (ADR-0009/C4). `restart`: the session starts over from the view, or from a point it passed
    before the view changed. `frees`: estimated tokens the session holds that the view does not show,
    which a restart drops. `tokens`: the prompt count the call is expected to stand in, from the
    backend's own last count, or None when it has none."""

    restart: bool
    frees: int
    tokens: int | None


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
    # the role's values for the job's bounds, taken where the job leaves one unset (ADR-0003/C1)
    defaults: dict[str, float] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        # the name is the notes file's name under `notes_dir`: one file per profile, none outside it
        if not _PROFILE_NAME.fullmatch(self.name) or ".." in self.name:
            raise ProfileError(f"profile name {self.name!r}: letters, digits, _ . - only, it names a file")
        if self.language not in ("core", "program"):
            raise ProfileError(f"profile language {self.language!r}: core or program")
        unknown = sorted(set(self.defaults) - set(_BOUNDS))
        if unknown:
            raise ProfileError(f"profile {self.name!r} defaults name no bound: {unknown}")


_PROFILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")  # whole: `$` matches before a final newline


HandlerFn = Callable[[BaseModel, "Context"], "Awaitable[Any] | Any"]
# a hook before a command runs, on its own copy of the request: None lets it run, a str refuses it with
# that reason, a dict or an instance of the spec replaces its arguments, rebuilt from its fields, and a
# model of another class refuses it; a hook that raises refuses it (a gate that fails admits nothing)
BeforeFn = Callable[
    ["Run", Lact, BaseModel], "Awaitable[str | BaseModel | dict | None] | str | BaseModel | dict | None"
]
# a hook after a command settled, ok or not, on its own copies: None keeps the result, a Result replaces
# it; a hook that raises is told in the next notice and the result stands (the command already ran)
AfterFn = Callable[["Run", Lact, BaseModel, "Result"], "Awaitable[Result | None] | Result | None"]

_EFFECTS = ("write", "read", "pure")  # what a handler does to live state, for the whole invocation


@dataclass(frozen=True)
class Handler:
    spec: Spec
    fn: HandlerFn
    requires: frozenset[str] = frozenset()
    # write: waits for every earlier command of its turn but the pure ones; read: for the nearest
    # earlier write; pure: for its pointers only (ADR-0005/C6)
    effect: str = "write"

    def __post_init__(self) -> None:
        if self.effect not in _EFFECTS:
            raise ValueError(f"handler effect {self.effect!r} is not one of {', '.join(_EFFECTS)}")

    @property
    def name(self) -> str:
        return spec_name(self.spec)


class ProfileError(Exception):
    """A profile asks for what its actor does not have."""


class JobError(Exception):
    """A job the run refuses before any backend call: its view is over its `context_budget`."""


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
class Exhausted:
    """A bound was spent at a turn's start, `turns` turns in: `reason` names it, turns (max_turns),
    idle (max_idle), time (time_budget), cost (cost_budget) or context (context_budget)."""

    turns: int
    reason: str = "turns"


@dataclass(frozen=True)
class Refused:
    """OUT{} was refused by the request's accept gate `times` times in a row; the model was not converging."""

    why: str
    times: int


@dataclass(frozen=True)
class Stopped:
    turn: int


Outcome = Success | Continue | Exhausted | Refused | Stopped

_CALL_SHAPED = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*\(.*\)\s*$", re.DOTALL)  # `name(...)` as a whole value
# the notification's own opening tag, in any case or spacing after `system`, its `<` as itself or `&lt;`
_SYSTEM_FRAME = re.compile(r"(?:<|&lt;)system\s+turn\s*=", re.IGNORECASE)
_BREAK = re.compile(r"\r\n|\r|\n")


# the runtime's value for each bound a job and its profile leave unset (ADR-0003/C1); None: unbounded
_BOUNDS: dict[str, float | None] = {
    "max_turns": 100,
    "max_idle": 3,
    "time_budget": None,
    "cost_budget": None,
    "context_budget": 200_000,
    "max_refusals": 3,
}


_NOTES_TOLD = 10  # the first notification names this many of the notes already kept, then counts the rest
_TOKEN = re.compile(r"[\w.:/-]+")  # a store's key or run id written as itself; anything else is quoted


def _said(text: str) -> str:
    """A key or run id a note store supplied, as the notification writes it: a plain token as itself,
    anything else by its repr, so none breaks the line, reads as another entry, or runs past 60."""
    text = str.__str__(text)  # the plain string: a subclass's own formatting never runs
    shown = text if _TOKEN.fullmatch(text) else repr(text)
    return shown if len(shown) <= 60 else shown[:57] + "..."


def _grant(run: Run, bound: str, amount: float, reason: str) -> bool:
    return True


@dataclass(frozen=True, kw_only=True)  # every field by name: the bounds are many and read alike
class RunRequest:
    inputs: tuple[str, ...] = ()
    emits: tuple[Spec, ...] = ()  # what OUT{} fills; empty: OUT{} ends the run with its dereferenced fields
    # the bounds; one left None takes the profile's `defaults`, then the runtime's (`_BOUNDS`)
    max_turns: int | None = None
    max_idle: int | None = None  # consecutive turns without LNDL
    time_budget: float | None = None  # seconds of wall clock for the whole run
    cost_budget: float | None = None  # what the backend's replies report the calls cost, summed
    context_budget: int | None = None  # tokens, the notification's `context:` figure
    # the system's gate on OUT{} beyond validation: returns why the output is not acceptable, or None;
    # the last before hook of `out`, and a refusal is told to the model and the run continues
    accept: Callable[[Run, Any], Awaitable[str | None] | str | None] | None = None
    max_refusals: int | None = None  # consecutive refusals of OUT{} before the run ends as Refused
    # answers `run.more(bound, amount, reason)`: truthy grants, and the bound rises by the amount
    extend: Callable[[Run, str, float, str], Awaitable[bool] | bool] = _grant
    # the view folds once, to about half, when it measures past this (tokens, estimated); until then
    # and after, nothing leaves it but by the model's own hides
    view_budget: int = 60_000
    # a conversation: called at the start of a turn after one that ran nothing, so the model's
    # plain text was its reply and the next turn takes what the other side says. It queues the next
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
        while self.id in actor.runs:  # an answer finds a live run by this id: no two share one
            self.id = uuid.uuid4().hex[:8]
        self.actor = actor
        self.profile = profile
        self.request = request
        self.record = Record(self.id)
        # each bound resolved once, here: the job's value, else the profile's default, else the
        # runtime's; the checks read these, `run.more` raises them, nothing reads the profile's again
        self.bounds: dict[str, float | None] = {
            name: next(
                (v for v in (getattr(request, name), profile.defaults.get(name)) if v is not None), value
            )
            for name, value in _BOUNDS.items()
        }
        self.extending = asyncio.Lock()  # `run.more` asks, answered one at a time in the order they came
        self.cost = 0.0  # what the backend's replies reported, summed
        self.context_figure = 0  # the `context:` figure the last notification told
        self.started = time.monotonic()
        self.turn = 0
        self.values: dict[str, tuple[int, Any]] = {}  # alias -> (seq of the TEXT that declared it, value)
        self.fields: dict[str, str] = {}  # alias -> the output field it was declared for, for the whole run
        self.pending: dict[str, asyncio.Task] = {}
        self.tasks: set[asyncio.Task] = set()  # every command and note-write task no settle saw through
        # the time limits of the walks and settles waiting now, which a grant of time by `run.more` moves
        self.timeouts: set[asyncio.Timeout] = set()
        self.entered: set[str] = set()  # pending aliases whose handler has been called
        # pending alias -> the notes its command read from the store, each read once (None: absent)
        self.reads: dict[str, dict[str, Note | None]] = {}
        self.pointing: dict[str, set[str]] = {}  # command alias -> the names its arguments point at
        self.waits: dict[str, set[str]] = {}  # command alias -> the earlier commands its effect waits for
        self.settled: list[str] = []  # aliases settled since the last notification
        self.refused: list[tuple[str, str]] = []  # (alias, why) since the last notification
        self.failed: list[tuple[str, str]] = []  # (alias, why) whose handler started, not succeeding
        self.unknown: list[tuple[str, str]] = []  # (alias, why) cancelled while their handler ran
        self.notes_written: list[str] = []
        self.hook_errors: list[tuple[str, str]] = []  # (alias, what) an after hook raised, this turn
        self.consumed: list[Inbound] = []  # inbound items this run took as INPUT
        self.inbox: list[Inbound] = []  # answers addressed to this run, taken at its next turn
        self.asked: dict[str, tuple[str, asyncio.Future]] = {}  # handle id -> (alias, the answer)
        self.retry_error: str | None = None
        self.idle_note: str | None = None  # the last turn carried no LNDL; told once
        self.idle_turns = 0  # consecutive turns without LNDL
        self.replied = (
            False  # under a conversation (request.wait), the last turn was the reply: plain text, or
            # commands that all landed (the work was done on the model's own tools; the turn carried
            # what goes through the runtime: sends, notes, context operations)
        )
        self.pause = False  # hand the conversation back at the next turn boundary (a person's interrupt)
        self.call: asyncio.Future | None = None  # the backend call in flight, what `interrupt` cuts
        self.cut = False  # `interrupt` cancelled the call: the turn ends with no message, not the run
        self.same_turns = 0  # consecutive turns byte-identical to the one before
        self.same_errors = 0  # consecutive turns ending in the same retry error
        self.viewed: set[int] = set()  # RESULT seqs in view at the last render
        self.view_est = 0  # the view the last notification measured, post-fold (tokens, estimated)
        self.anchor: tuple[int, int] | None = None  # (prompt tokens a backend reported, the view_est sent)
        self.grown = (0, 0)  # (reported, estimated) growth summed over counted calls whose view grew
        # the least a reported count stood above the view sent: the backend's own context beyond the
        # view (system prompt, tools), which no fold takes off; what it later stands above that is
        # history the record never saw (a session-keeping CLI's tool turns), which a fold does
        self.floor: int | None = None
        # a backend that keeps a session says what it will do with the view (`SessionPlan`); the
        # notification tells it, and what the session still holds beyond the view once told
        self.plan: Callable[..., SessionPlan] | None = None
        self.frees_told = 0
        self.refusals = 0  # consecutive refusals of OUT{}
        self.repairs: Counter[str] = Counter()  # repairs the reading applied to this run's turns, by name
        # each unknown command name, the nearest name the profile has and the edit distance between them:
        # what a name repair would have done, measured while it is off (ADR-0008/D2)
        self.misnamed: list[tuple[str, str | None, int | None]] = []
        self.thunks: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}  # a program command's arguments
        # asked as a command's handler is about to start, any one true refuses it whatever a later grant
        # raised: its walk's limits, or a grant that landed after the deadline while it was pending
        self.halts: dict[str, list[Callable[[], bool]]] = {}
        # results a program consumed, each with the seq it was consumed at, hidden by the next notification
        self.intermediates: dict[str, int] = {}
        # a program's values not computed yet, for the whole run: the seq of the TEXT that bound it and
        # its expression; a value nothing has read stays bound (ADR-0008/C6)
        self.unread: dict[str, tuple[int, ast.expr]] = {}
        self.computed: list[str] = []  # program values computed since the last notification, which keeps them
        self.bound: list[str] = []  # program values bound since the last notification, which carries them
        self.outcome: Outcome | None = None
        self.output: Any = None

    def interrupt(self) -> bool:
        """A person's interrupt. A backend call in flight is cut now and the conversation is theirs
        (True); otherwise the turn in flight finishes and it is theirs at its end (False)."""
        self.pause = True
        if self.call is not None and not self.call.done():
            self.cut = True
            self.call.cancel()
            return True
        return False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def notes(self) -> NoteStore:
        return self.actor.notes_for(self.profile)

    def resolve(self, name: str, reads: dict[str, Note | None] | None = None) -> Any:
        """Dereference a pointer: an alias, a runtime name, or a note. KeyError when nothing has that name.
        With `reads`, a command's own, a note comes from the store once and every later read is that one."""
        if name in self.values:
            return self.values[name][1]
        entry = self.record.get(name)
        if entry is not None:
            if entry.kind is Kind.RESULT:
                return entry.content.value if entry.content.ok else entry.content
            return entry.content
        key = key_of(name)
        if key is not None:
            note = reads[key] if reads is not None and key in reads else self.notes.get(key)
            if reads is not None:
                reads[key] = note
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
        return self.run.resolve(name, self.run.reads.get(self.alias))

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


class NoteRead(BaseModel):
    """Read one note of another profile whole: its value, version and the run that wrote it. Only a
    profile holding the note.read privilege may."""

    __spec_name__ = "note.read"
    profile: str
    key: str


class RunMore(BaseModel):
    """Ask for more of a bound before it is spent; the job decides. A grant raises the bound from the
    next check on; a refusal leaves it as it was, and the run ends when it is spent."""

    __spec_name__ = "run.more"
    bound: Literal["max_turns", "max_idle", "time_budget", "cost_budget", "context_budget", "max_refusals"]
    amount: float = Field(gt=0)
    reason: str


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
            "preview": (text := _text(n.value))[:120],
            "size": len(text),
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


async def _note_read(req: NoteRead, ctx: Context) -> dict:
    name, actor = Profile(req.profile).name, ctx.actor
    # the store kept under that name, or its file; a read makes none, so a name with neither stays
    # without one (a file store loads its file on every read, so the one made here is not kept)
    store = actor._notes.get(name)
    if store is None and actor.notes_dir is not None:
        store = FileNoteStore(actor.notes_dir / f"{name}.json")
    n = store.get(req.key) if store is not None else None
    if n is None:
        raise KeyError(f"no note {req.key!r} in the notes of {req.profile}")
    return {"profile": req.profile, "key": n.key, "value": n.value, "run": n.run, "version": n.version}


async def _more(req: RunMore, ctx: Context) -> str:
    run = ctx.run
    async with run.extending:  # the next ask is judged against the total this one leaves
        now = run.bounds[req.bound]
        if now is None:
            raise ValueError(f"{req.bound} is not bounded in this run; there is nothing to raise")
        # a count rises by whole turns, refusals or tokens; seconds and cost keep their fraction
        amount = req.amount if req.bound in ("time_budget", "cost_budget") else math.ceil(req.amount)
        granted = run.request.extend(run, req.bound, amount, req.reason)
        if inspect.isawaitable(granted):
            granted = await granted
        if not granted:
            raise PermissionError(f"{req.bound} stays at {now}: the job did not grant {amount} more")
        late = req.bound == "time_budget" and run.elapsed >= now
        run.bounds[req.bound] = now + amount
        if late:
            # the deadline passed before the grant landed: it raises the bound for what follows the turn, and
            # each command the turn still has pending, waited for or not, is refused if its handler has not
            # started; one already in its handler is settled by the turn's wait or the run's end
            for alias in run.pending:
                if alias != ctx.alias:
                    run.halts.setdefault(alias, []).append(lambda: True)
        if req.bound == "time_budget":  # a walk or a settle waiting now runs to the new deadline
            clock = asyncio.get_running_loop().time()
            for limit in run.timeouts:
                if limit.expired():  # one that fired has cancelled its walk already
                    continue
                when = limit.when()
                if when is not None and clock >= when:
                    # the deadline passed while a synchronous grant held the loop: the limit fires now, as
                    # the loop would have, and the grant lands after it
                    limit.fire()
                    continue
                limit.reschedule(_deadline(run))
    return f"{req.bound} raised from {now} to {now + amount}"


DEFAULT_HANDLERS: tuple[Handler, ...] = (
    Handler(ContextHide, _hide),
    Handler(ContextShow, _show),
    Handler(ContextSummarize, _summarize),
    Handler(NoteList, _note_list),
    Handler(NoteFind, _note_find),
    Handler(NoteGet, _note_get),
    Handler(NoteDelete, _note_delete),
    Handler(NoteRead, _note_read, frozenset({"note.read"})),
    Handler(RunMore, _more),
)


class Output(BaseModel):
    """What OUT{} filled, assembled and validated: the `out` command's request, which a before hook may
    rewrite and the job's `accept` judges last (ADR-0003/C4). OUT{} is how the model asks for it, so
    the Operable does not list it as a command."""

    __spec_name__ = "out"
    fields: dict[str, Any]


def _held(run: Run, fields: dict[str, Any]) -> dict[str, Any]:
    """OUT{} held to the run's outputs: a field off the list, a required output missing or a value
    its output refuses is an error. The assembled fields and a before hook's rewrite of them go
    through the same rule, so a rewrite cannot hand back what the model could not (ADR-0003/C4)."""
    emits = {spec_name(s): s for s in run.request.emits}
    if emits and set(fields) - set(emits):
        raise ValueError(f"OUT{{}} holds {sorted(set(fields) - set(emits))}; the outputs are {sorted(emits)}")
    missing = [
        n
        for n, s in emits.items()
        if n not in fields and any(f.is_required() for f in s.model_fields.values())
    ]
    if missing:
        raise ValueError(f"OUT{{...}} must fill: {missing}")
    return {
        k: v if (spec := emits.get(k)) is None or isinstance(v, spec) else spec.model_validate(v)
        for k, v in fields.items()
    }


async def _out(req: Output, ctx: Context) -> dict[str, Any]:
    return _held(ctx.run, req.fields)


_OUT = Handler(Output, _out)


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
        self.inbox: list[Inbound] = []  # unaddressed inbound, taken by the next run that starts a turn
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
            handler = Handler(
                spec,
                handler,
                frozenset(getattr(handler, "requires", ())),
                getattr(handler, "effect", "write"),
            )
        name = self.operable.add(handler.spec)
        self.handlers[name] = handler
        return handler

    def handler(
        self, spec: Spec, *, requires: Iterable[str] = (), effect: str = "write"
    ) -> Callable[[HandlerFn], HandlerFn]:
        def deco(fn: HandlerFn) -> HandlerFn:
            self.register(Handler(spec, fn, frozenset(requires), effect))
            return fn

        return deco

    def section(
        self, fn: Callable[[Run], Awaitable[str | None] | str | None]
    ) -> Callable[[Run], Awaitable[str | None] | str | None]:
        """A contributor to the notification: called every turn, awaited if it must look somewhere,
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
        the asking run's next turn starts when the answer is in. Running the other actor is not this
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
        run.plan = getattr(backend, "plan", None)
        subset = self.subset(profile)
        for e in request.history:
            replayed = run.record.append(e.kind, e.content, alias=e.alias, turn=e.turn, **e.meta)
            if e.kind is Kind.TEXT and profile.language == "core":  # the values that turn declared, admitted
                # against the record so far; a program's prose is scratch and its turn is folded, not rerun
                if replayed.meta.get("unread"):  # the live loop refused it: nothing of it was admitted
                    continue
                try:
                    program = _read(str(e.content), refused=e.meta.get("refused"))
                except lndl.LNDLError as err:
                    # refused by today's reader and not by the one that ran it: what that reader
                    # admitted rides the error, or nothing does and the turn was refused then too
                    program = getattr(err, "program", None)
                    if program is None:
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
            while True:
                spent = _spent(run)  # the turn's first act: every bound, as resolved or raised
                if spent is not None:
                    run.outcome = Exhausted(run.turn, spent)
                    break
                turn_no = run.turn = run.turn + 1
                if request.wait is not None and (run.replied or run.pause) and not run.pending:
                    run.replied = run.pause = False
                    if not await request.wait(run):
                        run.turn = turn_no - 1
                        run.outcome = Stopped(turn_no)
                        break
                    if (over := _over(run)) is not None:  # the wait may have outlasted the time left
                        run.turn = turn_no - 1
                        run.outcome = Exhausted(run.turn, over)
                        break
                self._take_inbox(run)
                text, folded = await self._notify(run)
                notice = run.record.append(Kind.SYSTEM, text, turn=turn_no)
                if folded is not None:  # the fold event rides its own notification, at its seq
                    notice.meta["directive"] = Directive(
                        folded.op, folded.refs, folded.text, at=notice.seq, seqs=folded.seqs
                    )
                if turn_no == 1 and run.context_figure > run.bounds["context_budget"]:
                    # over before any backend call: the job is the caller's error, not a run that ended
                    raise JobError(
                        f"the job's view is ~{run.context_figure} tokens before any call, over its "
                        f"context_budget of {run.bounds['context_budget']}"
                    )
                if run.intermediates:  # the hide of what a program consumed, dated at each consumption
                    notice.meta["consumed"] = _consumed(run)
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
                    run.turn = turn_no - 1
                    run.outcome = Stopped(turn_no)
                    break
                self.bus.emit("turn.started", run=run.id, turn=turn_no)
                messages = self._render(run, subset)
                text = backend(messages)
                if inspect.isawaitable(text):
                    run.call = asyncio.ensure_future(text)
                    task = asyncio.current_task()  # there is one: awaiting a future needs a task
                    asked = task.cancelling()
                    try:
                        text = await run.call
                    except asyncio.CancelledError:
                        # the run itself is being cancelled: no cut, or a cancel of the run's task
                        # that arrived with one. Both reach here as one error; the task's count of
                        # cancel requests is what tells them apart, and the run's own wins
                        if not run.cut or task.cancelling() > asked:
                            raise
                        run.cut = False
                        run.record.append(
                            Kind.SYSTEM,
                            "the person cut this turn short: its call was abandoned, no message came of it",
                            turn=turn_no,
                        )
                        continue
                    finally:
                        run.call = None
                reported = getattr(text, "prompt_tokens", None)
                cost = getattr(text, "cost", None)
                refused = getattr(text, "refused", None)
                if isinstance(text, str):
                    text = str(text)  # a Reply's count and cost are read; the record keeps a plain str
                if isinstance(cost, int | float) and not isinstance(cost, bool):
                    run.cost += cost
                elif cost is not None:  # a cost that is not a number reads as spent, never as room
                    run.cost = math.nan
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
                m = run.record.append(
                    Kind.TEXT, text, turn=turn_no, **({"refused": refused} if refused else {})
                )
                run.same_turns = (
                    run.same_turns + 1 if previous is not None and previous.content == text else 0
                )
                try:
                    program = _read(text, run.profile.language, refused)
                    run.repairs.update(program.repairs)  # applied by the reading, whatever the check says
                    if isinstance(program, Script):  # whole, before anything runs (ADR-0008/C1)
                        check_script(program, subset.names(), run.known)
                except lndl.LNDLError as e:
                    m.meta["unread"] = True  # a resumed log reads the refusal, not the turn again
                    run.idle_turns = 0  # a turn refused whole held something: it was not idle
                    run.repairs.update(getattr(e, "repairs", ()))  # the readings before the one that failed
                    run.misnamed += [_nearest(name, subset.names()) for name in getattr(e, "names", ())]
                    self._retry(run, str(e))
                    self.bus.emit("turn.retry", run=run.id, turn=turn_no, error=str(e))
                    continue
                run.idle_note = None
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
                failed = None
                if isinstance(program, Script):
                    walk = _Walk(self, run, subset, m.seq)
                    try:
                        found = await walk.execute(program)
                    except lndl.LNDLError as e:  # the output could not be computed: a retry (ADR-0008/C7)
                        found, failed = None, str(e)
                    # the turn's class is what its walk did, never the program's text: a command launched,
                    # one computing an earlier turn's value ran included, or OUT{} reached (ADR-0002/C1)
                    acted, spoke = walk.acted, bool(program.body)
                    program = Program(lacts=walk.lacts, out=None if found is None else lndl.Out(found))
                    referenced: set[str] = set()
                    tasks = walk.tasks
                else:
                    acted = bool(program.lacts) or program.out is not None
                    spoke = acted or bool(program.lvars)
                    self._declare(run, program, m.seq)
                    referenced = _referenced(program)
                    tasks = self._dispatch(run, program, subset)
                if request.wait is not None and not acted:
                    run.replied = True  # the reply; the other side speaks next
                elif not spoke:
                    run.idle_turns += 1
                    stray = lndl.stray_tags(text, subset.names())
                    run.idle_note = "your last turn contained no LNDL, so nothing was executed" + (
                        f"; <{stray[0]}> is not a command call, write <lact a1>{stray[0]}(...)</lact>"
                        if stray
                        else ""
                    )
                    if run.idle_turns >= 2:
                        run.idle_note += (
                            "; there is no tool-call API in this conversation, the tags are literal text "
                            'in your reply, for example <lact a1>list_dir(path=".")</lact>'
                        )
                else:
                    run.idle_turns = 0
                if failed is not None:
                    self._retry(run, failed)
                    self.bus.emit("turn.retry", run=run.id, turn=turn_no, error=failed)
                    continue
                if program.out is None:
                    await _settle(run, tasks)
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
                # OUT{} waits for what it points at still running, and for nothing else: an accepted
                # output ends the run and the reap takes the rest; one not accepted settles the turn
                await _settle(run, [run.pending[a] for a in referenced if a in run.pending])
                try:
                    run.output = self._assemble(run, program)
                except lndl.LNDLError as e:
                    await _settle(run, tasks)
                    # the error clears only once the whole turn, assembly included, read: a finish
                    # that fails the same way every turn is counted as the same error (ADR-0002/C3)
                    self._retry(run, str(e))
                    self.bus.emit("turn.retry", run=run.id, turn=turn_no, error=str(e))
                    continue
                # OUT{} is the `out` command: the actor's before hooks see the assembled output, and
                # the job's `accept` is the last of them, so it judges what they left (ADR-0003/C4)
                befores = list(self.befores)
                if request.accept is not None:

                    async def judge(run: Run, la: Lact, out: Output, accept=request.accept) -> Any:
                        # accept judges the rewrite held and validated, and what it approved is
                        # what leaves: a rewrite the outputs refuse never reaches it
                        try:
                            fields = _held(run, out.fields)
                        except ValueError as held:
                            return str(held)
                        verdict = accept(run, fields)
                        if inspect.isawaitable(verdict):
                            verdict = await verdict
                        return verdict if isinstance(verdict, str | BaseModel) else Output(fields=fields)

                    judge.__name__ = _hook_name(request.accept)
                    befores.append(judge)
                result, _ = await self._handle(
                    run, Lact("OUT", "out", {}), _OUT, Output(fields=run.output), befores, {}
                )
                run.entered.discard("OUT")
                if not result.ok:  # told as any refusal is, and the run goes on; the turn settles
                    await _settle(run, tasks)
                    run.output = None
                    run.refusals += 1
                    why = result.error.removeprefix("refused: ")
                    if run.refusals >= run.bounds["max_refusals"]:
                        run.outcome = Refused(why, run.refusals)
                        break
                    self._retry(run, f"OUT{{}} {result.error}")
                    self.bus.emit("turn.retry", run=run.id, turn=turn_no, error=why)
                    continue
                run.output = result.value
                run.retry_error, run.same_errors = None, 0
                run.outcome = Success(run.output)
                break
            await _reap(run)
            run.record.append(Kind.SYSTEM, _closure(run), turn=run.turn)
        except BaseException as e:
            # a backend that raised ends this run; whoever queued work here and is waiting on the
            # answer hears that, with the error, or its own command waits forever. The run has no
            # outcome (ADR-0002/C4): the Continue a settled turn left is not its end
            run.outcome = None
            try:
                cancelled = await _reaped(run)
            finally:
                self.runs.pop(run.id, None)
                self._deliver(run, error=e)
            if cancelled and not isinstance(e, asyncio.CancelledError):
                raise asyncio.CancelledError from e
            raise
        finally:
            for _alias, answer in run.asked.values():
                if not answer.done():
                    answer.cancel()

        self.runs.pop(run.id, None)
        self._deliver(run)
        self.bus.emit("run.completed", run=run.id, outcome=type(run.outcome).__name__, turns=run.turn)
        return run

    # pieces of a turn

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
                turn=run.turn,
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
        # Every command in the turn runs, OUT{} or not: a model writes "edit, then done" in one turn
        # and means both (why not scratch: ADR-0002, Alternatives).
        # every command of the turn is admitted before any of them runs: under an eager task factory
        # the coroutine would otherwise run inside create_task, settle, and only then be registered as
        # pending (a second RESULT under its alias from `_never_started`), and a pointer at a later
        # command of the same turn would find nothing to wait for
        admitted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tasks = []
        effectful: list[str] = []  # this turn's write and read commands so far, in source order
        last_write: str | None = None
        try:
            for la in program.lacts:
                if la.alias.startswith("auto_"):
                    # a generated alias (native tool-call syntax, no name given) may recur verbatim in a
                    # later turn; the model never chose it, so it is not held to uniqueness
                    alias, n = la.alias, run.record.seq
                    while run.record.has_alias(alias) or alias in run.pending or alias in run.values:
                        alias, n = f"{la.alias}_{n}", n + 1
                    la = dataclasses.replace(la, alias=alias)
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
                # an unknown command has no handler and nothing to wait for: it settles at once
                effect = self.handlers[la.name].effect if la.name in subset else "pure"
                if effect == "write":
                    run.waits[la.alias] = set(effectful)
                elif effect == "read":
                    run.waits[la.alias] = {last_write} if last_write is not None else set()
                if effect != "pure":
                    effectful.append(la.alias)
                if effect == "write":
                    last_write = la.alias
                task = asyncio.create_task(self._perform(run, la, subset, admitted))
                task.add_done_callback(functools.partial(self._never_started, run, la))
                run.pending[la.alias] = task
                run.tasks.add(task)
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
        run.waits.pop(la.alias, None)
        run.pointing.pop(la.alias, None)
        run.thunks.pop(la.alias, None)  # its arguments were never computed: nothing holds them now
        run.halts.pop(la.alias, None)
        result = Result(la.name, False, error="cancelled before it started", reason="cancelled")
        run.record.append(Kind.RESULT, result, alias=la.alias, turn=run.turn, execution="not executed")
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
        provenance: dict[str, Any] = {}  # the request the handler entered and its return, once reached
        try:
            try:
                result, directive = await self._execute(run, la, subset, provenance)
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
                        "a result of this turn is addressable next turn",
                        reason="failed",
                    )
                    directive = None
            try:
                entry = run.record.append(
                    Kind.RESULT,
                    result,
                    alias=la.alias,
                    turn=run.turn,
                    execution=execution,
                    **provenance,
                )
            except ValueError as e:  # the alias was taken meanwhile; the call ran, its result has no name
                told.append((la.alias, str(e)))
                return
            read = {k: n.version for k, n in run.reads.get(la.alias, {}).items() if n is not None}
            if read:  # the version each note was read at: a later write is told apart
                entry.meta["notes"] = read
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
            run.waits.pop(la.alias, None)  # its rows are read by nobody once it has settled
            run.pointing.pop(la.alias, None)
            run.thunks.pop(la.alias, None)  # refused before its arguments were computed
            run.entered.discard(la.alias)
            run.reads.pop(la.alias, None)
            run.halts.pop(la.alias, None)
        if cancelled:
            raise asyncio.CancelledError

    async def _execute(
        self, run: Run, la: Lact, subset: Operable, provenance: dict[str, Any]
    ) -> tuple[Result, Directive | None]:
        if la.name not in subset:
            run.misnamed.append(_nearest(la.name, subset.names()))
            return Result(la.name, False, error=f"unknown command {la.name!r}", reason="unknown"), None
        handler = self.handlers[la.name]
        run.reads[la.alias] = {}
        prior = [run.pending[a] for a in run.waits.get(la.alias, ()) if a in run.pending]
        if prior:  # the effect wait (C6); a command it waits for settles itself, however it ends
            await asyncio.wait(prior)
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
        return await self._handle(run, la, handler, request, self.befores, provenance)

    async def _handle(
        self,
        run: Run,
        la: Lact,
        handler: Handler,
        request: BaseModel,
        befores: list[BeforeFn],
        provenance: dict[str, Any],
    ) -> tuple[Result, Directive | None]:
        """A validated request, from the privilege check on: every command's path, `out`'s too.
        `provenance` takes the request the handler entered and its return, once reached."""
        admitted = await self.gate(run, la, request, handler.requires, befores)
        if isinstance(admitted, Result):
            return admitted, None
        request = admitted
        try:
            entered = _captured(request)  # the request as the handler entered, whatever it does to its own
            provenance["request"] = entered.model_dump()
        except Exception as e:  # a computed field or a serializer raised: nothing ran, the run goes on
            error = f"invalid arguments: the request cannot be captured, {type(e).__name__}: {e}"
            return Result(la.name, False, error=error, reason="invalid"), None
        spent = _over(run)  # re-read after the pointer waits and the hooks, before the handler starts
        if spent is not None:
            bound = f"{spent}_budget"
            why = f"refused: the {bound} of {run.bounds[bound]:g} is spent"
            return Result(la.name, False, error=why, reason="refused"), None
        # its walk's fired deadline, or a grant that landed late while it was pending, refuses it
        if any(halt() for halt in run.halts.get(la.alias, ())):
            why = "refused: the time budget its turn ran under is spent; a later grant does not reach it"
            return Result(la.name, False, error=why, reason="refused"), None
        ctx = Context(run, la.alias, handler)
        run.entered.add(la.alias)  # from here the handler may have acted on the world
        try:
            value = handler.fn(request, ctx)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, Handle) and value.id in run.asked:
                # the answer, under the same alias; the ask is dropped once consumed, answer or
                # failure, so an answer that lands before the handler returns is still found here
                handle_id = value.id
                try:
                    value = await run.asked[handle_id][1]
                except Exception:
                    run.asked.pop(handle_id, None)
                    raise
                run.asked.pop(handle_id, None)
            result = Result(la.name, True, value=value)
        except Exception as e:
            result = Result(la.name, False, error=f"{type(e).__name__}: {e}", reason="failed")
        # the return before the first after hook rides the entry; each hook works on copies, so only
        # a replacement it returns changes the result, and a failed hook's copy is dropped. The out
        # command returns the record's own captures, held: taken as they are, the output holds what
        # its names resolve to (ADR-0007/C1), not a copy of it
        try:
            result = result if handler is _OUT else _captured(result)
        except Exception as e:  # a value too deep to copy, or a model holding itself: the command failed
            error = f"the result cannot be captured, {type(e).__name__}: {e}"
            result = Result(la.name, False, error=error, reason="failed")
        provenance["returned"] = dict(vars(result))
        for hook in self.afters:
            try:
                seen = hook(run, la, _captured(entered), _captured(result))
                if inspect.isawaitable(seen):
                    seen = await seen
                if isinstance(seen, Result):
                    seen = _captured(seen)
            except Exception as e:
                run.hook_errors.append((la.alias, f"{_hook_name(hook)}: {type(e).__name__}: {e}"))
                continue
            if isinstance(seen, Result) and handler is _OUT:  # the caller receives what accept approved
                run.hook_errors.append((la.alias, f"{_hook_name(hook)}: a replacement of out is refused"))
            elif isinstance(seen, Result):
                result = seen
        return result, (ctx.directive if result.ok else None)

    async def gate(
        self,
        run: Run,
        la: Lact,
        request: BaseModel,
        requires: frozenset[str],
        befores: list[BeforeFn] | None = None,
    ) -> Result | BaseModel:
        """The privilege check, then the before hooks in registration order (the actor's, or the list
        handed in: `out` adds the job's `accept` last, ADR-0003/C4): the refusal as the command's
        Result, else the request the hooks left. A session CLI's own tool call asks it too (ADR-0009/C6)."""
        if not requires <= run.profile.privileges:
            missing = sorted(requires - run.profile.privileges)
            return Result(la.name, False, error=f"{la.name} requires {missing}", reason="privilege")
        spec = type(request)
        for hook in self.befores if befores is None else befores:
            try:
                verdict = hook(run, la, _captured(request))
                if inspect.isawaitable(verdict):
                    verdict = await verdict
                # a class is no receipt: a replacement is rebuilt from its fields by the Spec itself
                if isinstance(verdict, dict | spec):
                    fields = verdict if isinstance(verdict, dict) else _fields(verdict)
                    request = spec.model_validate(fields)
            except Exception as e:
                why = f"refused: {_hook_name(hook)} failed, {type(e).__name__}: {e}"
                return Result(la.name, False, error=why, reason="refused")
            if isinstance(verdict, str):
                return Result(la.name, False, error=f"refused: {verdict}", reason="refused")
            if isinstance(verdict, BaseModel) and not isinstance(verdict, spec):
                why = (
                    f"refused: {_hook_name(hook)} returned {type(verdict).__name__}, "
                    f"neither a dict nor {spec.__name__}"
                )
                return Result(la.name, False, error=why, reason="refused")
        return request

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
                        f"*{p.name} waits on {la.alias}: a circle of pointers never settles, and a "
                        "command that writes or reads waits for earlier commands of its turn"
                    )
                await asyncio.shield(task)

        def sub(v: Any) -> Any:
            if isinstance(v, Pointer):
                return run.resolve(v.name, run.reads[la.alias])
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
                        keys = field_keys(spec)
                        out[key] = spec.model_validate(
                            {k: _coerce(spec, _field_named(keys, k), v) for k, v in value.items()}
                        )
                    elif len(spec.model_fields) == 1 or len(required) == 1:
                        out[key] = spec.model_validate(
                            _keyed(spec, {(required or list(spec.model_fields))[0]: value})
                        )
                    else:
                        raise lndl.LNDLError(
                            f"OUT{{{key}: ...}}: {key} has several fields; give a dict literal or aliases"
                        )
                except ValidationError as e:
                    raise lndl.LNDLError(f"OUT{{{key}: ...}}: {_validation_text(e)}") from None
                continue
            fields: dict[str, Any] = {}
            whole: list[Any] = []
            keys: dict[str, list[str | int]] | None = None  # taken when a field's alias first needs it
            for alias in value:
                if alias not in run.values and run.record.get(alias) is None:
                    raise lndl.LNDLError(f"OUT{{{key}: ...}}: {alias!r} was never declared")
                fld = run.fields.get(alias)
                resolved = run.resolve(alias)
                if fld is not None and spec is None:
                    fields[fld] = resolved
                elif fld is not None:
                    keys = keys if keys is not None else field_keys(spec)
                    fields[fld] = _coerce(spec, _field_named(keys, fld), resolved)
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
                        _keyed(spec, {n: _coerce(spec, n, v) for n, v in zip(names, whole, strict=True)})
                    )
                else:
                    raise lndl.LNDLError(
                        f"OUT{{{key}: ...}}: give one alias holding the whole value, or one per field as "
                        f"<lvar {key}.field alias>"
                    )
            except ValidationError as e:
                raise lndl.LNDLError(f"OUT{{{key}: ...}}: {_validation_text(e)}") from None
        try:
            return _held(run, out)
        except ValueError as e:
            raise lndl.LNDLError(str(e)) from None

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
        """The turn's notification, and the fold event when the view has outgrown its budget."""
        r = run.record
        clock = _clock(run.elapsed)
        if run.bounds["time_budget"] is not None:
            clock += f"/{_clock(run.bounds['time_budget'])}"
        lines = [f"<system turn={run.turn}/{run.bounds['max_turns']} time={clock} seq={r.seq}>"]
        # consumed since the last notification: hidden before the model sees them, folded with the hides
        # this notification will carry, so a show the program made after a consumption stands over it
        # here as it will on the record (ADR-0008/D3) and the figure counts what the backend receives
        view = self._fold(run, _consumed(run))
        used = _tokens(_messages(view))
        context_line = len(lines)
        lines.append("")  # the context figure, written once the fold below has settled the view
        folded: Directive | None = None
        # the figure the fold decides on: the view's estimate plus what the backend's last reported
        # count stood above the view beyond its floor for this run, history the record never saw
        # (ADR-0009/A3). The floor itself, the backend's fixed context, is never counted: a fold
        # cannot take it off, and counting it would fold every turn past a budget it exceeds.
        excess = 0
        if run.anchor is not None and run.floor is not None:
            excess = max(0, run.anchor[0] - run.anchor[1] - run.floor)
        measured = used + excess
        keep = used
        if measured > run.request.view_budget:
            # one fold event, to about half the budget, oldest entries first (results; under
            # fold_inputs the inputs and replies of earlier exchanges too, the latest input stays); the
            # results of the turn just run are new to the model and stay. It rides this notification
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
        if run.plan is not None:
            # a backend keeping a session says what this call will stand in: its session may still hold
            # what a directive took out of the view (a restart that does not pay yet waits), or start
            # over from it, dropping more than the view shows (ADR-0009/C4)
            post = view
            if folded is not None:
                post = self._fold(run, _consumed(run) + (dataclasses.replace(folded, at=r.seq),))
            plan = run.plan(
                [dataclasses.replace(m, run=run.id) for m in _messages(post)],
                forced=folded is not None,
                run=run.id,  # an inputless run's first view holds no message to carry it
            )
            if plan.tokens is not None:
                shown = plan.tokens
            if plan.restart:
                gone = f"; ~{plan.frees} tokens it held beyond the view are gone" if plan.frees > 0 else ""
                lines.append(f"session: starts over from this view{gone}")
                run.frees_told = 0
            elif plan.frees > 0 and plan.frees != run.frees_told:
                lines.append(
                    f"session: still holds ~{plan.frees} tokens of what you hid or summarized, counted "
                    "in context: above; they go when a restart pays for itself, at the budget fold, or "
                    "in a new session"
                )
                run.frees_told = plan.frees
        run.context_figure = shown  # what the next turn's start checks against the budget
        lines[context_line] = f"context: ~{shown} / {run.bounds['context_budget']} tokens"
        if run.retry_error:
            again = f" (the same error {run.same_errors + 1} turns running)" if run.same_errors else ""
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
        if run.turn == 1:  # the notes this profile already keeps, from earlier runs, so none goes unread
            try:
                kept = sorted(run.notes.list(), key=lambda n: n.key)  # a store need not list in order
                told = []
                for n in kept[:_NOTES_TOLD]:
                    told.append(f"{_said(n.key)} ({len(_text(n.value))} chars, run {_said(n.run)})")
            except Exception as e:  # a store that cannot be read is a line, not the run's end
                try:
                    why = repr(f"{type(e).__name__}: {e}")[1:-1]  # escaped onto one line, unquoted
                    why = why if len(why) <= 200 else why[:197] + "..."
                except Exception:  # an error that cannot be said is not said
                    why = "an error that cannot be printed"
                lines.append(f"notes kept: unknown, the store could not be read: {why}")
            else:
                if kept:
                    more = f", and {len(kept) - _NOTES_TOLD} more" if len(kept) > _NOTES_TOLD else ""
                    lines.append(f"notes kept: {', '.join(told)}{more}")
        if run.hook_errors:
            lines.append("hooks failed (the commands ran; their results stand):")
            for alias, what in run.hook_errors:
                lines.append(f"  {alias}: {what}")
        inbound = [e for e in r.of_kind(Kind.INPUT) if e.turn == run.turn and e.meta.get("inbound")]
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
        for told in (run.settled, run.refused, run.failed, run.unknown, run.notes_written, run.hook_errors):
            told.clear()
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

    def _fold(self, run: Run, consumed: tuple[Directive, ...] = ()) -> list[ViewEntry]:
        return fold(run.record, protect_input=not run.request.fold_inputs, consumed=consumed)


def _consumed(run: Run) -> tuple[Directive, ...]:
    """The hides of what a program consumed since the last notification, dated at each consumption
    (ADR-0008/D3), so a show the program made after one stands over it."""
    by_at: dict[int, list[str]] = {}
    for alias, at in run.intermediates.items():
        by_at.setdefault(at, []).append(alias)
    return tuple(
        Directive("hide", tuple(refs), at=at, seqs=tuple(run.record.get(a).seq for a in refs))
        for at, refs in sorted(by_at.items())
    )


# --- helpers -----------------------------------------------------------------------------------


def _clock(seconds: float) -> str:
    """Wall clock the way a person says it: 42s, 3m10s, 1h05m."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _spent(run: Run) -> str | None:
    """The bound spent at a turn's start, in the order the record names them (ADR-0003/C2): time,
    cost, turns, idle turns, and the context figure the last notification told; a deadline that
    falls on the last turn reads time."""
    if over := _over(run):
        return over
    if run.turn >= run.bounds["max_turns"]:
        return "turns"
    if run.idle_turns >= run.bounds["max_idle"]:
        return "idle"
    return "context" if run.context_figure > run.bounds["context_budget"] else None


def _over(run: Run) -> str | None:
    """Time or cost spent: read at a turn's start and again before each handler starts."""
    if run.bounds["time_budget"] is not None and run.elapsed >= run.bounds["time_budget"]:
        return "time"
    # written as "not under" so a cost that is not a number (nan) reads as spent, never as room
    if run.bounds["cost_budget"] is not None and not run.cost < run.bounds["cost_budget"]:
        return "cost"
    return None


_OWN = "alias {alias!r} starts with _, which the record keeps for its own names (_r41, _m12); pick another"
_TAKEN = "alias {alias!r} already names {where}; aliases are unique for the whole run"


def _read(text: str, language: str = "core", refused: str | None = None) -> Program | Script:
    """A turn as the loop reads it, live or resumed; LNDLError is what the model is told."""
    if refused:  # the backend refused the turn (a mixed response, ADR-0004/C4); the record keeps why
        raise lndl.LNDLError(refused)
    seen = "".join(c for c in text if unicodedata.category(c) != "Cf")  # zero-width characters hide nothing
    if frame := _SYSTEM_FRAME.search(seen):
        # a model that writes the loop's own frame goes on as if the results in it were real, so none
        # of it runs (ADR-0004/C4). A mention is refused too: every rule that spared the tag in
        # backticks also spared a frame made to look like one
        line = len(_BREAK.findall(seen, 0, frame.start())) + 1
        shown = _BREAK.split(seen[frame.start() :], 1)[0].strip()
        shown = shown if len(shown) <= 80 else shown[:77] + "..."
        raise lndl.LNDLError(
            f"the turn carries a <system turn=...> frame, at line {line}: {shown}; only the loop "
            "writes those and the results inside it were never observed, so nothing in the turn was "
            "executed: write one program, end the turn, and read the real results in the next "
            "notification. To speak of it, call it the notice; never write its tag"
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


class _Limit(asyncio.Timeout):
    """A walk's or a settlement's time limit and the time budget its deadline comes from: a grant of
    time that moves the deadline moves the budget with it, and one that lands after the limit fired
    moves neither."""

    def __init__(self, run: Run):
        super().__init__(_deadline(run))
        self.run, self.budget = run, run.bounds["time_budget"]

    def reschedule(self, when: float | None) -> None:
        super().reschedule(when)
        self.budget = self.run.bounds["time_budget"]

    def fire(self) -> None:
        """What the loop's timer does at the deadline, done now: a deadline that passed while a
        synchronous call held the loop has not fired yet, and a walk that leaves its limit before the
        loop turns would never be cut."""
        self._timeout_handler.cancel()  # the timer, which would fire a second time
        self._on_timeout()


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
        # a write's task -> the values it and the writes before it read (through the values they read)
        # and the write before it
        self.write_reads: dict[asyncio.Task, tuple[frozenset[str], asyncio.Task | None]] = {}
        self.reads: dict[str, set[str]] = {}  # this program's command -> the results its arguments read
        # results read by a reader that settled ok, or taken by a note: the seq each was first consumed at
        self.used: dict[str, int] = {}
        self.named: set[str] = set()  # what OUT{} points at: never consumed
        self.told = False  # the turn's refusal for a spent time budget is on the run's list
        self.settling: _Limit | None = None  # the settlement's limit, once the walk's commands are settled
        # the walk launched a command, from any node or value, or reached OUT{}: the turn acted
        self.acted = False

    def out_of_time(self, budget: float) -> None:
        """The turn's refusal for a spent time budget, told once however many ways the walk finds it
        spent. `budget` is the one the walk's deadline came from; a grant that landed after the
        deadline is named beside it, never folded into it. Kept on the run's list: the spent budget
        ends the run before a notification tells it."""
        if self.told:
            return
        self.told = True
        raised = self.run.bounds["time_budget"] - budget
        late = f", and a grant of {raised:g} landed after the deadline" if raised > 0 else ""
        spent = f"the time_budget of {budget:g} is spent{late}"
        self.run.refused.append(("turn", f"{spent}; nothing after it ran"))

    def expired(self) -> bool:
        """Whether the walk's own limit or its settlement's has fired, whatever a later grant raised;
        a refusal on this alone tells nothing of the turn."""
        return any(t is not None and t.expired() for t in (self.limit, self.settling))

    def spent(self) -> bool:
        """Whether nothing the walk still holds may commit: its own limit has expired, or the time
        budget is spent. A grant that landed after the limit expired raised the bound for what follows
        the walk and leaves the walk expired. The refusal is told, once."""
        # the settlement's limit counts as the walk's own: a write it fired under, queued behind the grant
        # that held the loop past it, runs before the cut lands and must find the walk spent
        fired = next((t for t in (self.limit, self.settling) if t is not None and t.expired()), None)
        if fired is None and _left(self.run) != 0.0:
            return False
        self.out_of_time((fired or self.limit).budget)
        return True

    async def execute(self, script: Script) -> dict[str, Any] | None:
        # what the walk computes itself, a branch's test and OUT{}'s fields, the time budget bounds
        # as it bounds a command's arguments: one still computing at the deadline stops there
        limit = self.limit = _Limit(self.run)
        found: dict[str, Any] | None = None
        try:
            async with limit:
                self.run.timeouts.add(limit)  # while the walk runs, a grant of time moves its deadline
                try:
                    found = await self.block(script.body)
                finally:
                    self.run.timeouts.discard(limit)
        except _Failed:
            return None
        except TimeoutError:
            if not limit.expired():  # raised by what the walk read, not by this deadline
                raise
            self.out_of_time(limit.budget)
            return None
        except (
            asyncio.CancelledError
        ):  # the run is cancelled: its commands with it, each settling as cancelled
            for task in self.tasks:
                task.cancel()
            raise
        finally:
            try:
                # the time budget bounds it as any turn's; a write it cuts while the write waited for its
                # command is cut by the budget as the walk's own cut is, and is told as that is, once
                self.settling = _Limit(self.run)
                cut, budget = await _settle(self.run, self.tasks, limit=self.settling)
                if any(task in self.write_reads for task in cut):
                    self.out_of_time(budget)
                # a turn told spent, by a write the settlement cut or one refused as its deadline fired,
                # commits nothing the walk held: the output goes too, whatever bound a late grant raised
                if self.told:
                    found = None
            finally:
                # a value still computing now has no reader left (each was cancelled): it stops with
                # them, whether the settle returned or raised
                for c in self.computed.values():
                    c.cancel()
                # every value is seen through: one that failed after its readers were cut told no one
                await asyncio.gather(*self.computed.values(), return_exceptions=True)
            for alias, reads in self.reads.items():
                entry = self.run.record.get(alias)
                if entry.content.ok:
                    self.consume(reads, entry.seq)  # at the reader's settle
            consumed = sorted(self.used.keys() - self.named, key=lambda a: self.run.record.get(a).seq)
            self.run.intermediates.update((a, self.used[a]) for a in consumed)
        return found

    def consume(self, reads: set[str], at: int) -> None:
        for alias in reads:
            self.used[alias] = min(at, self.used.get(alias, at))

    async def block(self, nodes: tuple) -> dict[str, Any] | None:
        for node in nodes:
            if isinstance(node, Branch):
                reader = _Reader(self, self._snapshot(node.test))
                try:
                    taken = await evaluate(node.test, reader)
                except lndl.LNDLError as e:
                    self.run.refused.append((f"line {node.line}", f"if: {e}; nothing after it ran"))
                    raise _Failed from None
                self.consume(reader.reads, len(self.run.record))
                ended = await self.block(node.body if taken else node.orelse)
                if ended is not None:
                    return ended
            elif isinstance(node, ProgramOutput):
                self.acted = True
                reader = _Reader(self, self._snapshot(*(v for _, v in node.fields)))
                self.named |= node.named
                fields: dict[str, Any] = {}
                sizes: dict[str, int] = {}  # what each field landed as
                size = (
                    0  # what `_size(fields)` counts, kept as the fields land, scanned whole once at the end
                )
                past = f"line {node.line}: the OUT{{}} fields together are past the size limit of {LIMIT}"
                for key, v in node.fields:
                    if key in fields:  # a key written again holds only its last value: what that one landed
                        size -= sizes[key]  # as leaves, whatever a command made of it since
                    fields[key] = await evaluate(v, reader)
                    sizes[key] = 1 + len(key) + _size(fields[key])
                    size += sizes[key]
                    # counted as a dict literal is: no field after the one that takes it past the limit runs
                    if size > LIMIT:
                        raise EvalError(past)
                # a value a command grew after its field was counted, through the pointer both hold: the
                # whole is measured once more as it is emitted
                if _size(fields) > LIMIT:
                    raise EvalError(past)
                # what is emitted is a copy of what was measured: a command still settling after the walk
                # holds the stored value, and grows that, not the output
                try:
                    fields = _captured(fields)
                except Exception as e:  # a model's copy raised, or a nesting past the interpreter's depth
                    raise EvalError(
                        f"line {node.line}: the OUT{{}} fields cannot be copied, {type(e).__name__}: {e}"
                    ) from e
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
        self.acted = True
        admitted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        run.thunks[la.alias] = args
        run.halts[la.alias] = [self.expired]
        task = asyncio.create_task(self.actor._perform(run, la, self.subset, admitted))
        task.add_done_callback(functools.partial(self.actor._never_started, run, la))
        run.pending[la.alias] = task
        run.tasks.add(task)
        admitted.set_result(None)  # registered before it runs, under any task factory
        self.reads.setdefault(la.alias, set())
        self.tasks.append(task)
        self.lacts.append(la)
        return task

    def command(self, node: Command) -> str:
        alias = node.alias or self.auto()
        reader = _Reader(self, self._snapshot(*(v for _, v in node.args)))

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
        """The writes a value reads after, of the notes it reads. A write that reads the value comes
        after the value, not before it (waiting for it would wait for itself), and so does every write
        queued behind it: the value reads after the last write before that."""
        out: dict[str, asyncio.Task] = {}
        for key in self._keys(self.values[name][1]):
            task = writes.get(key)
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

    def _keys(self, *exprs: ast.expr) -> frozenset[str]:
        """The notes the expressions read, and those the values they read read."""
        return note_keys(*exprs, *(self.values[n][1] for n in self._reads(*exprs)))

    def _snapshot(self, *exprs: ast.expr) -> dict[str, asyncio.Task]:
        """The latest writes reached of the notes the expressions read: what their reader waits for,
        and no write it never reads, so a reader costs its reads, not the program's writes."""
        return {key: self.writes[key] for key in self._keys(*exprs) if key in self.writes}

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
        # a write reached on a spent walk launches nothing: not the command on its right side, not that
        # command's arguments or the hooks that gate it
        if self.spent():
            return
        prior = self.writes.get(node.key)
        target = ".".join(("note", node.key, *node.path))
        producer = self.command(node.value) if isinstance(node.value, Command) else None
        exprs = tuple(v for _, v in node.value.args) if producer is not None else (node.value,)
        # the writes before this one, of the notes it reads: what it and a value it names read after
        reader = _Reader(self, self._snapshot(*exprs))
        # what this write reads, and what the write it waits for reads: a value read by any write in
        # the chain is read before the whole chain
        reads = self._reads(*exprs)
        if prior is not None:
            reads |= self.write_reads[prior][0]
        # the values the expression itself names: computing one reaches what its branches reach and
        # nothing more, so a command in a branch not taken never runs for a write (C3)
        named = pointers(*exprs)
        named &= self.values.keys()

        async def put() -> None:
            if prior is not None:  # two assignments to one note are two writes, in the order written
                await asyncio.shield(prior)
            # a write reached after the walk expired or the budget is spent is refused as spent, whatever
            # its evaluation would have raised
            if self.spent():
                return
            try:
                # a write that names a value comes after it, whether or not evaluating reaches the
                # pointer (`false and *v`): the value reads the note before this write lands
                for name in sorted(named):
                    await self.value(name, reader.writes)
                value = await (
                    reader.point(producer) if producer is not None else evaluate(node.value, reader)
                )
            except lndl.LNDLError as e:  # what it writes failed: the note keeps its value
                self.run.refused.append((target, f"not written: {e}"))
                return
            # the deadline cuts the walk at its next yield, and a write that reached the store with none
            # in between is past it: once it is spent nothing is committed, the store as it was
            if self.spent():
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
        self.write_reads[task] = (reads, prior)
        self.writes[node.key] = task
        self.run.tasks.add(task)
        self.tasks.append(task)


class _Reader:
    """One node's reads while its expressions are computed: each note read once, so one expression
    sees one version (ADR-0008/C3), and the program's results it read."""

    def __init__(self, walk: _Walk, writes: dict[str, asyncio.Task]):
        self.walk = walk
        self.writes = writes  # its own: the writes of the notes it reads (`_Walk._snapshot`, `_before`)
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
    distance, best = min(((_distance(name, n), n) for n in names), default=(None, None))
    return name, best, distance


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
    """Whether following pointers and effect waits from the pending command `start` comes back to
    `target`."""
    seen, stack = set(), [start]
    while stack:
        a = stack.pop()
        if a == target:
            return True
        if a in seen:
            continue
        seen.add(a)
        stack.extend(n for n in run.pointing.get(a, set()) | run.waits.get(a, set()) if n in run.pending)
    return False


def _validate_whole(spec: Spec, value: Any) -> BaseModel:
    """One alias holding the whole output: a Spec instance, a JSON object, or, for a Spec with one
    field, the field's value itself (text, a number, a list: `OUT{count: [a]}` with `a` a bare 3)."""
    if isinstance(value, spec):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError, MemoryError):  # the last two: nested past reading
            pass
    if not isinstance(value, dict):
        fields = list(spec.model_fields)
        if len(fields) != 1:
            name = spec_name(spec)
            raise lndl.LNDLError(
                f"{name} has fields {fields}: give one <lvar {name}.<field> alias> per field and "
                f"OUT{{{name}: [the aliases]}}, or one alias holding a JSON object"
            )
        value = _keyed(spec, {fields[0]: _coerce(spec, fields[0], value)})
    return spec.model_validate(value)


def _field_named(keys: dict[str, list[str | int]], key: str) -> str:
    """The field a key the model wrote fills: the one whose key (`field_keys`) it is, else the field of
    that name."""
    return next((n for n, k in keys.items() if k == [key]), key)


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
            # built per call, never cached by Spec: a rebuilt Spec keeps its class and changes its fields
            TypeAdapter(info.annotation).validate_python(value, strict=True)
        except ValidationError:
            pass
        else:
            return value
    for reader in (json.loads, ast.literal_eval):
        try:
            return reader(value)
        # TypeError: a literal with an unhashable key; RecursionError, MemoryError: one nested past
        # what the reader walks, which the model is told of as any other value the field refuses
        except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
            continue
    return value


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


async def _settle(
    run: Run, tasks: list[asyncio.Task], by_deadline: bool = True, limit: _Limit | None = None
) -> tuple[list[asyncio.Task], float | None]:
    """Wait for the turn's commands, under the run's deadline unless `by_deadline` is False: one still
    running when it passes is cancelled, and a grant of time while it waits moves it, as it moves a
    walk's. A cancelled command has settled itself as a RESULT; any other way out of a command task
    is the runtime's own defect and is raised, never dropped. Each task is seen through once: what a
    settle did not wait for (a command `OUT{}` did not name) is seen through by the reap. The tasks its
    own deadline cancelled are returned, with the time budget that deadline came from. A walk hands in
    the `_Limit` it settles under, so its writes can ask whether that deadline fired, and its own list
    of tasks, which grows while it settles: a value a command's argument reads may launch a command.
    One launched before the deadline is waited for and cut as the rest are; one launched after it is
    refused where it starts."""
    cut: list[asyncio.Task] = []
    budget = None
    if tasks:
        if limit is None:
            limit = _Limit(run) if by_deadline else asyncio.timeout_at(None)
        try:
            async with limit:
                if by_deadline:
                    run.timeouts.add(limit)
                try:
                    # only tasks still running are waited on, so each pass yields to the loop
                    while waited := [task for task in tasks if not task.done()]:
                        await asyncio.wait(waited)
                finally:
                    run.timeouts.discard(limit)
        except TimeoutError:  # only this limit raises it: the wait itself never does
            budget = limit.budget  # the run's deadline fired, so this limit is the run's `_Limit`
            for task in list(tasks):
                if not task.done():
                    task.cancel()
                    cut.append(task)
        seen = 0
        while seen < len(tasks):  # a task the cut let start is refused there, and is seen through too
            seen = len(tasks)
            results = await asyncio.gather(*tasks, return_exceptions=True)
        run.tasks.difference_update(tasks)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                raise r
    return cut, budget


def _left(run: Run) -> float | None:
    """What the time budget leaves: the deadline beside a settle (ADR-0003/S3); None without one."""
    if run.bounds["time_budget"] is None:
        return None
    return max(0.0, run.bounds["time_budget"] - run.elapsed)


def _deadline(run: Run) -> float | None:
    """Where the time budget ends on the event loop's clock; None without one."""
    left = _left(run)
    return None if left is None else asyncio.get_running_loop().time() + left


async def _reap(run: Run) -> None:
    """At the run's end, whatever is still pending is cancelled and waited for, so its cancelled
    RESULT is on the record when `run` returns and no command task outlives it (ADR-0002/C1); every
    task no settle saw through is seen through here, so a defect in one `OUT{}` did not name is raised
    before the run returns (ADR-0002/S5)."""
    for task in run.pending.values():
        task.cancel()
    await _settle(run, list(run.tasks), by_deadline=False)


async def _reaped(run: Run) -> bool:
    """The abort's reap, seen through whatever else cancels the run meanwhile: a second cancel is held
    until the reap is done, so no command task outlives the run and its cancelled RESULT is on the
    record. True when one arrived."""
    reap, cancelled = asyncio.ensure_future(_reap(run)), False
    while not reap.done():
        try:
            await asyncio.shield(reap)
        except asyncio.CancelledError:
            cancelled = True
    reap.result()
    return cancelled


def _closure(run: Run) -> str:
    """The SYSTEM entry a run that ends by an outcome appends after the reap (ADR-0006/C1): the outcome
    and the commands cancelled since the last notification, which no notification told."""
    last = run.record.latest(Kind.SYSTEM)
    since = last.seq if last is not None else -1
    cancelled = [
        e.alias
        for e in run.record
        if e.kind is Kind.RESULT and e.seq > since and getattr(e.content, "reason", None) == "cancelled"
    ]
    outcome = "Success" if isinstance(run.outcome, Success) else repr(run.outcome)
    lines = [f"<system closure seq={run.record.seq}>", f"outcome: {outcome}"]
    if cancelled:
        lines.append("cancelled: " + ", ".join(cancelled))
    lines.append("</system>")
    return "\n".join(lines)


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


def _captured(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """A value as a capture holds it (ADR-0007/C1): a scalar shared, a list, dict, set or tuple copied,
    a Spec instance or a Result rebuilt on captured fields and extras; a value reached twice, or a list
    or dict holding itself, is copied once."""
    memo = {} if memo is None else memo
    if id(value) in memo:
        return memo[id(value)]
    if isinstance(value, dict):
        memo[id(value)] = out = {}
        out.update((k, _captured(v, memo)) for k, v in value.items())
        return out
    if isinstance(value, list):
        memo[id(value)] = out = []
        out.extend(_captured(v, memo) for v in value)
        return out
    if isinstance(value, tuple):
        memo[id(value)] = out = tuple(_captured(v, memo) for v in value)
        return out
    if isinstance(value, set):
        memo[id(value)] = out = {_captured(v, memo) for v in value}
        return out
    if isinstance(value, BaseModel):
        update = {k: _captured(getattr(value, k), memo) for k in type(value).model_fields}
        update.update((k, _captured(v, memo)) for k, v in (value.model_extra or {}).items())
        memo[id(value)] = out = value.model_copy(update=update)
        return out
    if isinstance(value, Result):
        memo[id(value)] = out = dataclasses.replace(value, value=_captured(value.value, memo))
        return out
    return value


def _fields(spec: BaseModel) -> dict[Any, Any]:
    # the declared fields only: a computed field is no input, and a spec that forbids extras refuses it.
    # Each sits where validation reads it, at the key `field_key` names, and the extras a spec keeps
    # come along; an alias path reads an int key from a dict as from a list
    cls = type(spec)
    return _keyed(cls, {name: getattr(spec, name) for name in cls.model_fields}, dict(spec.model_extra or {}))


def _keyed(spec: Spec, values: dict[str, Any], out: dict[Any, Any] | None = None) -> dict[Any, Any]:
    """Field values by field name, each set in `out` where validation reads it (`field_key`)."""
    out = {} if out is None else out
    keys = field_keys(spec)
    for name, value in values.items():
        *walk, last = keys[name]
        node = out
        for key in walk:
            node = node.setdefault(key, {})
        node[last] = value
    return out


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
        "user",
        "[hidden; *name or context.show reopens: " + ", ".join(parts) + "]",
        name=entries[0].name,
        folded=True,
    )


def _message(v: ViewEntry) -> Message:
    e = v.entry
    if v.summary is not None:
        return Message("user", f"[summary before {e.name}] {v.summary}", name=e.name, folded=True)
    if e.kind is Kind.SYSTEM:
        # the runtime's own fold rides a notification; a session-keeping backend reads the mark
        fold_mark = "fold" if isinstance(e.meta.get("directive"), Directive) else None
        return Message("system", e.content, name=e.name, spec=fold_mark)
    if e.kind is Kind.TEXT:
        return Message("assistant", e.content, name=e.name)
    if e.kind is Kind.RESULT:
        res: Result = e.content
        body = _text(res.value) if res.ok else f"error: {res.error}"
        # the command that landed; a failed one left the view as it was. A session-keeping backend reads
        # what a directive changed off the view itself, never off this mark (ADR-0009/C4)
        spec = res.spec if res.ok else None
        return Message("user", f"[{e.name} {e.alias} {res.spec}] {body}", name=e.name, spec=spec)
    label = e.name
    if e.meta.get("inbound"):
        label += f" from {e.meta['origin']}"
    return Message("user", f"[{label}] {_text(e.content)}", name=e.name)
