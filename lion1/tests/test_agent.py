"""The Agent: one wake per batch of mail, marked read before the model sees it, answered on the
thread under the privilege the profile holds, a cursor for the next wake, and the caps that keep a
long-running actor from answering forever or spending without bound."""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from lion_cli.agent import agent_main, stats

from hub.agent import Agent, AgentError, Budget, Wake, WakeRule
from hub.harness import claude_code
from hub.harness.claude_code import ClaudeCodeSession
from hub.khive import Khive
from lionagi import Actor, FileNoteStore, Kind, MemoryNoteStore, Message, Profile, Success

THREAD = "0a0a0a0a-0000-4000-8000-0a0a0a0a0a0a"
HOLD = (  # a process that takes the pid file's lock and keeps it until its stdin closes
    "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
    "fcntl.flock(fd, fcntl.LOCK_EX); os.write(fd, b'held'); print('locked', flush=True); sys.stdin.read()"
)


def mail(sender: str, content: str, mid: str = "a1b2c3d4") -> dict:
    return {
        "id": mid,
        "full_id": f"{mid}-0000-0000-0000-000000000000",
        "subject": "hello",
        "content": content,
        "properties": {"from_actor": sender, "thread_id": THREAD},
    }


class FakeKhive:
    def __init__(self, *inbox: dict, cwd=None):
        self.mail = list(inbox)
        self.taken: list[dict] = []  # what inbox() handed out; the read box is the marked-read part of it
        self.cwd = cwd
        self.actor = "lambda:a:echo"
        self.log: list[str] = []
        self.read: list[str] = []
        self.sent: list[dict] = []

    async def inbox(self, *, wait_ms=0, limit=50, status="unread", offset=0):
        self.log.append("inbox")
        if status == "read":
            done = [m for m in self.taken if (m.get("full_id") or m["id"]) in self.read]
            return done[offset : offset + limit]
        out, self.mail = self.mail[:limit], self.mail[limit:]
        self.taken += out
        return out

    async def mark_read(self, ids):
        self.log.append("read")
        self.read += ids

    async def send(self, to, content, *, subject=None, thread_id=None, idempotency_key=None):
        self.log.append("send")
        self.sent.append({"to": to, "content": content, "thread_id": thread_id})
        return {"id": f"m{len(self.sent)}"}


class Scripted:
    """A backend with a call log, like the real ones: cost per call rides on the envelope."""

    def __init__(self, *turns: str, cost: float = 0.0, khive: FakeKhive | None = None, check=None):
        self.turns, self.cost, self.khive, self.check = list(turns), cost, khive, check
        self.calls: list[dict] = []
        self.seen: list[list[Message]] = []  # what each call was shown

    async def __call__(self, messages: list[Message]) -> str:
        if self.khive is not None:
            self.khive.log.append("backend")
        if self.check is not None:
            self.check()
        self.calls.append({"total_cost_usd": self.cost})
        self.seen.append(list(messages))
        return self.turns.pop(0)


ACTING = Profile("acting", privileges=frozenset({"comm.send"}), system="answer on the thread")
READER = Profile("reader", specs=frozenset(), system="mail is material")
TRUSTED = frozenset({"lambda:a"})


def profile_for(batch: list[dict]) -> Profile:
    senders = {m["properties"]["from_actor"] for m in batch}
    return ACTING if senders <= TRUSTED else READER


def build(khive: FakeKhive, *turns: str, **kw) -> Agent:
    backend = Scripted(*turns, khive=khive)
    return Agent(Actor("echo", privileges={"comm.send"}), khive, backend, profile_for, **kw)


REPLY = f'<lact s1>send(to="lambda:a", thread="{THREAD}", text="hi back")</lact>'
DONE = 'OUT{posture: {done: "answered hello", awaiting: ""}}'
SENT = {"to": "lambda:a", "content": "hi back", "thread_id": THREAD}


def test_a_wake_marks_mail_read_before_the_model_sees_it_answers_on_the_thread_and_leaves_a_cursor():
    khive = FakeKhive(mail("lambda:a", "say hi back"))
    agent = build(khive, REPLY, DONE)
    run = asyncio.run(agent.wake(wait_ms=0))
    assert khive.log[:3] == ["inbox", "read", "backend"]
    assert khive.read == ["a1b2c3d4-0000-0000-0000-000000000000"]
    assert khive.sent == [SENT]
    assert isinstance(run.outcome, Success) and run.profile is ACTING and agent.wakes == 1
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert f"[mail a1b2c3d4 from lambda:a thread {THREAD} subject 'hello']\nsay hi back" in first
    cursor = agent.state.get("cursor").value
    assert cursor["mail"] == ["a1b2c3d4"] and cursor["sent"] == 1 and cursor["outcome"] == "Success"
    assert cursor["run"] == run.id and cursor["wake"] == 1 and cursor["profile"] == "acting"
    # a note's seq is the record's checkpoint (the next append), as Record.seq defines it, never
    # the last entry's own index
    assert agent.state.get("cursor").seq == run.record.seq == len(run.record) > 0
    assert agent.actor.notes_for(ACTING).get("posture").value == "answered hello"
    assert agent.state.get("threads").value == {THREAD: 1}


def test_a_continuous_chair_keeps_one_run_across_bursts_and_books_each_quiet_point_as_a_wake(tmp_path):
    # the owner, 2026-09-23: the chair never invokes a new run for new mail; it is the same conversation
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    later = mail("lambda:a", "one more", mid="c3d4e5f6")
    lines: list[str] = []
    # aliases are unique for the whole run, bursts included; a reply of commands that land ends the burst
    again = REPLY.replace("<lact s1>", "<lact s2>") + "\nanswered again"
    backend = Scripted(REPLY, again, khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}), khive, backend, profile_for, continuous=True, poll_ms=20
    )

    def until() -> bool:
        if len(lines) == 1 and later["full_id"] not in khive.read and later not in khive.mail:
            khive.mail.append(later)  # the world mails again once the first burst went quiet
        return len(lines) >= 2

    asyncio.run(agent.serve(until=until, log=lines.append))
    (run,) = agent.runs  # one run, two wakes
    assert agent.wakes == 2 and khive.sent == [SENT, SENT]
    assert lines[0].startswith("wake 1: Quiet after 1 rounds, 1 mail, 1 sent; 1 model call(s)")
    assert lines[1].startswith("wake 2: Quiet after 1 rounds, 1 mail, 1 sent; 1 model call(s)")
    inputs = [e.content for e in run.record if e.kind is Kind.INPUT]
    assert len(inputs) == 2 and "one more" in inputs[1] and "same conversation" in inputs[1]
    assert "'outcome': 'Quiet'" in inputs[1]  # the second burst is told how the first ended
    assert sorted(khive.read) == sorted(["a1b2c3d4-0000-0000-0000-000000000000", later["full_id"]])
    cursor = agent.state.get("cursor").value
    assert cursor["wake"] == 2 and cursor["mail"] == ["c3d4e5f6"] and cursor["run"] == run.id
    spend = agent.state.get("spend").value
    assert spend["runs"] == 2 and spend["mail"] == 2 and spend["calls"] == 2  # each burst booked once
    assert agent.actor.notes_for(ACTING).get("posture").value == "answered again"  # beside the command
    assert not (tmp_path / "agent.pid").exists()
    system = next(m.content for m in backend.seen[-1] if m.role == "system")
    assert "Finish by filling these with OUT{}" not in system  # no declared output, no OUTPUT section


def test_rerun_finds_read_mail_by_the_short_id_the_cursor_carries():
    """The box answers with full ids; the cursor and the owner's notice name the short one."""
    m = mail("lambda:a", "say hi back")
    m["id"] = m["full_id"]
    khive = FakeKhive(m)
    agent = build(khive, REPLY, DONE, REPLY, DONE)
    asyncio.run(agent.wake(wait_ms=0))
    run = asyncio.run(agent.rerun(["a1b2c3d4"]))
    assert isinstance(run.outcome, Success) and khive.sent == [SENT, SENT]
    with pytest.raises(AgentError, match="not in the read box"):
        asyncio.run(agent.rerun(["0ff1ce00"]))


def test_a_chair_woken_once_outside_serve_is_one_wake_that_returns():
    khive = FakeKhive(mail("lambda:a", "say hi back"))
    agent = build(khive, REPLY, DONE, continuous=True)
    run = asyncio.run(asyncio.wait_for(agent.wake(wait_ms=0), 2))
    assert isinstance(run.outcome, Success) and khive.sent == [SENT] and agent.wakes == 1


def test_a_failed_take_inside_the_conversation_is_a_logged_failure_the_run_outlives(tmp_path):
    class Flaky(FakeKhive):
        async def mark_read(self, ids):
            if len(self.read) == 1 and not getattr(self, "failed", False):
                self.failed = True
                self.mail.append(self.taken[-1])  # unread still: the box hands it out again
                raise RuntimeError("AdmissionDeadlineExpired")
            await super().mark_read(ids)

    khive = Flaky(mail("lambda:a", "say hi back"), cwd=tmp_path)
    later = mail("lambda:a", "one more", mid="c3d4e5f6")
    lines: list[str] = []
    again = REPLY.replace("<lact s1>", "<lact s2>") + "\ndone"
    backend = Scripted(REPLY, again, khive=khive)
    agent = build(khive, continuous=True, poll_ms=20, owner="lambda:owner")
    agent.backend = backend

    def until() -> bool:
        if len(lines) == 1 and later not in khive.taken:
            khive.mail.append(later)
        return len(lines) >= 3

    asyncio.run(agent.serve(until=until, log=lines.append))
    (run,) = agent.runs
    assert lines[1] == "wake 1 failed: RuntimeError: AdmissionDeadlineExpired"
    assert lines[2].startswith("wake 2: Quiet after 1 rounds, 1 mail, 1 sent; 1 model call(s)")
    spend = agent.state.get("spend").value
    assert spend["runs"] == 2 and spend["mail"] == 2 and spend["calls"] == 2  # nothing booked twice
    owed = [m for m in khive.sent if m["to"] == "lambda:owner"]
    assert len(owed) == 1 and "c3d4e5f6" in owed[0]["content"]  # the take that died is reported


