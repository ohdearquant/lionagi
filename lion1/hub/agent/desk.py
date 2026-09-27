"""The front desk: the chair's cheap first reader.

A chair's inbox is mostly questions of fact an instrument can answer (what state is PR X in, is the
head green, how much disk, when did you report Y) and notices that need no answer. Each of them
costs the chair a wake of a large model. The desk is the chair's long-running agent that wakes when a
message the chair has not handled lands in the chair's inbox (a long-poll of that box beside the desk's
own, at no model cost while nothing arrives) and answers, on the sender's thread, what one instrument
can answer; everything else it leaves for the chair, with the reason recorded. The chair's mail is
never marked read: the chair still sees everything, and the desk ledger says what was already answered.

What the desk may answer is a mechanical test, not the model's confidence: a question of fact,
answered by one instrument whose control passed, in a reply that commits the chair to nothing. The
model's part is routing: which listed message maps to which chore. The reply text is composed by code
from the outcome, as every chore report is; nothing the model writes reaches a sender. Mail that
cannot wait never reaches the model: a person's, a sender in `escalate_from`'s, one whose subject
carries `[NOW]`, and a sibling desk's `ESCALATE` go to the chair at once, the body word for word, by
code; a review daemon's verdict notice (a sender in `verdict_from`) is triaged by code
(`verdicts.Verdicts`) and filed, left or escalated, never listed; a message the model names that the
sweep did not list is refused.

The reader is a second client bound to the chair's own identity and allow-listed to two read verbs,
because khive does not yet let an actor read a visible namespace's inbox. When it does, the reader
becomes the agent's own client with `namespace=`; nothing else here changes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hub import guidance
from lionagi.actor import Context, Run

from ..khive import LOST, Khive, KhiveError, lit
from .agent import Agent, AgentError
from .chores import Bounded, Chores, Outcome, sender_of

__all__ = (
    "READ_VERBS",
    "WHY",
    "CLARIFY_SENDERS",
    "CLARIFY_MARK",
    "Reader",
    "Sweep",
    "Answer",
    "Leave",
    "Clarify",
    "Route",
    "Task",
    "Desk",
    "after",
)

READ_VERBS = frozenset({"comm.inbox", "comm.thread"})  # the chair reader's whole vocabulary
WHY = ("judgment", "action", "commitment", "from-person", "notice", "no-instrument")
CLARIFY_SENDERS = ("lambda:", "daemon:")  # a templated question in the chair's name goes to agents only
CLARIFY_MARK = "[desk clarify]"  # how a clarify begins; a message that begins so is never clarified
_REPLIES = ("answered", "clarified")  # sent to the sender on its thread; every other send is a forward
_BRIEFED = ("escalated", "routed", "tasked", "answered", "clarified")  # the brief's row lines, in order


class Reader:
    """The chair's box as the desk reads it: `exec` under the agent boundary with READ_VERBS, and the
    identity it reads as. Nothing else of the chair's client is reachable: no send, mark or grant."""

    allowed = READ_VERBS

    def __init__(self, khive: Khive):
        self.actor, self.cwd = khive.actor, khive.cwd
        self._exec = Bounded(khive, allowed=READ_VERBS, recipients=frozenset()).exec

    async def exec(self, ops: str) -> list:
        return await self._exec(ops)


class Sweep(BaseModel):
    """List the chair's unread mail since the desk went live that the desk has not answered or left,
    oldest first, a page at a time. Each message is then answered or left by its id."""


class Answer(BaseModel):
    """Answer one listed message with an instrument. The reply is composed by code from the outcome and
    sent to the sender on the message's thread; the chair's copy stays unread."""

    msg: str = Field(description="the message id exactly as the sweep listed it")
    chore: str = Field(description="the chore whose instrument answers the question")
    args: dict[str, Any] = Field(
        default_factory=dict, description="what the question narrowed to, if anything"
    )


class Leave(BaseModel):
    """Leave one listed message for the chair: it asks for judgment, an action or a commitment, comes
    from a person, is a notice with no question, or no instrument answers it. Nothing is sent."""

    msg: str = Field(description="the message id exactly as the sweep listed it")
    why: str = Field(description=f"which test failed, one of {WHY}")


class Clarify(BaseModel):
    """Ask the sender which of two readings a message means, when each reading is a question of fact
    one chore answers. Both readings are quoted word for word from the message; the question itself is
    composed by code; the answer arrives as a later message and is answered or left then. Once per
    thread; never to a person's mail; never for a message that is itself a clarify."""

    msg: str = Field(description="the message id exactly as the sweep listed it")
    a: str = Field(description="one reading, the message's own words")
    b: str = Field(description="the other reading, the message's own words")


class Route(BaseModel):
    """Forward one listed message from another department to the desk of this area that owns its work.
    Code sends the message word for word to the desk the routes table names; nothing else is sent."""

    msg: str = Field(description="the message id exactly as the sweep listed it")
    desk: str = Field(description="the desk's name in the routes table")


class Task(BaseModel):
    """File one listed message as work for the chair: an agent asks the chair to do, fix, build, review or
    decide something by a date and no chore answers it. Code writes one row in the chair's `inbox` lane
    with the message word for word; the chair triages it. Nothing is sent."""

    msg: str = Field(description="the message id exactly as the sweep listed it")
    title: str = Field(description="the work in a few words, at most 120 characters")


