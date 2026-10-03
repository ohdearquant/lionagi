"""Chores: a chore ends quiet, reported or escalated and never a fourth way; a tick names its chore,
an ask is answered where it was asked, the reporting path holds no model judgment, the ticks are
reminders the agent keeps for itself, a report names what it supersedes and is not restated per
recipient, mail from outside the trusted set never runs, the khive client refuses every verb but the
read list and a call it cannot name, the steward hears only escalations and what the owner left
unanswered, and an instrument that names no population, no known-positive or never answers is a
finding about the instrument."""

import asyncio
import itertools
import json
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from hub.agent import Agent, AgentError, Budget
from hub.agent.chores import (
    Aged,
    Bounded,
    Check,
    Chores,
    Digest,
    Outcome,
    _aged_key,
    _countable,
    period_s,
    sender_of,
    verbs,
)
from hub.khive import LOST, KhiveError
from lionagi import Actor, Kind, Message

OWNER, STEWARD, SELF = "lambda:x", "lambda:owner", "lambda:x:agent"
THREAD = "0a0a0a0a-0000-4000-8000-0a0a0a0a0a0a"


def mail(sender: str, content: str, mid: str = "a1b2c3d4", thread: str = THREAD) -> dict:
    return {
        "id": mid,
        "full_id": f"{mid}-0000-0000-0000-000000000000",
        "subject": "s",
        "content": content,
        "properties": {"from_actor": sender, "thread_id": thread},
    }


def full(**kw) -> Outcome:
    """An outcome with the fields every instrument must fill: population, predicate, count and what
    its known-positive read. A test that wants a refusal builds a bare `Outcome` instead."""
    return Outcome(
        **{
            "population": "probe rows",
            "predicate": "probed",
            "count": 1,
            "positive": "probe read 1 row",
            **kw,
        }
    )


class FakeKhive:
    def __init__(self, *inbox: dict, agenda: list[str] = (), events: list[dict] = ()):
        self.mail, self.cwd, self.actor = list(inbox), None, SELF
        self.sent: list[dict] = []
        self.ops: list[str] = []
        self.agenda = list(agenda)
        self.events = list(events)  # scheduled_event rows `list` returns, status beside properties as live
        self.filtered = False  # True: `list` applies creator and status before the page, as the store does
        self.bare_pages = False  # `list` answers a page as a bare list, with no has_more beside it
        self.threads: dict[str, list[dict]] = {}  # thread id -> messages `comm.thread` returns
        self.refuse: set[str] = set()  # recipients the transport refuses, as the hop cap would
        self.keyed: dict[str, tuple] = {}  # idempotency key -> (the request, what its send returned)
        self.tasks: dict[str, tuple[str, dict]] = {}  # gtd.assign key -> (the op, the row it created)
        self.gtd_refuse = ""  # a gtd.assign refusal the store answers, as an ungranted namespace would
        self.gtd_lose = 0  # gtd.assign answers to drop after the row lands, as a lost response would
        self.grows = False  # True: a reminder made lands on the agenda, created now, as the store keeps it
        self.paged = False  # True: the agenda answers the earliest `limit` rows from `from`
        self.agenda_error: Exception | None = None  # what a failed agenda read raises

    def event(self, n: int, c) -> dict:
        # a str is this actor's reminder; (content, actor[, status[, trigger_at[, repeat_anchor[, repeat[,
        # created_at]]]]]), None leaving a field out; the creation instant sits beside `properties`
        content, actor, status, trigger, anchor, repeat, born = (
            *((c, self.actor) if isinstance(c, str) else c),
            *[None] * 5,
        )[:7]
        given = {"trigger_at": trigger, "repeat_anchor": anchor, "repeat": repeat}
        return {
            "id": f"e{n}",
            "content": content,
            **({} if born is None else {"created_at": born}),
            "properties": {  # the agenda keeps a row's status here, none beside properties
                "status": status or "pending",
                "created_by_actor": actor,
                **{k: v for k, v in given.items() if v is not None},
            },
        }

    async def inbox(self, *, wait_ms=0, limit=50, status="unread", offset=0):
        out, self.mail = self.mail[:limit], self.mail[limit:]
        return out

    async def mark_read(self, ids):
        pass

    async def send(self, to, content, *, subject=None, thread_id=None, idempotency_key=None):
        """comm.send as the server keeps a key (measured 2026-09-22): the same request under a held key
        replays the first answer, `replayed: true`, and no second message; another request is refused."""
        if to in self.refuse:
            raise AgentError(f"hop limit: 2 messages already sent on thread {thread_id}; not again")
        request = (to, content, subject, thread_id)
        if idempotency_key in self.keyed:
            held, sent = self.keyed[idempotency_key]
            if held != request:
                raise KhiveError(f"comm.send: key_conflict: {idempotency_key} is held by another request")
            return {**sent, "replayed": True}
        n = len(self.sent) + 1
        row = {"to": to, "content": content, "subject": subject, "thread_id": thread_id or f"t{n}"}
        self.sent.append(row)
        sent = {"id": f"m{n}", "thread_id": row["thread_id"]}
        if idempotency_key is None:
            return sent
        self.keyed[idempotency_key] = (request, sent)
        return {**sent, "replayed": False}

    async def exec(self, ops: str):
        self.ops.append(ops)
        if ops.startswith("schedule.remind") and self.grows:
            content, at, repeat = re.search(
                r'content="([^"]*)", at="([^"]*)", repeat="([^"]*)"', ops
            ).groups()
            self.agenda.append(
                (content, self.actor, "pending", at, None, repeat, iso(time.time(), micros=True))
            )
        if ops.startswith("schedule.agenda"):
            if self.agenda_error is not None:
                raise self.agenda_error
            events = [self.event(n, c) for n, c in enumerate(self.agenda)]
            if self.paged:
                args = {k: json.loads(v) for k, v in re.findall(r'(\w+)=("[^"]*"|\d+)', ops)}
                events = sorted(
                    (e for e in events if e["properties"]["trigger_at"] >= args.get("from", "")),
                    key=lambda e: e["properties"]["trigger_at"],
                )[: args["limit"]]
            return [{"events": events}]
        if ops.startswith(
            '[list(kind="scheduled_event"'
        ):  # a page of `limit` from `offset` per call, filtered
            pages = []
            for call in re.findall(r"list\(([^)]*)\)", ops):
                args = {k: json.loads(v) for k, v in re.findall(r'(\w+)=("[^"]*"|\d+)', call)}
                want = {k: args[k] for k in ("created_by_actor", "status") if k in args and self.filtered}
                rows = [
                    e
                    for e in self.events
                    if all({**e["properties"], "status": e["status"]}.get(k) == v for k, v in want.items())
                ]
                offset = args.get("offset", 0)
                page = rows[offset : offset + args["limit"]]
                pages.append(
                    page if self.bare_pages else {"items": page, "has_more": offset + len(page) < len(rows)}
                )
            return pages
        if ops.startswith("get(id="):  # one scheduled_event row by id, as the store answers it
            wanted = json.loads(ops[len("get(id=") : -1])
            for e in self.events:
                if e["id"] == wanted:
                    return [e]
            raise KhiveError(
                f"get: {{'kind': 'not_found', 'message': 'not found: {wanted}'}}", kind="not_found"
            )
        if ops.startswith("comm.thread"):
            tid = ops.split('id="')[1].split('"')[0]
            msgs = self.threads.get(tid, [])
            return [{"thread_id": tid, "count": len(msgs), "messages": msgs}]
        if ops.startswith("gtd.assign("):  # keyed as comm.send is: the same op replays its row
            key = ops.split('idempotency_key="')[1].split('"')[0]
            if key in self.tasks:
                held, row = self.tasks[key]
                if held != ops:
                    raise KhiveError(f"gtd.assign: key_conflict: {key} is held by another request")
                return [{**row, "replayed": True}]
            if self.gtd_refuse:
                raise KhiveError(self.gtd_refuse)
            full = f"7a5c{len(self.tasks) + 1:04d}-0000-4000-8000-000000000000"
            self.tasks[key] = (ops, {"id": full[:8], "full_id": full})
            if self.gtd_lose:
                self.gtd_lose -= 1
                raise KhiveError(f"kkernel exited 1: gtd.assign: {LOST}")
            return [{"id": full[:8], "full_id": full, "replayed": False}]
        return [{"ok": True}]


class Scripted:
    def __init__(self, *turns: str, usage: dict | None = None):
        self.turns, self.calls, self.usage = list(turns), [], usage

    async def __call__(self, messages: list[Message]) -> str:
        self.calls.append({"total_cost_usd": 0.001 if self.usage else 0.0, "usage": self.usage or {}})
        return self.turns.pop(0)


class Probe:
    """An instrument whose outcomes the test chooses, one per run (the last repeats); `every` makes
    it a ticked chore."""

    direction = "over-report"
    mechanism = "the test says so"
    answers = "what the probe was pointed at"
    empty_expected = False

    def __init__(self, name: str, *outcomes: Outcome | Exception, every: str | None = "every:30m"):
        self.name, self.outcomes, self.every, self.about, self.args = (
            name,
            list(outcomes),
            every,
            f"probes {name}",
            [],
        )

    async def run(self, args: dict) -> Outcome:
        self.args.append(args)
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, Exception):
            raise out
        return out


DONE = 'OUT{posture: {done: "did the chore", awaiting: ""}}'
CHECK = '<lact c1>check(chore="disk")</lact>'


def build(
    tmp_path, khive: FakeKhive, turns: list[str], batch: int = 1, usage: dict | None = None, **probes: Probe
) -> tuple[Chores, Agent]:
    actor = Actor("chores", privileges={"comm.send"})
    agent = Agent(
        actor,
        khive,
        Scripted(*turns, usage=usage),
        lambda mail: chores_profile,
        admit=lambda m: chores.admit(m),
        max_rounds=4,
        batch=batch,
        budget=Budget(hops_per_thread=2),
    )
    chores = Chores(agent, OWNER, STEWARD, dict(probes), tmp_path / "ledger.jsonl")
    global chores_profile
    chores_profile, _ = chores.profiles()
    return chores, agent


def age_ledger(chores: Chores, hours: float) -> None:
    """Move every ledger row's `at` and `ts` into the past, as a test's clock."""
    rows = chores.rows()
    for r in rows:
        t = time.mktime(time.strptime(r["at"], "%Y-%m-%d %H:%M:%S")) - hours * 3600
        r["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        if "ts" in r:  # a row from before `ts` carries its label alone
            r["ts"] -= hours * 3600
    chores.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))


def results(run) -> list[str]:
    return [str(e.content) for e in run.record if e.kind is Kind.RESULT]


def test_a_tick_with_findings_reports_to_the_owner_on_a_chore_thread_the_day_keeps(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: disk", mid="b2c3d4e5"))
    first = full(findings=("apps steady: under the floor on /dev/disk3s5 (/a)",), evidence="df: ...")
    second = full(findings=("local_ci admit: under the floor on /dev/disk3s5 (/a)",), evidence="df: ...")
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 2, disk=Probe("disk", first, second))
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER, OWNER]
    assert khive.sent[0]["subject"] == "chore disk: 1 finding(s)"
    text = khive.sent[0]["content"]
    assert "apps steady: under the floor" in text and "evidence:\ndf: ..." in text
    assert "supersedes: no-prior" in text and "direction over-report" in text
    assert "answers: what the probe was pointed at" in text and "known-positive: probe read 1 row" in text
    assert "population: probe rows (1)\npredicate: probed" in text
    assert khive.sent[1]["thread_id"] == khive.sent[0]["thread_id"]  # the day's chore thread, reused
    rows = chores.rows()
    assert [r["sent_to"] for r in rows] == [[OWNER], [OWNER]] and rows[0]["chore"] == "disk"
    assert rows[0]["control"] is True and rows[0]["escalate"] is False and rows[0]["asked_by"] is None
    assert rows[0]["msg"] == "m1" and rows[0]["thread"] == "t1" and rows[0]["direction"] == "over-report"
    assert rows[0]["answered"] is True and rows[0]["count"] == 1 and rows[0]["positive"] == "probe read 1 row"
    assert f"supersedes: {rows[0]['at']} msg m1" in khive.sent[1]["content"]  # the handler fills it


def test_the_instrument_is_handed_its_chore_run_as_source_event_over_whatever_the_args_name(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: disk"))
    probe = Probe("disk", full())
    ask = '<lact c1>check(chore="disk", args={"source_event": "forged"})</lact>'
    chores, agent = build(tmp_path, khive, [ask, DONE], disk=probe)
    asyncio.run(agent.wake())
    (row,) = chores.rows()
    assert probe.args[0].get("source_event") == f"chore run {row['run']}"
    assert row["args"] == {"source_event": "forged"}  # the ledger keeps what was asked


def test_the_same_findings_again_are_not_resent_and_the_row_says_which_report_they_repeat(tmp_path):
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"m{i}000000") for i in range(3)))
    out = full(findings=("apps steady: under the floor on /dev/disk3s5 (/a)",), evidence="df: ...")
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 3, disk=Probe("disk", out))
    for _ in range(3):
        run = asyncio.run(agent.wake())
    assert len(khive.sent) == 1  # a standing state is told once; the actor reads changes
    rows = chores.rows()
    assert rows[1]["repeat_of"] == rows[0]["at"] and rows[1]["sent_to"] == [] and rows[1]["msg"] is None
    assert any(
        f"disk: unchanged since {rows[0]['at']}, not resent, 1 finding(s), sent to nobody" in r
        for r in results(run)
    )


class Yielding(FakeKhive):
    """A send that gives the loop away mid-flight, as the real transport's subprocess does: two
    checks of a turn interleave there."""

    async def send(self, *args, **kwargs):
        await asyncio.sleep(0)
        return await super().send(*args, **kwargs)


def test_two_checks_of_one_chore_in_a_turn_send_the_findings_once(tmp_path):
    khive = Yielding(mail(SELF, "tick: disk"))
    out = full(findings=("apps steady: under the floor on /dev/disk3s5 (/a)",), evidence="df: ...")
    twice = '<lact c1>check(chore="disk")</lact><lact c2>check(chore="disk")</lact>'
    chores, agent = build(tmp_path, khive, [twice, DONE], disk=Probe("disk", out))
    asyncio.run(agent.wake())
    assert len(khive.sent) == 1
    first, second = chores.rows()
    assert first["sent_to"] == [OWNER] and second["sent_to"] == [] and second["repeat_of"] == first["at"]


def test_two_chores_opening_their_threads_in_one_turn_both_keep_them(tmp_path):
    khive = Yielding(mail(SELF, "tick: disk"), mail(SELF, "tick: disk", mid="b2c3d4e5"))
    both = '<lact c1>check(chore="disk")</lact><lact c2>check(chore="mem")</lact>'
    disk = Probe("disk", *(full(findings=(f"disk {w}",), evidence="df: ...") for w in ("low", "lower")))
    mem = Probe("mem", *(full(findings=(f"mem {w}",), evidence="vm_stat: ...") for w in ("low", "lower")))
    chores, agent = build(tmp_path, khive, [both, DONE] * 2, disk=disk, mem=mem)
    asyncio.run(agent.wake())
    keys = {tuple(k.split(":", 2)[::2]) for k in agent.state.get("chore_threads").value}
    assert keys == {("disk", OWNER), ("mem", OWNER)}

    def threads(sent: list[dict]) -> dict:
        return {(s["subject"].split(":")[0], s["to"]): s["thread_id"] for s in sent}

    opened = threads(khive.sent)
    assert len(khive.sent) == len(set(opened.values())) == 2
    asyncio.run(agent.wake())  # a second loop: the locks of the first are not its locks
    assert len(khive.sent) == 4 and threads(khive.sent[2:]) == opened  # each reused its own


