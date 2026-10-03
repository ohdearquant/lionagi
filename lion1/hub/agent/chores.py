"""Chores: an actor's recurring chores, run by its long-running agent on ticks that arrive as mail, each
ending one of three ways and never a fourth.

A chore is an instrument: a read-only measurement with its own positive control, run by a handler
when the agent's model calls `check(chore=...)`. The model routes (a tick names its chore, an ask
from the actor names one) and the handler does everything that matters: it runs the instrument, writes
the ledger row, and sends what the outcome says. Nothing a cheap model might misjudge sits on the
reporting path.

The three ends of a chore:

1. quiet: the instrument ran, its control passed, it found nothing. A ledger row, no mail. A quiet
   chore costs the actor nothing, which is the point of handing it over.
2. report: findings, with the instrument's own output beside them, to the owning actor on the chore's
   thread of the day (hop-capped by the agent, so one chore cannot flood an actor). A report names the
   report it supersedes, and a report whose findings equal the last one sent is not sent again: the
   actor reads a change, not a restatement.
3. escalate: a finding whose remedy is destructive, or an instrument that failed or whose control did
   not pass (an empty answer from a broken instrument is not a clean one): to the actor and the
   steward, marked so. The agent never performs the remedy.

The steward hears of a report a second way: a report the owner has not answered on its thread within
`aged_after` periods of that chore goes to the steward once, by the `aged` handler. That is the whole
ladder; there is no digest of findings to the steward. The owner's digest is one line per chore, every
chore, a missing line being the finding.

Ticks are khive reminders the agent creates for itself (`schedule.remind` with a repeat), so a timed
chore is still a wake on mail and nothing loops. A reminder khive could not deliver (the agent was
down past the grace) is marked `missed` and never re-fired, so every check first reads the actor's own
missed rows and runs those chores now. Mail from a sender outside the actor's trusted set never reaches
a run (`Chores.admit`, the agent's gate); the trusted set is the owner, the steward, the actor itself and
whatever the actor's own config adds.
"""

from __future__ import annotations

import calendar
import dataclasses
import json
import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from hub import guidance
from lionagi.actor import Context, Profile, Run

from ..areas import partial
from ..khive import KhiveError, lit
from .agent import Agent, AgentError, LoopLock

__all__ = (
    "DIRECTIONS",
    "Outcome",
    "Instrument",
    "Check",
    "Digest",
    "Aged",
    "ALLOWED_VERBS",
    "SPAWN_SALIENCE",
    "verbs",
    "Bounded",
    "Chores",
    "sender_of",
    "period_s",
)

DIRECTIONS = ("over-report", "under-report")


@dataclass(frozen=True)
class Outcome:
    """What one run of an instrument found. No findings is quiet: nothing is sent. Every outcome names
    the population it measured, the predicate it ran over it and the count, and carries what its
    known-positive read in the same invocation as the answer; the handler refuses one that does not,
    and reports an empty population the chore did not declare expected. `commit` retires what the run
    consumed (a mail cursor): the handler calls it once, after the ledger row, and only when the owner's
    report landed or was a standing repeat; a failed or raised send leaves it uncalled, so the next check
    remeasures the window, and a report whose receipt was unknown may be sent twice (the accepted trade)."""

    findings: tuple[str, ...] = ()
    evidence: str = ""  # the instrument's own output, the reader's proof; a capped one says so itself
    control: bool = True  # the same-call positive control passed; False is an instrument failure
    escalate: bool = False  # a finding whose remedy is destructive or irreversible
    population: str = ""  # what was measured, as a noun phrase
    predicate: str = ""  # the predicate run over that population, as text
    count: int | None = None  # the population's size; None is "not counted", which the handler refuses
    positive: str = ""  # what the known-positive read, from the same invocation as the answer
    data: tuple[dict, ...] = ()  # the rows a consumer reads as structure (an observation per PR), never sent
    commit: Callable[[], None] | None = None  # never sent, never in the ledger row


class Instrument(Protocol):
    name: str
    every: str | None  # khive repeat: "every:30m", "daily", or a UTC cron; None = on ask only
    about: str  # one line, for the guidance and the digest: what the instrument does
    answers: str  # the question its output settles, read against the chore's name when the chore is accepted
    direction: str  # which way this instrument's failures run: "over-report" or "under-report"
    mechanism: str  # one sentence naming why
    empty_expected: bool  # an empty population is this chore's expected state, not a finding

    async def run(self, args: dict[str, Any]) -> Outcome: ...


class Check(BaseModel):
    """Run one chore now: its instrument, its control, its ledger row, and whatever its outcome
    sends. A tick names the chore and has no asker; an ask from an actor names the chore and is
    answered on its thread whatever the outcome."""

    chore: str = Field(description="the chore's name, as written in the tick or the ask")
    args: dict[str, Any] = Field(default_factory=dict, description="what the ask narrowed to, if anything")
    asker: str | None = Field(default=None, description="the asking actor's actor id from the mail header")
    thread: str | None = Field(default=None, description="the ask's thread id from the mail header")


class Digest(BaseModel):
    """Tell the owner what the ledger holds for one day: one line per chore, every chore, MISSING
    for a chore with a period and no run within one period and the store's fire grace before the
    digest's moment (a chore that has not run yet, only past its first due and the grace), `not judged`
    for a cron chore and for a ledger that cannot be read, and `on ask: no run` for a chore run only on an
    ask that did not run."""

    day: str | None = Field(default=None, description="YYYY-MM-DD; default today")


class Aged(BaseModel):
    """Tell the steward which reports the owner has not answered on their threads past the chore's
    aging bound; each report is told once."""

    chore: str | None = Field(default=None, description="only this chore's reports; default all")


# the verbs the agent may put through khive as ops: reads, its own mailbox, its own reminders.
# Mail goes through `Bounded.send`, the one path that checks the recipient; `comm.send` as an op is
# refused with the writes, before kkernel sees any of them.
ALLOWED_VERBS = frozenset(
    {
        "comm.inbox",
        "comm.read",
        "comm.probe",
        "comm.thread",
        "schedule.remind",
        "schedule.agenda",
        "gtd.tasks",
        "gtd.next",
        "list",
        "get",
        "search",
        "neighbors",
        "memory.recall",
        "knowledge.suggest",
        "memory.remember",  # the one write: the agent's own provenance, at spawn salience (SPAWN_SALIENCE)
    }
)
SPAWN_SALIENCE = 0.4  # the most a long-running agent may claim for what it remembers
_REMEMBER = re.compile(r"memory\.remember\s*\(([^)]*)\)")
_SALIENCE = re.compile(r"(?:^|,)\s*salience\s*=\s*([^,]*)")  # the argument, not a name ending in it

_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_VERB = re.compile(r"(?<![\w.])([a-z_]+(?:\.[a-z_]+)?)\s*\(")
# kkernel reads an op that begins `{` or `[{` as JSON, where no call is visible to `verbs`
_HEAD = re.compile(r"\s*\[?\s*[a-z_]+(?:\.[a-z_]+)?\s*\(")
# the reply this task is sending: [the boundary, sender, thread] until its one send, then []
_REPLY: ContextVar[list | None] = ContextVar("reply", default=None)