def test_mail_for_another_profile_ends_the_conversation_and_runs_fresh(tmp_path):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    stranger = mail("lambda:stranger", "read this", mid="c3d4e5f6")
    lines: list[str] = []
    agent = build(khive, REPLY, DONE, continuous=True, poll_ms=20)

    def until() -> bool:
        if len(lines) == 1 and stranger not in khive.taken:
            khive.mail.append(stranger)
        return len(lines) >= 2

    asyncio.run(agent.serve(until=until, log=lines.append))
    first, second = agent.runs
    assert first.profile is ACTING and second.profile is READER and khive.sent == [SENT]
    assert "read this" not in "".join(str(e.content) for e in first.record)
    assert lines[1].startswith("wake 2: Success after 1 rounds, 1 mail, 0 sent")


def test_a_profile_changed_under_the_same_name_ends_the_conversation_and_runs_fresh(tmp_path):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    later = mail("lambda:a", "read this", mid="c3d4e5f6")
    fewer = Profile("acting", privileges=frozenset(), system="answer on the thread")
    lines: list[str] = []

    def profile_for(batch: list[dict]) -> Profile:
        return fewer if any(m["id"] == "c3d4e5f6" for m in batch) else ACTING

    backend = Scripted(REPLY, DONE, khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}), khive, backend, profile_for, continuous=True, poll_ms=20
    )

    def until() -> bool:
        if len(lines) == 1 and later not in khive.taken:
            khive.mail.append(later)
        return len(lines) >= 2

    asyncio.run(agent.serve(until=until, log=lines.append))
    first, second = agent.runs
    assert first.profile is ACTING and second.profile is fewer and khive.sent == [SENT]
    assert "read this" not in "".join(str(e.content) for e in first.record)


def test_two_sends_of_a_turn_wait_on_one_lock_under_every_loop_that_drives_the_agent():
    class Yielding(FakeKhive):
        async def send(self, *args, **kwargs):
            await asyncio.sleep(0)  # as the real transport does, so the second send waits on the lock
            return await super().send(*args, **kwargs)

    khive = Yielding()
    agent = build(khive)

    async def two() -> None:
        await asyncio.gather(*(agent.deliver("lambda:a", "hi", thread=None, run="r1") for _ in range(2)))

    asyncio.run(two())
    asyncio.run(two())
    assert len(khive.sent) == 4


def test_a_refused_command_does_not_end_the_batch(tmp_path):
    """A reply of commands ends the batch only when they all landed; a refusal earns one more turn."""
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    lines: list[str] = []
    slip = REPLY + "<lact x1>frobnicate()</lact>"  # the send lands, the second command is unknown
    agent = build(khive, slip, "sent once; the second was a slip", continuous=True, poll_ms=20)
    asyncio.run(agent.serve(until=lambda: len(lines) >= 1, log=lines.append))
    assert lines[0].startswith("wake 1: Quiet after 2 rounds, 1 mail, 1 sent; 2 model call(s)")
    assert agent.actor.notes_for(ACTING).get("posture").value == "sent once; the second was a slip"


def test_a_hold_inside_the_conversation_is_logged_once(tmp_path):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    lines: list[str] = []
    agent = build(khive, REPLY, "waiting", continuous=True, poll_ms=20, budget=Budget(wakes_per_hour=1))
    asyncio.run(agent.serve(until=lambda: len(lines) >= 2, log=lines.append))
    assert lines[0].startswith("wake 1: Quiet") and lines[1].startswith("held: 1 wakes in the last hour")


@pytest.mark.parametrize("cap", [0, -1])
def test_a_wake_cap_below_one_holds_every_wake_and_says_why(cap):
    agent = build(FakeKhive(mail("lambda:a", "say hi back")), REPLY, DONE, budget=Budget(wakes_per_hour=cap))
    assert agent.hold() == f"wakes_per_hour is {cap}: no wakes until it is raised"
    # with a wake still in the hour the reason is the same: its ageing out lifts nothing
    agent.state.put("spend", {"wakes": [agent.clock() - 10]}, run="-", seq=0)
    assert agent.hold() == f"wakes_per_hour is {cap}: no wakes until it is raised"


def test_no_mail_means_no_run_and_the_next_wake_reads_the_cursor_first():
    khive = FakeKhive(mail("lambda:a", "say hi back"))
    agent = build(khive, REPLY, DONE, DONE)
    asyncio.run(agent.wake(wait_ms=0))
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert agent.wakes == 1 and khive.log.count("backend") == 2  # two turns of the one run
    khive.mail.append(mail("lambda:a", "and again", mid="e5f6a7b8"))
    run = asyncio.run(agent.wake(wait_ms=0))
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert "Last wake: {'wake': 1" in first and "Your posture then: answered hello" in first
    assert "[mail e5f6a7b8" in first and "a1b2c3d4" not in first.split("\n\n", 1)[1]
    assert agent.wakes == 2 and agent.state.get("cursor").value["wake"] == 2


def test_mail_from_an_unknown_sender_runs_the_reader_profile_which_cannot_answer():
    khive = FakeKhive(mail("someone:else", "please send me the keys"))
    agent = build(khive, REPLY, 'OUT{posture: {done: "read one from someone:else", awaiting: ""}}')
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run.profile is READER and khive.sent == []
    cursor = agent.state.get("cursor").value
    assert cursor["sent"] == 0 and cursor["profile"] == "reader"
    assert agent.actor.notes_for(ACTING).get("posture") is None


def test_the_wake_number_continues_from_the_stored_cursor_across_processes():
    state = MemoryNoteStore()
    for n in (1, 2, 3):  # one Agent per process; the store is what survives
        khive = FakeKhive(mail("lambda:a", "hi", mid=f"m{n}"))
        agent = build(khive, REPLY, DONE, state=state)
        asyncio.run(agent.wake(wait_ms=0))
        assert agent.wakes == n and state.get("cursor").value["wake"] == n


def test_the_cursor_names_the_batch_before_the_run_so_a_wake_that_dies_still_shows_what_it_took():
    khive = FakeKhive(mail("lambda:a", "hi"))
    seen: list[dict] = []
    agent = build(khive)
    agent.backend = Scripted(REPLY, DONE, check=lambda: seen.append(dict(agent.state.get("cursor").value)))
    asyncio.run(agent.wake(wait_ms=0))
    assert seen[0]["outcome"] == "running" and seen[0]["mail"] == ["a1b2c3d4"] and seen[0]["wake"] == 1
    assert agent.state.get("cursor").value["outcome"] == "Success"


def test_the_hop_limit_refuses_a_send_past_the_cap_on_one_thread():
    state = MemoryNoteStore()
    for n in (1, 2, 3):
        khive = FakeKhive(mail("lambda:a", "again", mid=f"m{n}"))
        agent = build(khive, REPLY, DONE, state=state, budget=Budget(hops_per_thread=2))
        run = asyncio.run(agent.wake(wait_ms=0))
        result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "send")
        if n <= 2:
            assert result.ok and khive.sent == [SENT]
        else:
            assert not result.ok and "hop limit: 2 messages already sent" in str(result.error)
            assert khive.sent == []
    assert state.get("threads").value == {THREAD: 2}


def test_the_wake_cap_holds_the_agent_for_the_rest_of_the_hour_and_mail_waits():
    now = [1_000_000.0]
    khive = FakeKhive(mail("lambda:a", "one"))
    agent = build(khive, REPLY, DONE, REPLY, DONE, budget=Budget(wakes_per_hour=1), clock=lambda: now[0])
    assert asyncio.run(agent.wake(wait_ms=0)) is not None
    khive.mail.append(mail("lambda:a", "two", mid="m2"))  # arrives after; a batch takes all that waits
    polls = khive.log.count("inbox")
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert agent.held.startswith("1 wakes in the last hour (cap 1)") and khive.log.count("inbox") == polls
    now[0] += 3601
    assert asyncio.run(agent.wake(wait_ms=0)) is not None and agent.held is None and agent.wakes == 2


def test_the_cost_cap_holds_the_agent_for_the_day_from_the_backends_own_cost_figures():
    now = [1_000_000.0]
    khive = FakeKhive()
    backend = Scripted(*([REPLY, DONE] * 4), cost=0.3, khive=khive)
    actor = Actor("echo", privileges={"comm.send"})
    agent = Agent(actor, khive, backend, profile_for, budget=Budget(cost_per_day=1.0), clock=lambda: now[0])
    for n in range(2):  # two wakes, two calls each: 1.2 spent
        khive.mail.append(mail("lambda:a", "go", mid=f"m{n}"))
        assert asyncio.run(agent.wake(wait_ms=0)) is not None
    assert agent.state.get("spend").value["cost"] == pytest.approx(1.2)
    khive.mail.append(mail("lambda:a", "go", mid="m2"))
    assert asyncio.run(agent.wake(wait_ms=0)) is None and agent.held.startswith("$1.2000 spent today")
    now[0] += 86_400  # the next day; the mail is still waiting
    assert asyncio.run(agent.wake(wait_ms=0)) is not None
    assert agent.state.get("spend").value["cost"] == pytest.approx(0.6)