def test_a_quiet_tick_sends_nothing_and_still_leaves_a_row(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: disk"))
    chores, agent = build(tmp_path, khive, [CHECK, DONE], disk=Probe("disk", full()))
    run = asyncio.run(agent.wake())
    assert khive.sent == []
    (row,) = chores.rows()
    assert row["findings"] == [] and row["sent_to"] == [] and row["supersedes"] == "no-prior"
    assert row["answered"] is True
    assert any("disk: quiet, 0 finding(s), sent to nobody" in r for r in results(run))


def test_an_instrument_that_raises_or_whose_control_fails_escalates_to_owner_and_steward(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: pr"), mail(SELF, "tick: gtd", mid="b2c3d4e5"))
    chores, agent = build(
        tmp_path,
        khive,
        ['<lact c1>check(chore="pr")</lact>', DONE, '<lact c1>check(chore="gtd")</lact>', DONE],
        pr=Probe("pr", RuntimeError("gh: HTTP 502")),
        gtd=Probe("gtd", Outcome(findings=(), evidence="[]", control=False)),
    )
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER, STEWARD, OWNER, STEWARD]
    assert khive.sent[0]["subject"].startswith("ESCALATE pr: instrument failed: RuntimeError: gh: HTTP 502")
    assert (
        "ESCALATE, the remedy is the actor's" in khive.sent[0]["content"]
        and "control FAILED" in khive.sent[0]["content"]
        and "evidence:\nRuntimeError: gh: HTTP 502" in khive.sent[0]["content"]
    )
    assert khive.sent[0]["thread_id"] != khive.sent[1]["thread_id"]  # one thread per recipient
    rows = chores.rows()
    assert rows[0]["control"] is False and rows[0]["escalate"] is True and rows[0]["answered"] is False
    assert rows[1]["findings"] == ["the instrument's control did not pass; its answer is void"]


def test_findings_without_the_instruments_own_output_are_refused_before_send(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: disk"))
    chores, agent = build(
        tmp_path, khive, [CHECK, DONE], disk=Probe("disk", full(findings=("low",), evidence="   "))
    )
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER, STEWARD]
    text = khive.sent[0]["content"]
    assert "ESCALATE" in text and "reported findings without its own output; refused" in text
    assert chores.rows()[0]["control"] is False


def test_an_outcome_without_population_predicate_count_or_known_positive_is_refused(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: pr", mid="b2c3d4e5"))
    chores, agent = build(
        tmp_path,
        khive,
        [CHECK, DONE, '<lact c1>check(chore="pr")</lact>', DONE],
        disk=Probe("disk", Outcome(evidence="df: fine", positive="own pid alive")),  # no population
        pr=Probe("pr", full(evidence="gh: fine", positive="")),  # no known-positive read
    )
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER, STEWARD, OWNER, STEWARD]
    assert "named no population, predicate or count for its answer; refused" in khive.sent[0]["content"]
    assert "control passed with no known-positive read in the same call; refused" in khive.sent[2]["content"]
    rows = chores.rows()
    assert [r["control"] for r in rows] == [False, False] and [r["answered"] for r in rows] == [False, False]


def test_an_empty_population_is_a_finding_unless_the_chore_declared_it_expected(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: pr", mid="b2c3d4e5"))
    pr = Probe("pr", full(evidence="gh: 0 open", count=0, population="open PRs in ['o/r']"))
    pr.empty_expected = True
    chores, agent = build(
        tmp_path,
        khive,
        [CHECK, DONE, '<lact c1>check(chore="pr")</lact>', DONE],
        disk=Probe("disk", full(evidence="df: no device", count=0, population="volumes under ['/a']")),
        pr=pr,
    )
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER]  # a report, not an escalation: the actor decides
    assert "population empty and not declared expected: volumes under ['/a']" in khive.sent[0]["content"]
    rows = chores.rows()
    assert rows[0]["answered"] is False and rows[0]["control"] is True and rows[0]["escalate"] is False
    assert rows[1]["answered"] is True and rows[1]["findings"] == []  # expected empty: quiet


def test_a_chore_that_never_answers_is_escalated_once_as_a_dead_instrument_and_an_answer_clears_it(
    tmp_path, monkeypatch
):
    # a second per row: one shared second let a comparison against the wrong row pass
    ticks, start = itertools.count(), datetime(2026, 9, 22)
    monkeypatch.setattr(
        "hub.agent.chores._now",
        lambda t=None: (start + timedelta(seconds=next(ticks))).strftime("%Y-%m-%d %H:%M:%S"),
    )
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"m{i}000000") for i in range(6)))
    dead = Outcome(findings=("NOT-MEASURED",), evidence="rc 1", control=False)
    chores, agent = build(
        tmp_path,
        khive,
        [CHECK, DONE] * 6,
        disk=Probe("disk", dead, dead, dead, dead, full(), dead),
    )
    for _ in range(2):
        asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER, STEWARD]  # the first failure escalates, then is same
    run = asyncio.run(agent.wake())  # the third unanswered run crosses
    assert [s["to"] for s in khive.sent[2:]] == [OWNER, STEWARD]
    assert khive.sent[2]["subject"] == "ESCALATE disk: instrument dead"
    text = khive.sent[2]["content"]
    assert "instrument dead: 3 consecutive run(s) without an answer since" in text and "NOT-MEASURED" in text
    assert any(
        "instrument dead after 3 unanswered run(s), told ['lambda:x', 'lambda:owner']" in r
        for r in results(run)
    )
    asyncio.run(agent.wake())  # the fourth: told once, not again
    assert len(khive.sent) == 4 and agent.state.get("dead_told").value == ["disk"]
    asyncio.run(agent.wake())  # an answer clears it
    assert agent.state.get("dead_told").value == []
    asyncio.run(agent.wake())  # one unanswered run again: below the bound, and the same failure as told
    rows = chores.rows()
    told = [r["at"] for r in rows if OWNER in r["sent_to"]]  # the row whose report reached the owner
    assert len(khive.sent) == 4 and told == [rows[5]["repeat_of"]]  # a standing state is told once
    assert [r["answered"] for r in rows] == [False, False, False, False, True, False]


def test_a_dead_instrument_notice_a_recipient_refused_is_sent_to_that_recipient_at_the_next_run(
    tmp_path, monkeypatch
):
    ticks, start = itertools.count(), datetime(2026, 9, 22)
    monkeypatch.setattr(
        "hub.agent.chores._now",
        lambda t=None: (start + timedelta(seconds=next(ticks))).strftime("%Y-%m-%d %H:%M:%S"),
    )
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"m{i}000000") for i in range(5)))
    khive.refuse = {STEWARD}  # the steward's copies are refused, as a hop cap would
    dead = Outcome(findings=("NOT-MEASURED",), evidence="rc 1", control=False)
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 5, disk=Probe("disk", dead))

    def notices() -> list[str]:
        return [s["to"] for s in khive.sent if s["subject"] == "ESCALATE disk: instrument dead"]

    for _ in range(3):
        run = asyncio.run(agent.wake())
    assert notices() == [OWNER]  # the crossing: the owner's copy landed, the steward's did not
    assert any(f"told ['{OWNER}', '{STEWARD} undelivered: hop limit" in r for r in results(run))
    assert agent.state.get("dead_told").value == [f"disk|{OWNER}"]
    khive.refuse = set()
    asyncio.run(agent.wake())  # the steward is still owed its copy; the owner is not told again
    assert notices() == [OWNER, STEWARD] and agent.state.get("dead_told").value == ["disk"]
    asyncio.run(agent.wake())
    assert notices() == [OWNER, STEWARD]  # every copy landed: told once


def test_a_dead_instrument_notice_that_reached_nobody_leaves_the_crossing_untold_until_a_send_lands(
    tmp_path, monkeypatch
):
    ticks, start = itertools.count(), datetime(2026, 9, 22)
    monkeypatch.setattr(
        "hub.agent.chores._now",
        lambda t=None: (start + timedelta(seconds=next(ticks))).strftime("%Y-%m-%d %H:%M:%S"),
    )
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"m{i}000000") for i in range(6)))
    khive.refuse = {OWNER, STEWARD}  # neither copy is delivered
    dead = Outcome(findings=("NOT-MEASURED",), evidence="rc 1", control=False)
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 6, disk=Probe("disk", dead))

    def notices() -> list[str]:
        return [s["to"] for s in khive.sent if s["subject"] == "ESCALATE disk: instrument dead"]

    for _ in range(3):
        run = asyncio.run(agent.wake())
    assert notices() == [] and agent.state.get("dead_told").value == []  # the crossing, told to nobody
    assert any(
        f"told ['{OWNER} undelivered: hop limit" in r and f"'{STEWARD} undelivered: hop limit" in r
        for r in results(run)
    )
    run = asyncio.run(agent.wake())  # still undelivered: still untold, so the notice is owed again
    assert any("instrument dead after 4 unanswered run(s)" in r for r in results(run))
    assert notices() == [] and agent.state.get("dead_told").value == []
    khive.refuse = set()
    asyncio.run(agent.wake())  # delivered: now the chore is marked told
    assert notices() == [OWNER, STEWARD] and agent.state.get("dead_told").value == ["disk"]
    asyncio.run(agent.wake())
    assert notices() == [OWNER, STEWARD]  # told once


def test_an_ask_is_answered_on_its_thread_whatever_the_outcome_and_the_owner_is_not_told_twice(tmp_path):
    khive = FakeKhive(
        mail(OWNER, "check disk please"), mail(STEWARD, "disk?", mid="b2c3d4e5", thread="t-steward")
    )
    quiet, loud = full(), full(findings=("a target over the cap",), evidence="du")
    chores, agent = build(
        tmp_path,
        khive,
        [f'<lact c1>check(chore="disk", asker="{OWNER}", thread="{THREAD}")</lact>', DONE],
        disk=Probe("disk", quiet),
    )
    asyncio.run(agent.wake())
    assert khive.sent == [
        {
            "to": OWNER,
            "content": khive.sent[0]["content"],
            "subject": "chore disk: quiet",
            "thread_id": THREAD,
        }
    ]
    assert "nothing to report" in khive.sent[0]["content"]
    chores.instruments["disk"].outcomes = [loud]
    agent.backend.turns = [
        f'<lact c1>check(chore="disk", asker="{STEWARD}", thread="t-steward")</lact>',
        DONE,
    ]
    asyncio.run(agent.wake())
    assert [(s["to"], s["thread_id"]) for s in khive.sent[1:]] == [(STEWARD, "t-steward"), (OWNER, "t3")]
    assert chores.rows()[1]["asked_by"] == STEWARD and chores.rows()[1]["sent_to"] == [STEWARD, OWNER]


def test_the_owners_own_ask_keeps_its_receipt_so_the_report_ages_and_is_not_repeated(tmp_path):
    khive = FakeKhive(
        mail(OWNER, "check disk please"),
        mail(SELF, "tick: aged", mid="b2c3d4e5"),
        mail(SELF, "tick: disk", mid="c3d4e5f6"),
    )
    loud = full(findings=("apps steady: under the floor on /dev/disk3s5 (/a)",), evidence="df")
    chores, agent = build(
        tmp_path,
        khive,
        [
            f'<lact c1>check(chore="disk", asker="{OWNER}", thread="{THREAD}")</lact>',
            DONE,
            "<lact a1>aged()</lact>",
            DONE,
            CHECK,
            DONE,
        ],
        disk=Probe("disk", loud),
    )
    asyncio.run(agent.wake())
    (row,) = chores.rows()
    assert row["sent_to"] == [OWNER] and row["msg"] == "m1" and row["thread"] == THREAD  # the receipt
    age_ledger(chores, hours=1.5)  # past two 30-minute periods, unanswered
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER, STEWARD]  # aged: the steward hears of it
    asyncio.run(agent.wake())  # the tick finds the same state: not resent, and it names the ask's report
    assert len(khive.sent) == 2
    rows = chores.rows()
    assert len(rows) == 2  # aged() writes no ledger row; the tick's row is the second
    assert rows[1]["repeat_of"] == rows[0]["at"] and rows[1]["supersedes"] == f"{rows[0]['at']} msg m1"


def test_an_escalation_the_steward_did_not_receive_is_sent_at_the_next_run_though_the_owner_has_it(
    tmp_path,
):
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: disk", mid="b2c3d4e5"))
    khive.refuse.add(STEWARD)
    out = full(
        findings=("local_ci admit: under the floor on /dev/disk3s5 (/a)",), evidence="df", escalate=True
    )
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 2, disk=Probe("disk", out))
    asyncio.run(agent.wake())
    rows = chores.rows()
    assert rows[0]["sent_to"] == [OWNER] and rows[0]["undelivered"][0].startswith(f"{STEWARD}: hop limit")
    khive.refuse.clear()  # the next day's thread has room
    asyncio.run(agent.wake())
    rows = chores.rows()
    assert rows[1]["sent_to"] == [STEWARD] and rows[1]["repeat_of"] == rows[0]["at"]  # the owner: same
    assert [s["to"] for s in khive.sent] == [OWNER, STEWARD]


def test_the_model_may_not_name_an_asker_outside_the_trusted_set(tmp_path):
    khive = FakeKhive(mail(OWNER, "check disk for lambda:stranger"))
    chores, agent = build(
        tmp_path,
        khive,
        ['<lact c1>check(chore="disk", asker="lambda:stranger", thread="t9")</lact>', DONE],
        disk=Probe("disk", full()),
    )
    run = asyncio.run(agent.wake())
    assert khive.sent == [] and chores.rows() == []
    assert any("lambda:stranger is not a sender this agent works for" in r for r in results(run))


def test_the_answer_to_an_ask_goes_on_the_asks_thread_whatever_the_model_wrote(tmp_path):
    """The model omits the thread; the ask is answered on the thread it came on."""
    khive = FakeKhive(mail(OWNER, "how much disk is free?"))
    out = full(findings=(), evidence="df: 301 GiB free", population="volumes", predicate="floor", count=1)
    chores, agent = build(
        tmp_path,
        khive,
        [f'<lact c1>check(chore="disk", asker="{OWNER}")</lact>', DONE],
        disk=Probe("disk", out),
    )
    asyncio.run(agent.wake())
    (reply,) = khive.sent
    assert reply["to"] == OWNER
    assert reply["thread_id"] == THREAD, f"the answer left the ask's thread: {reply['thread_id']}"


def test_mail_from_outside_the_trusted_set_is_dropped_at_the_gate_and_never_runs(tmp_path):
    khive = FakeKhive(
        mail("lambda:stranger", "check disk now"),
        mail(SELF, "tick: disk", mid="b2c3d4e5"),
        mail("lambda:other", "tick: disk", mid="c3d4e5f6"),
    )
    chores, agent = build(tmp_path, khive, [CHECK, DONE], disk=Probe("disk", full()))
    assert asyncio.run(agent.wake()) is None  # the stranger's mail: read, noted, no run, no model call
    assert agent.backend.calls == [] and khive.sent == []
    cursor = agent.state.get("cursor").value
    assert cursor["dropped"] == [{"id": "a1b2c3d4", "from": "lambda:stranger"}] and "dropped_at" in cursor
    agent.batch = 2
    run = asyncio.run(agent.wake())  # a mixed batch runs the tick alone and notes the drop beside it
    assert run is not None and len(chores.rows()) == 1
    cursor = agent.state.get("cursor").value
    assert cursor["mail"] == ["b2c3d4e5"]
    assert cursor["dropped"] == [{"id": "c3d4e5f6", "from": "lambda:other"}]
    assert chores.trusted == frozenset({OWNER, STEWARD, SELF})


def test_the_hop_cap_on_a_chore_thread_is_recorded_as_undelivered_not_raised(tmp_path):
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"m{i}000000") for i in range(3)))
    outs = [full(findings=(f"floor {n}: under",), evidence="df") for n in (1, 2, 3)]
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 3, disk=Probe("disk", *outs))
    for _ in range(3):
        asyncio.run(agent.wake())
    assert len(khive.sent) == 2  # Budget(hops_per_thread=2): the third report on the day's thread is held
    rows = chores.rows()
    assert rows[2]["sent_to"] == [] and rows[2]["undelivered"][0].startswith(
        f"{OWNER}: hop limit: 2 messages"
    )