@dataclass
class Desk:
    agent: Agent
    chores: Chores
    reader: Any  # the chair's box, read (`Reader`); it never marks or sends
    ledger: Path  # append-only JSONL, one row per message handled
    page: int = 20
    answers_per_hour: int = 10
    clarify_hours: int = 24  # a clarify unanswered this long is left for the chair, in the digest
    never: tuple[str, ...] = ("email:",)  # sender prefixes the desk never answers: a person's mail
    escalate_from: tuple[str, ...] = ()  # senders whose mail is escalated, never answered
    escalations_per_day: int = 40  # past this an escalation is left `cap: escalations` for the brief
    routes: dict[str, str] = field(default_factory=dict)  # `[desk.routes]`: name -> a desk of this area
    tasks_per_day: int = 20  # past this a task is left `cap: tasks` for the brief
    verdicts: Any = None  # `verdicts.Verdicts`: a notice from `verdict_from` is triaged by code
    verdict_from: tuple[str, ...] = ()  # the review daemons whose verdict notices are triaged
    _listed: tuple[str, frozenset[str]] = ("", frozenset())  # (run id, ids its sweeps listed)
    _busy: set[str] = field(default_factory=set)  # ids a handler, a send or its replay, or a task holds now
    _brief: tuple[str, int] | None = None  # (window end, sweeps counted) of the brief not yet delivered
    # the chair's own mail to itself is answered like any other: it is how the chair asks its desk

    def __post_init__(self) -> None:
        owner, me = self.chores.owner, self.agent.khive.actor
        bad = {n: a for n, a in self.routes.items() if not a.startswith(f"{owner}:") or a == me}
        if bad:
            raise ValueError(f"[desk.routes] names desks of {owner} other than {me} only; refused: {bad}")
        actor = self.agent.actor

        @actor.handler(Sweep, requires={"comm.send"})
        async def sweep(req: Sweep, ctx: Context) -> str:
            return await self.sweep(ctx.run)

        @actor.handler(Answer, requires={"comm.send"})
        async def answer(req: Answer, ctx: Context) -> str:
            return await self.answer(req, ctx.run)

        @actor.handler(Leave, requires={"comm.send"})
        async def leave(req: Leave, ctx: Context) -> str:
            return await self.leave(req, ctx.run)

        @actor.handler(Clarify, requires={"comm.send"})
        async def clarify(req: Clarify, ctx: Context) -> str:
            return await self.clarify(req, ctx.run)

        @actor.handler(Route, requires={"comm.send"})
        async def route(req: Route, ctx: Context) -> str:
            return await self.route(req, ctx.run)

        @actor.handler(Task, requires={"comm.send"})
        async def task(req: Task, ctx: Context) -> str:
            return await self.task(req, ctx.run)

        self.chores.extra_specs = frozenset({"sweep", "answer", "leave", "clarify", "route", "task"})
        self.chores.extra_digest = self.digest_lines
        self.chores.extra_digest_sent = self._briefed
        self.agent.accept = self._accept
        self.agent.watch = self.watch
        routes = ", ".join(f"{n} ({a})" for n, a in sorted(self.routes.items())) or "none, so route nothing"
        self.chores.extra_system = "\n" + guidance("desk", owner=owner, routes=routes)

    # the handlers

    def _accept(self, run: Run, output: Any) -> str | None:
        """OUT{} waits while a message this wake's sweep listed has no ledger row: a model can write
        OUT{} in the sweep's own round, and the listed message is then never routed. A delivery
        unresolved may wait: the next discovery replays it."""
        listed_run, listed = self._listed
        if listed_run != run.id:
            return None
        unresolved = {mid for mid, m in _val(self.agent.state, "desk_pending", {}).items() if m.get("send")}
        left = sorted(listed - {r["msg"] for r in self.rows()} - unresolved)
        if not left:
            return None
        return (
            f"the sweep listed {len(left)} message(s) you have not answered or left: {left}; "
            'call answer(msg="<id>", chore="<chore>", args={"query": "..."}) or leave(msg="<id>", '
            'why="<test>") for each, under a fresh alias, then OUT'
        )

    async def watch(self, wait_ms: int) -> list[dict]:
        """Wake the desk for the chair's unfinished mail: discovery waits up to `wait_ms` for a new row
        and does not wait while a message is pending, so a pending message wakes the desk again without a
        new arrival. A delivery unresolved or a task code filed wakes nothing: no new request may be made
        for it, and discovery replays its keyed request each time. The notice is `tick: sweep`; the sweep
        lists what is pending."""
        held = _val(self.agent.state, "desk_pending", {})
        pending = await self._discover(0 if any(not _by_code(m) for m in held.values()) else wait_ms)
        pending = {mid: m for mid, m in pending.items() if not _by_code(m)}
        if not pending:
            return []
        me = self.agent.khive.actor
        return [
            {
                "id": f"watch:{len(pending)}",
                "full_id": f"watch:{len(pending)}",
                "subject": f"watch: {len(pending)} message(s) pending for {self.chores.owner}",
                "content": "tick: sweep",
                "properties": {"from_actor": me, "thread_id": None},
            }
        ]

    async def _discover(self, wait_ms: int = 0) -> dict[str, dict]:
        """Every page of the chair's unread box since the discovery cursor (`desk_watch`, an instant; the
        box's `since` is an inclusive bound on `created_at` to the microsecond). A row
        no ledger row holds and the desk did not send joins `desk_pending`, which is written before the
        cursor moves one microsecond past the newest stamp read; a stampless row moves nothing. Only a
        one-page scan moves it: offsets index the unread set, which the chair changes between pages, so
        a scan that paged may have skipped a row and the next reads the interval again.
        Only a ledger row retires a pending message. A delivery unresolved is settled here by replaying its
        recorded request under its key: `replayed` means the attempt had landed, otherwise it lands now,
        once; a failure settles nothing. No clock and no thread order decide it. A new row that cannot
        wait is escalated here, by code, and never joins the sweep's list; its send is recorded like an
        answer's and settled the same way. A new verdict notice is triaged here, by code, and never joins
        it either: filed, left or escalated; a task it filed whose outcome is unknown is replayed as a
        send is. The pending messages, after."""
        state = self.agent.state
        since = _val(state, "desk_watch", "") or _val(state, "desk_since", "")
        if not since:  # the desk handles what arrives after it went live, not the chair's backlog
            since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            state.put("desk_since", since, run="-", seq=0)
        rows: dict[str, dict] = {}
        offset, paged = 0, False
        while True:
            args = ['status="unread"', f"limit={self.page}", f"since={lit(since)}"]
            if offset:
                args.append(f"offset={offset}")
            elif wait_ms:  # the long-poll is the first page's; the rest are read at once
                args.append(f"wait_ms={max(0, min(int(wait_ms), Khive.WAIT_CAP_MS))}")
            (page,) = await self.reader.exec(f"comm.inbox({', '.join(args)})")
            got = page.get("messages", []) if isinstance(page, dict) else (page or [])
            for m in got:
                rows.setdefault(str(m.get("full_id") or m.get("id")), m)
            if not got or not (isinstance(page, dict) and page.get("has_more")):
                break
            paged = True
            offset = int(page.get("next_offset") or offset + len(got))
        ledger = self.rows()
        done = {r["msg"] for r in ledger}
        me, owner = self.agent.khive.actor, self.chores.owner
        held = _val(state, "desk_pending", {})
        pending = {mid: m for mid, m in held.items() if mid not in done}
        now: list[tuple[str, dict]] = []
        notices: list[tuple[str, dict]] = []
        for mid, m in rows.items():
            if mid in done or mid in pending or sender_of(m) == me:
                continue
            p = m.get("properties") or {}
            entry = {
                "from": sender_of(m),
                "subject": m.get("subject") or "",
                "thread": p.get("thread_id") or m.get("thread_id"),
                "outbound_ref": p.get("outbound_ref") or m.get("outbound_ref"),
                "content": (m.get("content") or "")[:2000],
                "at": str(m.get("created_at") or ""),
            }
            sender, subject = entry["from"], entry["subject"]
            if (
                sender.startswith(self.never)
                or sender in self.escalate_from
                or "[NOW]" in subject
                or (sender.startswith(f"{owner}:") and subject.startswith("ESCALATE"))
            ):
                now.append((mid, entry))
            elif self.verdicts is not None and sender in self.verdict_from:
                notices.append((mid, entry))
            else:
                pending[mid] = entry
        if pending != held:
            state.put("desk_pending", pending, run="-", seq=0)
        for mid in sorted(k for k, m in pending.items() if m.get("send")):
            live = _val(state, "desk_pending", {}).get(mid) or {}
            if mid in self._busy or not live.get("send") or mid in {r["msg"] for r in self.rows()}:
                continue  # a send or a discovery beside this one holds or settled it
            send = live["send"]
            self._busy.add(mid)
            try:
                sent = await self._deliver(send, send["attempt"])
            except Exception as e:  # key_conflict or no answer settles nothing; the next discovery replays
                self.agent.errors.append(f"desk: replay of {mid} unresolved: {type(e).__name__}: {e}")
            else:
                if mid in {r["msg"] for r in self.rows()}:
                    continue  # settled while the replay was in flight
                row = {k: v for k, v in send["row"].items() if k != "class"}
                settled = "replayed" if sent.get("replayed") is True else "landed"
                reply = str(sent.get("full_id") or sent.get("id"))
                self._row(
                    mid, live, send["row"]["class"], run=send["attempt"], **row, reply=reply, settled=settled
                )
            finally:
                self._busy.discard(mid)
        for mid in sorted(k for k, m in pending.items() if m.get("by") == "code"):
            live = _val(state, "desk_pending", {})
            if mid in self._busy or mid not in live or mid in {r["msg"] for r in self.rows()}:
                continue  # a discovery beside this one holds or settled it; the next replays what is left
            try:
                await self._file(mid, live[mid], "-")
            except Exception as e:  # still unknown: the next discovery replays it again
                self.agent.errors.append(f"desk: task of {mid} unresolved: {type(e).__name__}: {e}")
        for mid, m in now:  # to the chair, before any model reads it; the chair's copy stays unread
            text = await self._forwarded(mid, m)
            if (
                mid in self._busy
                or mid in _val(state, "desk_pending", {})
                or mid in {r["msg"] for r in self.rows()}
            ):
                continue  # a discovery beside this one took it while the thread was read
            await self._escalate(mid, m, text)
        for mid, m in notices:  # the daemon mails the owning chair itself, so nothing is forwarded
            text = await self._forwarded(mid, m)
            try:
                got = await self.verdicts.triage(text.partition("\n\n")[2])
            except Exception as e:  # a triage that failed decided nothing
                got = {"errors": [f"triage failed: {type(e).__name__}: {e}"], "rungs": {}}
            if (
                mid in self._busy
                or mid in _val(state, "desk_pending", {})
                or mid in {r["msg"] for r in self.rows()}
            ):
                continue  # a discovery beside this one took it while the notice was read
            if got["errors"]:
                said = "; ".join(f"{k} {v}" for k, v in got["rungs"].items()) or "not read"
                await self._escalate(mid, m, f"verdict triage: {got['errors'][0]}\nrungs: {said}\n\n{text}")
                continue
            if got.get("stub"):  # no reasoned verdict: nothing to file or escalate until the engine answers
                why = f"verdict: engine unavailable ({got['stub']})"
                self._row(mid, m, "left", why=why, verdict=got["line"], run="-")
                continue
            line, ev, cur = got["line"], got["evaluated_head"][:8], got["current_head"][:8]
            if got["state"] != "OPEN":
                self._row(mid, m, "left", why=f"verdict: pr {got['state']}", verdict=line, run="-")
            elif not got["fresh"]:
                self._row(mid, m, "left", why=f"verdict: stale head {ev}, now {cur}", verdict=line, run="-")
            elif got["owner"] != owner:
                why = f"verdict: owner {got['owner']} by {got['owner_by']}"
                self._row(mid, m, "left", why=why, verdict=line, run="-")
            else:
                pr = f"{got['repo']}#{got['number']}"
                try:
                    await self._file(
                        mid,
                        m,
                        "-",
                        title=f"{pr} @ {ev}: {got['decision']} ({got['blocking']} blocking)",
                        text=f"{line}\n{got['coverage']}\n\n{text}",
                        tags=("verdict", f"pr:{pr}"),
                        by="code",
                        verdict=line,
                    )
                except Exception as e:  # the outcome is unknown: the next discovery replays its key
                    if not (_val(state, "desk_pending", {}).get(mid) or {}).get("assign"):
                        raise  # nothing recorded, nothing to replay: the cursor stays and the next reads it
                    self.agent.errors.append(f"desk: task of {mid} unresolved: {type(e).__name__}: {e}")
        stamps = [str(m["created_at"]) for m in rows.values() if m.get("created_at")]
        newest = max(stamps, key=_instant) if stamps else None
        # the cursor never moves back: compared with the one stored now, which a discovery beside this
        # one may have moved while this one read
        cursor = _val(state, "desk_watch", "") or since
        if not paged and newest is not None and _instant(newest) >= _instant(cursor):
            state.put("desk_watch", after(newest), run="-", seq=0)
        return _val(state, "desk_pending", {})

    async def sweep(self, run: Run) -> str:
        """List the pending messages after reading the box: those the desk can act on first, oldest first
        and at most a page; then, apart, at most a page of deliveries unresolved, which only wait or leave."""
        state = self.agent.state  # the brief's liveness line counts the sweeps run in its window
        state.put("desk_sweeps", _val(state, "desk_sweeps", 0) + 1, run=run.id, seq=run.record.seq)
        pending = await self._discover()
        since = _val(self.agent.state, "desk_since", "")
        expired = self._expire(run)
        note = (
            f"; {len(expired)} clarify unanswered past {self.clarify_hours}h, left: {expired}"
            if expired
            else ""
        )
        waiting = sorted(
            ((mid, m) for mid, m in pending.items() if mid not in self._busy),
            key=lambda kv: _instant(kv[1].get("at") or since),
        )
        actionable = [(mid, m) for mid, m in waiting if not _by_code(m)]
        shown = actionable[: self.page]
        # an escalation is code's alone: its replay settles it, no model leaves it
        held = [(mid, m) for mid, m in waiting if m.get("send") and m["send"]["row"]["class"] != "escalated"]
        held = held[: self.page]
        listed_run, listed = self._listed
        ids = {mid for mid, _ in shown + held}
        self._listed = (run.id, (listed if listed_run == run.id else frozenset()) | ids)
        if not ids:
            return f"sweep: nothing new for {self.chores.owner} since {since}{note}"
        more = (
            f"; {len(actionable) - len(shown)} more wait for the next sweep"
            if len(actionable) > len(shown)
            else ""
        )
        lines = [
            f"sweep: {len(shown)} new message(s) for {self.chores.owner}"
            f"{more}{note}; answer or leave each by id:"
        ]
        lines += [
            f"[msg {mid} from {m['from']} subject {m['subject']!r}]\n{m['content']}" for mid, m in shown
        ]
        lines += [_unresolved(mid, m["send"]) for mid, m in held]
        return "\n\n".join(lines)

    async def answer(self, req: Answer, run: Run) -> str:
        with self._take(req.msg, run, does="answer") as m:
            sender = m["from"]
            owner = self.chores.owner
            if any(sender.startswith(p) for p in self.never):
                self._row(req.msg, m, "left", why="from-person", run=run)
                return f"{req.msg}: left, mail from {sender} is never answered by the desk"
            inst = self.chores.instruments.get(req.chore)
            if inst is None:
                raise AgentError(f"no chore {req.chore!r}; known: {sorted(self.chores.instruments)}")
            only = tuple(
                getattr(inst, "senders", ())
            )  # a lookup that carries record content back is for agents
            if only and not sender.startswith(only):
                why = f"sender: {req.chore} answers {', '.join(p + '*' for p in only)} senders only"
                self._row(req.msg, m, "left", why=why, chore=req.chore, run=run)
                return f"{req.msg}: left, {why}"
            capped = f"{req.msg}: left, {self.answers_per_hour} answers already sent this hour"
            if self._answered_last_hour() >= self.answers_per_hour:  # spares the instrument; `_send` decides
                self._row(req.msg, m, "left", why="cap", chore=req.chore, run=run)
                return capped
            t0 = time.time()
            try:
                out = await inst.run(dict(req.args))
            except Exception as e:
                out = Outcome(
                    findings=(f"instrument failed: {type(e).__name__}: {e}",), evidence=f"{e}", control=False
                )
            out, answered = self.chores._vet(inst, out)
            if not answered:  # a void answer is not an answer: the chair reads the message itself
                self._row(
                    req.msg, m, "left", why=f"instrument: {out.findings[-1][:120]}", chore=req.chore, run=run
                )
                return f"{req.msg}: left, the instrument did not answer ({out.findings[-1][:80]})"
            text = (
                f"[desk] answered by {self.agent.khive.actor} from its `{req.chore}` instrument; "
                f"{owner} has not read your message.\n"
            ) + self.chores._text(req.chore, inst, out, time.time() - t0, supersedes="desk", same=False)
            if lead := getattr(
                inst, "lead", ""
            ):  # a lookup names its findings as candidates, never a verdict
                text = text.replace("\n", f"\n{lead}\n", 1)
            if tail := getattr(inst, "tail", ""):  # a lookup says what the asker does next
                text += "\n\n" + tail
            subject = "Re: " + (m["subject"] or f"chore {req.chore}")
            row = {"class": "answered", "chore": req.chore, "args": req.args, "findings": list(out.findings)}
            try:
                if not await self._send(req.msg, m, text, subject, row, run):
                    return capped
            except AgentError as e:  # refused before it was sent: the message stays listed and pending
                raise AgentError(
                    f"{req.msg}: reply not sent: {e}; the message stays listed: answer it again or leave it"
                ) from None
            return (
                f"{req.msg}: answered {sender} on thread {m['thread']} with {req.chore}, "
                f"{len(out.findings)} finding(s)"
            )

    async def leave(self, req: Leave, run: Run) -> str:
        with self._take(req.msg, run, does="leave") as m:
            if m.get("send"):  # the row keeps the attempt whose outcome nobody knows
                why, extra = "delivery unresolved", {"send": m["send"]}
            elif m.get("assign"):  # a task whose answer was lost may have filed its row: the row says so
                why, extra = "task unresolved", {"assign": m["assign"]}
            else:
                why, extra = (req.why if req.why in WHY else f"other: {req.why[:80]}"), {}
            self._row(req.msg, m, "left", why=why, run=run, **extra)
            return f"{req.msg}: left for {self.chores.owner} ({why})"

    async def clarify(self, req: Clarify, run: Run) -> str:
        """Both readings are substrings of the message or a chore's name, asserted here, so the cheap
        model writes no question of its own; agents only; once
        per thread; a clarify is never clarified; the message waits, in the digest, until the cap."""
        # a refused clarify leaves the message listed: answer or leave it
        with self._take(req.msg, run, does="clarify") as m:
            text = f"{m['subject']}\n{m['content']}".lower()
            vocab = {*self.chores.instruments, *WHY}
            a, b = req.a.strip(), req.b.strip()
            for opt in (a, b):
                if not opt or not (opt.lower() in text or opt in vocab):
                    raise AgentError(
                        f"clarify: {opt!r} is not in the message; quote its words or name a chore"
                    )
            if a.lower() == b.lower():
                raise AgentError("clarify: the two readings are the same words")
            if m["content"].startswith(CLARIFY_MARK):
                raise AgentError(f"clarify: {req.msg} is itself a clarify: answer or leave it, never clarify")
            # a clarify whose delivery is unresolved, pending or left, may have landed: it holds the thread
            rows, held = self.rows(), _val(self.agent.state, "desk_pending", {}).values()
            asked = [r for r in rows if r["class"] == "clarified"]
            asked += [
                r for r in (*rows, *held) if (r.get("send") or {}).get("row", {}).get("class") == "clarified"
            ]
            if any(r["thread"] == m["thread"] for r in asked):
                raise AgentError(f"clarify: once per thread; this desk already asked on thread {m['thread']}")
            sender, owner = m["from"], self.chores.owner
            if not sender.startswith(CLARIFY_SENDERS):
                why = f"sender: clarify asks {', '.join(p + '*' for p in CLARIFY_SENDERS)} senders only"
                self._row(req.msg, m, "left", why=why, run=run)
                return f"{req.msg}: left, {why}"
            body = (
                f"{CLARIFY_MARK} {self.agent.khive.actor} could not tell whether you mean {a!r} or {b!r}; "
                f"say which in a message to {owner} on this thread and the desk answers what one instrument "
                f"settles or leaves it for {owner}, who has not read your message."
            )
            subject = "Re: " + (m["subject"] or "your message")
            try:
                await self._send(req.msg, m, body, subject, {"class": "clarified", "a": a, "b": b}, run)
            except AgentError as e:  # refused before it was sent: the message stays listed and pending
                raise AgentError(
                    f"{req.msg}: clarify not sent: {e}; the message stays listed: answer it or leave it"
                ) from None
            return (
                f"{req.msg}: asked {sender} on thread {m['thread']} whether {a!r} or {b!r}; "
                "the answer comes in a later sweep"
            )

    async def route(self, req: Route, run: Run) -> str:
        """The front desk routes another department's work to the owning desk of its area. The
        forward is the escalation's composition, counted against the hour's answers."""
        with self._take(req.msg, run, does="route") as m:
            to = self.routes.get(req.desk)
            if to is None:
                raise AgentError(f"route: no desk {req.desk!r}; known: {sorted(self.routes)}")
            sender, owner = m["from"], self.chores.owner
            if sender == owner or sender.startswith((f"{owner}:", *self.never)):
                why = "sender: route forwards other departments' mail only"
                self._row(req.msg, m, "left", why=why, run=run)
                return f"{req.msg}: left, {why}"
            text = await self._forwarded(req.msg, m)
            row = {"class": "routed", "to": to}
            try:
                if not await self._send(req.msg, m, text, "route: " + m["subject"], row, run, to=to):
                    return f"{req.msg}: left, {self.answers_per_hour} answers already sent this hour"
            except AgentError as e:  # refused before it was sent: the message stays listed and pending
                raise AgentError(
                    f"{req.msg}: route not sent: {e}; the message stays listed: route it again or leave it"
                ) from None
            return f"{req.msg}: routed to {to} ({req.desk})"

    async def task(self, req: Task, run: Run) -> str:
        """Work an agent asks the chair for becomes one gtd row in the chair's own namespace,
        `inbox` lane, for the chair to triage; no rank, no other namespace. The op is composed by code
        and keyed by the message, and recorded on the pending message before it runs: a lost answer is
        settled by the next task on that message replaying the recorded request under its key, never by
        a second row. A refusal is final and the brief names it."""
        with self._take(req.msg, run, does="task") as m:
            title = req.title.strip()
            if not 0 < len(title) <= 120:
                raise AgentError(
                    f"task: a title of 1 to 120 characters, got {len(title)}; the message stays listed"
                )
            if not m["from"].startswith(("lambda:", "daemon:")):
                why = "sender: task files lambda:*, daemon:* senders' work only"
                self._row(req.msg, m, "left", why=why, run=run)
                return f"{req.msg}: left, {why}"
            text = await self._forwarded(req.msg, m) if m.get("assign") is None else ""
            try:
                return await self._file(req.msg, m, run, title=title, text=text)
            except KhiveError as e:
                raise AgentError(
                    f"{req.msg}: task outcome unknown ({str(e)[:120]}); the request is recorded: call "
                    "task on it again to replay it under its key, or leave it"
                ) from None

    async def _file(
        self,
        mid: str,
        m: dict,
        run: Run | str,
        title: str = "",
        text: str = "",
        tags: tuple = (),
        **mark: Any,
    ) -> str:
        """The one gtd write, for `task` and for a verdict notice code triaged: one budget, one key per
        message. A request recorded on the pending message replays as it stands; otherwise the day's count
        is read and the request, with `mark`, recorded before it runs, no await between, so two tasks of
        one turn never pass the cap. A refusal is final and the brief names it; an outcome unknown raises
        its `KhiveError` and the recorded request waits for its replay. The message is claimed (`_busy`) from
        the count to its row, so a discovery beside this one skips it. What was done, as a line."""
        state, owner = self.agent.state, self.chores.owner
        rid, seq = (run, 0) if isinstance(run, str) else (run.id, run.record.seq)
        m = {**m, **mark}
        extra = {"verdict": m["verdict"]} if "verdict" in m else {}
        self._busy.add(mid)
        try:
            if m.get("assign") is None:
                held, cut = _val(state, "desk_pending", {}), _midnight()
                # a possible row is a row for the budget: a task pending, or left `task unresolved` today
                unresolved = sum(1 for p in held.values() if p.get("assign"))
                left = (r for r in self.rows() if r["class"] == "left" and r.get("why") == "task unresolved")
                unresolved += sum(1 for r in left if _epoch(r) >= cut)
                # counted and recorded with no await between: two tasks of one turn never pass the cap
                if self._spent(("tasked",), cut) + unresolved >= self.tasks_per_day:
                    self._row(mid, m, "left", why="cap: tasks", run=run, **extra)
                    return f"{mid}: left, {self.tasks_per_day} tasks already filed today"
                m["assign"] = {
                    "title": title,
                    "namespace": owner,
                    "assignee": owner,
                    "status": "inbox",
                    "priority": "p2",
                    "tags": ["desk", "from-mail", f"from:{m['from']}", *tags],
                    "description": text,
                    "idempotency_key": f"desk:{self.agent.khive.actor}:{mid}:task",
                }
                state.put("desk_pending", {**held, mid: m}, run=rid, seq=seq)
            fields = m["assign"]
            try:
                got = await self.agent.khive.assign(**fields)
            except KhiveError as e:
                if any(s in str(e) for s in (LOST, "did not answer", "unparseable kkernel output")):
                    raise
                self._row(mid, m, "left", why=f"task refused: {str(e)[:80]}", run=run, **extra)
                return f"{mid}: left, khive refused the task: {str(e)[:80]}"
            task_id = str(got.get("full_id") or got.get("id"))
            replayed = got.get("replayed") is True
            self._row(
                mid, m, "tasked", task=task_id, title=fields["title"], replayed=replayed, run=run, **extra
            )
            return f"{mid}: filed for {owner} as task {task_id}" + (" (replayed)" if replayed else "")
        finally:  # a failure or a cancellation leaves the recorded request for the next discovery
            self._busy.discard(mid)

    async def _escalate(self, mid: str, m: dict, text: str) -> None:
        """To the chair, by code, the day's cap first; a refusal before the send is left for the brief,
        any other failure is a delivery unresolved the next discovery replays under its key."""
        if self._spent(("escalated",), _midnight()) >= self.escalations_per_day:
            self._row(mid, m, "left", why="cap: escalations", run="-")
            return
        subject = f"[NOW] {m['from']}: {m['subject']}"
        try:
            await self._send(mid, m, text, subject, {"class": "escalated"}, "-", to=self.chores.owner)
        except AgentError as e:  # refused before it was sent: the brief names it
            self._row(mid, m, "left", why=f"escalation refused: {str(e)[:80]}", run="-")
        except Exception as e:  # the outcome is unknown: the next discovery replays its key
            if not (_val(self.agent.state, "desk_pending", {}).get(mid) or {}).get("send"):
                raise  # nothing recorded, nothing to replay: the cursor stays and the next reads it
            self.agent.errors.append(f"desk: escalation of {mid} unresolved: {type(e).__name__}: {e}")

    # bookkeeping

    def rows(self) -> list[dict]:
        """The ledger's rows. A last line with no newline after it that does not decode is an append a
        crash cut short, never a row; an undecodable line anywhere else fails the read."""
        if not self.ledger.exists():
            return []
        text = self.ledger.read_text()
        lines = [line for line in text.splitlines() if line.strip()]
        if lines and not text.endswith("\n"):
            try:
                json.loads(lines[-1])
            except ValueError:
                lines.pop()
        return [json.loads(line) for line in lines]

    def digest_lines(self, day: str) -> list[str]:
        """The front desk's brief, the desk's part of the chair's digest: the window since
        the last brief delivered (the first starts at go-live) to the clock's second, the ledger's own
        resolution, so every row is briefed once. The liveness line always, anomalies named in it; one
        line per escalated, routed, tasked, answered (with the pointer it was given) and asked row; the
        left rows grouped by why; at most 60 of those, then the count left in the ledger; then the open
        clarifies. `day` is the chores half's; `summary(day)` stays the day's line for `--stats`."""
        state = self.agent.state
        start = _val(state, "desk_brief_at", "") or _val(state, "desk_since", "")
        end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(self.agent.clock())))
        sweeps = _val(state, "desk_sweeps", 0)
        self._brief = (end, sweeps)
        lo, hi = (_instant(start).timestamp() if start else 0.0), _instant(end).timestamp()
        rows = [r for r in self.rows() if r["class"] != "resolved" and lo <= _epoch(r) < hi]
        n = {c: sum(1 for r in rows if r["class"] == c) for c in _BRIEFED + ("left",)}
        left = [r for r in rows if r["class"] == "left"]
        held = _val(state, "desk_pending", {}).values()
        unresolved = sum(1 for m in held if m.get("send") or m.get("assign"))
        unresolved += sum(1 for r in left if r.get("why") in ("delivery unresolved", "task unresolved"))
        refusals = ("task refused", "instrument", "cap", "escalation refused")  # `cap` covers its kinds
        refused = sum(1 for r in left if r.get("why", "").startswith(refusals))
        opened = self._open_clarifies()
        anomalies = (n["escalated"], unresolved, len(opened), refused)
        head = (
            f"front desk {start or 'go-live'} to {end}: {sweeps} sweep(s), "
            f"{len({r['msg'] for r in rows})} message(s) read, {n['answered']} answered, "
            f"{n['routed']} routed, {n['tasked']} tasked, {n['escalated']} escalated, {n['left']} left, "
            f"{n['clarified']} asked; "
        ) + (
            "ANOMALOUS: {} escalated, {} delivery unresolved, {} clarify open, {} refused".format(*anomalies)
            if any(anomalies)
            else "nothing anomalous"
        )

        def who(r: dict) -> str:
            return f"{r['msg'][:8]} from {r['from']} {r['subject']!r}"

        say = {
            "escalated": lambda r: f"desk escalated {who(r)}",
            "routed": lambda r: f"desk routed {who(r)} to {r.get('to')}",
            "tasked": lambda r: (
                f"desk tasked {who(r)} as {r.get('task')} {r.get('title')!r}"
                + (f" verdict {r['verdict']}" if r.get("verdict") else "")
            ),
            "answered": lambda r: (
                f"desk answered {who(r)} with {r['chore']} on thread {str(r['thread'])[:8]}: "
                + (r.get("findings") or [""])[0][:120]
            ),
            "clarified": lambda r: (
                f"desk asked {r['from']} on thread {str(r['thread'])[:8]} {r['subject']!r}: "
                f"{r['a']!r} or {r['b']!r}"
            ),
        }
        lines = [say[c](r) for c in _BRIEFED for r in rows if r["class"] == c]
        lines += [
            f"left {r.get('why', '?')}: {who(r)}" for r in sorted(left, key=lambda r: r.get("why", "?"))
        ]
        if len(lines) > 60:
            lines = lines[:60] + [f"[{len(lines) - 60} more in the ledger]"]
        for mid, r in opened.items():  # a clarify nobody answered yet is still the chair's
            lines.append(f"desk clarify open since {r['at']}: {mid[:8]} from {r['from']} {r['subject']!r}")
        return [head, *lines]

    def _briefed(self) -> None:
        """The digest carrying the brief was delivered: the next window starts where this one ended and
        the sweeps it counted are spent. A digest that failed moved nothing: the same window comes again."""
        if self._brief is None:
            return
        (end, swept), self._brief = self._brief, None
        state = self.agent.state
        state.put("desk_brief_at", end, run="-", seq=0)
        state.put("desk_sweeps", max(0, _val(state, "desk_sweeps", 0) - swept), run="-", seq=0)

    def summary(self, day: str) -> str:
        """One line for the digest and `--stats`: what the desk did that day."""
        mine = [r for r in self.rows() if r["at"].startswith(day) and r["class"] != "resolved"]
        answered = [r for r in mine if r["class"] == "answered"]
        left = [r for r in mine if r["class"] == "left"]
        asked = [r for r in mine if r["class"] == "clarified"]
        whys: dict[str, int] = {}
        for r in left:
            key = r.get("why", "?").split(":")[0]
            whys[key] = whys.get(key, 0) + 1
        return (
            f"desk {day}: {len({r['msg'] for r in mine})} message(s) read, {len(answered)} answered, "
            f"{len(left)} left"
            + (f" ({', '.join(f'{k} {n}' for k, n in sorted(whys.items()))})" if whys else "")
            + (f", {len(asked)} asked" if asked else "")
        )

    def _open_clarifies(self) -> dict[str, dict]:
        """Messages this desk asked about that no later row settled: msg id -> the clarified row."""
        open_: dict[str, dict] = {}
        for r in self.rows():
            if r["class"] == "clarified":
                open_[r["msg"]] = r
            elif r["msg"] in open_:  # resolved by a later message on the thread, or left at the cap
                open_.pop(r["msg"])
        return open_

    def _expire(self, run: Run) -> list[str]:
        """A clarify unanswered past the cap is left for the chair; unbounded pending is a silent drop."""
        cut = time.time() - self.clarify_hours * 3600
        out = []
        for mid, r in self._open_clarifies().items():
            if _epoch(r) < cut:
                self._row(mid, r, "left", why="clarify unanswered", run=run)
                out.append(mid)
        return sorted(out)

    async def _send(
        self, mid: str, m: dict, text: str, subject: str, row: dict, run: Run | str, *, to: str | None = None
    ) -> bool:
        """The one send of an answer, a clarify or a forward, then its row; False when an answer was
        left `cap` instead. An answer's hourly count is read again and its record written with no await
        between, so two answers of one turn never both take the last slot. The pending entry records the
        exact request and its key before it starts: a refusal before the send (`AgentError`) clears the
        record, any other failure keeps it, and the message is a delivery unresolved that discovery
        settles by replaying that request under that key, never by a send of its own. A reply goes to
        the sender on the message's thread; a forward (`to`) opens a thread of its own."""
        state = self.agent.state
        rid, seq = (run, 0) if isinstance(run, str) else (run.id, run.record.seq)
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        send = {
            "to": to or m["from"],
            "thread": None if to else m["thread"],
            "subject": subject,
            "content": text,
            "key": f"desk:{self.agent.khive.actor}:{mid}:{row['class']}",  # the desk sends this once, ever
            "started": started,
            "attempt": rid,
            "row": row,
        }
        if row["class"] in ("answered", "routed") and self._answered_last_hour() >= self.answers_per_hour:
            self._row(mid, m, "left", why="cap", **{k: row[k] for k in ("chore", "to") if k in row}, run=run)
            return False
        self._busy.add(mid)  # from the record to its row: a discovery beside this one does not replay it
        try:
            state.put(
                "desk_pending",
                {**_val(state, "desk_pending", {}), mid: {**m, "send": send}},
                run=rid,
                seq=seq,
            )
            try:
                sent = await self._deliver(send, run)
            except AgentError:
                state.put("desk_pending", {**_val(state, "desk_pending", {}), mid: m}, run=rid, seq=seq)
                raise
            extra = {k: v for k, v in row.items() if k != "class"}
            self._row(mid, m, row["class"], run=run, **extra, reply=str(sent.get("id")))
            return True
        finally:  # a failure or a cancellation leaves the recorded request for the next discovery
            self._busy.discard(mid)

    async def _deliver(self, send: dict, run: Run | str) -> dict:
        """A recorded request, byte for byte under its key: the first attempt, or its replay."""
        # the message the sender wrote to the chair is the whole permission: one send, on its thread; a
        # forward goes to a recipient the agent's client already admits (the chair, a routed desk)
        reply = getattr(self.agent.khive, "reply", None) if send["row"]["class"] in _REPLIES else None
        with (reply or (lambda to, thread: nullcontext()))(send["to"], send["thread"]):
            return await self.agent.deliver(
                send["to"],
                send["content"],
                thread=send["thread"],
                run=run,
                subject=send["subject"],
                idempotency_key=send["key"],
            )

    @contextmanager
    def _take(self, mid: str, run: Run, *, does: str) -> Iterator[dict]:
        """A message this wake's sweep listed, held for one handler (`does`) while it works; it leaves
        `desk_pending` only by the ledger row the handler writes, so a handler that raises leaves it
        listed and pending. A delivery unresolved admits only `leave`; a task whose outcome is unknown
        admits only its replay (`task`) and `leave`, whose row keeps the request."""
        listed_run, listed = self._listed
        pending = _val(self.agent.state, "desk_pending", {})
        done = {r["msg"] for r in self.rows()}
        known = sorted(i for i in listed if i in pending and i not in done and i not in self._busy)
        known = known if listed_run == run.id else []
        if mid not in known:
            raise AgentError(f"desk: {mid!r} is not a message the sweep listed; known: {known}")
        if does != "leave" and pending[mid].get("send"):
            raise AgentError(_unresolved(mid, pending[mid]["send"]))
        if does not in ("task", "leave") and (a := pending[mid].get("assign")):
            raise AgentError(
                f"{mid}: task unresolved, {a['title']!r} under key {a['idempotency_key']}: "
                f"task(...) replays it or leave(...) keeps it for {self.chores.owner}"
            )
        self._busy.add(mid)
        try:
            yield pending[mid]
        finally:
            self._busy.discard(mid)

    def _answered_last_hour(self) -> int:
        """Answers and routes: a forward to a desk spends the hour as an answer does."""
        return self._spent(("answered", "routed"), time.time() - 3600)

    def _spent(self, classes: tuple[str, ...], since: float) -> int:
        """Rows of these classes written since `since`, and sends of them whose delivery is unresolved,
        pending or left, by the time their send started: a possible delivery is a delivery for a budget."""
        rows = self.rows()
        sent = sum(1 for r in rows if r["class"] in classes and _epoch(r) >= since)
        held = (r.get("send") or {} for r in (*rows, *_val(self.agent.state, "desk_pending", {}).values()))
        unresolved = [s for s in held if s.get("row", {}).get("class") in classes]
        return sent + sum(1 for s in unresolved if _instant(s["started"]).timestamp() >= since)

    async def _forwarded(self, mid: str, m: dict) -> str:
        """The originating line, a blank line, and the body word for word as the thread holds it; when
        the thread cannot be read or does not hold the message, the inbox snippet, and the line says so.
        The thread holds a peer's message under its author-side id, the row's `outbound_ref`."""
        head = f"originating msg {mid} from {m['from']} on thread {m['thread']} at {m['at']}"
        msgs, why = [], "the message names no thread"
        if m["thread"]:
            try:
                (t,) = await self.reader.exec(f"comm.thread(id={lit(m['thread'])})")
                msgs = t.get("messages", []) if isinstance(t, dict) else []
                why = f"not among the thread's {len(msgs)} message(s)"
            except Exception as e:
                why = f"{type(e).__name__}: {e}"
        ids = {i for i in (mid, m.get("outbound_ref")) if i}
        body = next((x.get("content") or "" for x in msgs if ids & {x.get("full_id"), x.get("id")}), None)
        if body is None:
            return f"{head}\n[body: inbox snippet, thread read failed: {why}]\n\n{m['content']}"
        return f"{head}\n\n{body}"

    def _row(self, mid: str, m: dict, cls: str, *, run: Run | str, **extra: Any) -> None:
        """`run` is the live run, or the id of the attempt a watch settles outside any run."""
        rid, seq = (run, 0) if isinstance(run, str) else (run.id, run.record.seq)
        now = time.time()  # `at` is local, for reading; `utc` is the instant every window compares
        row = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "utc": datetime.fromtimestamp(now, UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "msg": mid,
            "from": m["from"],
            "subject": m["subject"],
            "thread": m["thread"],
            "class": cls,
            "run": rid,
            **extra,
        }
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        held = self.ledger.read_bytes() if self.ledger.exists() else b""
        with self.ledger.open("a") as fh:
            if held and not held.endswith(b"\n"):  # the last append was cut short, or lost its newline
                cut = held.rfind(b"\n") + 1
                try:
                    json.loads(held[cut:])
                except ValueError:  # never a row: cut it, so it does not become a line in the middle
                    fh.truncate(cut)
                else:
                    fh.write("\n")
            fh.write(json.dumps(row) + "\n")
        pending = _val(self.agent.state, "desk_pending", {})
        if mid in pending:  # the row is the disposition: only now does the message leave the intake
            pending = {k: v for k, v in pending.items() if k != mid}
            self.agent.state.put("desk_pending", pending, run=rid, seq=seq)
        if cls in ("answered", "left"):  # the next message handled on a thread settles its open clarify
            for omid, o in self._open_clarifies().items():
                if o["thread"] == row["thread"] and omid != mid:
                    self._row(omid, o, "resolved", by=mid, run=run)