def test_a_call_of_unknown_or_incomplete_cost_adds_nothing_to_the_cost_and_the_readers_mark_the_sum(tmp_path):
    from hub.agent.agent import _line

    now = [1_000_000.0]
    khive = FakeKhive()
    envelopes = [
        {"total_cost_usd": 0.3},
        {"total_cost_usd": None},  # no figure: a subscription, or a CLI call that died before its result
        {"total_cost_usd": 0.4, "cost_complete": False},  # the router's known subtotal of its attempts
    ]

    class Billed(Scripted):
        async def __call__(self, messages):
            text = await super().__call__(messages)
            self.calls[-1] = envelopes.pop(0)
            return text

    state = FileNoteStore(tmp_path / "notes" / "agent.json")
    backend = Billed(DONE, DONE, DONE, khive=khive)
    actor = Actor("echo", privileges={"comm.send"})
    cap = Budget(cost_per_day=0.65)
    agent = Agent(actor, khive, backend, profile_for, state=state, budget=cap, clock=lambda: now[0])
    rows, lines = [], []
    for n in range(3):
        khive.mail.append(mail("lambda:a", "go", mid=f"m{n}"))
        assert asyncio.run(agent.wake(wait_ms=0)) is not None
        rows.append({k: agent.state.get("spend").value[k] for k in ("calls", "cost", "unknown")})
        lines.append(_line(agent.state.get("cursor").value))
    assert rows == [
        {"calls": 1, "cost": 0.3, "unknown": 0},
        {"calls": 2, "cost": 0.3, "unknown": 1},  # the call counts; the cost is as it was
        {"calls": 3, "cost": pytest.approx(0.7), "unknown": 2},
    ]
    assert "$0.3000; 0 idle poll(s)" in lines[0] and "partial" not in lines[0]
    assert "$0.0000 (partial: 1 call(s) of unknown cost); 0 idle poll(s)" in lines[1]
    assert "$0.4000 (partial: 1 call(s) of unknown cost); 0 idle poll(s)" in lines[2]
    khive.mail.append(mail("lambda:a", "go", mid="m3"))
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert agent.held == "$0.7000 spent today (partial: 2 call(s) of unknown cost) (cap $0.65); next day"
    day, _, last = stats(tmp_path).splitlines()
    assert "0 out, $0.7000 (partial: 2 call(s) of unknown cost); 3 wake(s) with 3 mail;" in day
    assert "0 out, $0.4000 (partial: 1 call(s) of unknown cost); 0 idle poll(s) before it" in last


def test_stats_prints_the_spend_row_the_last_wake_and_the_ledgers_as_this_text(tmp_path):
    notes_file = tmp_path / "notes" / "agent.json"
    assert stats(tmp_path) == f"no notes at {notes_file}: the agent has not run from this directory"
    spend = {
        "day": "2026-09-22",
        "cost": 0.0123,
        "unknown": 0,
        "calls": 5,
        "in": 12000,
        "out": 800,
        "mail": 4,
        "runs": 3,
        "idle": 17,
        "idle_last": 2,
        "wakes": [],
    }
    cursor = {
        "wake": 3,
        "at": "2026-09-22 11:09:15",
        "outcome": "Success",
        "rounds": 2,
        "mail": ["a1b2c3d4", "b2c3d4e5"],
        "sent": 1,
        "tokens": {"calls": 2, "in": 5000, "out": 300, "cost": 0.004, "unknown": 0, "context": 2400},
        "idle_polls": 2,
    }
    values = {"spend": spend, "cursor": cursor}
    notes = {"version": 2, "notes": {k: {"key": k, "value": v} for k, v in values.items()}}
    notes_file.parent.mkdir()
    notes_file.write_text(json.dumps(notes))
    (tmp_path / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in ({"answered": True}, {"control": True}, {"control": False})) + "\n"
    )
    desk = [
        {"at": "2026-09-22 09:00:00", "class": "answered"},
        {"at": "2026-09-22 10:00:00", "class": "routed"},
        {"at": "2026-09-21 23:00:00", "class": "answered"},  # another day's row is not today's
    ]
    (tmp_path / "desk.jsonl").write_text("\n".join(json.dumps(r) for r in desk) + "\n")
    assert stats(tmp_path) == (
        "day 2026-09-22: 5 model call(s), 12000 in, 800 out, $0.0123; 3 wake(s) with 4 mail; "
        "17 idle poll(s) at 0 tokens\n"
        "per handled message: 3200 tokens\n"
        "last wake 3 at 2026-09-22 11:09:15: Success after 2 round(s), 2 mail, 1 sent; 2 call(s), 5000 in, "
        "300 out, $0.0040; 2 idle poll(s) before it\n"
        "ledger: 3 check(s), 2 answered, 1 not\n"
        "desk: 2 of the owner's message(s) read today, 1 answered, 1 left"
    )
    notes["notes"]["spend"]["value"] = {**spend, "mail": 0}
    notes_file.write_text(json.dumps(notes))
    assert stats(tmp_path).splitlines()[1] == "per handled message: no mail handled today"