def test_an_unknown_chore_is_refused_by_name(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: nope"))
    chores, agent = build(
        tmp_path, khive, ['<lact c1>check(chore="nope")</lact>', DONE], disk=Probe("disk", full())
    )
    run = asyncio.run(agent.wake())
    assert khive.sent == [] and chores.rows() == []
    assert any("no chore 'nope'; known: ['disk']" in r for r in results(run))


def test_the_digest_is_one_line_per_chore_to_the_owner_with_missing_as_the_finding_and_the_tokens(tmp_path):
    khive = FakeKhive(
        mail(SELF, "tick: disk"),
        mail(SELF, "tick: pr", mid="b2c3d4e5"),
        mail(SELF, "tick: digest", mid="c3d4e5f6"),
    )
    chores, agent = build(
        tmp_path,
        khive,
        [CHECK, DONE, '<lact c1>check(chore="pr")</lact>', DONE, "<lact d1>digest()</lact>", DONE],
        usage={"prompt_tokens": 1000, "completion_tokens": 50},
        disk=Probe("disk", full(findings=("low",), evidence="df")),
        pr=Probe("pr", RuntimeError("down")),
        gtd=Probe("gtd", full(), every=None),
        idle=Probe("idle", full()),
    )
    for _ in range(3):
        asyncio.run(agent.wake())
    digest = khive.sent[-1]
    assert digest["to"] == OWNER and digest["subject"].startswith("chores digest ")
    assert [s["to"] for s in khive.sent] == [OWNER, OWNER, STEWARD, OWNER]  # no digest to the steward
    text, rows = digest["content"], chores.rows()
    t0, t1 = rows[0]["at"][11:16], rows[1]["at"][11:16]
    assert (
        f"{OWNER} disk {t0} report 1 run(s), 1 answered, 0 quiet, 1 report, 0 escalate, "
        "0 instrument-failed; last: low" in text
    )
    assert (
        f"{OWNER} pr {t1} instrument-failed:pr 1 run(s), 0 answered, 0 quiet, 0 report, 0 escalate, "
        "1 instrument-failed" in text
    )
    # a chore with a period that did not run is the finding; one run only on an ask was not due
    assert "MISSING: idle" in text and f"---\n{t0} disk msg m1: low" in text
    assert f"{OWNER} gtd on ask: no run" in text and "MISSING: gtd" not in text
    assert "a MISSING line is the finding" in text
    day = rows[0]["at"][:10]  # the third wake's tokens are not yet in the spend when the digest is built
    assert (
        f"tokens {day}: 4 model call(s), 4000 in, 200 out, $0.0040, over 3 wake(s) with 2 mail" in text
    )  # the third wake counts its mail after its run
    assert "0 idle poll(s) at 0 tokens" in text


def test_the_digests_tokens_line_marks_a_days_cost_that_misses_calls_of_unknown_cost(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: digest"))
    turns = ["<lact d1>digest()</lact>", DONE] * 2
    chores, agent = build(tmp_path, khive, turns, disk=Probe("disk", full()))
    day = time.strftime("%Y-%m-%d")
    spend = {"day": day, "calls": 3, "in": 900, "out": 40, "cost": 0.3, "unknown": 2, "runs": 1, "mail": 1}
    agent.state.put("spend", spend, run="-", seq=0)
    asyncio.run(agent.wake())
    text = khive.sent[-1]["content"]
    assert (
        f"tokens {day}: 3 model call(s), 900 in, 40 out, $0.3000 (partial: 2 call(s) of unknown cost), "
        "over 2 wake(s) with 1 mail; 0 idle poll(s) at 0 tokens" in text
    )
    khive.mail.append(mail(SELF, "tick: digest"))
    agent.state.put(
        "spend", {k: v for k, v in spend.items() if k != "unknown"}, run="-", seq=1
    )  # before the count
    asyncio.run(agent.wake())
    assert (
        "$0.3000 (partial: calls of unknown cost before this count went uncounted), over"
        in khive.sent[-1]["content"]
    )


def label(t: float) -> str:
    """A ledger row's `at` for the instant `t`, local time as `check` writes it."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def ledger(chores: Chores, *instants: tuple[str, float]) -> None:
    """The ledger holds one quiet run of each named chore at each instant."""
    rows = [
        {"at": label(t), "ts": t, "chore": c, "findings": [], "control": True, "escalate": False}
        for c, t in instants
    ]
    chores.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))


def digest_at(chores: Chores, agent: Agent, t: float, day: str | None = None) -> str:
    """The digest of `day` (the clock's own local day unless named) with the agent's clock at `t`."""
    agent.clock = lambda: t
    return asyncio.run(chores.digest(day or time.strftime("%Y-%m-%d", time.localtime(t)), "r1"))


def test_a_weekly_chore_is_judged_against_a_week_since_its_last_run_not_against_the_day(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], weekly=Probe("weekly", full(), every="weekly"))
    t, week = datetime(2026, 9, 14, 12, 0).timestamp(), 7 * 86400  # a local noon
    ledger(chores, ("weekly", t), ("weekly", t + 20 * 86400))  # the second run is after every moment below
    due = f"last run {label(t)}, due by {label(t + week)}"
    text = digest_at(chores, agent, t + 3 * 86400)  # no run that day, and not due
    assert f"{OWNER} weekly not due: {due}" in text and "MISSING: weekly" not in text
    text = digest_at(chores, agent, t + week + 300)  # one period and the store's fire grace is within it
    assert f"{OWNER} weekly not due: {due}" in text and "MISSING: weekly" not in text
    assert f"MISSING: weekly, {due}" in digest_at(chores, agent, t + week + 301)
    # a past day is judged at its own end, from the runs recorded by then
    day = time.strftime("%Y-%m-%d", time.localtime(t + 3 * 86400))
    assert f"{OWNER} weekly not due: {due}" in digest_at(chores, agent, t + 30 * 86400, day=day)
    day = time.strftime("%Y-%m-%d", time.localtime(t + 8 * 86400))
    assert f"MISSING: weekly, {due}" in digest_at(chores, agent, t + 30 * 86400, day=day)
    with pytest.raises(ValueError):  # a day that is not a date names no moment to judge at
        asyncio.run(chores.digest("yesterday", "r1"))


def test_a_run_stamped_at_the_next_midnight_belongs_to_the_next_day_and_not_to_the_day_before(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    midnight = datetime(2026, 9, 15).timestamp()  # a local midnight, as a ledger label is
    ledger(chores, ("w", midnight))
    none = "MISSING: w, no run recorded, no tick pending"
    for now in (midnight, midnight + 5 * 86400):  # the 14th is past at the midnight itself, and after it
        text = digest_at(chores, agent, now, day="2026-09-14")
        assert none in text.splitlines() and "not due" not in text
    line = f"{OWNER} w 00:00 quiet 1 run(s), 1 answered, 1 quiet, 0 report, 0 escalate, 0 instrument-failed"
    assert line in digest_at(chores, agent, midnight + 5 * 86400, day="2026-09-15")
    ledger(chores, ("w", midnight - 1))  # a second before it is the 14th's own, and counts
    text = digest_at(chores, agent, midnight + 5 * 86400, day="2026-09-14")
    assert f"{OWNER} w 23:59 quiet 1 run(s)" in text and none not in text
    ledger(chores, ("w", midnight + 3600))  # a run at the very instant of today's digest counts for today
    assert f"{OWNER} w 01:00 quiet 1 run(s)" in digest_at(chores, agent, midnight + 3600)


def test_an_every_90m_chore_reads_missing_past_90_minutes_though_it_ran_that_day(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], p=Probe("p", full(), every="every:90m"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("p", t))
    line = f"{OWNER} p 09:00 quiet 1 run(s), 1 answered, 1 quiet, 0 report, 0 escalate, 0 instrument-failed"
    assert line in digest_at(chores, agent, t + 5400 + 300)
    text = digest_at(chores, agent, t + 5400 + 301)
    assert f"MISSING: p, last run {label(t)}, due by {label(t + 5400)}" in text and line not in text


@pytest.mark.parametrize(
    ("tz", "last", "due"),
    [
        ("UTC", "2027-01-31T09:30", "2027-02-28T09:30"),  # the 31st: due by a short month's last day
        ("UTC", "2027-02-28T09:30", "2027-03-31T09:30"),  # a month's last day: due by the next month's last
        ("UTC", "2028-01-31T09:30", "2028-02-29T09:30"),  # a leap February
        ("UTC", "2028-02-28T09:30", "2028-03-28T09:30"),  # not that February's last day
        ("UTC", "2027-04-30T09:30", "2027-05-31T09:30"),
        ("UTC", "2027-12-15T09:30", "2028-01-15T09:30"),  # across the year
        # 21:00 on Feb 27 in New York is Feb 28 in UTC, the month's last day: the calendar is UTC's
        ("America/New_York", "2027-02-28T02:00", "2027-03-31T02:00"),
    ],
)
def test_a_monthly_chore_is_due_a_calendar_month_in_utc_after_its_last_run(
    tmp_path, monkeypatch, tz, last, due
):
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        chores, agent = build(tmp_path, FakeKhive(), [], m=Probe("m", full(), every="monthly"))
        t, d = (datetime.fromisoformat(x).replace(tzinfo=UTC).timestamp() for x in (last, due))
        ledger(chores, ("m", t))
        assert f"{OWNER} m not due: last run {label(t)}, due by {label(d)}" in digest_at(
            chores, agent, d + 300
        )
        assert f"MISSING: m, last run {label(t)}, due by {label(d)}" in digest_at(chores, agent, d + 301)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_cron_chore_is_not_judged_whether_or_not_it_ran_that_day(tmp_path):
    # an empty repeat makes no tick (`ensure_ticks`), so it is an on-ask chore, not a cron
    probes = {"c": Probe("c", full(), every="0 9 * * 1"), "e": Probe("e", full(), every="")}
    chores, agent = build(tmp_path, FakeKhive(), [], **probes)
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("c", t))
    text = digest_at(chores, agent, t + 3 * 86400)
    assert f"{OWNER} c not judged: cron" in text and "MISSING: c" not in text
    assert f"{OWNER} e on ask: no run" in text
    assert (
        f"{OWNER} c 09:00 quiet 1 run(s), 1 answered, 1 quiet, 0 report, 0 escalate, 0 instrument-failed; "
        "not judged: cron"
    ) in digest_at(chores, agent, t + 60)


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("torn", "JSONDecodeError"),
        ("bytes", "UnicodeDecodeError"),
        ("directory", "IsADirectoryError"),
        ("empty", ""),  # the controls: a ledger that reads is judged, an absent one is a ledger with no run
        ("absent", ""),
    ],
)
def test_a_ledger_the_digest_cannot_read_reads_no_chore_missing_and_says_so(tmp_path, kind, error):
    khive = FakeKhive()
    probes = {"w": Probe("w", full(), every="weekly"), "a": Probe("a", full(), every=None)}
    chores, agent = build(tmp_path, khive, [], **probes)
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    if kind == "torn":  # a row cut short by a crash mid-append, after a whole one
        ledger(chores, ("w", t - 30 * 86400))
        with chores.ledger.open("a") as fh:
            fh.write('{"at": "2026-09-1')
    elif kind == "bytes":
        chores.ledger.write_bytes(b'{"at": "\xff"}\n')
    elif kind == "directory":  # a path that exists and cannot be read as the ledger
        chores.ledger.mkdir()
    elif kind == "empty":
        chores.ledger.write_text("")
    else:
        assert not chores.ledger.exists()
    text = digest_at(chores, agent, t)
    assert khive.sent[-1]["content"] == text
    head, *lines = text.splitlines()
    if not error:
        assert head.endswith(": 0 check(s); a MISSING line is the finding")
        assert lines == [f"{OWNER} a on ask: no run", "MISSING: w, no run recorded, no tick pending"]
        return
    assert f": ledger unreadable ({error}: " in head and head.endswith("; a MISSING line is the finding")
    assert lines == [f"{OWNER} a not judged: ledger unreadable", f"{OWNER} w not judged: ledger unreadable"]


def test_a_daily_chore_reads_its_days_line_as_before_and_missing_a_day_after_its_last_run(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], d=Probe("d", full(), every="daily"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("d", t))
    line = f"{OWNER} d 09:00 quiet 1 run(s), 1 answered, 1 quiet, 0 report, 0 escalate, 0 instrument-failed"
    assert line in digest_at(chores, agent, t + 3600)
    assert "MISSING: d" in digest_at(chores, agent, t + 86400 + 301)


@pytest.mark.parametrize(
    "day",
    [
        "2026-09-04",
        "2026-9-04",
        "2026-09-4",
        "2026-9-4",
        "".join(chr(0xFF10 + int(c)) for c in "2026") + "-09-04",
    ],
    ids=["padded", "month", "day", "both", "full-width-year"],  # strptime reads each of them as the date
)
def test_a_day_the_digest_is_asked_about_selects_the_same_rows_in_any_spelling_a_date_has(tmp_path, day):
    khive = FakeKhive()
    chores, agent = build(tmp_path, khive, [], w=Probe("w", full(), every="weekly"))
    chores.extra_digest = lambda d: [f"extra {d}"]  # the desk's lines are asked for the day the digest names
    t = datetime(2026, 9, 4, 9, 0).timestamp()
    ledger(chores, ("w", t))
    text = digest_at(chores, agent, t + 30 * 86400, day=day)
    head, *lines = text.splitlines()
    assert head == f"[chores digest 2026-09-04] {SELF}: 1 check(s); a MISSING line is the finding"
    assert lines == [
        f"{OWNER} w 09:00 quiet 1 run(s), 1 answered, 1 quiet, 0 report, 0 escalate, 0 instrument-failed",
        "extra 2026-09-04",
    ]
    assert khive.sent[-1]["subject"] == "chores digest 2026-09-04"


def test_a_chore_with_no_run_yet_is_missing_only_past_its_first_due_and_the_grace(tmp_path):
    probes = {
        n: Probe(n, full(), every=e) for n, e in (("w", "weekly"), ("p", "every:15m"), ("a", "every:30m"))
    }
    chores, agent = build(tmp_path, FakeKhive(), [], **probes)
    asyncio.run(chores.ensure_ticks(first_in_s=60))
    first = agent.state.get("first_due").value
    # `a` ran at its first firing and the digest fires at that instant too: `w` and `p` have not run yet
    ledger(chores, ("a", first["a"] + 2))
    assert "MISSING: " not in digest_at(chores, agent, first["a"] + 10)
    for name in ("w", "p"):
        due = first[name]
        line = f"{OWNER} {name} no run yet, first due by {label(due)}"
        assert line in digest_at(chores, agent, due - 10) and line in digest_at(chores, agent, due + 300)
        text = digest_at(chores, agent, due + 301)  # one second past the grace
        assert f"MISSING: {name}, no run recorded, first due by {label(due)}" in text and line not in text


def test_a_chore_with_no_run_no_first_due_on_record_and_no_tick_pending_is_missing_and_says_so(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    chores.ledger.write_text("")  # a ledger that reads, with no row
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    assert "MISSING: w, no run recorded, no tick pending" in digest_at(chores, agent, t).splitlines()
    for record in ("soon", True, None):  # a record that is no instant is no evidence either
        agent.state.put("first_due", {"w": record}, run="-", seq=0)
        assert "MISSING: w, no run recorded, no tick pending" in digest_at(chores, agent, t).splitlines()


_END = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC).timestamp()  # the last second a UTC datetime names


def a_chore_w(tmp_path, ran: bool):
    """A weekly chore `w` that has never run, or a monthly one run the day before the moment `t`."""
    khive = FakeKhive()
    chores, agent = build(tmp_path, khive, [], w=Probe("w", full(), every="monthly" if ran else "weekly"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, *([("w", t - 86400)] if ran else []))
    return khive, chores, agent, t


@pytest.mark.parametrize("ran", [False, True], ids=["never-run", "monthly-run"])
@pytest.mark.parametrize(
    "stored",
    [float("nan"), float("inf"), float("-inf"), True, "x", 10**20, -(10**20), 10**400, _END + 1],
    ids=["nan", "inf", "-inf", "bool", "text", "1e20", "-1e20", "huge-int", "year-10000"],
)
def test_a_first_due_the_digest_cannot_read_as_an_instant_is_no_record_and_the_digest_delivers(
    tmp_path, stored, ran
):
    khive, chores, agent, t = a_chore_w(tmp_path, ran)
    none = digest_at(chores, agent, t)  # the reading to match: no first due on record
    agent.state.put("first_due", {"w": stored}, run="-", seq=0)
    text = digest_at(chores, agent, t)
    assert text == none
    assert [m["content"] for m in khive.sent] == [none, text] and {m["to"] for m in khive.sent} == {OWNER}
    if not ran:
        assert "MISSING: w, no run recorded, no tick pending" in text.splitlines()


@pytest.mark.parametrize("ran", [False, True], ids=["never-run", "monthly-run"])
def test_a_first_due_a_clock_can_name_is_a_record_to_the_last_second_of_the_year_9999(tmp_path, ran):
    _, chores, agent, t = a_chore_w(tmp_path, ran)
    agent.state.put("first_due", {"w": _END}, run="-", seq=0)
    text = digest_at(chores, agent, t)
    if ran:  # the next month's anchor day, at the first due's time of day: Oct 31, 23:59:59 UTC
        due = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC).timestamp()
        assert f"{OWNER} w not due: last run {label(t - 86400)}, due by {label(due)}" in text
    else:
        assert f"{OWNER} w no run yet, first due by {label(_END)}" in text


@pytest.mark.parametrize("ran", [False, True], ids=["never-run", "monthly-run"])
def test_a_first_due_a_platform_cannot_convert_and_refuses_with_an_oserror_is_no_record(
    tmp_path, monkeypatch, ran
):
    class Refusing(datetime):  # as the C library does on a platform for an instant it cannot convert
        @classmethod
        def fromtimestamp(cls, t, tz=None):
            if t == 12345.0:
                raise OSError("Value too large for defined data type")
            return super().fromtimestamp(t, tz)

    _, chores, agent, t = a_chore_w(tmp_path, ran)
    none = digest_at(chores, agent, t)
    monkeypatch.setattr("hub.agent.chores.datetime", Refusing)
    agent.state.put("first_due", {"w": 12345.0}, run="-", seq=0)
    assert digest_at(chores, agent, t) == none


def test_a_fresh_agent_with_no_ledger_file_reads_no_run_yet_until_its_first_due_and_the_grace(tmp_path):
    probes = {"w": Probe("w", full(), every="weekly"), "a": Probe("a", full(), every=None)}
    chores, agent = build(tmp_path, FakeKhive(), [], **probes)
    asyncio.run(chores.ensure_ticks(first_in_s=60))  # set up: ticks and first dues, no check has run
    first = agent.state.get("first_due").value["w"]
    assert not chores.ledger.exists()
    head, *lines = digest_at(chores, agent, first).splitlines()
    assert head.endswith(": 0 check(s); a MISSING line is the finding")
    assert lines == [f"{OWNER} a on ask: no run", f"{OWNER} w no run yet, first due by {label(first)}"]
    late = digest_at(chores, agent, first + 301).splitlines()
    assert late[1:] == [
        f"{OWNER} a on ask: no run",
        f"MISSING: w, no run recorded, first due by {label(first)}",
    ]


def test_a_python_digest_wake_reads_a_pending_tick_row_for_a_chore_with_no_first_due(tmp_path):
    now = float(int(time.time()))
    row = pending("disk", now + 60, "every:30m", now)  # made a moment ago, first firing a minute out
    khive = FakeKhive(mail(SELF, "tick: digest"), agenda=[row])
    probes = {"disk": Probe("disk", full()), "w": Probe("w", full(), every="weekly")}
    chores, agent = build(tmp_path, khive, ["<lact d1>digest()</lact>", DONE], **probes)
    asyncio.run(agent.wake())  # `ensure_ticks` never ran: no first due is on record
    lines = khive.sent[-1]["content"].splitlines()
    assert f"{OWNER} disk no run yet, first due by {label(now + 60)}" in lines  # inside the first grace
    assert "MISSING: w, no run recorded, no tick pending" in lines  # no tick row at all
    assert agent.state.get("first_due") is None  # read, never recorded
    assert not [op for op in khive.ops if op.startswith("schedule.remind")]  # and no tick made
    late = digest_at(chores, agent, now + 60 + 301).splitlines()
    assert f"MISSING: disk, no run recorded, first due by {label(now + 60)}" in late


def test_the_digest_reads_the_agenda_once_and_only_for_a_chore_with_no_run_and_no_first_due(tmp_path):
    khive = FakeKhive()
    chores, agent = build(tmp_path, khive, [], **{n: Probe(n, full(), every="weekly") for n in "abcd"})
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("a", t - 86400))  # a ran
    agent.state.put("first_due", {"b": t}, run="-", seq=0)  # b has its first due on record
    digest_at(chores, agent, t)
    assert len([op for op in khive.ops if op.startswith("schedule.agenda")]) == 1  # c and d share one read
    khive.ops.clear()
    agent.state.put("first_due", {"b": t, "c": t, "d": t}, run="-", seq=0)
    digest_at(chores, agent, t)
    assert not [op for op in khive.ops if op.startswith("schedule.agenda")]  # each has a run or a record


@pytest.mark.parametrize(
    ("tz", "first", "last", "due"),
    [
        # the store runs the 28th again on Mar 28, though Feb 28 is Feb's last day
        ("UTC", "2027-01-28T09:30:00", "2027-02-28T09:30:05", "2027-03-28T09:30:00"),
        # the 31st, clamped to Feb 28 and back on the 31st
        ("UTC", "2027-01-31T09:30:00", "2027-02-28T09:30:05", "2027-03-31T09:30:00"),
        ("UTC", "2027-01-28T09:30:00", "2027-02-28T17:45:00", "2027-03-28T09:30:00"),  # the first due's time
        ("UTC", "2028-01-30T09:30:00", "2028-02-29T09:31:00", "2028-03-30T09:30:00"),  # a leap February
        ("UTC", "2027-11-30T09:30:00", "2027-12-30T09:30:03", "2028-01-30T09:30:00"),  # across the year
        # a run off schedule, before the anchor's day: the store repeats from the anchor, so it moves nothing
        ("UTC", "2027-01-28T09:30:00", "2027-02-03T12:00:00", "2027-03-28T09:30:00"),
        # a run in January for the 31st: the anchor day clamps to the next month's last day
        ("UTC", "2027-01-31T09:30:00", "2027-01-31T09:30:05", "2027-02-28T09:30:00"),
        ("UTC", "2028-01-31T09:30:00", "2028-01-31T09:31:00", "2028-02-29T09:30:00"),
        # 21:00 on Jan 27 in New York is Jan 28 in UTC: the anchor day is UTC's
        ("America/New_York", "2027-01-28T02:00:00", "2027-02-28T02:00:04", "2027-03-28T02:00:00"),
    ],
)
def test_a_monthly_chore_is_due_on_its_anchor_day_when_its_first_due_is_on_record(
    tmp_path, monkeypatch, tz, first, last, due
):
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        chores, agent = build(tmp_path, FakeKhive(), [], m=Probe("m", full(), every="monthly"))
        f, t, d = (datetime.fromisoformat(x).replace(tzinfo=UTC).timestamp() for x in (first, last, due))
        agent.state.put("first_due", {"m": f}, run="-", seq=0)
        ledger(chores, ("m", t))
        assert f"{OWNER} m not due: last run {label(t)}, due by {label(d)}" in digest_at(
            chores, agent, d + 300
        )
        assert f"MISSING: m, last run {label(t)}, due by {label(d)}" in digest_at(chores, agent, d + 301)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_monthly_chore_with_no_first_due_on_record_keeps_the_month_end_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        chores, agent = build(tmp_path, FakeKhive(), [], m=Probe("m", full(), every="monthly"))
        t = datetime(2027, 2, 28, 9, 30, 5, tzinfo=UTC).timestamp()  # Jan 28's chore, run on Feb's last day
        ledger(chores, ("m", t))
        mar28, mar31 = (datetime(2027, 3, d, 9, 30, 5, tzinfo=UTC).timestamp() for d in (28, 31))
        text = digest_at(chores, agent, mar28 + 301)  # late, never false: due by the month's end
        assert f"{OWNER} m not due: last run {label(t)}, due by {label(mar31)}" in text
        assert "MISSING: m" not in text
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("mktime", ["raises", "goes-before-1970"])
def test_a_label_in_a_year_the_clock_cannot_count_is_no_label_whichever_way_mktime_fails(monkeypatch, mktime):
    import hub.agent.chores as chores_module

    def placed(named):  # macOS raises for year 1; glibc returns an instant before 1970
        if mktime == "raises":
            raise OverflowError("mktime argument out of range")
        return -62135596800.0

    year_one = time.strptime("0001-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")  # the round trip holds
    clock = SimpleNamespace(strptime=time.strptime, mktime=placed, localtime=lambda t: year_one)
    monkeypatch.setattr(chores_module, "time", clock)  # the module's clock only
    assert chores_module._is_label("0001-01-01 00:00:00") is False


@pytest.mark.parametrize(
    ("row", "error"),
    [
        ("{}", "ValueError"),
        ('{"at": 5, "chore": "x"}', "ValueError"),
        ('{"chore": "w"}', "ValueError"),
        ('{"at": "2026-09-14 09:00:00"}', "ValueError"),
        ('{"at": "2026-09-14 09:00:00", "chore": 5}', "ValueError"),
        # a row in every other respect whole, its label no time, nor a time the clock counts
        (
            '{"at": "yesterday", "chore": "w", "findings": [], "control": true, "escalate": false}',
            "ValueError",
        ),
        (  # one error on every platform, whether mktime raises for year 1 or returns a negative instant
            '{"at": "0001-01-01 00:00:00", "chore": "w", "findings": [], "control": true, "escalate": false}',
            "ValueError",
        ),
        ("[]", "ValueError"),
        ("null", "ValueError"),
    ],
)
def test_a_ledger_row_that_parses_but_names_no_chore_or_time_makes_the_ledger_unreadable(
    tmp_path, row, error
):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("w", t - 3600))  # a run that reads `not due` were the bad row skipped
    with chores.ledger.open("a") as fh:
        fh.write(row + "\n")
    head, *lines = digest_at(chores, agent, t).splitlines()
    assert f": ledger unreadable ({error}: " in head and head.endswith("; a MISSING line is the finding")
    assert lines == [f"{OWNER} w not judged: ledger unreadable"]


_DROP = object()


@pytest.mark.parametrize(
    "change",
    [
        {},  # the control: the row as a check writes it is judged
        {"chore": ""},
        {"chore": "  "},
        {"ts": 1e300},
        {"ts": -1e300},
        {"ts": float("nan")},
        {"ts": float("inf")},
        {"ts": -1.0},  # before the clock's first second
        {"ts": 253370764800.0},  # the calendar's last year, with no month after it
        {"findings": _DROP},
        {"control": _DROP},
        {"escalate": _DROP},
        {"findings": "x"},
        {"findings": [1]},
        {"findings": None},
        {"control": "yes"},
        {"control": None},
        {"control": 1},
        {"escalate": "no"},
        {"escalate": None},
        {"escalate": 0},
        # the digest selects a row's day by its label and reads its instant from `ts`: the label is the
        # `YYYY-MM-DD HH:MM:SS` a check writes, a time that exists, on the day of `ts`
        {"at": "yesterday"},
        {"at": "2026-09-13 09:00:00"},  # a label, of the day before `ts`
        {"at": "2026-09-14 99:99:99"},  # the day's characters, no time
        {"at": "2026-09-14T09:00:00"},
        {"at": "2026-09-14 09:00:00 "},
        {"at": "2026-09-14  09:00:00"},
        {"at": "2026-9-14 9:00:00"},  # not zero-padded: its first ten characters are not the day
        # `answered`, where a row has it, is read by truthiness, so only a boolean is a value it can use
        {"answered": "false"},
        {"answered": "yes"},
        {"answered": None},
        {"answered": 0},
        {"answered": 1},
    ],
    ids=lambda c: ", ".join(f"{k}={'missing' if v is _DROP else v!r}" for k, v in c.items()) or "intact",
)
def test_a_ledger_row_the_digest_cannot_judge_from_makes_the_ledger_unreadable(tmp_path, change):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("w", t - 3600))  # a run that reads `not due` were the bad row skipped
    row = {"at": label(t), "ts": t, "chore": "w", "findings": [], "control": True, "escalate": False}
    row = {k: v for k, v in {**row, **change}.items() if v is not _DROP}
    with chores.ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    head, *lines = digest_at(chores, agent, t).splitlines()
    if not change:
        assert head.endswith(": 2 check(s); a MISSING line is the finding")
        assert lines == [
            f"{OWNER} w 09:00 quiet 2 run(s), 2 answered, 2 quiet, 0 report, 0 escalate, 0 instrument-failed"
        ]
        return
    assert ": ledger unreadable (ValueError: " in head and head.endswith("; a MISSING line is the finding")
    assert lines == [f"{OWNER} w not judged: ledger unreadable"]


@pytest.mark.parametrize(
    ("at", "exists"),
    [
        ("2026-03-08 02:30:00", False),  # the hour the spring change skips
        ("2026-03-08 02:00:00", False),  # its first second
        ("2026-03-08 01:59:59", True),
        ("2026-03-08 03:30:00", True),
        ("2026-11-01 01:30:00", True),  # the hour the autumn change repeats: a time that exists, twice
    ],
    ids=["skipped-hour", "first-skipped-second", "second-before-skip", "after-skip", "repeated-hour"],
)
def test_a_label_in_a_skipped_hour_names_no_time_and_one_in_a_repeated_hour_names_one(
    tmp_path, monkeypatch, at, exists
):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
        row = {"at": at, "chore": "w", "findings": [], "control": True, "escalate": False}
        chores.ledger.write_text(json.dumps(row) + "\n")  # no `ts`: a row from before rows carried one
        t = datetime(2026, 11, 2, 12, 0).timestamp()
        head, *_ = digest_at(chores, agent, t, day=at[:10]).splitlines()
        if exists:
            assert head.endswith(": 1 check(s); a MISSING line is the finding")
        else:
            assert ": ledger unreadable (ValueError: a ledger row has no usable at: " in head
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize(
    ("rows", "line"),
    [
        # 01:30 names 05:30Z and 06:30Z: a 30-minute chore run at either is due by 06:05Z or 07:05Z
        ([("2026-11-01 01:30:00", None)], "h not judged: a run without `ts` names an hour the clock repeats"),
        # the same run with its `ts` is judged: its instant is one
        ([("2026-11-01 01:30:00", "06:30")], "h 01:30 quiet"),
        # a later run that either reading precedes leaves the repeated hour out of the judgement
        ([("2026-11-01 01:30:00", None), ("2026-11-01 01:31:00", "06:31")], "h 01:31 quiet"),
        # a run without `ts` outside the repeated hour is judged as it was
        ([("2026-11-01 00:50:00", None)], "MISSING: h"),
    ],
    ids=["repeated-hour", "repeated-hour-with-ts", "later-run-with-ts", "outside-the-hour"],
)
def test_a_run_without_ts_in_a_repeated_hour_is_not_judged_from_while_it_may_be_the_last(
    tmp_path, monkeypatch, rows, line
):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        chores, agent = build(tmp_path, FakeKhive(), [], h=Probe("h", full(), every="every:30m"))
        written = []
        for at, utc in rows:
            row = {"at": at, "chore": "h", "findings": [], "control": True, "escalate": False}
            if utc is not None:
                hh, mm = map(int, utc.split(":"))
                row["ts"] = datetime(2026, 11, 1, hh, mm, tzinfo=UTC).timestamp()
            written.append(json.dumps(row) + "\n")
        chores.ledger.write_text("".join(written))
        t = datetime(2026, 11, 1, 6, 35, tzinfo=UTC).timestamp()  # 01:35 EST
        _, *lines = digest_at(chores, agent, t, day="2026-11-01").splitlines()
        assert len(lines) == 1 and (lines[0].startswith(line) or lines[0].startswith(f"{OWNER} {line}"))
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_row_the_digest_cannot_select_by_its_label_is_not_a_chore_that_never_ran(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], a=Probe("a", full(), every=None))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    row = {"at": "yesterday", "ts": t, "chore": "a", "findings": [], "control": True, "escalate": False}
    chores.ledger.write_text(json.dumps(row) + "\n")  # an on-ask chore that ran, its row on no day
    head, *lines = digest_at(chores, agent, t).splitlines()
    assert ": ledger unreadable (ValueError: a ledger row has no usable at: " in head
    assert lines == [f"{OWNER} a not judged: ledger unreadable"]  # not `on ask: no run`


class Straddle:
    """`time` as the ledger writer sees it across local midnight: once armed, its first read is a second
    before midnight and every later one a second after."""

    def __init__(self, midnight: float):
        self.midnight, self.armed, self.reads = midnight, False, 0

    def _read(self) -> float:
        if not self.armed:
            return time.time()
        self.reads += 1
        return self.midnight - 1 if self.reads == 1 else self.midnight + 1

    def time(self) -> float:
        return self._read()

    def localtime(self, t: float | None = None):
        return time.localtime(self._read() if t is None else t)

    def __getattr__(self, name: str):
        return getattr(time, name)


def test_a_row_written_across_local_midnight_names_one_day_by_both_its_label_and_its_instant(
    tmp_path, monkeypatch
):
    chores, agent = build(
        tmp_path, FakeKhive(mail(SELF, "tick: disk")), [CHECK, DONE], disk=Probe("disk", full())
    )
    midnight = datetime(2026, 9, 15).timestamp()  # local
    clock, text = Straddle(midnight), Chores._text

    def armed(self, *args, **kwargs):
        clock.armed = True  # the row is built next
        return text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("hub.agent.chores.time", clock)
        patch.setattr(Chores, "_text", armed)
        asyncio.run(agent.wake())
    (row,) = chores.rows()
    head, *_ = digest_at(chores, agent, midnight + 3600, day="2026-09-14").splitlines()
    assert head.endswith(": 1 check(s); a MISSING line is the finding")  # a later digest reads the ledger
    assert row["ts"] == midnight - 1 and row["at"] == label(row["ts"]) == "2026-09-14 23:59:59"


def test_a_label_and_an_instant_on_different_days_make_the_ledger_unreadable_naming_both(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    row = {"at": label(t - 86400), "ts": t, "chore": "w", "findings": [], "control": True, "escalate": False}
    chores.ledger.write_text(json.dumps(row) + "\n")
    head, *lines = digest_at(chores, agent, t).splitlines()
    assert ": ledger unreadable (ValueError: a ledger row's `at` and `ts` name different days: " in head
    assert lines == [f"{OWNER} w not judged: ledger unreadable"]


@pytest.mark.parametrize(
    "ts",
    ["bad", True, False, None, "1789498790", [1789498790.0], float("nan"), float("inf"), float("-inf")],
    ids=repr,
)
def test_a_ts_that_is_not_a_finite_number_makes_the_ledger_unreadable_naming_the_field(tmp_path, ts):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("w", t - 3600))  # a run that reads `not due` were the bad row skipped
    row = {"at": label(t), "ts": ts, "chore": "w", "findings": [], "control": True, "escalate": False}
    with chores.ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")  # the standard library writes a non-finite float as NaN or Infinity
    head, *lines = digest_at(chores, agent, t).splitlines()
    assert ": ledger unreadable (ValueError: a ledger row has no usable ts: " in head
    assert lines == [f"{OWNER} w not judged: ledger unreadable"]


@pytest.mark.parametrize("kind", ["none-written", "int", "float"])
def test_a_row_with_no_ts_or_a_finite_number_for_one_is_judged(tmp_path, kind):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    ledger(chores, ("w", t - 3600))
    ts = {"none-written": _DROP, "int": int(t), "float": float(t)}[kind]
    row = {"at": label(t), "ts": ts, "chore": "w", "findings": [], "control": True, "escalate": False}
    row = {k: v for k, v in row.items() if v is not _DROP}  # a row from before rows carried `ts` has none
    assert ("ts" in row) is (kind != "none-written")
    with chores.ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    head, *lines = digest_at(chores, agent, t + 1800).splitlines()
    assert head.endswith(": 2 check(s); a MISSING line is the finding")
    assert lines == [
        f"{OWNER} w 09:00 quiet 2 run(s), 2 answered, 2 quiet, 0 report, 0 escalate, 0 instrument-failed"
    ]


def test_a_row_counts_as_answered_by_its_answered_field_and_by_its_control_where_it_has_none(tmp_path):
    chores, agent = build(tmp_path, FakeKhive(), [], w=Probe("w", full(), every="weekly"))
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    base = {"at": label(t), "ts": t, "chore": "w", "findings": [], "control": True, "escalate": False}
    rows = [  # an instrument that failed has no answer unless the row says otherwise; one that passed has
        {**base, "findings": ["x"], "control": False},
        {**base, "answered": False},
        {**base, "answered": True},
        base,  # a row from before the field: its control says
    ]
    chores.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    line = f"{OWNER} w 09:00 quiet 4 run(s), 2 answered, 3 quiet, 0 report, 0 escalate, 1 instrument-failed"
    assert line in digest_at(chores, agent, t).splitlines()


def test_aged_tells_the_steward_once_what_the_owner_left_unanswered_and_nothing_acknowledged(tmp_path):
    khive = FakeKhive(
        mail(SELF, "tick: disk"),
        mail(SELF, "tick: pr", mid="b2c3d4e5"),
        mail(SELF, "tick: aged", mid="c3d4e5f6"),
        mail(SELF, "tick: aged", mid="d4e5f6a7"),
    )
    chores, agent = build(
        tmp_path,
        khive,
        [
            CHECK,
            DONE,
            '<lact c1>check(chore="pr")</lact>',
            DONE,
            "<lact a1>aged()</lact>",
            DONE,
            "<lact a1>aged()</lact>",
            DONE,
        ],
        disk=Probe("disk", full(findings=("low",), evidence="df"), every="every:30m"),
        pr=Probe("pr", full(findings=("#7 red",), evidence="gh"), every="every:30m"),
    )
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER, OWNER]
    age_ledger(chores, hours=1.5)  # past two 30-minute periods
    now = datetime.now(UTC).isoformat()
    khive.threads["t2"] = [{"properties": {"from_actor": OWNER, "sent_at": now}, "content": "seen, fixing"}]
    run = asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent[2:]] == [STEWARD]
    text = khive.sent[2]["content"]
    assert khive.sent[2]["subject"] == f"aged: 1 unanswered report(s) from {OWNER}"
    assert "- disk " in text and "no reply among 0 message(s); thread t1): low" in text
    assert "#7 red" not in text  # the owner answered on t2: closed, never aged
    assert any(f"aged: 1 report(s) sent to {STEWARD}" in r for r in results(run))
    run = asyncio.run(agent.wake())  # told once: the second pass has nothing
    assert len(khive.sent) == 3
    assert any("aged: nothing unanswered" in r for r in results(run))
    rows = chores.rows()
    assert sorted(agent.state.get("aged_sent").value) == sorted(_aged_key(r) for r in rows[:2])
    assert period_s("every:30m") == 1800 and period_s("daily") == 86400 and period_s(None) == 86400
    assert Aged().chore is None


@pytest.mark.parametrize(
    "time_of",
    [
        lambda row: {"at": "0001-01-01 00:00:00"},
        lambda row: {"ts": -62135596800.0},
        lambda row: {"ts": "soon"},
        lambda row: {"ts": row["ts"] - 3 * 86400},  # old enough to age, on a day its label does not name
        lambda row: {"ts": 10**400},  # a JSON integer no float holds
    ],
    ids=["year-one-label", "ts-before-1970", "ts-not-a-number", "ts-on-another-day", "ts-no-float-holds"],
)
def test_aged_names_and_never_ages_a_report_whose_time_is_no_instant_to_count_from(tmp_path, time_of):
    # a time the digest cannot count is none here either: no age, so no false escalation, and no raise
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: aged", mid="c3d4e5f6"))
    chores, agent = build(
        tmp_path,
        khive,
        [CHECK, DONE, "<lact a1>aged()</lact>", DONE],
        disk=Probe("disk", full(findings=("low",), evidence="df"), every="every:30m"),
    )
    asyncio.run(agent.wake())
    assert [s["to"] for s in khive.sent] == [OWNER]
    age_ledger(chores, hours=1.5)  # past two periods: a countable time would be aged
    (row,) = chores.rows()
    change = time_of(row)
    if "at" in change:
        row.pop("ts", None)  # a row from before `ts` reads its label
    row.update(change)
    chores.ledger.write_text(json.dumps(row) + "\n")
    run = asyncio.run(agent.wake())
    assert len(khive.sent) == 1  # the steward is told nothing
    said = [e.content.value for e in run.record if e.kind is Kind.RESULT and e.content.spec == "aged"]
    named = f"1 report(s) not aged, their time is no instant to count from: disk {row['at']!r}"
    assert said == [f"aged: nothing unanswered; {named}"]


def test_two_aged_passes_of_one_turn_tell_the_steward_once_and_keep_both_markers(tmp_path):
    khive = Yielding(
        mail(SELF, "tick: disk"),
        mail(SELF, "tick: pr", mid="b2c3d4e5"),
        mail(SELF, "tick: aged", mid="c3d4e5f6"),
    )
    twice = '<lact a1>aged()</lact><lact a2>aged(chore="pr")</lact>'
    chores, agent = build(
        tmp_path,
        khive,
        [CHECK, DONE, '<lact c1>check(chore="pr")</lact>', DONE, twice, DONE],
        disk=Probe("disk", full(findings=("low",), evidence="df"), every="every:30m"),
        pr=Probe("pr", full(findings=("#7 red",), evidence="gh"), every="every:30m"),
    )
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    age_ledger(chores, hours=1.5)
    asyncio.run(agent.wake())  # both passes run together; each finds what the other has not marked yet
    aged = "".join(s["content"] for s in khive.sent if s["to"] == STEWARD)
    assert (aged.count("\n- disk "), aged.count("\n- pr ")) == (1, 1)  # each report told once
    rows = chores.rows()
    assert sorted(agent.state.get("aged_sent").value) == sorted(_aged_key(r) for r in rows)


@pytest.mark.parametrize("n", [1000, 1001])
def test_each_unanswered_report_is_told_to_the_steward_once_however_many_markers_the_ledger_holds(
    tmp_path, n
):
    khive = FakeKhive(*(mail(SELF, "tick: aged", mid=f"a{i:07x}") for i in range(3)))
    chores, agent = build(
        tmp_path, khive, ["<lact a1>aged()</lact>", DONE] * 3, disk=Probe("disk", full(), every="every:30m")
    )
    start = time.time() - 86400  # a day back: every report is past two 30-minute periods
    rows = [
        {
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start + i)),
            "ts": start + i,
            "chore": "disk",
            "findings": ["low"],
            "msg": f"m{i}",
            "thread": f"t{i}",
            "sent_to": [OWNER],
        }
        for i in range(n)
    ]
    chores.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    agent.state.put("aged_sent", ["disk|2026-01-01 00:00:00"], run="-", seq=0)  # its row left the ledger
    told = []
    for _ in range(3):
        before = len(khive.sent)
        asyncio.run(agent.wake())
        told.append(sum(s["content"].count("\n- disk ") for s in khive.sent[before:]))
    assert told == [n, 0, 0]
    assert all(s["to"] == STEWARD for s in khive.sent)
    assert sorted(agent.state.get("aged_sent").value) == sorted(_aged_key(r) for r in rows)


def test_a_report_in_the_hour_a_clock_change_repeats_is_aged_from_its_own_instant(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        khive = FakeKhive()
        chores, _ = build(tmp_path, khive, [], disk=Probe("disk", full()))
        label = "2026-11-01 01:30:00"  # 01:30 EDT, then 01:30 EST an hour later
        first, second = (datetime(2026, 11, 1, h, 30, tzinfo=UTC).timestamp() for h in (5, 6))
        assert {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) for t in (first, second)} == {label}
        reply = datetime(2026, 11, 1, 6, 0, tzinfo=UTC).isoformat()  # between the two instants
        khive.threads["t1"] = [{"properties": {"from_actor": OWNER, "sent_at": reply}, "content": "on it"}]
        acked = [
            asyncio.run(chores._acknowledged({"at": label, "ts": ts, "thread": "t1"}))[0]
            for ts in (first, second)
        ]
        assert acked == [True, False]  # the reply came after the first report and before the second
        keys = {_aged_key({"chore": "disk", "at": label, "ts": ts}) for ts in (first, second)}
        assert len(keys) == 2  # two reports, two aged markers, though both read 01:30
        old = {"at": "2026-09-22 12:00:00", "thread": "t1"}  # a row from before `ts`: its label is read
        assert asyncio.run(chores._acknowledged(old)) == (True, "answered")
        # one from before `ts` in the repeated hour counts from the later instant, as it ages: a reply
        # between the two may precede the report, so it acknowledges nothing
        legacy = {"at": label, "thread": "t1"}
        import hub.agent.chores as chores_module

        with (
            monkeypatch.context() as m
        ):  # the earlier instant, as macOS converts the label: on every platform
            m.setattr(chores_module, "_epoch", lambda row: first)
            assert asyncio.run(chores._acknowledged(legacy)) == (False, "no reply among 1 message(s)")
        nowhen = {"at": "0001-01-01 00:00:00", "thread": "t1"}
        assert asyncio.run(chores._acknowledged(nowhen)) == (False, "no instant to count from")
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_report_without_ts_in_a_repeated_hour_ages_from_the_later_instant_it_names(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        label = "2026-11-01 01:30:00"  # 01:30 EDT, then 01:30 EST an hour later
        first, second = (datetime(2026, 11, 1, h, 30, tzinfo=UTC).timestamp() for h in (5, 6))
        assert _countable({"at": label}) == second  # told only once both readings are old enough
        assert _countable({"at": label, "ts": first}) == first  # a row with `ts` names one instant
        assert _countable({"at": label, "ts": first - 86400}) is None  # a `ts` the label's day does not hold
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_missed_tick_of_the_actors_own_is_run_at_the_next_check_and_only_once(tmp_path):
    events = [
        {
            "id": "ev1",
            "content": "tick: pr (fired at 2026-09-21T03:04:09Z)",
            "status": "missed",
            "properties": {"created_by_actor": SELF},
        },
        {
            "id": "ev2",
            "content": "tick: disk",
            "status": "missed",
            "properties": {"created_by_actor": "lambda:other"},
        },
        {"id": "ev3", "content": "tick: disk", "status": "fired", "properties": {"created_by_actor": SELF}},
    ]
    # a store that ignores the filters: the code's own check still keeps the actor's missed and failed rows
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: disk", mid="b2c3d4e5"), events=events)
    chores, agent = build(
        tmp_path,
        khive,
        [CHECK, DONE] * 2,
        disk=Probe("disk", full()),
        pr=Probe("pr", full(findings=("#7 red",), evidence="gh")),
    )
    run = asyncio.run(agent.wake())
    rows = chores.rows()
    assert [r["chore"] for r in rows] == ["pr", "disk"] and rows[0]["args"] == {"missed_tick": "ev1"}
    assert [s["to"] for s in khive.sent] == [OWNER] and "#7 red" in khive.sent[0]["content"]
    assert rows[1]["caught_up"] == [
        "tick: pr (fired at 2026-09-21T03:04:09Z): missed, ran now: pr: reported, 1 finding(s), "
        "sent to ['lambda:x']"
    ]
    assert any("missed ticks run: [" in r and "tick: pr (fired" in r for r in results(run))
    asyncio.run(agent.wake())
    assert [r["chore"] for r in chores.rows()] == ["pr", "disk", "disk"]  # ev1 seen, not run again
    assert agent.state.get("missed_seen").value == ["ev1"]


@pytest.mark.parametrize("bare_pages", [False, True])
def test_missed_ticks_past_the_first_page_of_the_store_are_each_run_once(tmp_path, bare_pages):
    events = [
        {"id": f"ev{i}", "content": "tick: pr", "status": "missed", "properties": {"created_by_actor": SELF}}
        for i in range(201)
    ]
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: disk", mid="b2c3d4e5"), events=events)
    khive.filtered = True
    khive.bare_pages = bare_pages  # a full bare page is read as a page with more behind it
    chores, agent = build(
        tmp_path, khive, [CHECK, DONE] * 2, disk=Probe("disk", full()), pr=Probe("pr", full())
    )
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    caught = [r["args"]["missed_tick"] for r in chores.rows() if r["chore"] == "pr"]
    assert sorted(caught) == sorted(e["id"] for e in events)  # all 201, each once, over two passes
    assert [r["chore"] for r in chores.rows()][-2:] == ["disk", "disk"]
    missed = f'list(kind="scheduled_event", created_by_actor="{SELF}", status="missed", limit=200'
    failed = f'list(kind="scheduled_event", created_by_actor="{SELF}", status="failed", limit=200)'
    assert [o for o in khive.ops if o.startswith("[list(")] == [
        f"[{missed}), {failed}]",  # the failed list ends on its first page, the missed one goes on
        f"[{missed}, offset=200)]",
    ] * 2


def test_a_seen_missed_tick_is_remembered_while_the_store_lists_it_however_many_there_are(tmp_path):
    events = [
        {
            "id": f"ev{i}",
            "content": "tick: gone",
            "status": "failed",
            "properties": {"created_by_actor": SELF},
        }
        for i in range(501)
    ]
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"b{i:07x}") for i in range(5)), events=events)
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 5, disk=Probe("disk", full()))
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    noted = [len(r["caught_up"]) for r in chores.rows()]
    assert noted == [501, 0]  # a chore this agent does not hold is noted, once each
    assert chores.rows()[0]["caught_up"][0] == "tick: gone: failed, noted"
    khive.events = events[
        1:
    ]  # the store no longer lists the first: a list read by offset can skip a row once
    khive.events.append(
        {"id": "ev501", "content": "tick: gone", "status": "missed", "properties": {"created_by_actor": SELF}}
    )
    asyncio.run(agent.wake())
    assert chores.rows()[-1]["caught_up"] == ["tick: gone: missed, noted"]
    assert agent.state.get("missed_seen").value == [f"ev{i}" for i in range(502)]  # ev0 missed once: kept
    khive.events = events[400:]  # ev0 missed twice: let go, on a pass that notes nothing; the rest once
    asyncio.run(agent.wake())
    assert chores.rows()[-1]["caught_up"] == []
    assert agent.state.get("missed_seen").value == [f"ev{i}" for i in range(1, 502)]
    khive.events = events[400:] + [events[1]]  # ev1 is back and kept; the others missed twice: let go
    asyncio.run(agent.wake())
    assert chores.rows()[-1]["caught_up"] == []
    assert agent.state.get("missed_seen").value == [f"ev{i}" for i in (1, *range(400, 501))]


@pytest.mark.parametrize(
    "page", [{"items": [], "has_more": True}, {"has_more": True}, {"items": None}, None, "rows"]
)
def test_a_page_that_is_not_a_page_fails_the_missed_tick_read_and_lets_no_id_go(tmp_path, page):
    events = [
        {"id": f"ev{i}", "content": "tick: pr", "status": "missed", "properties": {"created_by_actor": SELF}}
        for i in range(201)
    ]
    khive = FakeKhive(mail(SELF, "tick: disk"), mail(SELF, "tick: disk", mid="b2c3d4e5"), events=events)
    khive.filtered = True
    paged = khive.exec

    async def exec(ops):  # the store answers the second page with something that is no page
        return [page] if "offset=200" in ops else await paged(ops)

    khive.exec = exec
    chores, agent = build(
        tmp_path, khive, [CHECK, DONE] * 2, disk=Probe("disk", full()), pr=Probe("pr", full())
    )
    asyncio.run(agent.wake())
    asyncio.run(agent.wake())
    assert [r["chore"] for r in chores.rows()] == ["disk", "disk"]  # the first page's rows ran nowhere
    failed = [r["caught_up"] for r in chores.rows()]
    assert all(
        len(f) == 1
        and f[0].startswith(
            "missed-tick read failed: ValueError: the missed page at offset 200 is not a page: "
        )
        for f in failed
    ), failed
    assert agent.state.get("missed_seen") is None  # nothing seen, so nothing let go
    assert agent.state.get("missed_gone") is None


def test_the_seen_ids_are_matched_by_set_so_a_long_list_costs_each_row_one_look(tmp_path):
    looks = []

    class Id(str):  # an id that counts how often the sweep compares it
        __hash__ = str.__hash__

        def __eq__(self, other):
            looks.append(1)
            return str.__eq__(self, other)

    n = 2000
    events = [
        {
            "id": f"ev{i}",
            "content": "tick: gone",
            "status": "failed",
            "properties": {"created_by_actor": SELF},
        }
        for i in range(n)
    ]
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"c{i:07x}") for i in range(2)), events=events)
    khive.filtered = True
    chores, agent = build(tmp_path, khive, [CHECK, DONE] * 2, disk=Probe("disk", full()))
    asyncio.run(agent.wake())  # the warm pass: every id lands under missed_seen
    assert len(chores.rows()[0]["caught_up"]) == n
    for e in events:
        e["id"] = Id(e["id"])
    asyncio.run(agent.wake())  # the second pass sees every id already
    assert chores.rows()[-1]["caught_up"] == []
    assert agent.state.get("missed_seen").value == [f"ev{i}" for i in range(n)]  # the order kept
    assert 0 < len(looks) < 10 * n, len(looks)  # a list scan would cost n * n / 2


@pytest.mark.parametrize("bare_pages", [False, True])
@pytest.mark.parametrize("word", ["the row", "a row without a status", "fired", "a failed read"])
def test_a_row_the_pages_skip_twice_is_kept_on_the_stores_word_and_runs_once(tmp_path, bare_pages, word):
    events = [
        {"id": f"ev{i}", "content": "tick: pr", "status": "missed", "properties": {"created_by_actor": SELF}}
        for i in range(201)
    ]
    khive = FakeKhive(*(mail(SELF, "tick: disk", mid=f"d{i:07x}") for i in range(4)), events=events)
    khive.filtered, khive.bare_pages = True, bare_pages
    paged, passes = khive.exec, []

    async def exec(ops):  # on the second and third pass a row ahead of ev200 leaves between the two page
        if ops.startswith("[list(") and "offset=" not in ops:  # reads, so ev200 shifts into the gap and
            passes.append(1)  # neither page shows it
        if ops == 'get(id="ev200")' and word != "the row":  # the store's answer, as asked for
            khive.ops.append(ops)
            if word == "a failed read":  # its text names not_found, but it is no word from the store
                raise KhiveError("kkernel exited 1: get: upstream_not_found: no route to the store")
            return [
                {"id": "ev200"} if word == "a row without a status" else {"id": "ev200", "status": "fired"}
            ]
        pages = await paged(ops)
        if 2 <= len(passes) <= 3 and "offset=200" in ops:
            pages = [
                [r for r in p if r["id"] != "ev200"]
                if isinstance(p, list)
                else {**p, "items": [r for r in p["items"] if r["id"] != "ev200"]}
                for p in pages
            ]
        return pages

    khive.exec = exec
    chores, agent = build(
        tmp_path, khive, [CHECK, DONE] * 4, disk=Probe("disk", full()), pr=Probe("pr", full())
    )
    for _ in range(4):
        asyncio.run(agent.wake())
    caught = [r["args"]["missed_tick"] for r in chores.rows() if r["chore"] == "pr"]
    if word == "fired":  # the store's word: no longer missed, let go, and it runs again when it comes back
        assert caught.count("ev200") == 2 and len(caught) == 202
    else:  # skipped twice and asked for by id: a row, an answer with no status, or no answer keeps it
        assert caught.count("ev200") == 1 and len(caught) == 201
        assert "ev200" in agent.state.get("missed_seen").value
    assert [o for o in khive.ops if o.startswith("get(id=")] == [
        'get(id="ev200")'
    ]  # asked once, on the second miss


def test_a_missed_tick_of_the_chore_this_check_runs_is_covered_by_the_check_itself(tmp_path):
    events = [
        {
            "id": f"ev{i}",
            "content": "tick: disk",
            "status": "missed",
            "properties": {"created_by_actor": SELF},
        }
        for i in range(3)
    ]
    khive = FakeKhive(mail(SELF, "tick: disk"), events=events)
    khive.filtered = True
    chores, agent = build(tmp_path, khive, [CHECK, DONE], disk=Probe("disk", full()))
    asyncio.run(agent.wake())
    rows = chores.rows()
    assert [r["chore"] for r in rows] == ["disk"]  # one run of disk, not four
    assert rows[0]["caught_up"] == ["tick: disk: missed, this check runs it"] * 3
    assert agent.state.get("missed_seen").value == ["ev0", "ev1", "ev2"]


def test_a_check_the_ask_narrowed_covers_no_missed_tick_of_its_chore(tmp_path):
    events = [
        {"id": "ev0", "content": "tick: disk", "status": "missed", "properties": {"created_by_actor": SELF}}
    ]
    khive = FakeKhive(mail(SELF, "tick: disk"), events=events)
    khive.filtered = True
    narrowed = '<lact c1>check(chore="disk", args={"paths": ["/"]})</lact>'
    chores, agent = build(tmp_path, khive, [narrowed, DONE], disk=Probe("disk", full()))
    asyncio.run(agent.wake())
    rows = chores.rows()
    assert [r["chore"] for r in rows] == ["disk", "disk"]  # the missed sweep, then the narrowed check
    assert rows[0]["args"] == {"missed_tick": "ev0"} and rows[1]["args"] == {"paths": ["/"]}
    assert rows[1]["caught_up"][0].startswith("tick: disk: missed, ran now: disk: ")
    assert agent.state.get("missed_seen").value == ["ev0"]


def test_the_missed_tick_read_asks_the_store_for_the_actors_missed_and_failed_rows_before_the_page(tmp_path):
    noise = [
        {"id": f"n{i}", "content": "tick: disk", "status": status, "properties": {"created_by_actor": actor}}
        for i, (actor, status) in enumerate([("lambda:other", "missed"), (SELF, "fired")] * 100)
    ]
    late = [
        {"id": "ev9", "content": "tick: pr", "status": "missed", "properties": {"created_by_actor": SELF}},
        {"id": "ev8", "content": "tick: pr", "status": "failed", "properties": {"created_by_actor": SELF}},
    ]
    khive = FakeKhive(mail(SELF, "tick: disk"), events=noise + late)  # both past an unfiltered page of 200
    khive.filtered = True
    chores, agent = build(tmp_path, khive, [CHECK, DONE], disk=Probe("disk", full()), pr=Probe("pr", full()))
    asyncio.run(agent.wake())
    assert [op for op in khive.ops if "scheduled_event" in op] == [
        f'[list(kind="scheduled_event", created_by_actor="{SELF}", status="missed", limit=200), '
        f'list(kind="scheduled_event", created_by_actor="{SELF}", status="failed", limit=200)]'
    ]
    rows = chores.rows()
    assert [(r["chore"], r["args"]) for r in rows] == [
        ("pr", {"missed_tick": "ev9"}),
        ("pr", {"missed_tick": "ev8"}),
        ("disk", {}),
    ]
    assert agent.state.get("missed_seen").value == ["ev9", "ev8"]


def test_ensure_ticks_creates_the_reminders_that_are_missing_and_only_those(tmp_path):
    # another agent's "tick: digest" sits in the same namespace's agenda: not this actor's, created anyway
    khive = FakeKhive(agenda=["tick: disk", "unrelated reminder", ("tick: digest", "lambda:other:agent")])
    chores, _ = build(
        tmp_path,
        khive,
        [],
        disk=Probe("disk", full()),
        pr=Probe("pr", full(), every="every:15m"),
        ask=Probe("ask", full(), every=None),
    )
    created = asyncio.run(chores.ensure_ticks(first_in_s=60))
    assert created == ["tick: pr", "tick: digest", "tick: aged"]
    reminds = [op for op in khive.ops if op.startswith("schedule.remind")]
    assert len(reminds) == 3
    assert 'content="tick: pr"' in reminds[0] and 'repeat="every:15m"' in reminds[0]
    assert 'content="tick: digest"' in reminds[1] and 'repeat="daily"' in reminds[1]
    assert 'content="tick: aged"' in reminds[2] and 'repeat="every:1h"' in reminds[2]
    assert asyncio.run(chores.ensure_ticks()) == created  # the fake agenda does not grow; a real one would


def test_ensure_ticks_keeps_a_chores_first_firing_once_and_never_overwrites_an_earlier_one(tmp_path):
    khive = FakeKhive(agenda=["tick: disk"])  # its tick is pending and names no firing: none to keep
    chores, agent = build(
        tmp_path,
        khive,
        [],
        disk=Probe("disk", full()),
        pr=Probe("pr", full(), every="every:15m"),
        ask=Probe("ask", full(), every=None),
    )
    asyncio.run(chores.ensure_ticks(first_in_s=60))
    (remind,) = [op for op in khive.ops if op.startswith("schedule.remind") and 'content="tick: pr"' in op]
    sent = datetime.fromisoformat(re.search(r'at="([^"]+)"', remind).group(1)).timestamp()
    first = agent.state.get("first_due").value
    assert first == {"pr": sent}  # not disk (no firing named), ask (no tick), digest or aged (not chores)
    chores.instruments["new"] = Probe("new", full(), every="daily")
    asyncio.run(
        chores.ensure_ticks(first_in_s=7200)
    )  # the fake agenda does not grow: pr's tick is made again
    kept = agent.state.get("first_due").value
    assert kept["pr"] == sent and set(kept) == {"pr", "new"} and kept["new"] > sent


def test_ensure_ticks_records_a_first_due_for_a_tick_it_finds_pending_from_its_own_row(tmp_path):
    # ticks set before first dues were kept, or by a start that stopped between its reminder and the record
    nine = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    khive = FakeKhive(
        agenda=[
            ("tick: disk", SELF, "pending", "2026-09-14T09:00:00+00:00"),  # the store's own spelling
            # no creation instant to step back to: the firing, or now if that is earlier (the firing is past)
            ("tick: pr", SELF, "pending", "2026-09-14T09:15:00Z"),
            ("tick: w", SELF, "pending"),  # names no firing
            ("tick: m", SELF, "pending", "soon"),  # names no instant
            ("tick: old", SELF, "cancelled", "2026-09-01T00:00:00Z"),  # not pending: its firing is not coming
            ("tick: other", "lambda:other:agent", "pending", "2026-09-01T00:00:00Z"),  # not this actor's
        ]
    )
    chores, agent = build(
        tmp_path,
        khive,
        [],
        disk=Probe("disk", full()),
        pr=Probe("pr", full(), every="every:15m"),
        w=Probe("w", full(), every="weekly"),
        m=Probe("m", full(), every="monthly"),
        old=Probe("old", full(), every="daily"),
        other=Probe("other", full(), every="daily"),
    )
    created = asyncio.run(chores.ensure_ticks(first_in_s=60))  # nothing pending is made again
    assert created == ["tick: old", "tick: other", "tick: digest", "tick: aged"]
    sent = {  # a tick made now is first due at the moment it was asked for
        c: datetime.fromisoformat(re.search(r'at="([^"]+)"', op).group(1)).timestamp()
        for op in khive.ops
        if op.startswith("schedule.remind")
        for c in ("old", "other")
        if f'content="tick: {c}"' in op
    }
    first = agent.state.get("first_due").value
    assert first == {"disk": nine.timestamp(), "pr": (nine + timedelta(minutes=15)).timestamp(), **sent}
    # the digest reads each as not yet due, not missing, until its first due and the grace are past
    ledger(chores, ("old", nine.timestamp() - 3600))
    text = digest_at(chores, agent, nine.timestamp() - 600)
    assert f"{OWNER} disk no run yet, first due by {label(nine.timestamp())}" in text.splitlines()
    assert "MISSING: disk" not in text and "MISSING: w" not in text and "MISSING: m" not in text
    # a row naming no firing, or no instant, may be the earliest: the chore is not judged, not missing
    for chore in ("w", "m"):
        assert f"{OWNER} {chore} not judged: a pending tick row names no first due" in text.splitlines()
    assert f"MISSING: disk, no run recorded, first due by {label(nine.timestamp())}" in digest_at(
        chores, agent, nine.timestamp() + 301
    )


def test_a_pending_monthly_tick_gives_its_anchor_not_its_next_clamped_firing(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        # anchored Jan 31: its next firing is Feb 28, a month's last day, and the one after is Mar 31
        khive = FakeKhive(
            agenda=[
                ("tick: m", SELF, "pending", "2027-02-28T09:30:00Z", "2027-01-31T09:30:00Z", "monthly"),
                # a row with no anchor counts its months from its own firing, as the store does for one
                # (made a minute before it, as a tick is: the firing is the first inside its creation's month)
                (
                    "tick: legacy",
                    SELF,
                    "pending",
                    "2027-02-28T09:30:00Z",
                    None,
                    "monthly",
                    "2027-02-28T09:29:00Z",
                ),
            ]
        )
        chores, agent = build(
            tmp_path,
            khive,
            [],
            m=Probe("m", full(), every="monthly"),
            legacy=Probe("legacy", full(), every="monthly"),
        )
        asyncio.run(chores.ensure_ticks())
        jan31, feb28, mar31 = (
            datetime(2027, mo, d, 9, 30, tzinfo=UTC).timestamp() for mo, d in ((1, 31), (2, 28), (3, 31))
        )
        assert agent.state.get("first_due").value == {"m": jan31, "legacy": feb28}
        ledger(chores, ("m", feb28 + 5), ("legacy", feb28 + 5))  # both ran when their Feb firing came
        mar28 = mar31 - 3 * 86400
        text = digest_at(chores, agent, mar28 + 301)  # past the 28th and its grace
        assert f"{OWNER} m not due: last run {label(feb28 + 5)}, due by {label(mar31)}" in text.splitlines()
        assert f"MISSING: legacy, last run {label(feb28 + 5)}, due by {label(mar28)}" in text
    finally:
        monkeypatch.undo()
        time.tzset()


# a store that keeps no `repeat_anchor` moves `trigger_at` a calendar month on per delivery, in UTC
_MONTHS = {
    "three-months-on": ("2027-04-15T09:30:00+00:00", "2027-01-15T09:29:00.250000Z", "2027-01-15T09:30:00Z"),
    "feb-28-clamp": ("2027-03-31T09:30:00+00:00", "2027-02-28T09:29:00Z", "2027-02-28T09:30:00Z"),
    "leap-feb-29-clamp": ("2028-03-31T09:30:00+00:00", "2028-02-29T09:29:00Z", "2028-02-29T09:30:00Z"),
    "two-back-a-31-day-month": ("2027-03-31T09:30:00+00:00", "2027-01-31T09:29:00Z", "2027-01-31T09:30:00Z"),
    "across-the-year": ("2027-02-10T09:30:00+00:00", "2026-11-10T09:29:00Z", "2026-11-10T09:30:00Z"),
    # Feb 28 21:00 in UTC is Mar 1 in +05:00: the month steps back in UTC, to Jan 28 and not to Jan 31
    "utc-day": ("2027-03-01T02:00:00+05:00", "2027-01-28T20:59:00Z", "2027-01-28T21:00:00Z"),
    # made Sep 15 with its first firing a month on, in the store's offset spelling: a lead longer than the
    # default, which no row carries, so the latest firing not after the ceiling is a month early
    "first-firing-a-month-after-creation": (
        "2027-10-15T14:00:00-04:00",
        "2027-09-15T18:59:50.091369Z",
        "2027-09-15T18:00:00Z",
    ),
    "fired-before-creation-is-its-own": (
        "2027-01-15T09:30:00+00:00",
        "2027-03-01T00:00:00Z",
        "2027-01-15T09:30:00Z",
    ),
    "creation-month-is-its-own": (
        "2027-01-15T09:30:00+00:00",
        "2027-01-15T09:29:00Z",
        "2027-01-15T09:30:00Z",
    ),
    "no-creation-firing-ahead-of-now": ("2028-09-15T09:30:00+00:00", None, "2028-06-01T00:00:00Z"),
    "no-creation-firing-behind-now": ("2027-05-15T09:30:00+00:00", None, "2027-05-15T09:30:00Z"),
    "creation-no-month-counts": (
        "2028-09-15T09:30:00+00:00",
        "0001-01-01T00:00:00+05:00",
        "2028-06-01T00:00:00Z",
    ),
    # a firing on a short month's last day may be a clamped later day: the first is the latest day the clamp
    # admits, at the firing's time of day, not after the ceiling (the creation, the lead and a second)
    "clamped-into-a-leap-february": (
        "2028-02-29T09:31:00+00:00",
        "2028-01-30T09:30:00Z",
        "2028-01-30T09:31:00Z",
    ),
    "clamped-into-february": ("2027-02-28T09:31:00+00:00", "2027-01-30T09:30:00Z", "2027-01-30T09:31:00Z"),
    "clamped-into-a-30-day-month": (
        "2027-04-30T09:31:00+00:00",
        "2027-03-31T09:30:00Z",
        "2027-03-31T09:31:00Z",
    ),
    "two-months-back-lands-before-creation": (
        "2027-02-28T09:00:00+00:00",
        "2026-12-30T12:00:00Z",
        "2026-12-30T09:00:00Z",
    ),
    "clamped-later-in-the-day-than-its-ceiling": (
        "2027-02-28T21:00:00+00:00",
        "2027-01-30T09:30:00Z",
        "2027-01-29T21:00:00Z",
    ),
    # the step-back is after the ceiling: a clamped day leaves the ceiling as the bound, an unclamped one
    # is a month earlier
    "clamped-step-back-after-the-ceiling": (
        "2027-04-30T09:31:00+00:00",
        "2027-03-01T09:00:00Z",
        "2027-03-01T09:01:01Z",
    ),
    "step-back-after-the-ceiling": (
        "2027-02-15T09:31:00+00:00",
        "2027-01-15T09:00:00Z",
        "2026-12-15T09:31:00Z",
    ),
    # no clamp: the day is not its month's last, or no later day than the 31st exists, so the step-back is
    # the month's firing
    "day-29-created-after-its-day": (
        "2026-04-29T09:31:00+00:00",
        "2026-03-30T09:30:00Z",
        "2026-03-29T09:31:00Z",
    ),
    "thirty-first-a-month-back": (
        "2027-01-31T09:00:00+00:00",
        "2026-12-31T12:00:00Z",
        "2026-12-31T09:00:00Z",
    ),
}


@pytest.mark.parametrize(("trigger", "born", "first"), list(_MONTHS.values()), ids=list(_MONTHS))
def test_a_pending_monthly_tick_with_no_anchor_gives_the_month_its_history_admits(
    tmp_path, trigger, born, first
):
    now, want = (datetime.fromisoformat(x).timestamp() for x in ("2028-06-01T00:00:00Z", first))
    row = ("tick: m", SELF, "pending", trigger, None, "monthly", born)
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], m=Probe("m", full(), every="monthly"))
    agent.clock = lambda: now
    asyncio.run(chores.ensure_ticks())
    assert agent.state.get("first_due").value == {"m": want}
    text = digest_at(chores, agent, want + 301)  # a second past the grace: the first firing is the finding
    assert f"MISSING: m, no run recorded, first due by {label(want)}" in text.splitlines()


def test_a_clamped_monthly_row_reads_missing_a_month_and_the_grace_after_its_creation(
    tmp_path,
):
    # first fired Jan 30; Feb 29 is the leap year's clamp of 30
    born = datetime(2028, 1, 30, 9, 30, tzinfo=UTC)
    row = ("tick: m", SELF, "pending", "2028-02-29T09:31:00+00:00", None, "monthly", iso(born.timestamp()))
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], m=Probe("m", full(), every="monthly"))
    agent.clock = lambda: datetime(2028, 6, 1, tzinfo=UTC).timestamp()
    asyncio.run(chores.ensure_ticks())
    assert agent.state.get("first_due").value == {"m": born.timestamp() + 60}
    month_on = datetime(2028, 2, 29, 9, 30, tzinfo=UTC).timestamp() + 300  # a calendar month, and the grace
    line = f"MISSING: m, no run recorded, first due by {label(born.timestamp() + 60)}"
    assert line in digest_at(chores, agent, month_on).splitlines()


def test_a_clamped_monthly_row_is_missing_a_tick_lead_and_the_grace_after_its_creation(tmp_path):
    born = datetime(2028, 1, 30, 9, 30, tzinfo=UTC).timestamp()
    row = ("tick: m", SELF, "pending", "2028-02-29T09:31:00+00:00", None, "monthly", iso(born))
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], m=Probe("m", full(), every="monthly"))
    agent.clock = lambda: datetime(2028, 6, 1, tzinfo=UTC).timestamp()
    asyncio.run(chores.ensure_ticks())
    first = born + 60  # the latest day the clamp admits not after the ceiling: a minute after the creation
    inside = digest_at(chores, agent, first + 300 - 1).splitlines()  # the grace runs a second more
    assert f"{OWNER} m no run yet, first due by {label(first)}" in inside
    past = digest_at(chores, agent, first + 300 + 1).splitlines()
    assert f"MISSING: m, no run recorded, first due by {label(first)}" in past
    assert agent.state.get("first_due").value == {"m": first}


def test_the_stand_in_and_the_tick_a_start_makes_share_one_lead(tmp_path, monkeypatch):
    monkeypatch.setattr("hub.agent.chores._TICK_LEAD_S", 120)
    born = datetime(2027, 3, 30, 9, 30, tzinfo=UTC).timestamp()
    # Apr 30 may be a clamped Mar 31, and its step-back, Mar 30 09:33, is after the ceiling: that stands in
    row = ("tick: m", SELF, "pending", "2027-04-30T09:33:00+00:00", None, "monthly", iso(born))
    fixed = pending("f", born + 120, "every:45s", born)  # a period the lead is not a multiple of
    khive = FakeKhive(agenda=[row, fixed])
    chores, agent = build(
        tmp_path,
        khive,
        [],
        m=Probe("m", full(), every="monthly"),
        f=Probe("f", full(), every="every:45s"),
        disk=Probe("disk", full()),
    )
    agent.clock = lambda: datetime(2028, 6, 1, tzinfo=UTC).timestamp()
    asyncio.run(chores.ensure_ticks())  # no lead is passed: the constant gives it
    first = agent.state.get("first_due").value
    # the stand-in, and a fixed period's step-back, stop at the creation plus the lead and a second
    assert (first["m"], first["f"]) == (born + 121, born + 120)
    (remind,) = [op for op in khive.ops if op.startswith("schedule.remind") and 'content="tick: disk"' in op]
    made = datetime.fromisoformat(re.search(r'at="([^"]+)"', remind).group(1)).timestamp()
    assert 110 <= made - time.time() <= 125  # two minutes ahead, not the usual one


def test_a_first_due_on_record_is_never_overwritten_by_a_pending_tick(tmp_path):
    june, nine = datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    khive = FakeKhive(
        agenda=[
            ("tick: disk", SELF, "pending", "2026-09-14T09:00:00Z"),
            ("tick: pr", SELF, "pending", "2026-09-14T09:30:00Z"),  # two reminders of one tick:
            ("tick: pr", SELF, "pending", "2026-09-14T09:15:00Z"),  # the earlier firing is the first due
        ]
    )
    chores, agent = build(
        tmp_path, khive, [], disk=Probe("disk", full()), pr=Probe("pr", full(), every="every:15m")
    )
    agent.state.put("first_due", {"disk": june.timestamp()}, run="-", seq=0)
    assert asyncio.run(chores.ensure_ticks()) == ["tick: digest", "tick: aged"]
    first = agent.state.get("first_due").value
    assert first == {"disk": june.timestamp(), "pr": (nine + timedelta(minutes=15)).timestamp()}
    asyncio.run(chores.ensure_ticks())  # once: a second start with the same agenda changes nothing
    assert agent.state.get("first_due").value == first


def iso(t: float, micros: bool = False) -> str:
    """An instant as the store spells it: a firing to the second, a row's creation to the microsecond."""
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S" + (".%fZ" if micros else "Z"))


def pending(chore: str, firing: float, repeat: str, born: float | str | None = None) -> tuple:
    """A pending tick as `schedule.agenda` returns it: its firing and repeat inside `properties`, its
    creation beside them (an instant, or text that is none, or nothing)."""
    made = iso(born, micros=True) if isinstance(born, float | int) else born
    return (f"tick: {chore}", SELF, "pending", iso(firing), None, repeat, made)


class Clock:
    """`time` as `ensure_ticks` reads it: an instant that a store call moves on by its latency."""

    def __init__(self, t: float):
        self.t = t

    def time(self) -> float:
        return self.t

    def __getattr__(self, name: str):
        return getattr(time, name)


class Stamping(FakeKhive):
    """A store whose `schedule.remind` takes `lag` seconds and keeps the row it makes, stamped at its end."""

    def __init__(self, clock: Clock, lag: float):
        super().__init__()
        self.clock, self.lag = clock, lag

    async def exec(self, ops: str):
        if not ops.startswith("schedule.remind"):
            return await super().exec(ops)
        self.ops.append(ops)
        self.clock.t += self.lag
        content, at, repeat = re.search(r'content="([^"]*)", at="([^"]*)", repeat="([^"]*)"', ops).groups()
        self.agenda.append((content, self.actor, "pending", at, None, repeat, iso(self.clock.t, micros=True)))
        return [{"ok": True}]


def test_a_row_stamped_after_its_clock_read_and_delivered_once_recovers_its_true_first_firing(
    tmp_path, monkeypatch
):
    read = datetime(2026, 9, 14, 12, 0, 0, 250000, tzinfo=UTC).timestamp()
    clock = Clock(read)
    monkeypatch.setattr("hub.agent.chores.time", clock)
    khive = Stamping(clock, 2.0)  # the store stamps the row two seconds after the tick's clock read
    chores, _ = build(tmp_path, khive, [], disk=Probe("disk", full()))
    asyncio.run(chores.ensure_ticks())
    (row,) = [r for r in khive.agenda if r[0] == "tick: disk"]
    first = datetime.fromisoformat(row[3]).timestamp()
    assert first == int(read) + 60 and row[6] == iso(read + 2, micros=True)
    # delivered once, the row's trigger is a period on; an agent started with no state reads the row
    khive.agenda = [(*r[:3], iso(first + 1800), *r[4:]) if r is row else r for r in khive.agenda]
    (tmp_path / "again").mkdir()
    chores, agent = build(tmp_path / "again", khive, [], disk=Probe("disk", full()))
    assert asyncio.run(chores.ensure_ticks()) == []
    assert agent.state.get("first_due").value == {"disk": first}
    text = digest_at(chores, agent, first + 301)  # past its first firing and the grace: overdue
    assert f"MISSING: disk, no run recorded, first due by {label(first)}" in text.splitlines()


def test_each_tick_ensure_ticks_makes_is_timed_from_its_own_read_of_the_clock(tmp_path, monkeypatch):
    read = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    clock = Clock(read)
    monkeypatch.setattr("hub.agent.chores.time", clock)
    khive = Stamping(clock, 2.0)  # each call takes two seconds
    chores, _ = build(tmp_path, khive, [], disk=Probe("disk", full()), w=Probe("w", full(), every="weekly"))
    asyncio.run(chores.ensure_ticks())
    firings = [datetime.fromisoformat(r[3]).timestamp() for r in khive.agenda]
    assert [r[0] for r in khive.agenda] == ["tick: disk", "tick: w", "tick: digest", "tick: aged"]
    assert firings[1] - firings[0] == 2 and firings == [read + 60 + 2 * n for n in range(4)]
    # so each row's stamp trails the clock read that fixed its firing by its own call alone
    assert [
        datetime.fromisoformat(r[6]).timestamp() - (f - 60)
        for r, f in zip(khive.agenda, firings, strict=True)
    ] == [2] * 4


@pytest.mark.parametrize(
    ("repeat", "period", "delivered"),
    [
        ("every:30m", 1800, 0),  # not delivered yet: its trigger is its first firing, ahead of now
        ("every:30m", 1800, 1),
        ("every:30m", 1800, 5),
        ("every:90m", 5400, 2),
        ("daily", 86400, 3),
        ("weekly", 7 * 86400, 2),
    ],
)
def test_a_pending_repeating_tick_gives_the_firing_its_history_admits_not_the_next_one(
    tmp_path, repeat, period, delivered
):
    first = datetime(2026, 9, 14, 9, 0, tzinfo=UTC).timestamp()
    born = first - 60  # a tick is made a minute before its first firing
    # the store moves a delivered repeat's `trigger_at` on to its next firing and keeps the row pending
    row = pending("disk", first + delivered * period, repeat, born)
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], disk=Probe("disk", full(), every=repeat))
    agent.clock = lambda: first - 30 if not delivered else first + (delivered - 1) * period + 100
    asyncio.run(chores.ensure_ticks())  # an agent restarted after its tick was delivered, before a row
    text = digest_at(chores, agent, first + 301)  # a second past the first firing and the grace
    assert f"MISSING: disk, no run recorded, first due by {label(first)}" in text.splitlines()
    line = f"{OWNER} disk no run yet, first due by {label(first)}"
    assert line in digest_at(chores, agent, first + 300).splitlines()
    assert agent.state.get("first_due").value == {"disk": first}


@pytest.mark.parametrize(
    ("repeat", "born", "trigger", "first"),
    [
        ("every:30s", 0.0, 60, 60),  # the lead is two periods: the first firing is the trigger
        ("every:45s", 0.0, 60, 60),  # the lead is no multiple of the period
        (
            "every:30s",
            0.25,
            60,
            60,
        ),  # the store stamped the row a fraction of a second after the tick's clock
        ("every:30s", 0.0, 180, 60),  # delivered four times: the history still steps back to the first
        ("every:30s", 2.0, 90, 60),  # the store stamped the row two seconds after the tick's clock read
        ("every:1s", 0.0, 65, 61),  # the ceiling's second: a one-second period recovers a second late
        ("every:30s", 31.0, 90, 90),  # stamped a period after the read: a period late, the limit
        ("every:30m", 0.0, 7200, 0),  # made two hours out, a longer lead than the default: early
    ],
    ids=[
        "aligned-30s",
        "lead-not-a-multiple-45s",
        "stamped-a-fraction-late",
        "delivered-four-times",
        "stamped-two-seconds-late",
        "1s",
        "stamped-a-period-late",
        "a-longer-lead",
    ],
)
def test_a_pending_fixed_tick_gives_its_latest_firing_not_after_the_ceiling_its_creation_carries(
    tmp_path, repeat, born, trigger, first
):
    day = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    row = pending("disk", day + trigger, repeat, day + born)
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], disk=Probe("disk", full(), every=repeat))
    agent.clock = lambda: day + trigger
    asyncio.run(chores.ensure_ticks())
    want = day + first
    assert agent.state.get("first_due").value == {"disk": want}
    assert "MISSING: " not in digest_at(chores, agent, day + 30)  # inside the first minute
    line = f"{OWNER} disk no run yet, first due by {label(want)}"
    assert line in digest_at(chores, agent, want + 300).splitlines()  # the grace runs to its last second
    assert f"MISSING: disk, no run recorded, first due by {label(want)}" in digest_at(
        chores, agent, want + 301
    )