def after(stamp: str) -> str:
    """The instant one microsecond past an RFC 3339 stamp, as the box's `since` reads it."""
    t = _instant(stamp) + timedelta(microseconds=1)
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _unresolved(mid: str, send: dict) -> str:
    """How the sweep lists a delivery unresolved, and how `answer` and `clarify` refuse it."""
    return (
        f"{mid}: delivery unresolved since {send['started']}, to {send['to']} on {send['thread']}: "
        "leave(...) it or wait"
    )


def _by_code(m: dict) -> bool:
    """A pending message only code settles, by replaying its recorded request: a delivery unresolved, or
    a task code filed; no model is woken for it or shown it."""
    return bool(m.get("send")) or m.get("by") == "code"


def _instant(stamp: str) -> datetime:
    """An RFC 3339 stamp as an instant: compared as text, `...17.5Z` sorts before `...17Z`."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _val(store: Any, key: str, default: Any) -> Any:
    note = store.get(key)
    return note.value if note is not None and isinstance(note.value, type(default)) else default


def _epoch(row: dict) -> float:
    """A ledger row's instant: its `utc`; a row written before it carried one, its local `at`, which
    cannot tell apart the two passes of an hour a DST change repeats."""
    if row.get("utc"):
        return _instant(row["utc"]).timestamp()
    return time.mktime(time.strptime(row["at"], "%Y-%m-%d %H:%M:%S"))


def _midnight() -> float:
    """The start of today in the ledger's local time: a daily cap's cut."""
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