def verbs(ops: str) -> list[str]:
    """The verbs an ops string calls, string literals blanked first so a quoted `x(` is not one."""
    return _VERB.findall(_STRING.sub('""', ops))


class Bounded:
    """The agent's khive: every op is checked against the read list, and every send against the
    recipients or the one reply being sent, in code and before the call. The denial text is what the
    boundary arm quotes."""

    def __init__(
        self, khive: Any, *, allowed: frozenset[str] = ALLOWED_VERBS, recipients: frozenset[str] | None = None
    ):
        self._khive, self.allowed, self.recipients = khive, frozenset(allowed), recipients

    @contextmanager
    def reply(self, to: str, thread: str | None) -> Iterator[None]:
        """One send to `to` on `thread`, the sender and thread of the message being answered, for the
        task inside this block: spent by that send, delivered or refused, and gone when the block ends."""
        if not thread:
            raise AgentError(
                f"agent boundary: a reply to {to} goes on the thread it answers; the message names none"
            )
        token = _REPLY.set([self, to, thread])
        try:
            yield
        finally:
            _REPLY.reset(token)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._khive, name)

    async def exec(self, ops: str) -> list:
        found = verbs(ops)
        opens = _STRING.sub('""', ops).count("(")
        if opens != len(found):  # a call the boundary cannot name is not a permission: fail closed
            raise AgentError(
                f"agent boundary: {opens} call(s) in the op, {len(found)} verb(s) the boundary can name; "
                f"refused: {ops[:120]!r}"
            )
        bad = sorted(set(found) - self.allowed)
        if bad:
            raise AgentError(
                f"agent boundary: {bad} are not verbs the agent may call; allowed: {sorted(self.allowed)}"
            )
        if not _HEAD.match(ops):
            raise AgentError(f"agent boundary: an op begins `verb(` or `[verb(`; refused: {ops!r}")
        for m in _REMEMBER.finditer(_STRING.sub('""', ops)):  # a memory carries its salience, capped
            sal = _SALIENCE.findall(m.group(1))
            try:
                value = float(sal[0]) if len(sal) == 1 else None  # given twice: the store's parse decides
            except ValueError:
                value = None
            if value is None or value > SPAWN_SALIENCE:
                raise AgentError(
                    f"agent boundary: memory.remember takes salience <= {SPAWN_SALIENCE} (spawn provenance), "
                    f"given once; got {sal[0].strip() if len(sal) == 1 else ('none' if not sal else 'twice')}"
                )
        return await self._khive.exec(ops)

    async def assign(self, **fields: Any) -> dict:
        """The one gtd write, composed here from the fields code chose (the desk's task):
        `gtd.assign` as an op stays refused by `exec`, so no model composes it."""
        (row,) = await self._khive.exec(
            f"gtd.assign({', '.join(f'{k}={lit(v)}' for k, v in fields.items())})"
        )
        return row

    async def send(
        self,
        to: str,
        content: str,
        *,
        subject: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        if self.recipients is not None and to not in self.recipients:
            attempt = _REPLY.get()
            if attempt != [self, to, thread_id]:
                raise AgentError(
                    f"agent boundary: {to} is not a recipient this agent reports to, and no message of its "
                    f"on thread {thread_id} is being answered; allowed: {sorted(self.recipients)}"
                )
            attempt.clear()  # one send per message: this attempt spends it, whatever the transport says
        return await self._khive.send(
            to, content, subject=subject, thread_id=thread_id, idempotency_key=idempotency_key
        )


@dataclass
class Chores:
    agent: Agent
    owner: str  # the actor whose chores these are: reports go here
    steward: str | None  # escalations and aged reports go here too; None: only the owner
    instruments: dict[str, Instrument]
    ledger: Path  # append-only JSONL, one row per check
    trusted: frozenset[str] = frozenset()  # senders beyond the floor (owner, steward, self)
    evidence_cap: int = 4000  # chars of evidence carried in a message and a row
    aged_after: int = 2  # chore periods a report may wait for the owner's reply before the steward hears
    aged_every: str = "every:1h"  # the aged pass's own tick
    dead_after: int = 3  # consecutive unanswered runs before a chore is escalated as a dead instrument
    digest_every: str = "daily"  # the digest's own tick; a front desk's brief sets it (`[desk] brief`)
    extra_specs: frozenset[str] = frozenset()  # handlers another part of the agent adds (the desk)
    extra_ticks: dict[str, str] = field(default_factory=dict)  # their ticks, content -> repeat
    extra_system: str = ""  # their rules, appended to the chores profile
    extra_digest: Callable[[str], list[str]] | None = None  # their lines for the day's digest (the desk)
    extra_digest_sent: Callable[[], None] | None = None  # told once a digest carrying them was delivered
    # their lines rendered, delivered and committed as one step: two digests of a turn never share a window
    _briefing: LoopLock = field(default_factory=LoopLock, repr=False)
    # a check's report, from reading the last one sent to writing its row: two checks of a turn run
    # together, and both reading "no prior" before either wrote would each send the same findings
    _reporting: LoopLock = field(default_factory=LoopLock, repr=False)
    # an aged pass, from reading `aged_sent` to writing it back: two passes of a turn run together, and
    # both reading the same markers would each tell the steward, or the later write drop the earlier's
    _aging: LoopLock = field(default_factory=LoopLock, repr=False)
    _caught_up: str | None = field(default=None, repr=False)  # the run whose missed-tick sweep ran

    def __post_init__(self) -> None:
        floor = {self.owner, self.agent.khive.actor} | ({self.steward} if self.steward else set())
        self.trusted = frozenset(self.trusted) | floor
        bad = {n: getattr(i, "direction", None) for n, i in self.instruments.items()}
        bad = {n: d for n, d in bad.items() if d not in DIRECTIONS}
        if bad:
            raise ValueError(f"every chore declares its error direction, one of {DIRECTIONS}: {bad}")
        blank = [n for n, i in self.instruments.items() if not str(getattr(i, "answers", "") or "").strip()]
        if blank:
            raise ValueError(f"every chore says which question its instrument answers (`answers`): {blank}")
        actor = self.agent.actor

        @actor.handler(Check, requires={"comm.send"})
        async def check(req: Check, ctx: Context) -> str:
            return await self.check(req, ctx.run)

        @actor.handler(Digest, requires={"comm.send"})
        async def digest(req: Digest, ctx: Context) -> str:
            return await self.digest(req.day, ctx.run)

        @actor.handler(Aged, requires={"comm.send"})
        async def aged(req: Aged, ctx: Context) -> str:
            return await self.aged(req.chore, ctx.run)

    def admit(self, m: dict) -> bool:
        """The agent's gate: mail from outside the trusted set is read, noted on the cursor, never run."""
        return sender_of(m) in self.trusted

    # the handlers

    async def check(self, req: Check, run: Run) -> str:
        inst = self.instruments.get(req.chore)
        if inst is None:
            raise AgentError(f"no chore {req.chore!r}; known: {sorted(self.instruments)}")
        if (
            req.asker and req.asker not in self.trusted
        ):  # the gate admitted the mail; the model may not widen it
            raise AgentError(f"{req.asker} is not a sender this agent works for")
        if req.asker:  # the ask is a message this wake admitted: its sender, answered on its thread
            threads = {
                str(t)
                for m in self.agent.admitted
                if sender_of(m) == req.asker and (t := (m.get("properties") or {}).get("thread_id"))
            }
            if req.thread is None and len(threads) == 1:
                req = req.model_copy(update={"thread": next(iter(threads))})
            if req.thread not in threads:
                raise AgentError(
                    f"{req.asker} on thread {req.thread} is not a message this wake admitted; "
                    f"its threads here: {sorted(threads) or 'none'}"
                )
        # a check the ask narrowed (args) covers no missed tick: the tick was the whole sweep
        caught = await self._catch_up(run, skip=req.chore if not req.args else None)
        t0 = time.time()
        try:
            # the run id is the handler's fact, set after the model's args so they cannot name another
            out = await inst.run({**req.args, "source_event": f"chore run {run.id}"})
        except Exception as e:  # the failure is the finding; it must reach the actor, not the log alone
            out = Outcome(
                findings=(f"instrument failed: {type(e).__name__}: {e}",),
                evidence=f"{type(e).__name__}: {e}",
                control=False,
            )
        out, answered = self._vet(inst, out)
        async with self._reporting:
            prior = self._last_to(req.chore, self.owner)
            same = prior is not None and prior["findings"] == list(out.findings)
            supersedes = "no-prior" if prior is None else f"{prior['at']} msg {prior.get('msg') or '?'}"
            text = self._text(req.chore, inst, out, time.time() - t0, supersedes=supersedes, same=same)
            subject = self._subject(req.chore, out)
            sent_to: list[str] = []
            undelivered: list[str] = []
            msg = thread = None
            if req.asker:  # an ask is answered where it was asked, quiet or not
                try:
                    sent = await self.agent.deliver(
                        req.asker, text, thread=req.thread, run=run, subject=subject
                    )
                    sent_to.append(req.asker)
                    if req.asker == self.owner:  # the owner's receipt, whichever thread carried it
                        msg, thread = str(sent.get("id")), str(sent.get("thread_id") or sent.get("id"))
                except AgentError as e:  # the hop cap: the row says so, the check still lands
                    undelivered.append(f"{req.asker}: {e}")
            recipients = [self.owner]
            if out.escalate and self.steward and self.steward != self.owner:
                recipients.append(self.steward)
            repeat = False  # the owner's send skipped: the owner already holds these findings
            for to in recipients if out.findings else []:
                if to in sent_to:
                    continue
                last = self._last_to(req.chore, to)  # a standing state is told once, per recipient
                if last is not None and last["findings"] == list(out.findings):
                    repeat = repeat or to == self.owner
                    continue
                try:
                    sent = await self._on_chore_thread(req.chore, to, text, run, subject=subject)
                    sent_to.append(to)
                    if to == self.owner:
                        msg, thread = str(sent.get("id")), str(sent.get("thread_id") or sent.get("id"))
                except AgentError as e:
                    undelivered.append(f"{to}: {e}")
            ts = time.time()  # one read: the row's label is that instant's, so the two name one day
            self._append(
                {
                    "at": _now(ts),
                    "ts": ts,  # the instant; `at` is its local label, twice in a repeated hour
                    "chore": req.chore,
                    "args": req.args,
                    "findings": list(out.findings),
                    "control": out.control,
                    "escalate": out.escalate,
                    "answered": answered,
                    "direction": inst.direction,
                    "population": out.population,
                    "predicate": out.predicate,
                    "count": out.count,
                    "positive": out.positive,
                    "seconds": round(time.time() - t0, 1),
                    "evidence": self._cap(out.evidence),
                    "supersedes": supersedes,
                    "repeat_of": prior["at"] if same and out.findings else None,
                    "msg": msg,
                    "thread": thread,
                    "sent_to": sent_to,
                    "undelivered": undelivered,
                    "asked_by": req.asker,
                    "caught_up": caught,
                    "run": run.id,
                }
            )
            owner_held = any(u.startswith(f"{self.owner}: ") for u in undelivered)
            if out.commit is not None and (self.owner in sent_to or (repeat and not owner_held)):
                out.commit()
            dead = await self._dead(req.chore, answered, run)
        if same and out.findings and not sent_to:
            end = "unchanged since " + prior["at"] + ", not resent"
        else:
            end = "escalated" if out.escalate else "reported" if out.findings else "quiet"
        tail = f"; undelivered: {undelivered}" if undelivered else ""
        tail += f"; {dead}" if dead else ""
        tail += f"; missed ticks run: {caught}" if caught else ""
        return f"{req.chore}: {end}, {len(out.findings)} finding(s), sent to {sent_to or 'nobody'}{tail}"

    def _vet(self, inst: Instrument, out: Outcome) -> tuple[Outcome, bool]:
        """The refusals that keep a wrong-shaped answer off the reporting path, and whether the run
        answered its question: a control that passed over a counted population, empty only where
        the chore declared emptiness expected."""
        if out.findings and not out.evidence.strip():  # a finding without the instrument's line is refused
            out = dataclasses.replace(
                out,
                findings=out.findings + ("the instrument reported findings without its own output; refused",),
                evidence="(none)",
                control=False,
            )
        if out.control and (not out.population.strip() or not out.predicate.strip() or out.count is None):
            out = dataclasses.replace(
                out,
                findings=out.findings
                + ("the instrument named no population, predicate or count for its answer; refused",),
                control=False,
            )
        if out.control and not out.positive.strip():
            out = dataclasses.replace(
                out,
                findings=out.findings
                + ("the instrument's control passed with no known-positive read in the same call; refused",),
                control=False,
            )
        answered = out.control
        if out.control and out.count == 0 and not getattr(inst, "empty_expected", False):
            answered = False  # a subject that went away reads like one that never arrived: say so
            out = dataclasses.replace(
                out,
                findings=out.findings + (f"population empty and not declared expected: {out.population}",),
            )
        if not out.control:
            out = dataclasses.replace(
                out,
                findings=out.findings + ("the instrument's control did not pass; its answer is void",),
                escalate=True,
            )
        return out, answered

    async def _dead(self, chore: str, answered: bool, run: Run) -> str:
        """A chore that has not answered in `dead_after` consecutive runs is a finding about the
        instrument, told to the owner and the steward once at the crossing; an answer clears it."""
        told = set(_val(self.agent.state, "dead_told", []))
        # `chore` once every copy landed; `chore|recipient` for a copy that landed while another is owed
        mine = {t for t in told if t == chore or t.startswith(f"{chore}|")}
        if answered:
            if mine:
                self.agent.state.put("dead_told", sorted(told - mine), run=run.id, seq=run.record.seq)
            return ""
        streak: list[dict] = []
        for r in reversed(self.rows()):
            if r["chore"] != chore:
                continue
            if r.get("answered", r.get("control", True)):
                break
            streak.append(r)
        everyone = [self.owner] + ([self.steward] if self.steward and self.steward != self.owner else [])
        owed = [to for to in everyone if chore not in told and f"{chore}|{to}" not in told]
        if len(streak) < self.dead_after or not owed:
            return ""
        text = (
            f"[chore {chore}] ESCALATE, instrument dead: {len(streak)} consecutive run(s) without an answer "
            f"since {streak[-1]['at']}; a chore that never answers is furniture, not a quiet chore. "
            f"Last: {'; '.join(streak[0]['findings'])[:300]}"
        )
        sent = []
        for to in owed:
            try:
                await self._on_chore_thread(
                    chore, to, text, run, subject=f"ESCALATE {chore}: instrument dead"
                )
                sent.append(to)
                told.add(f"{chore}|{to}")  # told once it landed; a refused copy is owed at the next run
            except AgentError as e:
                sent.append(f"{to} undelivered: {e}")
        if all(f"{chore}|{to}" in told for to in everyone):  # every copy landed: the chore's one mark
            told = (told - {f"{chore}|{to}" for to in everyone}) | {chore}
        self.agent.state.put("dead_told", sorted(told), run=run.id, seq=run.record.seq)
        return f"instrument dead after {len(streak)} unanswered run(s), told {sent}"

    async def digest(self, day: str | None, run: Run) -> str:
        now = self.agent.clock()
        day = day or time.strftime("%Y-%m-%d", time.localtime(now))
        named = datetime.strptime(day, "%Y-%m-%d")
        day = named.date().isoformat()  # padded as a ledger label opens: `2026-9-4` is `2026-09-04`
        # the moment a chore is judged at: now, or the next midnight for a past day the digest was asked about
        end = (named + timedelta(days=1)).timestamp()
        moment, ended = min(now, end), end <= now
        try:  # a ledger unreadable or torn judges nothing: a missing subject is not an empty one. An absent
            # file is an empty ledger: an agent whose chores have not run has not written one
            ledger, unread = self.rows(), ""
            # a row the judgment below cannot read judges nothing either: skipped, its run would read missing
            for r in ledger:
                row = r if isinstance(r, dict) else {}
                usable = {  # each field the digest reads from a row, and whether it is one it can use
                    "chore": isinstance(row.get("chore"), str) and bool(row["chore"].strip()),
                    "at": isinstance(row.get("at"), str) and _is_label(row["at"]),
                    # a row from before rows carried `ts` has none; one that has it holds a number to read
                    "ts": "ts" not in row or _is_number(row["ts"]),
                    "findings": isinstance(row.get("findings"), list)
                    and all(isinstance(f, str) for f in row["findings"]),
                    "control": isinstance(row.get("control"), bool),
                    "escalate": isinstance(row.get("escalate"), bool),
                    # a row from before the field has none, and its control says
                    "answered": "answered" not in row or isinstance(row["answered"], bool),
                }
                if not all(usable.values()):
                    bad = ", ".join(k for k, ok in usable.items() if not ok)
                    raise ValueError(f"a ledger row has no usable {bad}: {str(r)[:80]}")
                # a count must be a time the digest can step from, on the day the label it selects by names
                t = _epoch(r)
                if not 0 <= t < _TIME_END:
                    raise ValueError(f"a ledger row's time is no instant the digest counts: {str(r)[:80]}")
                if time.strftime("%Y-%m-%d", time.localtime(t)) != row["at"][:10]:
                    raise ValueError(f"a ledger row's `at` and `ts` name different days: {str(r)[:80]}")
        except (OSError, ValueError, OverflowError) as e:
            ledger, unread = [], f"{type(e).__name__}: {e}"
        rows = [r for r in ledger if r["at"].startswith(day)]
        firsts = {  # chore -> the instant its tick first came due; a value that is no instant is no record
            k: t for k, v in _val(self.agent.state, "first_due", {}).items() if (t := _instant(v)) is not None
        }
        actor = self.owner
        # the pending ticks' firings: read once, and only if a chore needs one (`ensure_ticks` reads the same)
        pending: dict[Any, list[float]] | None = None
        short, agenda_error = None, ""
        lines = [
            f"[chores digest {day}] {self.agent.khive.actor}: "
            + (f"ledger unreadable ({unread})" if unread else f"{len(rows)} check(s)")
            + "; a MISSING line is the finding"
        ]
        for name in sorted(self.instruments):
            every = self.instruments[name].every
            mine = [r for r in rows if r["chore"] == name]
            if unread:
                lines.append(f"{actor} {name} not judged: ledger unreadable")
                continue
            # a cron is not judged: no parser here says when it is next due
            cron = bool(every) and every not in _NAMED and _EVERY.fullmatch(every) is None
            if not mine and (not every or cron):  # runs only when asked (no tick), so never due; or a cron
                lines.append(f"{actor} {name} " + ("not judged: cron" if cron else "on ask: no run"))
                continue
            if every and not cron:  # judged against one period before the moment
                # a past day is judged before its next midnight: a run stamped at it is the next day's
                ran = [
                    r
                    for r in ledger
                    if r["chore"] == name and (_epoch(r) < moment if ended else _epoch(r) <= moment)
                ]
                latest = max(ran, key=_epoch, default=None)
                # a run without `ts` in the hour a clock change repeats names two instants: while one of them
                # may be the last run, or fall either side of the moment, the period is unknown
                if any(
                    two[0] <= moment and (two[1] >= moment or two[1] >= _epoch(latest))
                    for r in ledger
                    if r["chore"] == name and (two := _readings(r)) is not None
                ):
                    lines.append(
                        f"{actor} {name} not judged: a run without `ts` names an hour the clock repeats"
                    )
                    continue
                first = firsts.get(name)
                if latest is None:  # no run is missing only once its first due and the grace are past
                    ticked = True
                    if first is None:  # none on record: the tick's pending row gives it, read and not kept
                        if pending is None and not agenda_error:
                            try:  # a failed read judges each chore that needs it, and the digest goes out
                                pending, short = await self._pending_firsts(now)
                            except Exception as e:
                                agenda_error = f"{type(e).__name__}: {e}"
                        fires = (pending or {}).get(f"tick: {name}")
                        if agenda_error or short:  # a row past the read's reach may give an earlier first due
                            reason = f"agenda unreadable: {agenda_error}" if agenda_error else short
                            lines.append(f"{actor} {name} not judged: {reason}")
                            continue
                        if fires and None in fires:  # a row naming no instant may be the earliest firing
                            lines.append(f"{actor} {name} not judged: a pending tick row names no first due")
                            continue
                        first, ticked = (min(fires) if fires else None), fires is not None
                    if first is not None and moment <= first + _FIRE_GRACE_S:
                        lines.append(f"{actor} {name} no run yet, first due by {_now(first)}")
                    else:
                        lines.append(
                            f"MISSING: {name}, no run recorded"
                            + (f", first due by {_now(first)}" if first is not None else "")
                            + ("" if ticked else ", no tick pending")
                        )
                    continue
                if every == "monthly":  # a calendar month in UTC, as the store counts its monthly repeat
                    t = datetime.fromtimestamp(_epoch(latest), UTC)
                    y, m = divmod(t.year * 12 + t.month, 12)
                    end = calendar.monthrange(y, m + 1)[1]
                    if first is not None:  # the next month's anchor day: the first due's UTC day, clamped
                        a = datetime.fromtimestamp(first, UTC)  # to that month's last, at its time of day
                        due = a.replace(year=y, month=m + 1, day=min(a.day, end)).timestamp()
                    else:
                        # no first due on record: a run on its month's last day is due by the next
                        # month's last (Jan 31, Feb 28, Mar 31)
                        d = end if t.day == calendar.monthrange(t.year, t.month)[1] else min(t.day, end)
                        due = t.replace(year=y, month=m + 1, day=d).timestamp()
                else:
                    due = _epoch(latest) + period_s(every)
                since = f"last run {latest['at']}, due by {_now(due)}"
                if moment > due + _FIRE_GRACE_S:
                    lines.append(f"MISSING: {name}, {since}")
                    continue
                if not mine:
                    lines.append(f"{actor} {name} not due: {since}")
                    continue
            last = mine[-1]
            if not last["control"]:
                state = f"instrument-failed:{name}"
            elif last["escalate"]:
                state = "escalate"
            elif last["findings"]:
                state = "report"
            else:
                state = "quiet"
            quiet = sum(1 for r in mine if not r["findings"])
            failed = sum(1 for r in mine if not r["control"])
            esc = sum(1 for r in mine if r["escalate"] and r["control"])
            answered = sum(1 for r in mine if r.get("answered", r["control"]))
            clause = (
                f"{len(mine)} run(s), {answered} answered, {quiet} quiet, "
                f"{len(mine) - quiet - esc - failed} report, {esc} escalate, {failed} instrument-failed"
            )
            if last["findings"]:
                clause += f"; last: {last['findings'][0][:80]}"
            if cron:
                clause += "; not judged: cron"
            lines.append(f"{actor} {name} {last['at'][11:16]} {state} {clause}")
        spend = _val(self.agent.state, "spend", {})
        if spend.get("day") == day:  # the cost of the day, the number the actor's question is about
            lines.append(
                f"tokens {day}: {spend.get('calls', 0)} model call(s), {spend.get('in', 0)} in, "
                f"{spend.get('out', 0)} out, ${spend.get('cost', 0.0):.4f}"
                + partial(spend.get("unknown"))  # a row with no count is from before the count
                + f", over {spend.get('runs', 0)} wake(s) with {spend.get('mail', 0)} mail; "
                f"{spend.get('idle', 0)} idle poll(s) at 0 tokens"
            )
        told = [r for r in rows if r["findings"] and r.get("msg")]
        if told:
            lines.append("---")
            for r in told[:60]:
                lines.append(
                    f"{r['at'][11:16]} {r['chore']} msg {r['msg']}: " + "; ".join(r["findings"])[:200]
                )
            if len(told) > 60:
                lines.append(f"[{len(told) - 60} more report(s) in the ledger]")
        async with self._briefing:
            if self.extra_digest is not None:
                lines.extend(self.extra_digest(day))
            text = "\n".join(lines)
            await self.agent.deliver(self.owner, text, thread=None, run=run, subject=f"chores digest {day}")
            if self.extra_digest_sent is not None:  # a delivery that raised told nobody: the lines come again
                self.extra_digest_sent()
        return text

    async def aged(self, chore: str | None, run: Run) -> str:
        """Reports the owner has not answered on their threads past `aged_after` periods of the chore
        go to the steward, once each. An acknowledged report is closed; an unreadable thread counts
        as unanswered and says so."""
        if not self.steward:
            return "aged: no steward"
        async with self._aging:
            now, done, rows = time.time(), set(_val(self.agent.state, "aged_sent", [])), self.rows()
            due: list[tuple[dict, float, str]] = []
            uncounted: list[dict] = []  # reports whose time is no instant to age from: named, never aged
            for r in rows:
                if not r.get("msg") or not r["findings"] or self.steward in (r.get("sent_to") or []):
                    continue  # only reports the owner alone received; the steward has the rest already
                if chore and r["chore"] != chore:
                    continue
                key = _aged_key(r)
                if key in done:
                    continue
                inst = self.instruments.get(r["chore"])
                period = period_s(inst.every if inst else None)
                if (t := _countable(r)) is None:
                    uncounted.append(r)
                    continue
                age = now - t
                if age < self.aged_after * period:
                    continue
                acked, how = await self._acknowledged(r)
                if acked:
                    done.add(key)
                    continue
                due.append((r, age, how))
            if due:
                lines = [
                    f"[chores aged] {len(due)} report(s) to {self.owner} unanswered past "
                    f"{self.aged_after} period(s) of their chore; the remedy and the reply are the actor's"
                ]
                for r, age, how in due:
                    more = f" (+{len(r['findings']) - 1} more)" if len(r["findings"]) > 1 else ""
                    lines.append(
                        f"- {r['chore']} {r['at']} ({age / 3600:.1f}h; {how}; thread {r.get('thread')}): "
                        f"{r['findings'][0][:120]}{more}"
                    )
                await self.agent.deliver(
                    self.steward,
                    "\n".join(lines),
                    thread=None,
                    run=run,
                    subject=f"aged: {len(due)} unanswered report(s) from {self.owner}",
                )
                for r, _, _ in due:
                    done.add(_aged_key(r))
            # a marker lives as long as the ledger row it covers: no count of reports lets one be told twice
            live = {_aged_key(r) for r in rows}
            self.agent.state.put("aged_sent", sorted(done & live), run=run.id, seq=run.record.seq)
            said = f"aged: {len(due)} report(s) sent to {self.steward}" if due else "aged: nothing unanswered"
            if uncounted:
                said += (
                    f"; {len(uncounted)} report(s) not aged, their time is no instant to count from: "
                    + ", ".join(f"{r['chore']} {r.get('at')!r}" for r in uncounted[:5])
                )
            return said

    # ticks

    async def ensure_ticks(self, *, first_in_s: int | None = None) -> list[str]:
        """Every instrument with a cadence has a pending reminder saying `tick: <chore>`; the digest
        has one (`digest_every`: daily, or a front desk's brief); the aged pass has its own when there is
        a steward. Creates what is missing and returns what it created; after a short agenda read (capped or
        stalled) it creates and records nothing, drops a record beside a row naming no instant, and names
        each tick it left. A chore's first firing is kept
        in the agent's state (`first_due`) for the digest: one on record stands, and where there is none the
        tick's own firing is recorded when it was just made, else what its pending rows give
        (`_first_firing`); a tick with a row naming no instant has none, and one on record is dropped."""
        pending, short = await self._pending_firsts(self.agent.clock())
        wanted = {f"tick: {name}": inst.every for name, inst in self.instruments.items() if inst.every}
        wanted["tick: digest"] = self.digest_every
        if self.steward:
            wanted["tick: aged"] = self.aged_every
        wanted.update(self.extra_ticks)
        # a row naming no instant may be the earliest firing: a tick with one has no first due on record,
        # and one it had is dropped, from a short read too, since the row it saw stands; the digest reads
        # the rows
        blind = {c for c in self.instruments if None in pending.get(f"tick: {c}", ())}
        firsts = _val(self.agent.state, "first_due", {})
        if blind & firsts.keys():
            self.agent.state.put(
                "first_due", {k: v for k, v in firsts.items() if k not in blind}, run="-", seq=0
            )
        if short:  # a read that did not see every row says neither that a tick is absent nor its first due
            return [f"{content}: not created, {short}" for content in wanted]
        created = []
        lead = _TICK_LEAD_S if first_in_s is None else first_in_s
        for content, every in wanted.items():
            fires = pending.get(content)
            if fires is None:
                # the clock is read for each call, so a row's stamp trails its own call's read alone
                at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + lead))
                await self.agent.khive.exec(
                    f"schedule.remind(content={lit(content)}, at={lit(at)}, repeat={lit(every)})"
                )
                created.append(content)
                fires = [_iso_epoch(at)]
            chore, firsts = content.removeprefix("tick: "), _val(self.agent.state, "first_due", {})
            if chore not in self.instruments:
                continue
            if None in fires:  # its record was dropped above
                continue
            # a first due on record stands: a row cannot say what lead its tick was made with, so what it
            # gives is no reason to lower one
            if _instant(firsts.get(chore)) is None:
                self.agent.state.put("first_due", {**firsts, chore: min(fires)}, run="-", seq=0)
        return created

    async def _pending_firsts(self, now: float) -> tuple[dict[Any, list[float | None]], str | None]:
        """content -> the first firing each of this actor's pending reminders' rows gives (`_first_firing`,
        None for a row naming no instant), and why the read is short, if it is. The agenda answers at most
        200 rows, earliest first, so while a page is full it is read again from its last row's `trigger_at`,
        rows merged by id, up to 25 pages: a read ending on a full 25th page did not see every row
        (`_CAPPED`). `from` is inclusive, so a full page of rows at one instant comes back whole on the
        read after it: a full page that brings no new row is a stall, the read ends there, and the rows
        past that instant are out of its reach. A read: it makes no reminder and records nothing, for
        `ensure_ticks` and the digest alike."""
        me = self.agent.khive.actor  # the agenda is the namespace's: another agent's tick is not this one's
        pending: dict[Any, list[float | None]] = {}
        seen: set[Any] = set()
        after = ""
        for _ in range(_AGENDA_PAGES):
            (agenda,) = await self.agent.khive.exec(
                f"schedule.agenda(limit={_AGENDA_PAGE}" + (f", from={lit(after)})" if after else ")")
            )
            events = agenda.get("events") if isinstance(agenda, dict) else None
            if not isinstance(events, list):  # an answer that is no page is a failed read, not an empty one
                raise ValueError(f"the agenda answer is no page: {str(agenda)[:80]}")
            new = 0
            for e in events:
                if e["id"] in seen:  # the row a page ends on is the first of the next
                    continue
                seen.add(e["id"])
                new += 1
                p = e.get("properties") or {}
                if p.get("status", "pending") != "pending" or p.get("created_by_actor") != me:
                    continue
                pending.setdefault(e.get("content"), []).append(_first_firing(e, now))
            if len(events) < _AGENDA_PAGE:
                return pending, None
            if not new:
                return pending, f"the agenda read stalled at {after}: a full page of rows share that instant"
            after = (events[-1].get("properties") or {}).get("trigger_at")
            if not isinstance(after, str) or not after:
                raise ValueError(
                    f"a full agenda page ends at a row with no trigger_at: {str(events[-1])[:80]}"
                )
        return pending, _CAPPED

    async def _catch_up(self, run: Run, *, skip: str | None) -> list[str]:
        """Once per run: the actor's own reminders khive marked missed (never fired) or failed, not
        seen before, run now. A missed digest or aged tick runs those handlers."""
        if self._caught_up == run.id:
            return []
        self._caught_up = run.id
        me, items = self.agent.khive.actor, []
        try:  # the store filters by creator and status before a page; a row caught up keeps its status, so
            # the first page alone comes back every pass: each list is paged by offset until it says no more
            offsets = {"missed": 0, "failed": 0}
            while offsets:
                live = list(offsets)
                pages = await self.agent.khive.exec(
                    "["
                    + ", ".join(
                        f'list(kind="scheduled_event", created_by_actor={lit(me)}, status={lit(s)}, limit=200'
                        + (f", offset={offsets[s]}" if offsets[s] else "")
                        + ")"
                        for s in live
                    )
                    + "]"
                )
                for s, rows in zip(live, pages, strict=True):
                    # a page is a dict with a list under `items`, or a bare list; anything else, or an
                    # empty page that says more is behind it, is a broken read: nothing runs or is let go
                    if isinstance(rows, dict):
                        got, more = rows.get("items"), rows.get("has_more")
                        more = len(got) == 200 if more is None and isinstance(got, list) else more
                    else:
                        got, more = rows, isinstance(rows, list) and len(rows) == 200
                    if not isinstance(got, list) or (not got and more):
                        raise ValueError(
                            f"the {s} page at offset {offsets[s]} is not a page: {str(rows)[:80]!r}"
                        )
                    items += got
                    offsets[s] += len(got)
                    if not got or not more:
                        del offsets[s]
        except Exception as e:
            return [f"missed-tick read failed: {type(e).__name__}: {e}"]
        seen, done = list(_val(self.agent.state, "missed_seen", [])), []
        seen_ids = set(seen)  # membership by set: the list keeps the order the record shows
        for it in items:
            p, status = it.get("properties") or {}, it.get("status")  # status sits beside properties
            if p.get("created_by_actor") != me or status not in ("missed", "failed") or it["id"] in seen_ids:
                continue
            seen.append(it["id"])
            seen_ids.add(it["id"])
            content = str(it.get("content") or "")
            m = re.match(r"tick: ([\w-]+)", content)
            chore = m.group(1) if m else None
            if chore == "digest":
                await self.digest(None, run)
                done.append(f"{content}: {status}, digest sent now")
            elif chore == "aged":
                done.append(f"{content}: {status}, " + await self.aged(None, run))
            elif chore == skip:  # the chore this check runs: its run now covers the missed tick
                done.append(f"{content}: {status}, this check runs it")
            elif chore in self.instruments:
                done.append(
                    f"{content}: {status}, ran now: "
                    + await self.check(Check(chore=chore, args={"missed_tick": it["id"]}), run)
                )
            else:
                done.append(f"{content}: {status}, noted")
        # an id is kept while the store lists its row: no count of rows lets one run twice. A list read
        # by offset can skip a row one pass, so an id is let go only when two passes in a row miss it,
        # and then only on the store's own word for that id: a row it still holds was skipped twice
        listed = {it["id"] for it in items}
        gone_before = set(_val(self.agent.state, "missed_gone", []))
        gone = [i for i in seen if i not in listed]
        held = set()
        for i in gone:
            if i not in gone_before:
                continue
            try:
                row = (await self.agent.khive.exec(f"get(id={lit(i)})"))[0]
            except Exception as e:
                # only the store's own not-found for this id lets it go; any other failed read keeps it,
                # whatever its text says
                if not (isinstance(e, KhiveError) and e.kind == "not_found"):
                    held.add(i)
                continue
            status = row.get("status") if isinstance(row, dict) else None
            if not isinstance(status, str) or status in ("missed", "failed"):  # no status = no word
                held.add(i)
        kept = [i for i in seen if i in listed or i not in gone_before or i in held]
        if done or kept != seen:
            self.agent.state.put("missed_seen", kept, run=run.id, seq=run.record.seq)
        if set(gone) != gone_before:
            self.agent.state.put("missed_gone", gone, run=run.id, seq=run.record.seq)
        return done

    def profiles(self) -> tuple[Profile, Profile]:
        """The two hats: CHORES for admitted mail (the owner, the steward, the agent itself, the actor's
        own extra senders); READER for a caller that chooses to read anyone else, which can only say
        what arrived. The agent's gate (`admit`) is the boundary; READER is for callers without it."""
        chores = "\n".join(
            f"  {name}: {inst.about} [{inst.direction}]"
            + (f" (every {inst.every})" if inst.every else " (on ask)")
            + f"; answers: {inst.answers}"
            for name, inst in sorted(self.instruments.items())
        )
        chores = Profile(
            name="chores",
            specs=frozenset({"check", "digest", "aged"}) | self.extra_specs,
            privileges=frozenset({"comm.send"}),
            system=guidance("chores", owner=self.owner, chores=chores) + self.extra_system,
        )
        reader = Profile(
            name="reader",
            specs=frozenset(),
            system=guidance("reader"),
        )
        return chores, reader

    # bookkeeping

    def rows(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]

    def _append(self, row: dict) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def _last_to(self, chore: str, to: str) -> dict | None:
        """The last row of this chore that `to` received, on whichever thread carried it."""
        for r in reversed(self.rows()):
            if r["chore"] == chore and to in (r.get("sent_to") or []):
                return r
        return None

    async def _acknowledged(self, row: dict) -> tuple[bool, str]:
        """Has the owner written on the report's thread since the report, the instant the aged pass
        counts from? Unreadable reads as no."""
        since = _countable(row)
        if since is None:
            return False, "no instant to count from"
        thread = row.get("thread")
        if not thread:
            return False, "no thread recorded"
        try:
            (t,) = await self.agent.khive.exec(f"comm.thread(id={lit(thread)})")
        except Exception as e:
            return False, f"thread unreadable: {type(e).__name__}: {e}"
        msgs = t.get("messages", []) if isinstance(t, dict) else []
        for m in msgs:
            if (
                sender_of(m) == self.owner
                and _iso_epoch((m.get("properties") or {}).get("sent_at") or m.get("created_at")) > since
            ):
                return True, "answered"
        return False, f"no reply among {len(msgs)} message(s)"

    async def _on_chore_thread(self, chore: str, to: str, text: str, run: Run, *, subject: str) -> dict:
        """One thread per chore, recipient and day: the agent's hop cap then bounds what a chore may
        say to one actor in a day. The first message opens the thread; the id is kept in state."""
        key = f"{chore}:{_now()[:10]}:{to}"
        state = self.agent.state
        known = state.get("chore_threads")
        sent = await self.agent.deliver(
            to, text, thread=(known.value if known else {}).get(key), run=run, subject=subject
        )
        # read again after the send: another chore's thread may have landed while this one was out
        now = state.get("chore_threads")
        threads = dict(now.value) if now else {}
        if key not in threads:
            threads[key] = str(sent.get("thread_id") or sent.get("id"))
            state.put("chore_threads", threads, run=run.id, seq=run.record.seq)
        return sent

    def _cap(self, evidence: str) -> str:
        if len(evidence) <= self.evidence_cap:
            return evidence
        return (
            evidence[: self.evidence_cap]
            + f"\n[evidence capped: {self.evidence_cap} of {len(evidence)} chars]"
        )

    def _text(
        self, chore: str, inst: Instrument, out: Outcome, seconds: float, *, supersedes: str, same: bool
    ) -> str:
        head = f"[chore {chore}] "
        if out.escalate:
            head += "ESCALATE, the remedy is the actor's: "
        head += (
            f"{len(out.findings)} finding(s), control {'ok' if out.control else 'FAILED'}, {seconds:.1f}s, "
            f"direction {inst.direction}"
        )
        lines = [
            head,
            f"supersedes: {supersedes}" + (" (unchanged: same findings as that report)" if same else ""),
            f"answers: {inst.answers}",
        ]
        if out.positive:
            lines.append(f"known-positive: {out.positive}")
        if out.population or out.predicate:
            n = "" if out.count is None else f" ({out.count})"
            lines.append(f"population: {out.population or '?'}{n}\npredicate: {out.predicate or '?'}")
        lines.append("\n".join(f"- {f}" for f in out.findings) or "- nothing to report")
        ev = self._cap(out.evidence)
        if ev:
            lines.append(f"evidence:\n{ev}")
        return "\n".join(lines)

    def _subject(self, chore: str, out: Outcome) -> str:
        if out.escalate:
            return f"ESCALATE {chore}: {out.findings[0][:80] if out.findings else 'instrument'}"
        return f"chore {chore}: {'quiet' if not out.findings else f'{len(out.findings)} finding(s)'}"