def test_a_row_created_a_period_ago_and_next_due_in_one_gives_the_firing_at_its_creation(tmp_path):
    now, period = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp(), 1800
    row = pending("disk", now + period, "every:30m", now - period)
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], disk=Probe("disk", full()))
    agent.clock = lambda: now
    asyncio.run(chores.ensure_ticks())
    first = now - period  # the latest firing not after the creation, the lead and a second
    assert agent.state.get("first_due").value == {"disk": first}
    line = f"{OWNER} disk no run yet, first due by {label(first)}"
    assert line in digest_at(chores, agent, first + 300).splitlines()
    assert f"MISSING: disk, no run recorded, first due by {label(first)}" in digest_at(
        chores, agent, first + 301
    )


def test_a_pending_tick_with_no_instant_to_step_back_to_gives_its_firing_or_now_if_earlier(tmp_path):
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    khive = FakeKhive(
        agenda=[
            pending("ahead", now + 1800, "every:30m"),  # no creation instant
            pending("behind", now - 600, "every:30m"),  # its firing is past: its own
            pending("stamp", now + 1800, "every:30m", "yesterday"),  # a creation that is no instant
            pending("cron", now + 1800, "0 9 * * 1", now - 86400),  # no fixed period to step by
            pending("zero", now + 1800, "every:0m", now - 60),  # a period that steps nowhere
        ]
    )
    probes = {
        n: Probe(n, full(), every=e)
        for n, e in (("ahead", "every:30m"), ("behind", "every:30m"), ("stamp", "every:30m"))
    }
    chores, agent = build(
        tmp_path,
        khive,
        [],
        cron=Probe("cron", full(), every="0 9 * * 1"),
        zero=Probe("zero", full(), every="every:0m"),
        **probes,
    )
    agent.clock = lambda: now
    asyncio.run(chores.ensure_ticks())
    first = agent.state.get("first_due").value
    assert first == {"ahead": now, "behind": now - 600, "stamp": now, "cron": now, "zero": now}
    text = digest_at(chores, agent, now + 301).splitlines()  # past the grace: the next firing is no wait
    assert f"MISSING: ahead, no run recorded, first due by {label(now)}" in text
    assert f"MISSING: stamp, no run recorded, first due by {label(now)}" in text


