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
        if ops.startswith("schedule.agenda"):  # a str is this actor's reminder; (content, actor) another's
            events = [
                {
                    "content": c if isinstance(c, str) else c[0],
                    "properties": {
                        "status": "pending",
                        "created_by_actor": self.actor if isinstance(c, str) else c[1],
                    },
                }
                for c in self.agenda
            ]
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
            raise KhiveError(f"get: {{'kind': 'not_found', 'message': 'not found: {wanted}'}}")
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
        lambda: (start + timedelta(seconds=next(ticks))).strftime("%Y-%m-%d %H:%M:%S"),
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
        lambda: (start + timedelta(seconds=next(ticks))).strftime("%Y-%m-%d %H:%M:%S"),
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
    assert "MISSING: gtd" in text and f"---\n{t0} disk msg m1: low" in text
    assert "a MISSING line is the finding" in text
    day = rows[0]["at"][:10]  # the third wake's tokens are not yet in the spend when the digest is built
    assert (
        f"tokens {day}: 4 model call(s), 4000 in, 200 out, $0.0040, over 3 wake(s) with 2 mail" in text
    )  # the third wake counts its mail after its run
    assert "0 idle poll(s) at 0 tokens" in text


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
def test_a_row_the_pages_skip_twice_is_kept_on_the_stores_word_and_runs_once(tmp_path, bare_pages):
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
    assert caught.count("ev200") == 1 and len(caught) == 201  # skipped twice, asked for by id, never re-run
    assert "ev200" in agent.state.get("missed_seen").value
    assert [o for o in khive.ops if o.startswith("get(id=")] == [
        'get(id="ev200")'
    ]  # asked once, on the second miss


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