def sender_of(m: dict) -> str:
    return (m.get("properties") or {}).get("from_actor") or m.get("from") or "?"


_EVERY = re.compile(r"every:(\d+)([smhd])")
_NAMED = {"daily": 86400.0, "weekly": 7 * 86400.0, "monthly": 30 * 86400.0}
_FIRE_GRACE_S = 300.0  # the store fires a reminder up to this late before it marks the reminder missed
_TICK_LEAD_S = 60  # a tick is made to first fire this long after its creation (`ensure_ticks`, the ceiling)
_AGENDA_PAGE = 200  # the most rows one `schedule.agenda` read answers
_AGENDA_PAGES = 25  # the pages one read of the agenda follows
_CAPPED = f"the agenda read was capped at {_AGENDA_PAGE * _AGENDA_PAGES} rows"
_TIME_END = datetime(9999, 1, 1, tzinfo=UTC).timestamp()  # a monthly chore steps a month past its last run


def period_s(every: str | None) -> float:
    """Seconds in one period of a khive repeat, as the aged pass counts it; a cron or nothing reads as
    a day. The digest judges a monthly chore by the calendar instead."""
    if not every:
        return 86400.0
    m = _EVERY.fullmatch(every)
    if m:
        return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return _NAMED.get(every, 86400.0)