def test_a_pending_tick_whose_firing_precedes_its_creation_is_its_own_first_not_stepped_forward(tmp_path):
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    row = pending("disk", now - 7200, "every:30m", now - 60)
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], disk=Probe("disk", full()))
    agent.clock = lambda: now
    asyncio.run(chores.ensure_ticks())
    assert agent.state.get("first_due").value == {"disk": now - 7200}


@pytest.mark.parametrize(
    ("kept", "replaced"),
    [
        (datetime(2026, 9, 14, 9, 0, tzinfo=UTC).timestamp() + 5 * 1800, False),  # the row's own next firing
        (datetime(2026, 9, 14, 9, 0, tzinfo=UTC).timestamp() + 1, False),  # a second late
        (datetime(2026, 9, 14, 9, 0, tzinfo=UTC).timestamp(), False),
        (datetime(2026, 9, 14, 9, 0, tzinfo=UTC).timestamp() - 3600, False),  # earlier
        ("soon", True),  # a record that is no instant is no record
        (True, True),
        (float("nan"), True),
        (float("inf"), True),
        (-(10**20), True),  # a number no UTC date names is no record, however early it reads
        (-1e11, True),
        (10**20, True),
        (_END + 1, True),
    ],
    ids=[
        "a-period-run-late",
        "a-second-late",
        "equal",
        "earlier",
        "text",
        "bool",
        "nan",
        "inf",
        "below-year-1-huge",
        "below-year-1",
        "1e20",
        "year-10000",
    ],
)
def test_a_first_due_on_record_stands_over_the_pending_rows_and_one_that_is_no_instant_is_replaced(
    tmp_path, kept, replaced
):
    first = datetime(2026, 9, 14, 9, 0, tzinfo=UTC).timestamp()
    row = pending("disk", first + 5 * 1800, "every:30m", first - 60)  # delivered five times, still pending
    chores, agent = build(tmp_path, FakeKhive(agenda=[row]), [], disk=Probe("disk", full()))
    agent.clock = lambda: first + 4 * 1800 + 100
    agent.state.put("first_due", {"disk": kept, "other": 7.0}, run="-", seq=0)
    asyncio.run(chores.ensure_ticks())
    want = first if replaced else kept
    assert agent.state.get("first_due").value == {"disk": want, "other": 7.0}  # nothing else is touched
    line = f"MISSING: disk, no run recorded, first due by {label(want)}"
    assert line in digest_at(chores, agent, want + 301).splitlines()  # the finding is not hidden