def test_a_failed_session_cli_call_is_counted_in_the_days_spend_row_and_still_ends_the_run(monkeypatch):
    printed = {"total_cost_usd": 0.5, "usage": {"input_tokens": 9000, "output_tokens": 700}}

    async def capped(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        raise claude_code.ClaudeCodeError("claude reported error_max_turns: cap", printed)

    monkeypatch.setattr(claude_code, "_exec", capped)
    khive = FakeKhive(mail("lambda:a", "go"))
    agent = Agent(
        Actor("echo", privileges={"comm.send"}), khive, ClaudeCodeSession(cli="/bin/claude"), profile_for
    )
    with pytest.raises(claude_code.ClaudeCodeError, match="error_max_turns"):
        asyncio.run(agent.wake(wait_ms=0))
    spend = agent.state.get("spend").value
    assert spend["calls"] == 1 and spend["cost"] == pytest.approx(0.5)
    assert spend["in"] == 9000 and spend["out"] == 700
    assert agent.state.get("cursor").value["outcome"].startswith("error ClaudeCodeError: ")


def test_a_wake_killed_after_its_mark_read_is_reported_to_the_owner_and_rerun_takes_it_back():
    state = MemoryNoteStore()
    khive = FakeKhive(mail("lambda:a", "hi"))

    class Died(Scripted):
        async def __call__(self, messages):
            raise SystemExit(1)  # the process died mid-run: nothing after the mark-read ran

    actor = Actor("echo", privileges={"comm.send"})
    agent = Agent(actor, khive, Died(khive=khive), profile_for, state=state, owner="lambda:a")
    with pytest.raises(SystemExit):
        asyncio.run(agent.wake(wait_ms=0))
    assert state.get("cursor").value["outcome"] == "running" and khive.sent == []
    khive.mail.append(mail("lambda:a", "again", mid="m2"))
    agent = build(khive, REPLY, DONE, state=state, owner="lambda:a")  # the next process
    run = asyncio.run(agent.wake(wait_ms=0))
    notice = khive.sent[0]
    assert notice["to"] == "lambda:a" and "['a1b2c3d4']" in notice["content"] and "rerun" in notice["content"]
    assert khive.sent[1] == SENT and isinstance(
        run.outcome, Success
    )  # told first, then the new batch answered
    assert state.get("cursor").value["outcome"] == "Success" and agent.wakes == 2
    agent.backend = Scripted(REPLY, DONE, khive=khive)
    run = asyncio.run(agent.rerun(["a1b2c3d4-0000-0000-0000-000000000000"]))
    assert isinstance(run.outcome, Success) and khive.sent[2] == SENT and agent.wakes == 3
    assert khive.log.count("read") == 2  # rerun never touches the unread view
    with pytest.raises(AgentError, match="not in the read box"):
        asyncio.run(agent.rerun(["nope"]))


def test_a_wake_whose_run_raises_tells_the_owner_which_batch_went_unanswered():
    khive = FakeKhive(mail("lambda:a", "hi"))

    class Boom(Scripted):
        async def __call__(self, messages):
            raise RuntimeError("boom")

    actor = Actor("echo", privileges={"comm.send"})
    agent = Agent(actor, khive, Boom(khive=khive), profile_for, owner="lambda:a")
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(agent.wake(wait_ms=0))
    assert agent.state.get("cursor").value["outcome"] == "error RuntimeError: boom"
    assert khive.sent[0]["to"] == "lambda:a" and "['a1b2c3d4']" in khive.sent[0]["content"]


def test_serve_refuses_a_held_identity_reports_a_died_wake_first_and_logs_one_line_per_wake(tmp_path):
    khive = FakeKhive(cwd=tmp_path)
    holder = subprocess.Popen(  # another live process holds the identity: it holds the lock
        [sys.executable, "-c", HOLD, str(tmp_path / "agent.pid")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AgentError, match="held by live process"):
            asyncio.run(build(khive, state=MemoryNoteStore()).serve(until=lambda: True))
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)
        holder.stdout.close()
    (tmp_path / "agent.pid").write_text("999999999")  # a stale file nobody holds does not
    khive.mail.append(mail("lambda:a", "hi"))
    state = MemoryNoteStore()
    state.put(
        "cursor", {"wake": 4, "outcome": "running", "mail": ["deadbeef"]}, run="-", seq=0
    )  # a died wake
    lines: list[str] = []
    agent = build(khive, REPLY, DONE, state=state, owner="lambda:a")
    asyncio.run(agent.serve(until=lambda: bool(lines), log=lines.append))
    assert (
        "['deadbeef']" in khive.sent[0]["content"] and khive.sent[1] == SENT
    )  # told at start, then the batch
    assert (
        len(lines) == 1
        and lines[0].startswith("wake 5: Success after")
        and lines[0].endswith(
            "1 mail, 1 sent; 2 model call(s), 0 in, 0 out, $0.0000; 0 idle poll(s) before it at 0 tokens"
        )
    )
    assert not (tmp_path / "agent.pid").exists()


def test_two_sends_in_one_turn_share_the_thread_cap():
    class Yielding(FakeKhive):
        async def send(self, *args, **kwargs):
            await asyncio.sleep(0.01)  # the real transport awaits a subprocess
            return await super().send(*args, **kwargs)

    khive = Yielding(mail("lambda:a", "reply twice"))
    state = MemoryNoteStore()
    twice = REPLY + "\n" + REPLY.replace("s1>", "s2>")
    agent = build(khive, twice, DONE, state=state, budget=Budget(hops_per_thread=1))
    run = asyncio.run(agent.wake(wait_ms=0))
    results = [e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "send"]
    assert len(khive.sent) == 1 and state.get("threads").value == {THREAD: 1}
    assert sorted(r.ok for r in results) == [False, True]
    assert "hop limit: 1" in str(next(r.error for r in results if not r.ok))


def test_a_wake_that_dies_after_taking_its_mail_is_reported_on_the_next_start():
    class ReadThenDie(FakeKhive):
        async def mark_read(self, ids):
            await super().mark_read(ids)
            raise RuntimeError("audit failed after the mark-read committed")

    khive = ReadThenDie(mail("lambda:a", "do this once", mid="deadbeef"))
    state = MemoryNoteStore()
    agent = build(khive, DONE, state=state, owner="lambda:a")
    with pytest.raises(RuntimeError):
        asyncio.run(agent.wake(wait_ms=0))
    assert khive.read and state.get("cursor") is None and khive.sent == []
    again = build(khive, DONE, state=state, owner="lambda:a")
    asyncio.run(again._recover())  # what serve does first
    (notice,) = khive.sent
    assert notice["to"] == "lambda:a" and "deadbeef" in notice["content"] and "rerun" in notice["content"]
    asyncio.run(again._recover())
    assert len(khive.sent) == 1  # told once


def test_the_identity_is_a_lock_the_process_keeps_not_a_pid_it_wrote(tmp_path):
    khive = FakeKhive(cwd=tmp_path)
    first = build(khive, state=MemoryNoteStore())
    path = first._claim()
    assert path.read_text() == str(os.getpid())
    with pytest.raises(AgentError, match="held by live process"):  # a second claim while the first holds
        build(khive, state=MemoryNoteStore())._claim()
    first._release(path)
    assert not path.exists()
    path.write_text("999999999")  # a stale file, nobody holding it
    second = build(khive, state=MemoryNoteStore())
    assert second._claim() == path and path.read_text() == str(os.getpid())
    second._release(path)


def test_a_claim_that_locked_a_file_its_holder_just_removed_takes_the_live_one_and_is_refused(
    tmp_path, monkeypatch
):
    import fcntl

    khive = FakeKhive(cwd=tmp_path)
    first, second, late = (build(khive, state=MemoryNoteStore()) for _ in range(3))
    path = first._claim()
    real, raced = fcntl.flock, []

    def flock(fd, op):
        if not raced:  # `late` opened the old file; its holder lets go and `second` claims a new one
            raced.append(fd)
            first._release(path)
            second._claim()
        return real(fd, op)

    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(AgentError, match="held by live process"):
        late._claim()
    assert late._lock_fd is None and raced and path.read_text() == str(os.getpid())
    second._release(path)


def test_a_recovery_notice_the_transport_dropped_is_told_at_the_next_start():
    class FirstSendFails(FakeKhive):
        attempts = 0

        async def send(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transport down")
            return await super().send(*args, **kwargs)

    khive, state = FirstSendFails(), MemoryNoteStore()
    state.put("taking", {"mail": ["deadbeef"]}, run="-", seq=0)
    first = build(khive, state=state, owner="lambda:a")
    asyncio.run(first.serve(until=lambda: True))
    assert khive.sent == [] and first.errors == ["owner notice failed: RuntimeError: transport down"]
    assert state.get("taking").value == {} and state.get("unreported").value[0]["mail"] == ["deadbeef"]
    asyncio.run(build(khive, state=state, owner="lambda:a").serve(until=lambda: True))  # a restart
    assert len(khive.sent) == 1 and "['deadbeef']" in khive.sent[0]["content"]
    assert state.get("unreported").value == []


def test_rerun_is_a_wake_the_caps_hold_and_it_needs_ids():
    khive = FakeKhive(mail("lambda:a", "hi"))
    agent = build(khive, REPLY, DONE, REPLY, DONE, budget=Budget(wakes_per_hour=1))
    asyncio.run(agent.wake(wait_ms=0))
    assert agent.hold() is not None
    with pytest.raises(AgentError, match="held: "):
        asyncio.run(agent.rerun(["a1b2c3d4"]))
    with pytest.raises(AgentError, match="none were given"):
        asyncio.run(agent.rerun([]))
    assert khive.log.count("backend") == 2  # the one wake's two turns; nothing ran again


class FailingSends(FakeKhive):
    """A transport that drops the first `fail` sends."""

    def __init__(self, *inbox: dict, fail: int = 1):
        super().__init__(*inbox)
        self.fail = fail

    async def send(self, *args, **kwargs):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("transport down")
        return await super().send(*args, **kwargs)


class Boom(Scripted):
    async def __call__(self, messages: list[Message]) -> str:
        raise RuntimeError("boom")


def test_raise_then_failed_tell_survives_restart():
    """A wake whose run raises owes its batch to the owner (ADR-0013/C3); the telling was dropped, so
    the next start tells it, with the error and the ids."""
    khive, state = FailingSends(mail("lambda:a", "hi"), fail=1), MemoryNoteStore()
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        Boom(),
        profile_for,
        state=state,
        owner="lambda:a",
    )
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(agent.wake(wait_ms=0))
    assert khive.sent == [] and agent.errors == ["owner notice failed: RuntimeError: transport down"]
    asyncio.run(build(khive, state=state, owner="lambda:a").serve(until=lambda: True))  # a restart
    told = [s["content"] for s in khive.sent]
    assert [("a1b2c3d4" in t, "boom" in t, "rerun" in t) for t in told] == [(True, True, True)]
    assert state.get("unreported").value == []


def test_new_failure_preserves_older_owed_batch():
    """A batch already owed stays owed while a new failure is recorded beside it; both are told once the
    transport comes back."""
    khive, state = FailingSends(mail("lambda:a", "hi", mid="bbbbbbbb"), fail=2), MemoryNoteStore()
    state.put("unreported", [{"why": "an earlier wake died mid-run", "mail": ["aaaaaaaa"]}], run="-", seq=0)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        Boom(),
        profile_for,
        state=state,
        owner="lambda:a",
    )
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(agent.wake(wait_ms=0))  # the recovery's telling and the failed wake's telling both drop
    asyncio.run(build(khive, state=state, owner="lambda:a").serve(until=lambda: True))
    told = " ".join(s["content"] for s in khive.sent)
    assert ("aaaaaaaa" in told, "bbbbbbbb" in told) == (True, True)
    assert state.get("unreported").value == [] and len(khive.sent) == 1


def test_the_rerun_flag_takes_read_mail_through_one_wake_by_id(tmp_path, monkeypatch):
    """--rerun ID...: the batch a killed round left read and unanswered, back through Agent.rerun."""
    from types import SimpleNamespace

    seen: dict = {}

    class Held:
        khive = SimpleNamespace(actor="lambda:x")

        def _claim(self):
            return None

        def _release(self, pid):
            seen["released"] = True

        async def rerun(self, ids):
            seen["ids"] = ids
            return SimpleNamespace(record=[])

    (tmp_path / "chores.toml").write_text('model = "deepseek/deepseek-v4.1-flash"\n')
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: (None, Held()))
    a = argparse.Namespace(
        dir=str(tmp_path), stats=False, model=None, check=None, ticks=False, arm=False, once=False, wait=0
    )
    asyncio.run(agent_main(argparse.Namespace(**{**vars(a), "rerun": ["12345678", "0ff1ce00"]})))
    assert seen == {"ids": ["12345678", "0ff1ce00"], "released": True}


def test_once_refuses_served_identity(tmp_path, kkernel_stub, monkeypatch):
    """While a live process holds the identity, --check, --ticks, --arm, --once and a normal start each
    refuse before any act: no kkernel call, no landing, nothing written. --stats reads and stays
    available."""
    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:agent"\n')
    (d / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n\n[chores.gtd-inbox]\n'
        'every = "every:1h"\n'
    )
    ops: list[str] = []

    async def fake_exec(self, op):
        ops.append(op)
        return [{"messages": [], "events": [], "items": []}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    base = dict(
        dir=str(d),
        stats=False,
        model=None,
        check=None,
        ticks=False,
        arm=False,
        once=False,
        rerun=None,
        wait=0,
    )
    branches = {
        "check": {"check": "gtd-inbox"},
        "ticks": {"ticks": True},
        "arm": {"arm": True},
        "once": {"once": True},
        "rerun": {"rerun": ["a1b2c3d4"]},
        "start": {},
    }
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(d / "agent.pid")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    outcome = {}
    try:
        assert holder.stdout.readline().strip() == "locked"  # `lion agent --dir d` is serving
        for name, kw in branches.items():
            try:
                asyncio.run(agent_main(argparse.Namespace(**{**base, **kw})))
                outcome[name] = "ran"
            except AgentError as e:
                outcome[name] = "refused" if "held by live process" in str(e) else f"AgentError: {e}"
            except (Exception, SystemExit) as e:
                outcome[name] = f"{type(e).__name__}: {e}"
        asyncio.run(agent_main(argparse.Namespace(**{**base, "stats": True})))  # reads, under the lock
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)
        holder.stdout.close()
    files = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
    assert (outcome, ops, files) == (
        dict.fromkeys(branches, "refused"),
        [],
        [".khive/config.toml", "agent.pid", "chores.toml"],
    )


class Measured:
    """An instrument whose one outcome the test chooses."""

    direction, answers, every, about, mechanism, empty_expected = (
        "under-report",
        "what the test pointed it at",
        None,
        "a scripted instrument",
        "the test says so",
        False,
    )

    def __init__(self, out):
        self.out, self.args = out, []

    async def run(self, args):
        self.args.append(args)
        return self.out


def test_render_prints_the_measurement_unvetted_and_says_so():
    from lion_cli.agent import render

    from hub.agent.chores import Outcome

    blank = Outcome(findings=("a finding",), evidence="the instrument's own line")
    assert render("probe", Measured(blank), blank).splitlines() == [
        "probe: control ok, 1 finding(s), escalate=False, count=None, direction under-report",
        "unvetted: run outside the chore path, so no refusal ran over it (a blank population, predicate, "
        "count or known-positive passes), nothing was sent and no ledger row was written",
        "answers: what the test pointed it at",
        "known-positive: (none)",
        "population: ",
        "predicate: ",
        "- a finding",
        "the instrument's own line",
    ]
    failed = Outcome(control=False, escalate=True, population="rows", predicate="p", count=0, positive="x")
    text = render("probe", Measured(failed), failed)
    assert text.startswith("probe: control FAILED, 0 finding(s), escalate=True, count=0,")
    assert "known-positive: x\npopulation: rows\npredicate: p" in text