def _now(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _aged_key(row: dict) -> str:
    """An aged marker: the chore and its report's label, and the instant when the row carries one, so
    two reports in the hour a clock change repeats are two markers."""
    ts = row.get("ts")
    key = f"{row['chore']}|{row['at']}"
    if not isinstance(ts, int | float) or isinstance(ts, bool):
        return key
    try:
        return f"{key}|{ts:.6f}"
    except OverflowError:  # an integer no float holds: `_countable` never ages that row
        return key


_LABEL = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def _is_label(at: str) -> bool:
    """`at` as `_now` writes it, in that form only (the digest selects a day by its first ten characters)
    and naming a time that exists: it comes back from local time as itself, so the hour a clock change
    skips names none, and a repeated hour names one. A year the digest cannot count names none either,
    the same on every platform: some C libraries raise for it, others return an instant before 1970."""
    try:
        named = time.strptime(at, "%Y-%m-%d %H:%M:%S")
        t = time.mktime(named)
        back = time.localtime(t)[:6]
    except (ValueError, OverflowError, OSError):
        return False
    return _LABEL.fullmatch(at) is not None and 0 <= t < _TIME_END and back == named[:6]


def _is_number(v: Any) -> bool:
    """A number that is no bool and no NaN or infinity, which a JSON line can spell."""
    return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and math.isfinite(v))