@pytest.mark.parametrize("shift", [86400, -100], ids=["later", "earlier"])
def test_a_tick_made_now_leaves_a_first_due_on_record_where_it_is_later_or_earlier(tmp_path, shift):
    khive = FakeKhive()
    chores, agent = build(tmp_path, khive, [], pr=Probe("pr", full(), every="every:15m"))
    kept = time.time() + 60 + shift  # the tick is made for a minute from now
    agent.state.put("first_due", {"pr": kept}, run="-", seq=0)
    asyncio.run(chores.ensure_ticks(first_in_s=60))
    assert [op for op in khive.ops if op.startswith("schedule.remind") and 'content="tick: pr"' in op]
    assert agent.state.get("first_due").value == {"pr": kept}


def test_the_tick_check_reads_a_reminders_status_inside_properties_where_the_agenda_keeps_it(tmp_path):
    # a row the agenda holds under another status is not a pending tick: it is created again
    khive = FakeKhive(agenda=["tick: disk", ("tick: pr", SELF, "cancelled")])
    chores, _ = build(
        tmp_path, khive, [], disk=Probe("disk", full()), pr=Probe("pr", full(), every="every:15m")
    )
    assert asyncio.run(chores.ensure_ticks()) == ["tick: pr", "tick: digest", "tick: aged"]