def test_land_writes_the_days_file_of_that_name_and_the_latest_run_wins(tmp_path):
    from lion_cli.agent import land

    path = land(tmp_path, "check-probe", "first")
    assert path.parent == tmp_path / "landing" and re.fullmatch(r"\d{8}-check-probe\.txt", path.name)
    assert land(tmp_path, "check-probe", "second") == path and path.read_text() == "second"
    assert sorted(p.name for p in (tmp_path / "landing").iterdir()) == [path.name]


def test_check_runs_one_instrument_prints_and_lands_it_and_sends_nothing(
    tmp_path, kkernel_stub, monkeypatch, capsys
):
    """`--check <name>`: the instrument runs once with no args under the identity's lock, its unvetted
    measurement is printed and landed, and no mail, ledger row or store call follows."""
    from hub.agent.chores import Outcome

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:agent"\n')
    (d / "chores.toml").write_text('actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n')
    ops: list[str] = []

    async def fake_exec(self, op):
        ops.append(op)
        return [{}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    held: list[bool] = []
    probe = Measured(Outcome(population="rows", predicate="probed", count=0, evidence="read 0 rows"))
    orig = probe.run

    async def run(args):  # the identity is held while the instrument runs
        held.append((d / "agent.pid").read_text().strip() == str(os.getpid()))
        return await orig(args)

    probe.run = run
    monkeypatch.setattr("lion_cli.agent.instruments", lambda cfg, khive, state: {"probe": probe})
    base = dict(dir=str(d), stats=False, model=None, ticks=False, arm=False, once=False, rerun=None, wait=0)
    asyncio.run(agent_main(argparse.Namespace(**base, check="probe")))
    printed = capsys.readouterr().out
    landed = Path(printed.splitlines()[-1].removeprefix("landed: "))
    assert landed.parent == d / "landing" and re.fullmatch(r"\d{8}-check-probe\.txt", landed.name)
    assert printed == landed.read_text() + f"\nlanded: {landed}\n"
    assert printed.startswith(
        "probe: control ok, 0 finding(s), escalate=False, count=0, direction under-report\n"
    )
    # unvetted: an empty population nobody declared expected and a blank known-positive pass unrefused
    assert "unvetted: run outside the chore path" in printed and "known-positive: (none)" in printed
    assert probe.args == [{}] and held == [True] and ops == []
    assert not (d / "ledger.jsonl").exists() and not (d / "agent.pid").exists()


def test_check_with_the_identity_free_prints_and_lands_the_measurement_and_sends_nothing(
    tmp_path, kkernel_stub, monkeypatch, capsys
):
    """With no live holder, --check claims the identity, runs the named instrument alone, prints it and
    lands the same text, then lets the identity go: no send, no ledger row, no tick."""
    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:agent"\n')
    (d / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n\n[chores.gtd-inbox]\n'
        'every = "every:1h"\n'
    )
    ops: list[str] = []

    async def fake_exec(self, op):
        ops.append(op)
        old = {"id": "t1", "title": "triage me", "created_at": "2026-01-01T00:00:00Z"}
        return [[old], [old]]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    a = argparse.Namespace(
        dir=str(d),
        stats=False,
        model=None,
        check="gtd-inbox",
        ticks=False,
        arm=False,
        once=False,
        rerun=None,
        wait=0,
    )
    asyncio.run(agent_main(a))
    text, landed = capsys.readouterr().out.rsplit("\nlanded: ", 1)
    path = Path(landed.strip())
    assert text.startswith(
        "gtd-inbox: control ok, 1 finding(s), escalate=False, count=1, direction under-report"
    )
    assert "- 1 gtd inbox row(s) older than 24h of 1 in inbox" in text
    assert path.parent == d / "landing" and re.fullmatch(r"\d{8}-check-gtd-inbox\.txt", path.name)
    assert path.read_text() == text
    assert ops == [
        '[gtd.tasks(status="inbox", assignee="lambda:x", limit=100), gtd.tasks(assignee="lambda:x", limit=1)]'
    ]
    files = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
    assert files == [".khive/config.toml", "chores.toml", f"landing/{path.name}"]  # the identity let go


def test_serve_keeps_the_lock_its_process_already_claimed(tmp_path):
    """The CLI claims before its first act; serve takes that lock over instead of claiming it again, and
    lets it go when it stops."""
    khive = FakeKhive(cwd=tmp_path)
    agent = build(khive, state=MemoryNoteStore())
    path = agent._claim()
    asyncio.run(agent.serve(until=lambda: True))
    assert agent.errors == [] and not path.exists() and agent._lock_fd is None


# the actor as the resident: `[chair]` in chores.toml, the claude CLI as the backend


# the chair as the resident: `[chair]` in chores.toml, the claude CLI as the backend


def test_backend_for_claude_code_is_the_cli_chat_shape_on_the_named_model(monkeypatch):
    from lion_cli.agent import backend_for

    from hub.harness import ClaudeCodeSession

    monkeypatch.setattr("hub.harness.claude_code.shutil.which", lambda name: "/opt/bin/claude")
    b = backend_for("claude_code/claude-opus-5-5")
    assert isinstance(b, ClaudeCodeSession) and (b.model, b.cli) == ("claude-opus-5-5", "/opt/bin/claude")
    assert isinstance(backend_for("claude_code"), ClaudeCodeSession)
    assert backend_for("claude_code").model is None
    p = backend_for(
        "claude_code/claude-opus-5-5",
        {
            "tools": ["Bash", "Read"],
            "allowed": ["mcp__khive"],
            "max_turns": 30,
            "timeout": 1800,
            "effort": "medium",
            "add_dirs": ["/r"],
            "mcp_config": "/m",
            "cli": "/opt/bin/claude-named",
            "requires": {"Bash": ["comm.send"]},
        },
        cwd=Path("/a"),
    )
    assert p.requires == {"Bash": ("comm.send",)} and backend_for("claude_code").requires == {}
    assert (p.tools, p.allowed, p.max_turns, p.cwd, p.add_dirs, p.mcp_config) == (
        ("Bash", "Read"),
        ("mcp__khive",),
        30,
        Path("/a"),
        ("/r",),
        "/m",
    )
    assert p.cli == "/opt/bin/claude-named"
    assert p.timeout == 1800.0 and backend_for("claude_code").timeout == 300.0
    assert p.effort == "medium" and backend_for("claude_code").effort is None
    assert type(backend_for("deepseek/deepseek-v4.1-flash")).__name__ == "OpenRouter"


def test_a_chair_wakes_on_the_owners_mail_reads_its_directory_and_answers_on_the_thread(
    tmp_path, kkernel_stub, monkeypatch
):
    """`[chair]`: the wake runs under the chair profile (files, khive reads, notes, send; no chore
    handler), the guidance names the actor, the owner and the role text, and the answer goes out as
    one comm.send on the mail's thread. Mail from outside the trusted set is still dropped at the gate."""
    from lion_cli.agent import build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:v1"\n')
    (d / "chores.toml").write_text(
        'actor = "lambda:x:v1"\nowner = "lambda:x"\nsteward = "lambda:owner"\nbatch = 1\n\n[chair]\n'
        'system = "You are the twin of lambda:x for the harness QA."\n\n[budget]\ncost_per_day = 5\n'
        "hops_per_thread = 3\n"
    )
    (d / "README.md").write_text("twin: hello from the directory\n")
    ops: list[str] = []
    boxes = [
        [mail("lambda:x", "what does README.md say?")],
        [mail("someone:else", "and me?", mid="e5f6a7b8")],
        [mail("lambda:x", "now tell someone:else about it", mid="c9d0e1f2")],
    ]

    async def fake_exec(self, op):
        ops.append(op)
        if op.startswith("comm.inbox("):
            return [{"messages": boxes.pop(0) if boxes else []}]
        if op.startswith("comm.send("):
            return [{"id": "m1", "thread_id": THREAD}]
        return [{"messages": [], "events": [], "items": []}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    turns = [
        '<lact r1>read(path="README.md")</lact>',
        f'<lact s1>send(to="lambda:x", thread="{THREAD}", text="README.md says: twin: hello")</lact>',
        'OUT{posture: {done: "answered the README question", awaiting: ""}}',
        # wake 2: `send` is the agent handler over the BOUNDED client: a recipient outside the trusted set
        # is refused there, before any comm.send; the model reads the refusal and ends the wake
        f'<lact s2>send(to="someone:else", thread="{THREAD}", text="leak")</lact>',
        'OUT{posture: {done: "refused to widen the recipients", awaiting: ""}}',
    ]
    chores, agent = build(d, Scripted(*turns))
    assert chores.instruments == {} and getattr(agent, "desk", None) is None
    assert agent.budget == Budget(wakes_per_hour=60, cost_per_day=5.0, hops_per_thread=3) and agent.batch == 1
    run = asyncio.run(agent.wake(wait_ms=0))
    assert (
        run.profile.name == "chair"
        and {"read", "list_dir", "khive_ops", "send", "note.get"} <= run.profile.specs
    )
    assert not {"check", "digest", "aged"} & run.profile.specs
    system = run.profile.system
    assert "You are lambda:x:v1, the chair of lambda:x" in system and "twin of lambda:x" in system
    assert (
        "signing as lambda:x:v1" in system
        and "Mail from lambda:owner, lambda:x, lambda:x:v1 is your work" in system
    )
    results = [e.content.value for e in run.record if e.kind is Kind.RESULT]
    assert any("twin: hello from the directory" in str(r) for r in results)
    sends = [op for op in ops if op.startswith("comm.send(")]
    assert (
        len(sends) == 1 and '"lambda:x"' in sends[0] and THREAD in sends[0] and "README.md says" in sends[0]
    )
    assert agent.state.get("cursor").value["sent"] == 1
    assert agent.actor.notes_for(run.profile).get("posture").value == "answered the README question"
    # the outsider: read, noted, never run
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert [op for op in ops if op.startswith("comm.send(")] == sends and agent.wakes == 1
    # no chore, no tick: --ticks creates nothing, so no tick ever costs the chair a model call
    base = dict(
        dir=str(d),
        stats=False,
        model=None,
        check=None,
        ticks=True,
        arm=False,
        once=False,
        rerun=None,
        wait=0,
    )
    asyncio.run(agent_main(argparse.Namespace(**base)))
    assert not [op for op in ops if op.startswith("schedule.")]
    run2 = asyncio.run(agent.wake(wait_ms=0))
    refused = [e.content for e in run2.record if e.kind is Kind.RESULT and not e.content.ok]
    assert (
        len(refused) == 1
        and "agent boundary" in str(refused[0].error)
        and "someone:else" in str(refused[0].error)
    )
    assert [op for op in ops if op.startswith("comm.send(")] == sends and agent.wakes == 2


def test_a_chair_that_is_not_a_table_is_refused_by_name(tmp_path, kkernel_stub):
    from lion_cli.agent import build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:v1"\n')
    (d / "chores.toml").write_text('actor = "lambda:x:v1"\nowner = "lambda:x"\nchair = true\n')
    with pytest.raises(ValueError, match=r"\[chair\] is a table"):
        build(d, Scripted())


def test_the_session_clis_own_tool_calls_ask_the_agents_actor(tmp_path, kkernel_stub):
    from lion_cli.agent import build

    from hub.harness import ClaudeCodeSession

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:v1"\n')
    (d / "chores.toml").write_text('actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n')
    backend = ClaudeCodeSession(cli="/bin/claude", tools=("Bash",))
    _, agent = build(d, backend)
    assert backend.actor is agent.actor


def test_arm_prints_the_auth_env_the_claude_child_inherits_and_fails_on_an_api_key(
    tmp_path, kkernel_stub, monkeypatch
):
    """A resident on the subscription must never bill the API key, so the arm landing says which of the
    two the CLI child would see, and an ANTHROPIC_API_KEY fails the arm."""
    from lion_cli.agent import arm, build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:v1"\n')
    (d / "chores.toml").write_text('actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n')

    async def fake_exec(self, op):
        if op.startswith("comm.inbox("):
            return [{"messages": [{"id": "m1", "properties": {"to_actor": "lambda:x:v1"}}]}]
        raise AgentError("agent boundary: refused")

    monkeypatch.setattr(Khive, "exec", fake_exec)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CC_ACCOUNT", raising=False)
    chores, agent = build(d, Scripted())
    rc, lines = asyncio.run(arm(chores, agent))
    assert rc == 0
    absent = "{'ANTHROPIC_API_KEY': 'absent', 'CLAUDE_CODE_OAUTH_TOKEN': 'absent'}"
    assert (
        f"auth env the claude child would inherit: {absent}; account: unnamed (the CLI's own login)" in lines
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("CC_ACCOUNT", "second")
    rc, lines = asyncio.run(arm(chores, agent))
    assert rc == 0 and any(
        line.endswith("'CLAUDE_CODE_OAUTH_TOKEN': 'set'}; account: second") for line in lines
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc, lines = asyncio.run(arm(chores, agent))
    assert rc == 1 and any(line.startswith("auth: FAILED, ANTHROPIC_API_KEY is set") for line in lines)


def test_a_chair_admitting_all_runs_mail_from_any_sender_and_still_sends_only_to_the_trusted(
    tmp_path, kkernel_stub, monkeypatch
):
    """`[chair] admit = "all"`: the chair's box takes every sender; the outsider's mail runs (the guidance
    decides what to do with it) while `send` stays bounded to the trusted recipients."""
    from lion_cli.agent import build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    (d / "chores.toml").write_text(
        'actor = "lambda:x"\nowner = "lambda:owner"\nbatch = 1\n\n[chair]\nadmit = "all"\n'
    )
    ops: list[str] = []
    boxes = [[mail("someone:else", "hello from outside")]]

    async def fake_exec(self, op):
        ops.append(op)
        if op.startswith("comm.inbox("):
            return [{"messages": boxes.pop(0) if boxes else []}]
        if op.startswith("comm.send("):
            return [{"id": "m1", "thread_id": THREAD}]
        return [{"messages": [], "events": [], "items": []}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    turns = [
        f'<lact s1>send(to="someone:else", thread="{THREAD}", text="hi")</lact>',
        'OUT{posture: {done: "read one from outside, could not answer it", awaiting: ""}}',
    ]
    chores, agent = build(d, Scripted(*turns))
    assert agent.admit is None
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None and run.profile.name == "chair"
    refused = [e.content for e in run.record if e.kind is Kind.RESULT and not e.content.ok]
    assert len(refused) == 1 and "agent boundary" in str(refused[0].error)
    assert not [op for op in ops if op.startswith("comm.send(")]


def test_the_wake_line_carries_the_last_turns_context_and_the_sessions_started_over():
    # `in` sums a call's turns (its cost); the last turn's own context is what the next turn pays for
    # and what the fold decides on, so the line carries it, with the sessions the backend restarted
    from types import SimpleNamespace

    from hub.agent.agent import Agent, _line

    usage = {
        "input_tokens": 50,
        "cache_read_input_tokens": 173160,
        "cache_creation_input_tokens": 15716,
        "iterations": [
            {"input_tokens": 8, "cache_read_input_tokens": 31000, "cache_creation_input_tokens": 887}
        ],
    }
    backend = SimpleNamespace(calls=[{"total_cost_usd": 0.1, "usage": usage, "num_turns": 6}], restarts=1)
    u = Agent._usage(SimpleNamespace(backend=backend), 0)
    assert (u["tokens_in"], u["context"]) == (188926, 31895)
    cursor = {
        "wake": 44,
        "outcome": "Quiet",
        "rounds": 1,
        "mail": [1],
        "sent": 0,
        "tokens": {"calls": 1, "in": 188926, "out": 601, "cost": 0.1, "context": 31895, "restarts": 1},
        "idle_polls": 3,
    }
    assert _line(cursor).endswith(
        "3 idle poll(s) before it at 0 tokens; last turn stood in 31895 tokens, 1 session restart(s)"
    )
    cursor["tokens"] = {"calls": 1, "in": 0, "out": 0, "cost": 0.0}
    assert _line(cursor).endswith("3 idle poll(s) before it at 0 tokens")  # a backend without envelopes


def test_wake_rules_read_top_down_by_sender_glob_subject_and_spent_fraction():
    # the owner 2026-09-24: a wake per mail is too dear; the trigger is a rule
    import time

    rules = Wake.load(
        {
            "default": "batch",
            "batch_after": "30m",
            "batch_size": 3,
            "quiet": ["23:00-07:00"],
            "rule": [
                {"from": ["lambda:owner", "email:*"], "then": "now"},
                {"subject": "^\\[NOW\\]", "then": "now"},
                {"from": "*:desk", "spent_over": 0.75, "then": "drop"},
                {"from": ["daemon:*"], "then": "batch"},
            ],
        }
    )
    assert rules.batch_after == 1800.0 and rules.batch_size == 3 and rules.quiet == ((23 * 60, 7 * 60),)
    assert rules.decide(mail("lambda:owner", "x"), 0.0) == "now"
    assert rules.decide(mail("email:person@example.com", "x"), 0.0) == "now"
    assert rules.decide({**mail("lambda:c", "x"), "subject": "[NOW] merge"}, 0.0) == "now"
    assert rules.decide(mail("lambda:b:desk", "x"), 0.5) == "batch"  # not past the spent fraction yet
    assert rules.decide(mail("lambda:b:desk", "x"), 0.9) == "drop"
    assert rules.decide(mail("daemon:review", "x"), 0.0) == "batch"
    assert rules.decide(mail("lambda:d", "x"), 0.0) == "batch"  # the default
    night = time.mktime(time.strptime("2026-09-24 23:30:00", "%Y-%m-%d %H:%M:%S"))
    noon = time.mktime(time.strptime("2026-09-24 12:00:00", "%Y-%m-%d %H:%M:%S"))
    assert rules.is_quiet(night) and rules.is_quiet(night + 3 * 3600) and not rules.is_quiet(noon)
    one = [{"deferred_ts": noon - 100}]
    assert not rules.window_open(one, noon) and rules.window_open(one, noon + 1800)  # old enough
    assert rules.window_open([{"deferred_ts": noon}] * 3, noon)  # enough of it
    assert not rules.window_open([{"deferred_ts": night - 7200}] * 3, night)  # quiet hours wait
    assert rules.describe() == "default batch, 4 rule(s), window 1800s or 3 mail, quiet 23:00-07:00"
    assert Wake.load(None) is None and Wake.load({}) is None
    with pytest.raises(ValueError, match="then"):
        Wake.load({"rule": [{"from": "x", "then": "later"}]})
    with pytest.raises(ValueError, match="span"):
        Wake.load({"batch_after": "soon"})
    with pytest.raises(ValueError, match="quiet"):
        Wake.load({"quiet": ["night"]})
    with pytest.raises(ValueError, match="minutes 00-59"):  # 07:75 is refused, never read as 08:15
        Wake.load({"quiet": ["07:75-08:00"]})


def test_batch_mail_waits_read_under_the_deferred_note_and_now_mail_takes_it_along(tmp_path):
    rules = Wake(default="batch", batch_after=3600, batch_size=5, rules=(WakeRule("now", subject="^urgent"),))
    khive = FakeKhive(mail("lambda:a", "a verdict", mid="d1d1d1d1"), cwd=tmp_path)
    agent = build(khive, REPLY, DONE, wake_rules=rules)
    lines: list[str] = []
    agent._say = lines.append
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # read, kept, not run
    assert khive.read == ["d1d1d1d1-0000-0000-0000-000000000000"] and "backend" not in khive.log
    waiting = agent.state.get("deferred").value["mail"]
    assert [m["id"] for m in waiting] == ["d1d1d1d1"] and waiting[0]["deferred_at"]
    assert agent.state.get("taking").value == {}  # nothing owed: the mail is safe under `deferred`
    assert lines == ["waiting: 1 mail (oldest 0s) for the window, 3600s or 5 mail"]
    assert asyncio.run(agent.wake(wait_ms=0)) is None and len(lines) == 1  # still waiting, said once
    khive.mail.append({**mail("lambda:a", "now please", mid="e2e2e2e2"), "subject": "urgent: now"})
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None and agent.state.get("cursor").value["mail"] == ["d1d1d1d1", "e2e2e2e2"]
    assert agent.state.get("deferred").value == {"mail": []}
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert "waited since" in first and "now please" in first and "2 message(s)" in first


def test_the_window_opens_by_age_with_no_new_mail_and_the_wake_now_file_opens_it_at_once(tmp_path):
    now = [1_000_000.0]
    rules = Wake(default="batch", batch_after=600, batch_size=5)
    khive = FakeKhive(mail("lambda:a", "later", mid="d1d1d1d1"), cwd=tmp_path)
    agent = build(khive, REPLY, DONE, wake_rules=rules, clock=lambda: now[0])
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    now[0] += 599
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    now[0] += 1
    run = asyncio.run(agent.wake(wait_ms=0))  # the box is empty; the window opened on age alone
    assert run is not None and agent.state.get("cursor").value["mail"] == ["d1d1d1d1"]
    khive.mail.append(mail("lambda:a", "later too", mid="e2e2e2e2"))
    agent = build(khive, REPLY, DONE, wake_rules=rules, clock=lambda: now[0])  # the same notes on disk
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    (tmp_path / "wake-now").write_text("the owner from the app\n")
    lines: list[str] = []
    agent._say = lines.append
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None and not (tmp_path / "wake-now").exists()  # the lever is consumed by its wake
    assert agent.state.get("cursor").value["mail"] == ["e2e2e2e2"]
    assert lines[0] == "wake-now: taking 1 waiting and 0 new"


class FailsOnce(MemoryNoteStore):
    """A store whose write of `key` raises once, the first time it follows a write of `after`, while
    `armed`: the disk that fills between two notes of one wake."""

    def __init__(self, key: str, after: str):
        super().__init__()
        self.key, self.after, self.armed, self.seen = key, after, False, False

    def put(self, key, value, *, run, seq):
        if self.armed and self.seen and key == self.key:
            self.armed = False
            raise OSError("disk full")
        if self.armed and key == self.after:
            self.seen = True
        return super().put(key, value, run=run, seq=seq)


def test_released_mail_is_named_under_taking_before_deferred_lets_go_so_a_failed_write_still_owes_it():
    rules = Wake(default="batch", batch_after=3600, batch_size=5, rules=(WakeRule("now", subject="^urgent"),))
    khive, state = FakeKhive(mail("lambda:a", "a verdict", mid="d1d1d1d1")), FailsOnce("taking", "deferred")
    agent = build(khive, state=state, wake_rules=rules, owner="lambda:a")
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # read and kept under `deferred`
    khive.mail.append({**mail("lambda:a", "now please", mid="e2e2e2e2"), "subject": "urgent: now"})
    state.armed = True  # `deferred` lets go of d1d1d1d1, then the write naming the run's batch fails
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(agent.wake(wait_ms=0))
    assert state.get("deferred").value == {"mail": []}
    asyncio.run(build(khive, state=state, wake_rules=rules, owner="lambda:a").serve(until=lambda: True))
    told = [s["content"] for s in khive.sent]
    assert len(told) == 1 and "d1d1d1d1" in told[0] and "e2e2e2e2" in told[0]
    assert state.get("unreported").value == [] and state.get("taking").value == {}


def test_a_batch_told_as_lost_leaves_deferred_so_it_is_never_run_later_too():
    rules = Wake(default="batch", batch_after=3600, batch_size=5)
    khive, state = FakeKhive(), MemoryNoteStore()
    waiting = [{**mail("lambda:a", "a verdict", mid="d1d1d1d1"), "deferred_ts": 0.0}]
    waiting.append({**mail("lambda:a", "another", mid="f3f3f3f3"), "deferred_ts": 0.0})
    state.put("deferred", {"mail": waiting}, run="-", seq=0)  # a take whose `deferred` write never landed
    state.put("taking", {"mail": ["d1d1d1d1"]}, run="-", seq=0)
    agent = build(khive, state=state, wake_rules=rules, owner="lambda:a")
    asyncio.run(agent.serve(until=lambda: True))
    assert len(khive.sent) == 1 and "d1d1d1d1" in khive.sent[0]["content"]
    assert [m["id"] for m in state.get("deferred").value["mail"]] == ["f3f3f3f3"]


def test_a_hold_file_holds_the_agent_by_its_first_line_and_its_removal_resumes_it(tmp_path):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    agent = build(khive, REPLY, DONE)
    (tmp_path / "hold").write_text("held by the owner from the app: reviewing\nmore\n")
    assert asyncio.run(agent.wake(wait_ms=0)) is None and khive.log == []  # not even polled
    assert agent.held == "held by file: held by the owner from the app: reviewing"
    (tmp_path / "hold").unlink()
    assert asyncio.run(agent.wake(wait_ms=0)) is not None and agent.held is None


def test_budget_and_wake_rules_follow_the_config_file_while_serving(tmp_path):
    import time

    cfg = tmp_path / "chores.toml"
    cfg.write_text("[budget]\ncost_per_day = 5.0\n")
    agent = build(FakeKhive(cwd=tmp_path), config=cfg, budget=Budget.load({"cost_per_day": 5.0}))
    lines: list[str] = []
    agent._say = lines.append
    agent._reload()
    assert lines == [] and agent.budget.cost_per_day == 5.0  # unchanged since the build
    cfg.write_text(
        '[budget]\ncost_per_day = 9.0\nwakes_per_hour = 7\n\n[wake]\ndefault = "batch"\nbatch_after = "10m"\n'
        '[[wake.rule]]\nfrom = ["lambda:owner"]\nthen = "now"\n'
    )
    os.utime(cfg, (time.time() + 5, time.time() + 5))  # a same-second rewrite is still a change
    agent._reload()
    assert agent.budget.cost_per_day == 9.0 and agent.budget.wakes_per_hour == 7
    assert agent.wake_rules.default == "batch" and agent.wake_rules.batch_after == 600.0
    assert lines == [
        "reloaded: budget cost_per_day=9.0 wakes_per_hour=7; "
        "wake default batch, 1 rule(s), window 600s or 5 mail; gtd off"
    ]
    cfg.write_text('[wake]\ndefault = "sometimes"\n')
    os.utime(cfg, (time.time() + 10, time.time() + 10))
    agent._reload()
    assert agent.wake_rules.default == "batch" and agent.budget.cost_per_day == 9.0  # in force stays
    assert lines[-1].startswith("reload failed, the values in force stay: ValueError: [wake] default")


def test_build_reads_the_wake_rules_and_names_the_config_for_reload(tmp_path, kkernel_stub):
    from lion_cli.agent import build as build_dir

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    (d / "chores.toml").write_text(
        'actor = "lambda:x"\nowner = "lambda:owner"\n\n[chair]\nadmit = "all"\n'
        "\n[budget]\ncost_per_day = 3.0\n"
        '\n[wake]\ndefault = "batch"\nbatch_size = 2\n'
        '[[wake.rule]]\nfrom = ["lambda:owner", "email:*"]\nthen = "now"\n\n[gtd]\npoll = "30s"\n'
    )
    chores, agent = build_dir(d, Scripted())
    assert agent.gtd is not None and agent.gtd.poll == 30.0 and agent.gtd.work == "off"
    assert agent.wake_rules.default == "batch" and agent.wake_rules.batch_size == 2
    assert agent.wake_rules.rules[0].senders == ("lambda:owner", "email:*")
    assert agent.config == d / "chores.toml" and agent.budget.cost_per_day == 3.0


class QueueKhive(FakeKhive):
    """A box plus a gtd queue, answered to every `exec` (the chair reads its queue with one op)."""

    def __init__(self, *inbox: dict, queue: list[dict] = (), cwd=None):
        super().__init__(*inbox, cwd=cwd)
        self.queue, self.ops = list(queue), []

    async def exec(self, ops: str) -> list:
        self.ops.append(ops)
        self.log.append("exec")
        return [list(self.queue)]


def task(tid: str, title: str, *, status="next", priority="p1", at=None, description="") -> dict:
    t = {"id": tid, "title": title, "status": status, "priority": priority, "properties": {}}
    if description:
        t["properties"]["description"] = description
    if at is not None:
        t["at"] = at
    return t


def test_gtd_config_and_the_task_time():
    import time

    from hub.agent.agent import Gtd, _at

    g = Gtd.load({"poll": "2m", "due": "now", "work": "idle", "idle_after": "10m", "notice": 3})
    assert (g.poll, g.due, g.work, g.idle_after, g.notice, g.limit) == (120.0, "now", "idle", 600.0, 3, 20)
    assert g.describe() == "poll 120s, due now, work idle after 600s quiet"
    assert Gtd.load(None) is None and Gtd.load({}) is None
    with pytest.raises(ValueError, match="due"):
        Gtd.load({"due": "later"})
    with pytest.raises(ValueError, match="work"):
        Gtd.load({"work": "always"})
    assert (
        _at({}) is None and _at({"at": ""}) is None and _at({"at": "soon"}) is None and _at({"at": 5}) == 5.0
    )
    local = time.mktime(time.strptime("2026-09-24 20:14:00", "%Y-%m-%d %H:%M:%S"))
    assert _at({"at": "2026-09-24T20:14:00"}) == local  # naive ISO is local time


def test_a_due_task_wakes_the_chair_once_per_at_as_a_notice_not_mail(tmp_path):
    # the chair keeps its gtd over the khive MCP; the runtime reads the queue and wakes on it
    from hub.agent.agent import Gtd, _line

    now = [1_000_000.0]
    due = task("t1", "cost report", at=now[0] - 1, description="send the numbers")
    khive = QueueKhive(queue=[due, task("t2", "later", at=now[0] + 3600)], cwd=tmp_path)
    actor = Actor("echo", privileges={"comm.send"})
    backend = Scripted(DONE, DONE, khive=khive)
    agent = Agent(actor, khive, backend, lambda b: ACTING, gtd=Gtd(poll=60), clock=lambda: now[0])
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None and khive.read == []  # a task is never marked read: it is not mail
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert (
        "0 message(s) arrived since the last wake and 1 task(s) of your queue are in front of you." in first
    )
    assert f"[task t1 p1 next at {now[0] - 1}: due now] cost report\nsend the numbers" in first
    cursor = agent.state.get("cursor").value
    assert cursor["mail"] == [] and cursor["tasks"] == ["t1"] and cursor["notices"] == ["t1"]
    assert _line(cursor).endswith("; 1 task(s) of the queue")
    notice = next(e for e in run.record if e.kind is Kind.SYSTEM).content
    assert "queue: 0 active, 2 next; t1 p1 next cost report · t2 p1 next later" in notice
    now[0] += 30  # within the poll: no read, and t1 woke already
    assert asyncio.run(agent.wake(wait_ms=0)) is None and len(khive.ops) == 1
    now[0] += 60
    khive.queue[0] = {**due, "at": now[0] - 1}  # the model moved it: a new `at` is a new wake
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None and agent.state.get("cursor").value["tasks"] == ["t1"] and len(khive.ops) == 2


def test_a_quiet_chair_works_the_top_of_its_queue_after_idle_after(tmp_path):
    from hub.agent.agent import Gtd

    now = [2_000_000.0]
    khive = QueueKhive(queue=[task("t9", "tidy the corpus", status="active", priority="p0")], cwd=tmp_path)
    backend = Scripted(DONE, DONE, khive=khive)
    gtd = Gtd(poll=1, work="idle", idle_after=600)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        backend,
        lambda b: ACTING,
        gtd=gtd,
        clock=lambda: now[0],
    )
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # quiet, but only since the start
    now[0] += 600
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert "[task t9 p0 active: the top of your queue; no mail, you are working it] tidy the corpus" in first
    assert agent.state.get("cursor").value["tasks"] == ["t9"]
    now[0] += 10
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # the wake moved the clock: idle_after again first
    off = Gtd(poll=1, work="off")
    agent = Agent(
        Actor("echo2", privileges={"comm.send"}),
        khive,
        Scripted(khive=khive),
        lambda b: ACTING,
        gtd=off,
        clock=lambda: now[0] + 10_000,
    )
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # off: the queue is a line in the notice, never a wake


def test_idle_work_reads_the_queue_when_due_is_off_and_due_tasks_still_do_not_wake(tmp_path):
    from hub.agent.agent import Gtd

    now = [2_000_000.0]
    top = task("t9", "tidy the corpus", status="active", priority="p0", at=now[0] - 1)
    khive = QueueKhive(queue=[top], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        Scripted(DONE, khive=khive),
        lambda b: ACTING,
        gtd=Gtd(poll=1, due="off", work="idle", idle_after=600),
        clock=lambda: now[0],
    )
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # past its `at`, but `due` is off: no wake
    assert khive.ops  # the queue was read all the same
    now[0] += 600
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert (
        "[task t9 p0 active at " in first and "the top of your queue; no mail, you are working it]" in first
    )


def test_a_wake_now_file_with_no_mail_waiting_runs_the_queue_as_it_is_now_or_is_consumed_saying_so(tmp_path):
    from hub.agent.agent import Gtd

    now = [2_000_000.0]
    khive = QueueKhive(queue=[], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        Scripted(DONE, khive=khive),
        lambda b: ACTING,
        gtd=Gtd(poll=600, work="off"),
        clock=lambda: now[0],
    )
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # quiet; the queue was read empty and is cached
    khive.queue = [task("t3", "read the desk", status="next", priority="p1")]  # assigned within `poll`
    now[0] += 5
    (tmp_path / "wake-now").write_text("the owner from the app\n")
    lines: list[str] = []
    agent._say = lines.append
    reads = len(khive.ops)
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None and not (tmp_path / "wake-now").exists()
    assert len(khive.ops) == reads + 1  # the pull read the queue as it is now, not the cache
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert "[task t3 p1 next: a person asked for a wake; the top of your queue] read the desk" in first
    assert agent.state.get("cursor").value["tasks"] == ["t3"]
    assert lines[0] == "wake-now: no mail waiting; the queue's top runs"
    # an empty queue: the file is consumed and said, and no model call is spent
    khive.queue = []
    now[0] += 5
    (tmp_path / "wake-now").write_text("the owner again\n")
    lines.clear()
    calls = khive.log.count("backend")
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert not (tmp_path / "wake-now").exists() and khive.log.count("backend") == calls
    assert lines == ["wake-now: nothing waiting and no task; no wake"]
    (tmp_path / "wake-now").mkdir()  # a directory of that name is not a pull
    assert asyncio.run(agent.wake(wait_ms=0)) is None and len(lines) == 1


def test_the_pull_reads_the_queue_with_due_off_and_keeps_when_refused_mail_used_the_poll(tmp_path):
    from hub.agent.agent import Gtd

    now = [3_000_000.0]
    khive = QueueKhive(mail("lambda:a", "junk"), queue=[task("t4", "answer the desk")], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        Scripted(DONE, khive=khive),
        lambda b: ACTING,
        gtd=Gtd(poll=600, due="off"),
        admit=lambda m: False,
        clock=lambda: now[0],
    )
    (tmp_path / "wake-now").write_text("the owner\n")
    lines: list[str] = []
    agent._say = lines.append
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # the mail was refused: no wake, the pull is kept
    assert (tmp_path / "wake-now").exists() and lines == [] and khive.ops == []  # due off: no read yet
    now[0] += 5
    run = asyncio.run(agent.wake(wait_ms=0))  # the box is empty: the pull reads the queue, due off or not
    assert run is not None and not (tmp_path / "wake-now").exists() and len(khive.ops) == 1
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert "[task t4 p1 next: a person asked for a wake; the top of your queue] answer the desk" in first


def test_a_pull_beside_a_due_task_is_one_wake_and_no_queue_config_is_no_task(tmp_path):
    from hub.agent.agent import Gtd

    now = [4_000_000.0]
    due = task("t5", "cost report", at=now[0] - 1)
    khive = QueueKhive(queue=[due], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        Scripted(DONE, DONE, khive=khive),
        lambda b: ACTING,
        gtd=Gtd(poll=60),
        clock=lambda: now[0],
    )
    (tmp_path / "wake-now").write_text("the owner\n")
    lines: list[str] = []
    agent._say = lines.append
    run = asyncio.run(agent.wake(wait_ms=0))  # the due task wakes the chair; the pull is spent by it
    assert run is not None and not (tmp_path / "wake-now").exists()
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert "due now] cost report" in first and "a person asked" not in first
    assert lines == ["wake-now: taking 0 waiting and 0 new"]
    now[0] += 5
    assert asyncio.run(agent.wake(wait_ms=0)) is None and khive.log.count("backend") == 1  # not twice
    agent.gtd = None  # a reload took [gtd] away: the cached queue is not a queue
    (tmp_path / "wake-now").write_text("the owner\n")
    lines.clear()
    assert asyncio.run(agent.wake(wait_ms=0)) is None and not (tmp_path / "wake-now").exists()
    assert lines == ["wake-now: nothing waiting and no task; no wake"]


def test_a_pull_during_the_continuous_chairs_wait_is_one_wake_on_that_path(tmp_path):
    """The deployed path: after its first wake a chair polls in `_wait`, not `wake()`. A pull there
    with a task queued adds one wake to the same run; a pull in the poll of a due task adds none."""
    from hub.agent.agent import Gtd

    now = [5_000_000.0]
    queued = [task("t3", "read the desk"), task("t6", "cost report", at=now[0] + 1000)]
    khive = QueueKhive(mail("lambda:a", "say hi back"), queue=queued, cwd=tmp_path)
    # the mail is answered on its thread; a task notice has no thread, so those bursts end on plain words
    backend = Scripted(REPLY, "on it", "on it", "on it", khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        backend,
        profile_for,
        continuous=True,
        poll_ms=20,
        gtd=Gtd(poll=600),
        clock=lambda: now[0],
    )
    lines: list[str] = []
    file = tmp_path / "wake-now"
    step = {"n": 0, "after": 0, "polls": 0}

    def wake_lines() -> list[str]:
        return [line for line in lines if re.match(r"wake \d+:", line)]

    def until() -> bool:
        step["polls"] += 1
        w = len(wake_lines())
        if w == 1 and step["n"] == 0:
            file.write_text("the owner from the app\n")  # the pull lands in the wait, with a task queued
            step["n"] = 1
        elif w == 2 and step["n"] == 1:
            now[0] += 1000  # t6 is due now, and the pull lands in the same poll
            file.write_text("the owner again\n")
            step["n"] = 2
        elif w >= 3:
            step["after"] += 1
        return step["after"] >= 8 or step["polls"] >= 400  # the bound: a lost pull must not hang the test

    asyncio.run(agent.serve(until=until, log=lines.append))
    # three wakes: the mail, the pull, the due task with the pull spent on it. The wait carries a task
    # notice to a run of its own when its profile differs, so the inputs are read across the runs
    assert agent.wakes == 3 and len(wake_lines()) == 3 and not file.exists()
    inputs = [e.content for run in agent.runs for e in run.record if e.kind is Kind.INPUT]
    assert len(inputs) == 3
    assert "[task t3 p1 next: a person asked for a wake; the top of your queue] read the desk" in inputs[1]
    assert "due now] cost report" in inputs[2] and "a person asked" not in inputs[2]
    assert "wake-now: no mail waiting; the queue's top runs" in lines
    assert "wake-now: taking 0 waiting and 0 new" in lines