def _instant(v: Any) -> float | None:
    """A stored first due as an instant: a finite number, no bool, that a UTC date can name. Anything else is
    none, to the digest that reads it and to `ensure_ticks` that keeps or replaces it."""
    if not _is_number(v):
        return None
    try:
        datetime.fromtimestamp(v, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return float(v)


def _epoch(row: dict) -> float:
    """The row's instant: its `ts`; a row written before rows carried one reads its local `at`, which
    names two instants in the hour a clock change repeats."""
    ts = row.get("ts")
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        return float(ts)
    return time.mktime(time.strptime(row["at"], "%Y-%m-%d %H:%M:%S"))


def _countable(row: dict) -> float | None:
    """The row's instant as the digest counts one: its `at` a label `_is_label` admits, its `ts` a finite
    number where it has one, and the time from 1970 to before `_TIME_END`, on the day the `at` names. A row
    without `ts` in the hour a clock change repeats counts from the later of its two instants, so it ages
    once both are old enough. None for any other row, on every platform alike."""
    at = row.get("at")
    if not isinstance(at, str) or not _is_label(at) or ("ts" in row and not _is_number(row["ts"])):
        return None
    try:
        two = _readings(row)
        t = two[1] if two else _epoch(row)
    except OverflowError:  # an integer `ts` no float holds
        return None
    if not 0 <= t < _TIME_END or time.strftime("%Y-%m-%d", time.localtime(t)) != at[:10]:
        return None
    return t


def _readings(row: dict) -> tuple[float, float] | None:
    """The two instants a row without `ts` names when its local `at` is in the hour a clock change repeats,
    earlier first; None for any other row."""
    ts = row.get("ts")
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        return None
    named = time.strptime(row["at"], "%Y-%m-%d %H:%M:%S")
    both = {time.mktime((*named[:8], dst)) for dst in (0, 1)}
    both = {t for t in both if time.localtime(t)[:6] == named[:6]}
    return (min(both), max(both)) if len(both) == 2 else None


def _iso_epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _months_before(t: datetime, n: int) -> datetime:
    """`t` n calendar months earlier, its day clamped to that month's last, as the store clamps a month."""
    y, m = divmod(t.year * 12 + t.month - 1 - n, 12)
    return t.replace(year=y, month=m + 1, day=min(t.day, calendar.monthrange(y, m + 1)[1]))


def _first_firing(row: dict, now: float) -> float | None:
    """The first firing a pending reminder's row implies, or None when it names no instant. The store moves
    `trigger_at` on to the next firing after each delivery and keeps the row pending, so it is no first
    due. A monthly row keeps its first firing as `repeat_anchor`, the instant its months count from. Else
    the first is the latest firing not after the ceiling of the row's creation (`created_at`, beside
    `properties`): a fixed period's firings lie a period apart, a monthly row's a calendar month in UTC. A
    monthly firing on the last day of a month of fewer than 31 days may be a clamped later day: that month's
    latest day at the firing's time not after the ceiling, or the ceiling itself, an upper bound, when the
    step-back is already after it. A row naming no creation instant, or a period that is not fixed (a cron),
    gives its firing or now, if that is earlier."""
    p = row.get("properties") or {}
    repeat = p.get("repeat")
    anchor = p.get("repeat_anchor") if repeat == "monthly" else None
    when = anchor or p.get("trigger_at")
    if not isinstance(when, str) or not when:
        return None
    try:
        first = _iso_epoch(when)
    except (ValueError, OverflowError):
        return None
    if anchor:
        return first
    made = row.get("created_at")
    try:
        born = _iso_epoch(made) if isinstance(made, str) and made else None
    except (ValueError, OverflowError):
        born = None
    # no tick first fires after this: `ensure_ticks` fixes a first firing the lead after its own clock read,
    # to the whole second, and the store stamps the row after that read, a second more for a whole-second
    # stamp. The stamp trails the read by the call's latency, which has no bound: a period no longer than
    # that latency and up to two seconds may recover whole periods late, and a longer custom lead, early.
    ceiling = None if born is None else born + _TICK_LEAD_S + 1
    if repeat == "monthly" and ceiling is not None:
        try:
            t, c = datetime.fromtimestamp(first, UTC), datetime.fromtimestamp(ceiling, UTC)
        except (ValueError, OverflowError, OSError):
            return min(first, now)
        if t <= c:
            return first
        n = (t.year - c.year) * 12 + t.month - c.month  # months from the ceiling's month to the firing's
        step = _months_before(t, n)
        last = calendar.monthrange(t.year, t.month)[1]
        clamped = t.day == last and last < 31  # the month's firing may be a later day, clamped to this one
        if step > c:  # that month's firing is after the ceiling
            return ceiling if clamped else _months_before(t, n + 1).timestamp()
        if not clamped:
            return step.timestamp()
        # the latest day the firing may have been on, at its time of day, not after the ceiling
        latest = c.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=t.microsecond)
        return (latest - timedelta(days=1) if latest > c else latest).timestamp()
    fixed = isinstance(repeat, str) and (repeat in _NAMED or _EVERY.fullmatch(repeat) is not None)
    period = period_s(repeat) if fixed else 0.0
    if ceiling is None or period <= 0:
        return min(first, now)
    return first - max(0.0, -((ceiling - first) // period)) * period


def _val(store: Any, key: str, default: Any) -> Any:
    note = store.get(key)
    return note.value if note is not None and isinstance(note.value, type(default)) else default