def test_a_second_start_keeps_the_first_due_of_a_tick_made_with_a_longer_lead(
    tmp_path,
):
    khive = FakeKhive()
    khive.grows = True  # the store keeps the row made, created now: the lead is on no row
    chores, agent = build(tmp_path, khive, [], p=Probe("p", full(), every="every:15m"))
    asyncio.run(chores.ensure_ticks(first_in_s=7200))
    (remind,) = [op for op in khive.ops if op.startswith("schedule.remind") and 'content="tick: p"' in op]
    first = datetime.fromisoformat(re.search(r'at="([^"]+)"', remind).group(1)).timestamp()  # two hours out
    assert agent.state.get("first_due").value == {"p": first}
    agent.clock = lambda: first - 6000  # twenty minutes after the creation
    assert asyncio.run(chores.ensure_ticks()) == []  # the row is found and nothing is made
    # the row alone gives the latest firing not after its creation, the default lead and a second: two
    # hours early, at the creation itself
    assert agent.state.get("first_due").value == {"p": first}
    text = digest_at(chores, agent, first - 6000 + 60)  # past that firing and its grace
    assert f"{OWNER} p no run yet, first due by {label(first)}" in text.splitlines()
    assert "MISSING: " not in text


def agenda_rows(count: int, base: float, ticks: dict[int, tuple]) -> list[tuple]:
    """`count` pending reminders of another actor, one second apart from `base`, with this actor's ticks
    (index -> (chore, repeat)) in place of them, each made a minute before its firing."""
    rows = [(f"other: {i}", "lambda:other:agent", "pending", iso(base + i)) for i in range(count)]
    for i, (chore, repeat) in ticks.items():
        rows[i] = pending(chore, base + i, repeat, base + i - 60)
    return rows


def agenda_reads(khive: FakeKhive) -> list[str]:
    return [op for op in khive.ops if op.startswith("schedule.agenda")]


def test_the_agenda_is_read_in_pages_of_200_each_from_the_last_row_of_the_page_before(tmp_path):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    # the store answers 200 rows, then the 150 from the last of them, which it gives again
    khive = FakeKhive(agenda=agenda_rows(349, base, {199: ("a", "every:30m"), 348: ("disk", "every:30m")}))
    khive.paged = True
    chores, agent = build(tmp_path, khive, [], a=Probe("a", full()), disk=Probe("disk", full()))
    lines = digest_at(chores, agent, base).splitlines()  # one read, in two pages, for both chores
    assert f"{OWNER} a no run yet, first due by {label(base + 199)}" in lines
    assert f"{OWNER} disk no run yet, first due by {label(base + 348)}" in lines  # found on the second page
    assert agenda_reads(khive) == [
        "schedule.agenda(limit=200)",
        f'schedule.agenda(limit=200, from="{iso(base + 199)}")',
    ]
    assert asyncio.run(chores.ensure_ticks()) == ["tick: digest", "tick: aged"]  # no second tick: disk
    assert agent.state.get("first_due").value == {"a": base + 199, "disk": base + 348}
    # the row both pages carry is merged by its id: one firing
    found = asyncio.run(chores._pending_firsts(base))
    assert found == ({"tick: a": [base + 199], "tick: disk": [base + 348]}, None)


def test_an_agenda_read_that_ends_short_on_its_25th_page_is_not_capped(tmp_path):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    khive = FakeKhive(
        agenda=agenda_rows(4926, base, {4925: ("disk", "every:30m")})
    )  # 24 pages of 200, then 150
    khive.paged = True
    chores, agent = build(tmp_path, khive, [], disk=Probe("disk", full()))
    line = f"{OWNER} disk no run yet, first due by {label(base + 4925)}"
    assert line in digest_at(chores, agent, base).splitlines()
    assert len(agenda_reads(khive)) == 25


def test_a_read_ending_on_a_full_25th_page_is_capped_and_judges_and_makes_no_tick(
    tmp_path,
):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    # 5001 rows a second apart: 25 pages overlapping by their last row see 4976, the 25th full, and the
    # tick behind them is never reached
    khive = FakeKhive(agenda=agenda_rows(5001, base, {5000: ("disk", "every:30m")}))
    khive.paged = True
    chores, agent = build(
        tmp_path, khive, [], disk=Probe("disk", full()), w=Probe("w", full(), every="weekly")
    )
    text = digest_at(chores, agent, base)
    note = "not judged: the agenda read was capped at 5000 rows"
    assert text.splitlines()[1:] == [f"{OWNER} disk {note}", f"{OWNER} w {note}"] and "MISSING: " not in text
    assert len(agenda_reads(khive)) == 25
    khive.ops.clear()
    capped = "not created, the agenda read was capped at 5000 rows"
    assert asyncio.run(chores.ensure_ticks()) == [
        f"tick: {name}: {capped}" for name in ("disk", "w", "digest", "aged")
    ]
    assert len(agenda_reads(khive)) == 25 and not [op for op in khive.ops if op.startswith("schedule.remind")]
    assert agent.state.get("first_due") is None


def test_a_full_page_of_rows_at_one_instant_stalls_the_read_which_ends_there_and_judges_nothing(tmp_path):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    # `from` is inclusive: 300 rows at one instant answer the same 200 to the read from their instant, so
    # the second page brings no new row, the read ends there, two pages not 25, and the tick behind them
    # is out of its reach
    rows = [(f"other: {i}", "lambda:other:agent", "pending", iso(base)) for i in range(300)]
    khive = FakeKhive(agenda=[*rows, pending("disk", base + 600, "every:30m", base + 540)])
    khive.paged = True
    chores, agent = build(
        tmp_path, khive, [], disk=Probe("disk", full()), w=Probe("w", full(), every="weekly")
    )
    text = digest_at(chores, agent, base)
    stalled = f"the agenda read stalled at {iso(base)}: a full page of rows share that instant"
    assert text.splitlines()[1:] == [
        f"{OWNER} disk not judged: {stalled}",
        f"{OWNER} w not judged: {stalled}",
    ]
    assert "MISSING: " not in text
    assert agenda_reads(khive) == [
        "schedule.agenda(limit=200)",
        f'schedule.agenda(limit=200, from="{iso(base)}")',
    ]
    khive.ops.clear()
    assert asyncio.run(chores.ensure_ticks()) == [
        f"tick: {name}: not created, {stalled}" for name in ("disk", "w", "digest", "aged")
    ]
    assert len(agenda_reads(khive)) == 2 and not [op for op in khive.ops if op.startswith("schedule.remind")]
    assert agent.state.get("first_due") is None


def test_a_stalled_read_that_shows_one_of_a_ticks_rows_judges_records_and_makes_nothing(tmp_path):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    # the tick's row read first gives base; a second row of it, whose history admits base less an hour,
    # sits past 300 rows at the first row's instant, which the read from that instant answers again
    rows = [pending("disk", base, "every:30m", base - 60)]
    rows += [(f"other: {i}", "lambda:other:agent", "pending", iso(base)) for i in range(300)]
    khive = FakeKhive(agenda=[*rows, pending("disk", base + 3600, "every:30m", base - 3660)])
    khive.paged = True
    chores, agent = build(tmp_path, khive, [], disk=Probe("disk", full()))
    text = digest_at(chores, agent, base - 1800)  # overdue by the row out of reach, not by the one read
    stalled = f"the agenda read stalled at {iso(base)}: a full page of rows share that instant"
    assert text.splitlines()[1:] == [f"{OWNER} disk not judged: {stalled}"]
    khive.ops.clear()
    left = asyncio.run(chores.ensure_ticks())
    assert agent.state.get("first_due") is None  # the row read is no first due either
    assert left == [f"tick: {n}: not created, {stalled}" for n in ("disk", "digest", "aged")]
    assert len(agenda_reads(khive)) == 2 and not [op for op in khive.ops if op.startswith("schedule.remind")]


def test_a_pending_tick_row_naming_no_instant_is_not_judged_and_gives_no_first_due(tmp_path):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    khive = FakeKhive(
        agenda=[
            # a monthly row whose anchor is no instant: its firing is the next, not the first
            ("tick: m", SELF, "pending", "2027-02-28T09:30:00Z", "soon", "monthly"),
            pending("disk", base + 60, "every:30m", base),
            # two rows of one tick, one past its grace, one naming no instant: the unknown may be earlier
            pending("w", base - 7200, "weekly", base - 7260),
            ("tick: w", SELF, "pending", "soon"),
        ]
    )
    chores, agent = build(
        tmp_path,
        khive,
        [],
        disk=Probe("disk", full()),
        m=Probe("m", full(), every="monthly"),
        w=Probe("w", full(), every="weekly"),
    )
    lines = digest_at(chores, agent, base).splitlines()
    for chore in ("m", "w"):
        assert f"{OWNER} {chore} not judged: a pending tick row names no first due" in lines
    assert f"{OWNER} disk no run yet, first due by {label(base + 60)}" in lines
    assert "MISSING: " not in "\n".join(lines)
    assert asyncio.run(chores.ensure_ticks()) == ["tick: digest", "tick: aged"]  # the rows stand
    assert agent.state.get("first_due").value == {"disk": base + 60}  # and record no first due
    assert f"{OWNER} w not judged: a pending tick row names no first due" in digest_at(chores, agent, base)


def test_a_first_due_on_record_is_dropped_by_ensure_ticks_when_a_pending_tick_row_names_no_instant(
    tmp_path,
):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    khive = FakeKhive(
        agenda=[
            pending("disk", base + 60, "every:30m", base),
            # the tick on record was made again, by a row naming no instant: the record is stale
            ("tick: w", SELF, "pending", "soon"),
        ]
    )
    chores, agent = build(
        tmp_path, khive, [], disk=Probe("disk", full()), w=Probe("w", full(), every="weekly")
    )
    agent.state.put("first_due", {"disk": base + 60, "w": base - 7200}, run="-", seq=0)
    # the record stands until `ensure_ticks` reads the rows
    assert f"MISSING: w, no run recorded, first due by {label(base - 7200)}" in digest_at(chores, agent, base)
    for _ in range(2):  # and is not recorded again while the row stands
        assert asyncio.run(chores.ensure_ticks()) == ["tick: digest", "tick: aged"]
        assert agent.state.get("first_due").value == {"disk": base + 60}  # w's record dropped, disk's kept
    lines = digest_at(chores, agent, base).splitlines()
    assert f"{OWNER} w not judged: a pending tick row names no first due" in lines
    assert f"{OWNER} disk no run yet, first due by {label(base + 60)}" in lines
    assert "MISSING: " not in "\n".join(lines)


def test_a_short_read_seeing_a_tick_row_naming_no_instant_drops_the_first_due_on_record(
    tmp_path,
):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    # the row naming no instant sorts first and is read; 300 rows at one instant behind it stall the read
    rows = [("tick: w", SELF, "pending", "1 hour")]
    rows += [(f"other: {i}", "lambda:other:agent", "pending", iso(base)) for i in range(300)]
    khive = FakeKhive(agenda=rows)
    khive.paged = True
    chores, agent = build(tmp_path, khive, [], w=Probe("w", full(), every="weekly"))
    agent.state.put("first_due", {"w": base - 7200}, run="-", seq=0)
    stalled = f"the agenda read stalled at {iso(base)}: a full page of rows share that instant"
    left = asyncio.run(chores.ensure_ticks())
    # the read is short, so nothing is made and nothing recorded, but the row it did see may be the earliest
    # firing: the stale record goes, and the digest reads the rows, which cannot judge it either
    assert left == [f"tick: {n}: not created, {stalled}" for n in ("w", "digest", "aged")]
    assert agent.state.get("first_due").value == {}
    lines = digest_at(chores, agent, base).splitlines()
    assert lines[1:] == [f"{OWNER} w not judged: {stalled}"]


@pytest.mark.parametrize("answer", [{}, None, {"events": "x"}, []], ids=["empty", "none", "text", "list"])
def test_an_agenda_answer_that_is_no_page_is_a_failed_read_not_an_empty_one(tmp_path, answer):
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp()
    khive = FakeKhive()
    inner = khive.exec

    async def exec(ops):
        return [answer] if ops.startswith("schedule.agenda") else await inner(ops)

    khive.exec = exec
    chores, agent = build(tmp_path, khive, [], w=Probe("w", full(), every="weekly"))
    reason = f"agenda unreadable: ValueError: the agenda answer is no page: {str(answer)[:80]}"
    assert digest_at(chores, agent, base).splitlines()[1:] == [f"{OWNER} w not judged: {reason}"]
    with pytest.raises(ValueError, match="the agenda answer is no page"):
        asyncio.run(chores.ensure_ticks())
    assert not [op for op in khive.ops if op.startswith("schedule.remind")]  # no second tick made
    assert agent.state.get("first_due") is None


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("raises", "KhiveError: agenda: boom"), ("no-trigger", "ValueError: a full agenda page ends at a row")],
)
def test_a_digest_whose_agenda_read_fails_says_so_for_each_chore_that_needed_it_and_is_sent(
    tmp_path, kind, reason
):
    khive = FakeKhive()
    if kind == "raises":
        khive.agenda_error = KhiveError("agenda: boom")
    else:  # a full page whose last row names no instant to read the next page from
        khive.agenda = [f"row {i}" for i in range(200)]
    probes = {
        n: Probe(n, full(), every=e) for n, e in (("k", "every:30m"), ("w", "weekly"), ("x", "every:30m"))
    }
    chores, agent = build(tmp_path, khive, [], **probes)
    t = datetime(2026, 9, 14, 9, 0).timestamp()
    agent.state.put("first_due", {"k": t + 600}, run="-", seq=0)  # k needs no read
    text = digest_at(chores, agent, t)
    assert khive.sent[-1]["content"] == text and [m["to"] for m in khive.sent] == [OWNER]
    _, k, w, x = text.splitlines()
    assert k == f"{OWNER} k no run yet, first due by {label(t + 600)}"
    for line, name in ((w, "w"), (x, "x")):
        assert line.startswith(f"{OWNER} {name} not judged: agenda unreadable: {reason}")
    assert "MISSING: " not in text and len(agenda_reads(khive)) == 1  # read once, for both
    with pytest.raises(KhiveError if kind == "raises" else ValueError, match="boom|no trigger_at"):
        asyncio.run(chores.ensure_ticks())  # a tick cannot be made without knowing what is pending


def test_the_chores_profile_names_each_chore_with_its_direction_and_question_and_only_three_handlers(
    tmp_path,
):
    chores, agent = build(
        tmp_path, FakeKhive(), [], disk=Probe("disk", full()), ask=Probe("ask", full(), every=None)
    )
    chores_p, reader = chores.profiles()
    assert chores_p.specs == frozenset({"check", "digest", "aged"}) and reader.specs == frozenset()
    assert chores_p.name == "chores" and "You are the long-running agent of lambda:x" in chores_p.system
    assert (
        "  disk: probes disk [over-report] (every every:30m); answers: what the probe was pointed at"
        in chores_p.system
    )
    assert "  ask: probes ask [over-report] (on ask); answers: what the probe" in chores_p.system
    agent.actor.check(chores_p)
    assert (
        sender_of({"properties": {"from_actor": "lambda:owner"}}) == "lambda:owner" and sender_of({}) == "?"
    )


def test_a_chore_without_an_error_direction_or_a_question_is_refused_at_build(tmp_path):
    class Blank(Probe):
        direction = "sideways"

    class Mute(Probe):
        answers = "  "

    with pytest.raises(ValueError, match="every chore declares its error direction.*'disk': 'sideways'"):
        build(tmp_path, FakeKhive(), [], disk=Blank("disk", full()))
    with pytest.raises(
        ValueError, match=r"every chore says which question its instrument answers.*\['disk'\]"
    ):
        build(tmp_path, FakeKhive(), [], disk=Mute("disk", full()))


def test_the_bounded_client_refuses_every_verb_but_the_read_list_and_every_outside_recipient():
    inner = FakeKhive()
    k = Bounded(inner, recipients=frozenset({OWNER, STEWARD}))
    ops = '[gtd.tasks(status="inbox", assignee="x"), comm.probe(actor="lambda:x", stale_minutes=20)]'
    assert verbs(ops) == ["gtd.tasks", "comm.probe"]
    assert verbs('comm.read(id="x", note="run gtd.complete(id=1) now")') == [
        "comm.read"
    ]  # quoted, not a call
    assert asyncio.run(k.exec('[gtd.tasks(assignee="x"), list(kind="scheduled_event")]')) == [{"ok": True}]
    with pytest.raises(
        AgentError, match=r"agent boundary: \['gtd.complete'\] are not verbs the agent may call"
    ):
        asyncio.run(k.exec('gtd.complete(id="x", result="success")'))
    with pytest.raises(AgentError, match=r"agent boundary: \['schedule.cancel', 'update'\]"):
        asyncio.run(k.exec('[update(id="x", content="y"), schedule.cancel(id="z")]'))
    with pytest.raises(AgentError, match="agent boundary: email:owner@example.com is not a recipient"):
        asyncio.run(k.send("email:owner@example.com", "hi"))
    assert asyncio.run(k.send(OWNER, "hi", subject="s")) == {"id": "m1", "thread_id": "t1"}
    assert k.actor == SELF and asyncio.run(k.inbox()) == []  # the rest is forwarded
    assert inner.ops == [
        '[gtd.tasks(assignee="x"), list(kind="scheduled_event")]'
    ]  # refused ops never reached it


def test_the_bounded_client_fails_closed_on_a_call_it_cannot_name_and_on_mail_as_an_op():
    inner = FakeKhive()
    k = Bounded(inner, recipients=frozenset({OWNER}))
    assert verbs('x.gtd.complete(id="a")') == []  # a parse residue is not a permission
    with pytest.raises(
        AgentError, match="agent boundary: 1 call\\(s\\) in the op, 0 verb\\(s\\) the boundary"
    ):
        asyncio.run(k.exec('x.gtd.complete(id="a")'))
    with pytest.raises(AgentError, match="3 call\\(s\\) in the op, 1 verb"):  # every paren group counts
        asyncio.run(k.exec('[gtd.tasks(assignee="x"), (gtd.complete)(id="a")]'))
    with pytest.raises(AgentError, match=r"\['comm.send'\] are not verbs the agent may call"):
        asyncio.run(k.exec('comm.send(to="lambda:stranger", content="probe", subject="probe")'))
    assert inner.ops == [] and inner.sent == []


def test_bounded_refuses_json_and_option_dispatch_before_transport():
    """kkernel reads an op that begins `{` or `[{` as JSON (khive-request dispatch.rs:38-47), where the
    verb scan sees no call at all, and argv that begins `-` as an option: an op begins `verb(`."""
    inner = FakeKhive()
    k = Bounded(inner, recipients=frozenset({OWNER}))
    reached = []
    for ops in (
        '{"tool": "comm.send", "args": {"to": "email:someone@example.com", "content": "hi"}}',
        '[{"tool": "gtd.complete", "args": {"id": "00000000-0000-0000-0000-000000000000"}}]',
        ' [ {"tool": "memory.remember", "args": {"content": "x", "salience": 0.99}}]',
        '{"tool": "delete", "args": {"id": "x"}}',
        "--ops-file=gtd.tasks().json",
        "--ops-file=x_gtd.tasks().json",
        "-h gtd.tasks()",
    ):
        try:
            asyncio.run(k.exec(ops))
            reached.append(ops)
        except AgentError as e:
            assert str(e).startswith("agent boundary"), e
    assert reached == [] and inner.ops == [], f"reached kkernel: {reached}"


def test_bounded_admits_quoted_json_batches_and_chains():
    inner = FakeKhive()
    k = Bounded(inner, recipients=frozenset({OWNER}))
    ops = [
        'memory.remember(content="{\\"tool\\": \\"comm.send\\"}", salience=0.3)',
        'search(query="[{\\"tool\\": \\"delete\\"}] --ops-file=x")',
        '[gtd.tasks(assignee="x"), list(kind="scheduled_event")]',
        "  gtd.next(limit=1) | get(id=$prev[0].id)",
        "\n[ comm.inbox(limit=1) ]",
    ]
    for op in ops:
        assert asyncio.run(k.exec(op)) == [{"ok": True}]
    assert inner.ops == ops


def test_the_bounded_client_lets_the_agent_remember_at_leg_salience_only():
    inner = FakeKhive()
    k = Bounded(inner, recipients=frozenset({OWNER}))
    assert asyncio.run(k.exec('memory.remember(content="disk hovered at 299 all day", salience=0.4)')) == [
        {"ok": True}
    ]
    with pytest.raises(AgentError, match="memory.remember takes salience <= 0.4 .*got 0.9"):
        asyncio.run(k.exec('memory.remember(content="x", salience=0.9)'))
    with pytest.raises(AgentError, match="memory.remember takes salience <= 0.4 .*got none"):
        asyncio.run(k.exec('memory.remember(content="x (salience=0.1 in the text)")'))
    # the guard reads every `salience=`, not the first substring: a duplicate key, a name ending in
    # it and a single-quoted `)` must not reach the store at 0.9
    for op in (
        'memory.remember(content="x", salience=0.4, salience=0.9)',
        'memory.remember(content="x", my_salience=0.4, salience=0.9)',
        "memory.remember(content='salience=0.4)', salience=0.9)",
        'memory.remember(content="x", salience=abc)',
    ):
        with pytest.raises(AgentError, match="memory.remember takes salience"):
            asyncio.run(k.exec(op))
    assert asyncio.run(k.exec("memory.remember(content='a (b)', salience=0.3)")) == [{"ok": True}]
    assert len(inner.ops) == 2


def test_the_one_gtd_write_is_composed_by_code_and_the_model_may_not_compose_it_as_an_op():
    """Ruling 3: the desk files work in the chair's namespace through `Bounded.assign`, the op composed
    here from the fields code chose; `gtd.assign` as an op string stays refused before transport."""
    inner = FakeKhive()
    k = Bounded(inner, recipients=frozenset({OWNER}))
    with pytest.raises(AgentError, match=r"\['gtd.assign'\] are not verbs the agent may call"):
        asyncio.run(k.exec(f'gtd.assign(title="x", namespace="{OWNER}", idempotency_key="k0")'))
    assert inner.ops == []
    assign = getattr(k, "assign", None)
    assert callable(assign), "Bounded has no assign"
    row = asyncio.run(
        assign(title='say "hi"', namespace=OWNER, tags=["desk", "from:a"], idempotency_key="k1")
    )
    assert inner.ops == [
        'gtd.assign(title="say \\"hi\\"", namespace="lambda:x", tags=["desk", "from:a"], '
        'idempotency_key="k1")'
    ]
    assert row == {"id": "7a5c0001", "full_id": "7a5c0001-0000-4000-8000-000000000000", "replayed": False}
    assert "gtd.assign" not in k.allowed


def test_evidence_is_capped_in_the_message_and_the_row_and_says_so(tmp_path):
    khive = FakeKhive(mail(SELF, "tick: disk"))
    out = full(
        findings=("x",),
        evidence="y" * 5000,
        population="targets under the root",
        predicate="size > 20 GiB",
        count=3,
    )
    chores, agent = build(tmp_path, khive, [CHECK, DONE], disk=Probe("disk", out))
    chores.evidence_cap = 100
    asyncio.run(agent.wake())
    text = khive.sent[0]["content"]
    assert text.endswith("y" * 100 + "\n[evidence capped: 100 of 5000 chars]")
    assert "population: targets under the root (3)\npredicate: size > 20 GiB" in text
    row = json.loads((tmp_path / "ledger.jsonl").read_text())
    assert row["evidence"].endswith("[evidence capped: 100 of 5000 chars]")
    assert row["population"] == "targets under the root" and row["predicate"] == "size > 20 GiB"
    assert Check(chore="disk").args == {} and Digest().day is None
