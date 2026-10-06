"""The Agent: one wake per batch of mail, marked read before the model sees it, answered on the
thread under the privilege the profile holds, a cursor for the next wake, and the caps that keep a
long-running actor from answering forever or spending without bound."""

import argparse
import asyncio
import itertools
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lion_cli.agent import agent_main, stats
from lion_cli.main import main

from hub.agent import Agent, AgentError, Budget, Wake, WakeRule
from hub.agent.chores import ALLOWED_VERBS
from hub.backend import claude_code
from hub.backend.claude_code import ClaudeCodeSession
from lion1 import Actor, KhiveNoteStore, Kind, Message, Profile, Store, Success
from lion1.khive import LOST, Khive, KhiveError
from tests.lionfake import LionFake, LionKhive, resident_store, routed_config
from tests.lionfake import store as fresh_store

THREAD = "0a0a0a0a-0000-4000-8000-0a0a0a0a0a0a"
HOLD = (  # a process that takes the pid file's lock and keeps it until its stdin closes
    "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
    "fcntl.flock(fd, fcntl.LOCK_EX); os.write(fd, b'held'); print('locked', flush=True); sys.stdin.read()"
)


def noted(state) -> dict:
    """What `stats` reads of a store: every note's value by key."""
    return {n.key: n.value for n in state.list()}


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

    async def exec(self, ops: str) -> list:
        # the mark-read is the one op the agent writes as DSL; the double keeps a method for its books
        m = re.fullmatch(r"comm\.read\(ids=(\[.*\])\)", ops)
        assert m, ops
        await self.mark_read(json.loads(m.group(1)))
        return [{"status": "success"}]

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


def build(khive: FakeKhive, *turns: str, store=None, **kw) -> Agent:
    backend = Scripted(*turns, khive=khive)
    return Agent(
        Actor("echo", privileges={"comm.send"}, store=store or fresh_store()),
        khive,
        backend,
        profile_for,
        **kw,
    )


REPLY = f'<lact s1>send(to="lambda:a", thread="{THREAD}", text="hi back")</lact>'
# a later wake continues the last run, whose aliases it replays: its reply takes a name of its own
REPLY2 = REPLY.replace("<lact s1>", "<lact s2>")
DONE = 'OUT{posture: {done: "answered hello", awaiting: ""}}'
SENT = {"to": "lambda:a", "content": "hi back", "thread_id": THREAD}


def own(run) -> list:
    """The run's own entries: what follows the history it replayed from the run it continues."""
    return list(run.record)[len(run.request.history) :]


def test_a_wake_marks_mail_read_before_the_model_sees_it_answers_on_the_thread_and_leaves_a_cursor():
    khive = FakeKhive(mail("lambda:a", "say hi back"))
    agent = build(khive, REPLY, DONE)
    run = asyncio.run(agent.wake(wait_ms=0))
    assert khive.log[:3] == ["inbox", "read", "backend"]
    assert khive.read == ["a1b2c3d4-0000-0000-0000-000000000000"]
    assert khive.sent == [SENT]
    assert isinstance(run.outcome, Success) and run.profile is ACTING and agent.wakes == 1
    assert run.request.trigger == ("a1b2c3d4-0000-0000-0000-000000000000",)  # the run names its wake's mail
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


def test_a_continuous_chair_keeps_one_run_across_bursts_and_books_each_quiet_point_as_a_wake(tmp_path, store):
    # the owner, 2026-09-23: the chair never invokes a new run for new mail; it is the same conversation
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    later = mail("lambda:a", "one more", mid="c3d4e5f6")
    lines: list[str] = []
    # aliases are unique for the whole run, bursts included; a command's result comes back next turn
    # and a turn of plain text, the posture, ends the burst
    again = REPLY.replace("<lact s1>", "<lact s2>")
    backend = Scripted(REPLY, "answered hello", again, "answered again", khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
        khive,
        backend,
        profile_for,
        continuous=True,
        poll_ms=20,
    )

    def until() -> bool:
        if len(lines) == 1 and later["full_id"] not in khive.read and later not in khive.mail:
            khive.mail.append(later)  # the world mails again once the first burst went quiet
        return len(lines) >= 2

    asyncio.run(agent.serve(until=until, log=lines.append))
    (run,) = agent.runs  # one run, two wakes
    assert agent.wakes == 2 and khive.sent == [SENT, SENT]
    assert lines[0].startswith("wake 1: Quiet after 2 turns, 1 mail, 1 sent; 2 model call(s)")
    assert lines[1].startswith("wake 2: Quiet after 2 turns, 1 mail, 1 sent; 2 model call(s)")
    inputs = [e.content for e in run.record if e.kind is Kind.INPUT]
    assert len(inputs) == 2 and "one more" in inputs[1] and "same conversation" in inputs[1]
    assert "'outcome': 'Quiet'" in inputs[1]  # the second burst is told how the first ended
    assert sorted(khive.read) == sorted(["a1b2c3d4-0000-0000-0000-000000000000", later["full_id"]])
    cursor = agent.state.get("cursor").value
    assert cursor["wake"] == 2 and cursor["mail"] == ["c3d4e5f6"] and cursor["run"] == run.id
    spend = agent.state.get("spend").value
    assert spend["runs"] == 2 and spend["mail"] == 2 and spend["calls"] == 4  # each burst booked once
    assert agent.actor.notes_for(ACTING).get("posture").value == "answered again"
    assert not (tmp_path / "agent.pid").exists()
    system = next(m.content for m in backend.seen[-1] if m.role == "system")
    assert "Finish by filling these with OUT{}" not in system  # no declared output, no OUTPUT section


@pytest.mark.parametrize(
    ("stop", "posture"),
    [
        ('OUT{posture: {done: "answered", awaiting: "the owner"}}', "answered Awaiting: the owner"),
        ('OUT{posture: "stopping, the store is gone"}', "stopping, the store is gone"),
        ('OUT{posture: {why: "the store is gone"}}', '{"why": "the store is gone"}'),
        ('OUT{why: "the store is gone"}', '{"why": "the store is gone"}'),
        ('OUT{posture: {(1, 2): "x"}}', "{(1, 2): 'x'}"),  # a key JSON cannot hold
    ],
    ids=["done-awaiting", "text", "other-fields", "no-posture", "not-json"],
)
def test_a_continuous_chairs_out_is_its_stop_and_its_posture_as_written(tmp_path, stop, posture):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    later = mail("lambda:a", "one more", mid="c3d4e5f6")
    lines: list[str] = []
    again = REPLY.replace("<lact s1>", "<lact s2>")
    backend = Scripted(REPLY, stop, again, "answered again", khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=fresh_store()),
        khive,
        backend,
        profile_for,
        continuous=True,
        poll_ms=20,
    )
    agent.actor.notes_for(ACTING).put("posture", "an earlier wake's posture", run="r-old", seq=3)

    def wakes() -> int:
        return sum(1 for line in lines if line.startswith("wake "))

    def until() -> bool:
        if wakes() == 1 and later["full_id"] not in khive.read and later not in khive.mail:
            khive.mail.append(later)
        return wakes() >= 2

    asyncio.run(agent.serve(until=until, log=lines.append))
    # the stop ended the conversation; the next mail runs a new one, which continues it from its rows
    first, second = agent.runs
    assert isinstance(first.outcome, Success) and khive.sent == [SENT, SENT]
    assert second.request.continues == first.id
    n = len(second.request.history)
    assert [e.content for e in list(second.record)[:n]] == [e.content for e in first.record]
    opening = next(e.content for e in own(second) if e.kind is Kind.INPUT)
    assert "say hi back" not in opening
    assert f"Your posture then: {posture}\n" in opening and "an earlier wake's posture" not in opening
    assert "'outcome': 'Success'" in opening


@pytest.mark.parametrize("reply", ["", "OUT{}"], ids=["empty", "bare-out"])  # a bare OUT{} reads as nothing
def test_an_empty_turn_ending_a_burst_leaves_an_empty_posture_of_that_burst(tmp_path, reply, store):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    lines: list[str] = []
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
        khive,
        Scripted(REPLY, reply, khive=khive),
        profile_for,
        continuous=True,
        poll_ms=20,
    )
    agent.actor.notes_for(ACTING).put("posture", "an earlier wake's posture", run="r-old", seq=3)
    asyncio.run(agent.serve(until=lambda: len(lines) >= 1, log=lines.append))
    (run,) = agent.runs
    note, cursor = agent.actor.notes_for(ACTING).get("posture"), agent.state.get("cursor")
    assert lines[0].startswith("wake 1: Quiet after 2 turns") and khive.sent == [SENT]
    assert (note.value, note.run, note.seq) == ("", run.id, cursor.seq) and cursor.run == run.id


def agent_dir(d: Path) -> Path:
    (d / ".khive").mkdir()
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:a:echo", d))
    return d


def on(store, khive: FakeKhive, *turns: str) -> Agent:
    """A per-wake agent whose actor writes to `store`: a second one over the same store is the next
    process of the same agent."""
    backend = Scripted(*turns, khive=khive)
    return Agent(Actor("echo", privileges={"comm.send"}, store=store), khive, backend, profile_for)


def run_row(store, run_id: str) -> dict:
    (got,) = store.khive.exec_sync(f'lion.get(table="runs", id="{run_id}")')
    return got


def test_a_second_wake_replays_from_the_firsts_rows_and_writes_no_checkpoint_file(tmp_path, store):
    d = agent_dir(tmp_path)
    tree = sorted(p.relative_to(d) for p in d.rglob("*"))
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    first = on(store, khive, REPLY, DONE)
    one = asyncio.run(first.wake(wait_ms=0))
    assert first.state.get("cursor").value["run"] == one.id  # the cursor names the run that returned
    # the next process: a new agent over the same store, woken by the next mail
    khive.mail.append(mail("lambda:a", "one more", mid="c3d4e5f6"))
    second = on(store, khive, REPLY2, DONE)
    two = asyncio.run(second.wake(wait_ms=0))
    assert isinstance(two.outcome, Success) and khive.sent == [SENT, SENT]
    assert run_row(store, one.id)["continues"] is None and run_row(store, two.id)["continues"] == one.id
    # its history is the first wake's message rows, and the model is shown them
    (rows,) = store.khive.exec_sync(f'lion.get(table="messages", id="{one.id}")')
    history = two.request.history
    assert [m["seq"] for m in rows["rows"]] == [e.seq for e in history] == [e.seq for e in one.record]
    assert [(e.kind, e.content) for e in history] == [(e.kind, e.content) for e in one.record]
    assert [e.content for e in list(two.record)[: len(history)]] == [e.content for e in one.record]
    assert "say hi back" in "\n".join(m.content for m in second.backend.seen[0])
    # no file at all was written: the store is the whole of what the next wake reads
    assert sorted(p.relative_to(d) for p in d.rglob("*")) == tree


def test_a_wake_with_no_last_run_continues_nothing(tmp_path, store):
    d = agent_dir(tmp_path)
    tree = sorted(p.relative_to(d) for p in d.rglob("*"))
    agent = on(store, FakeKhive(mail("lambda:a", "say hi back"), cwd=d), REPLY, DONE)
    run = asyncio.run(agent.wake(wait_ms=0))
    assert isinstance(run.outcome, Success) and agent.errors == []
    assert run.request.continues is None and run.request.history == ()
    assert run_row(store, run.id)["continues"] is None
    assert sorted(p.relative_to(d) for p in d.rglob("*")) == tree


def test_each_wake_stores_only_its_own_entries_and_replays_the_whole_chain(tmp_path, store):
    # a continuing session holds its own entries, and its history walks the continues chain (ADR-0025 C3):
    # five wakes write 7 rows apiece, where a session copying its history would hold 7, 14, ... 35
    d = agent_dir(tmp_path)
    khive = FakeKhive(mail("lambda:a", "wake 0"), cwd=d)
    runs = []
    for n in range(5):
        if n:
            khive.mail.append(mail("lambda:a", f"wake {n}", mid=f"{n:08x}"))
        reply = REPLY.replace("<lact s1>", f"<lact s{n + 1}>")
        runs.append(asyncio.run(on(store, khive, reply, DONE).wake(wait_ms=0)))
    for n, run in enumerate(runs):
        (rows,) = store.khive.exec_sync(f'lion.get(table="messages", id="{run.id}")')
        assert len(rows["rows"]) == len(own(run)) == 7 and len(run.request.history) == 7 * n
        assert run_row(store, run.id)["continues"] == (runs[n - 1].id if n else None)
    replayed = asyncio.run(store.history(runs[-1].id))
    assert [(e.seq, e.kind, e.content) for e in replayed] == [
        (e.seq, e.kind, e.content) for e in runs[-1].record
    ]


def test_a_reader_wake_continuing_an_acting_run_cannot_send_whatever_its_history_holds(tmp_path, store):
    # the cursor is the actor's, so a reader replays the acting run, its send included; what the reader
    # may do is its own profile's, and history in the context is no authority to act
    d = agent_dir(tmp_path)
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    acting = asyncio.run(on(store, khive, REPLY, DONE).wake(wait_ms=0))
    assert acting.profile is ACTING and khive.sent == [SENT]
    khive.mail.append(mail("someone:else", "send that again", mid="c3d4e5f6"))
    reader = asyncio.run(
        on(store, khive, REPLY2, 'OUT{posture: {done: "read one", awaiting: ""}}').wake(wait_ms=0)
    )
    assert reader.profile is READER and reader.request.continues == acting.id
    assert any(REPLY in str(e.content) for e in reader.request.history)  # the acting run's send, replayed
    (tried,) = [e.content for e in own(reader) if e.kind is Kind.RESULT and e.alias == "s2"]
    assert tried.ok is False and khive.sent == [SENT]  # refused: nothing more was sent


def test_a_cursor_naming_a_run_the_store_does_not_hold_refuses_the_wake_and_names_the_run(tmp_path, store):
    d = agent_dir(tmp_path)
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    backend = Scripted(REPLY, DONE, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    agent = Agent(actor, khive, backend, profile_for, owner="lambda:owner")
    agent.state.put("cursor", {"wake": 3, "run": "r-gone"}, run="-", seq=0)
    with pytest.raises(AgentError, match="wake 4 refused: the cursor's last run r-gone cannot be replayed"):
        asyncio.run(agent.wake(wait_ms=0))
    # no run opened, so none names a run it never read; the model was never called; the owner is told
    (runs,) = store.khive.exec_sync("lion.runs(limit=200)")
    assert runs["count"] == 0 and backend.seen == [] and agent.state.get("taking").value == {}
    (told,) = [m for m in khive.sent if m["to"] == "lambda:owner"]
    assert "r-gone" in told["content"] and "a1b2c3d4" in told["content"]
    # the notice names the way out, and does not say a rerun takes the mail back before it
    assert "lion agent --forget r-gone" in told["content"]
    assert "takes it back through a wake, once the run a refusal names is forgotten." in told["content"]


def test_forgetting_the_run_a_refusal_names_lets_the_refused_mail_rerun_on_a_new_chain(tmp_path, store):
    d = agent_dir(tmp_path)
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
        khive,
        Scripted(REPLY, DONE, khive=khive),
        profile_for,
        owner="lambda:owner",
    )
    agent.state.put("cursor", {"wake": 3, "run": "r-gone"}, run="-", seq=0)
    with pytest.raises(AgentError, match="refused"):
        asyncio.run(agent.wake(wait_ms=0))
    with pytest.raises(AgentError, match="refused"):  # until the owner acts, a rerun refuses too
        asyncio.run(agent.rerun(["a1b2c3d4"]))
    asyncio.run(agent.forget("r-gone"))
    run = asyncio.run(agent.rerun(["a1b2c3d4"]))
    assert isinstance(run.outcome, Success) and SENT in khive.sent
    assert run.request.continues is None and run.request.history == ()
    assert run_row(store, run.id)["continues"] is None
    # a later wake moves the cursor on; the forget stays readable, which run and when
    khive.mail.append(mail("lambda:a", "one more", mid="c3d4e5f6"))
    agent.backend = Scripted(REPLY2, DONE, khive=khive)
    later = asyncio.run(agent.wake(wait_ms=0))
    assert agent.state.get("cursor").value["run"] == later.id and later.request.continues == run.id
    (gone,) = agent.state.get("forgotten").value
    assert gone["run"] == "r-gone" and "not" in gone["why"] and gone["at"]


def test_forget_refuses_a_run_the_store_can_replay_and_any_run_but_the_cursors(store):
    khive = FakeKhive(mail("lambda:a", "say hi back"))
    agent = on(store, khive, REPLY, DONE)
    one = asyncio.run(agent.wake(wait_ms=0))
    cursor = agent.state.get("cursor").value
    with pytest.raises(AgentError, match=f"run {one.id} can be replayed: nothing forgotten"):
        asyncio.run(agent.forget(one.id))
    with pytest.raises(AgentError, match=f"the cursor's last run is {one.id}, not r-other"):
        asyncio.run(agent.forget("r-other"))
    assert agent.state.get("cursor").value == cursor and agent.state.get("forgotten") is None


def test_a_forget_retried_after_dying_between_its_writes_keeps_one_record(store, monkeypatch):
    """A process that dies after the forget is kept and before the cursor lets go leaves the cursor
    naming the run, so the owner forgets it again: the cursor lets go on the record already kept."""

    class Died(Exception):
        pass

    agent = on(store, FakeKhive(mail("lambda:a", "say hi back")), REPLY, DONE)
    now = [1_000_000.0]
    agent.clock = lambda: now[0]
    agent.state.put("cursor", {"wake": 3, "run": "r-gone"}, run="-", seq=0)
    put = agent.state.put

    def dies_at_the_cursor(key, value, **kw):
        if key == "cursor":
            raise Died("the process ended between the two writes")
        return put(key, value, **kw)

    monkeypatch.setattr(agent.state, "put", dies_at_the_cursor)
    with pytest.raises(Died):
        asyncio.run(agent.forget("r-gone"))
    monkeypatch.undo()
    (kept,) = agent.state.get("forgotten").value
    assert agent.state.get("cursor").value["run"] == "r-gone"
    now[0] += 60  # the retry is a later process, so a record of its own would carry a later stamp
    asyncio.run(agent.forget("r-gone"))
    assert agent.state.get("forgotten").value == [kept]
    cursor = agent.state.get("cursor").value
    assert cursor["run"] is None and cursor["continues"] is None and cursor["forgot"] == kept


def test_a_history_the_store_never_answered_propagates_and_is_neither_refused_nor_forgotten(
    store, monkeypatch
):
    """Only the store's answer that it does not hold the chain (`not_found`, `invalid`) is a refusal the
    owner forgets past: a call it never answered raises as raised, from the wake and from the forget,
    and leaves the cursor naming the run, nothing owed the owner and nothing forgotten."""
    agent = on(store, FakeKhive(mail("lambda:a", "say hi back")), REPLY, DONE)
    agent.state.put("cursor", {"wake": 3, "run": "r-gone"}, run="-", seq=0)

    async def unanswered(run_id):
        raise KhiveError(LOST)

    monkeypatch.setattr(store, "history", unanswered)
    with pytest.raises(KhiveError, match=LOST):
        asyncio.run(agent.wake(wait_ms=0))
    with pytest.raises(KhiveError, match=LOST):
        asyncio.run(agent.forget("r-gone"))
    assert agent.state.get("cursor").value == {"wake": 3, "run": "r-gone"}
    assert agent.state.get("unreported").value == [] and agent.state.get("forgotten") is None


def test_the_batch_of_a_wake_whose_history_went_unanswered_is_owed_and_taken_again(
    tmp_path, store, monkeypatch
):
    """The propagated error leaves the batch read and held under `taking`, never marked handled: the next
    wake on mail tells the owner it is owed, and the owner's rerun takes it through a wake on the chain."""
    d = agent_dir(tmp_path)
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    agent = Agent(actor, khive, Scripted(REPLY, DONE, khive=khive), profile_for, owner="lambda:owner")
    first = asyncio.run(agent.wake(wait_ms=0))
    khive.mail.append(mail("lambda:a", "one more", mid="c3d4e5f6"))

    async def unanswered(run_id):
        raise KhiveError(LOST)

    monkeypatch.setattr(store, "history", unanswered)
    with pytest.raises(KhiveError, match=LOST):
        asyncio.run(agent.wake(wait_ms=0))
    monkeypatch.undo()
    assert agent.state.get("taking").value == {"mail": ["c3d4e5f6"]}
    assert agent.state.get("cursor").value["run"] == first.id
    khive.mail.append(mail("lambda:a", "and another", mid="e5f6a7b8"))
    agent.backend = Scripted(REPLY2, DONE, khive=khive)
    second = asyncio.run(agent.wake(wait_ms=0))
    (told,) = [m for m in khive.sent if m["to"] == "lambda:owner"]
    assert "c3d4e5f6" in told["content"] and "marked read and unanswered" in told["content"]
    assert second.request.continues == first.id and agent.state.get("taking").value == {}
    agent.backend = Scripted(REPLY.replace("<lact s1>", "<lact s3>"), DONE, khive=khive)
    again = asyncio.run(agent.rerun(["c3d4e5f6"]))
    assert isinstance(again.outcome, Success) and again.request.continues == second.id


def _a_wake_left_c3d4e5f6_under_taking(tmp_path, store, monkeypatch):
    d = agent_dir(tmp_path)
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    agent = Agent(actor, khive, Scripted(REPLY, DONE, khive=khive), profile_for, owner="lambda:owner")
    first = asyncio.run(agent.wake(wait_ms=0))
    khive.mail.append(mail("lambda:a", "one more", mid="c3d4e5f6"))

    async def unanswered(run_id):
        raise KhiveError(LOST)

    monkeypatch.setattr(store, "history", unanswered)
    with pytest.raises(KhiveError, match=LOST):
        asyncio.run(agent.wake(wait_ms=0))
    monkeypatch.undo()
    assert agent.state.get("taking").value == {"mail": ["c3d4e5f6"]}
    return agent, khive, first


def test_a_rerun_of_other_mail_tells_the_owner_of_the_batch_a_wake_left_under_taking(
    tmp_path, store, monkeypatch
):
    """The rerun's run lets go of `taking` once it enters, so the batch a wake left there is owed first:
    the owner hears of it, and the rerun's own mail runs."""
    agent, khive, first = _a_wake_left_c3d4e5f6_under_taking(tmp_path, store, monkeypatch)
    agent.backend = Scripted(REPLY2, DONE, khive=khive)
    again = asyncio.run(agent.rerun(["a1b2c3d4"]))
    assert isinstance(again.outcome, Success) and again.request.continues == first.id
    (told,) = [m for m in khive.sent if m["to"] == "lambda:owner"]
    assert "c3d4e5f6" in told["content"] and "marked read and unanswered" in told["content"]
    assert agent.state.get("taking").value == {} and agent.state.get("unreported").value == []


def test_a_rerun_of_the_batch_under_taking_does_not_owe_it_again(tmp_path, store, monkeypatch):
    """The batch a rerun takes back is answered by that rerun, so it is not also told as owed."""
    agent, khive, first = _a_wake_left_c3d4e5f6_under_taking(tmp_path, store, monkeypatch)
    agent.backend = Scripted(REPLY2, DONE, khive=khive)
    again = asyncio.run(agent.rerun(["c3d4e5f6"]))
    assert isinstance(again.outcome, Success) and again.request.continues == first.id
    assert [m for m in khive.sent if m["to"] == "lambda:owner"] == []
    assert agent.state.get("taking").value == {}


def test_a_rerun_whose_history_went_unanswered_leaves_its_batch_under_taking(tmp_path, store, monkeypatch):
    """A rerun raises as a wake does, and its mail is held under `taking` for the next wake to tell."""
    d = agent_dir(tmp_path)
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    agent = Agent(actor, khive, Scripted(REPLY, DONE, khive=khive), profile_for, owner="lambda:owner")
    asyncio.run(agent.wake(wait_ms=0))

    async def unanswered(run_id):
        raise KhiveError(LOST)

    monkeypatch.setattr(store, "history", unanswered)
    with pytest.raises(KhiveError, match=LOST):
        asyncio.run(agent.rerun(["a1b2c3d4"]))
    monkeypatch.undo()
    assert agent.state.get("taking").value == {"mail": ["a1b2c3d4"]}
    khive.mail.append(mail("lambda:a", "and another", mid="e5f6a7b8"))
    agent.backend = Scripted(REPLY2, DONE, khive=khive)
    asyncio.run(agent.wake(wait_ms=0))
    (told,) = [m for m in khive.sent if m["to"] == "lambda:owner"]
    assert "a1b2c3d4" in told["content"] and "marked read and unanswered" in told["content"]


def test_a_wake_whose_run_raised_leaves_the_run_it_continued_for_the_next_wake(store):
    khive = FakeKhive(mail("lambda:a", "say hi back"))
    agent = on(store, khive, REPLY, DONE)
    one = asyncio.run(agent.wake(wait_ms=0))
    khive.mail.append(mail("lambda:a", "hi", mid="c3d4e5f6"))
    agent.backend = Boom(khive=khive)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(agent.wake(wait_ms=0))
    cursor = agent.state.get("cursor").value
    assert cursor["run"] is None and cursor["continues"] == one.id  # no run returned: the last stands
    khive.mail.append(mail("lambda:a", "again", mid="e5f6a7b8"))
    agent.backend = Scripted(REPLY2, DONE, khive=khive)
    three = asyncio.run(agent.wake(wait_ms=0))
    assert three.request.continues == one.id and run_row(store, three.id)["continues"] == one.id


def test_rerun_finds_read_mail_by_the_short_id_the_cursor_carries():
    """The box answers with full ids; the cursor and the owner's notice name the short one."""
    m = mail("lambda:a", "say hi back")
    m["id"] = m["full_id"]
    khive = FakeKhive(m)
    agent = build(khive, REPLY, DONE, REPLY2, DONE)
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
    again = REPLY.replace("<lact s1>", "<lact s2>")
    backend = Scripted(REPLY, "sent", again, "done", khive=khive)
    agent = build(khive, continuous=True, poll_ms=20, owner="lambda:owner")
    agent.backend = backend

    def until() -> bool:
        if len(lines) == 1 and later not in khive.taken:
            khive.mail.append(later)
        return len(lines) >= 3

    asyncio.run(agent.serve(until=until, log=lines.append))
    (run,) = agent.runs
    assert lines[1] == "wake 1 failed: RuntimeError: AdmissionDeadlineExpired"
    assert lines[2].startswith("wake 2: Quiet after 2 turns, 1 mail, 1 sent; 2 model call(s)")
    spend = agent.state.get("spend").value
    assert spend["runs"] == 2 and spend["mail"] == 2 and spend["calls"] == 4  # nothing booked twice
    owed = [m for m in khive.sent if m["to"] == "lambda:owner"]
    assert len(owed) == 1 and "c3d4e5f6" in owed[0]["content"]  # the take that died is reported


def test_mail_for_another_profile_ends_the_conversation_and_runs_fresh(tmp_path):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    stranger = mail("lambda:stranger", "read this", mid="c3d4e5f6")
    lines: list[str] = []
    agent = build(khive, REPLY, "answered hello", DONE, continuous=True, poll_ms=20)

    def until() -> bool:
        if len(lines) == 1 and stranger not in khive.taken:
            khive.mail.append(stranger)
        return len(lines) >= 2

    asyncio.run(agent.serve(until=until, log=lines.append))
    first, second = agent.runs
    assert first.profile is ACTING and second.profile is READER and khive.sent == [SENT]
    assert "read this" not in "".join(str(e.content) for e in first.record)
    assert second.request.continues == first.id  # a new run, continuing the actor's last
    assert lines[1].startswith("wake 2: Success after 1 turns, 1 mail, 0 sent")


def test_a_profile_changed_under_the_same_name_ends_the_conversation_and_runs_fresh(tmp_path, store):
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    later = mail("lambda:a", "read this", mid="c3d4e5f6")
    fewer = Profile("acting", privileges=frozenset(), system="answer on the thread")
    lines: list[str] = []

    def profile_for(batch: list[dict]) -> Profile:
        return fewer if any(m["id"] == "c3d4e5f6" for m in batch) else ACTING

    backend = Scripted(REPLY, "answered hello", DONE, khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
        khive,
        backend,
        profile_for,
        continuous=True,
        poll_ms=20,
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
    """A turn of commands never ends the batch: their results, a refusal among them, come back next
    turn, and the turn of plain text after them ends it."""
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    lines: list[str] = []
    slip = REPLY + "<lact x1>frobnicate()</lact>"  # the send lands, the second command is unknown
    agent = build(khive, slip, "sent once; the second was a slip", continuous=True, poll_ms=20)
    asyncio.run(agent.serve(until=lambda: len(lines) >= 1, log=lines.append))
    assert lines[0].startswith("wake 1: Quiet after 2 turns, 1 mail, 1 sent; 2 model call(s)")
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
    first = next(e for e in own(run) if e.kind is Kind.INPUT).content
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
    store = fresh_store()
    state = store.notes("agent")
    for n in (1, 2, 3):  # one Agent per process; the store, its rows and its notes, is what survives
        khive = FakeKhive(mail("lambda:a", "hi", mid=f"m{n}"))
        agent = build(khive, REPLY.replace("<lact s1>", f"<lact s{n}>"), DONE, state=state, store=store)
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
    store = fresh_store()
    state = store.notes("agent")
    for n in (1, 2, 3):
        khive = FakeKhive(mail("lambda:a", "again", mid=f"m{n}"))
        reply = REPLY.replace("<lact s1>", f"<lact s{n}>")
        agent = build(khive, reply, DONE, state=state, store=store, budget=Budget(hops_per_thread=2))
        run = asyncio.run(agent.wake(wait_ms=0))
        result = next(e.content for e in own(run) if e.kind is Kind.RESULT and e.content.spec == "send")
        if n <= 2:
            assert result.ok and khive.sent == [SENT]
        else:
            assert not result.ok and "hop limit: 2 messages already sent" in str(result.error)
            assert khive.sent == []
    assert state.get("threads").value == {THREAD: 2}


def test_the_wake_cap_holds_the_agent_for_the_rest_of_the_hour_and_mail_waits():
    now = [1_000_000.0]
    khive = FakeKhive(mail("lambda:a", "one"))
    agent = build(khive, REPLY, DONE, REPLY2, DONE, budget=Budget(wakes_per_hour=1), clock=lambda: now[0])
    assert asyncio.run(agent.wake(wait_ms=0)) is not None
    khive.mail.append(mail("lambda:a", "two", mid="m2"))  # arrives after; a batch takes all that waits
    polls = khive.log.count("inbox")
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert agent.held.startswith("1 wakes in the last hour (cap 1)") and khive.log.count("inbox") == polls
    now[0] += 3601
    assert asyncio.run(agent.wake(wait_ms=0)) is not None and agent.held is None and agent.wakes == 2


def test_the_cost_cap_holds_the_agent_for_the_day_from_the_backends_own_cost_figures(store):
    now = [1_000_000.0]
    khive = FakeKhive()
    # each wake names its reply afresh: it continues the last run and replays its aliases
    turns = [t for n in range(1, 5) for t in (REPLY.replace("<lact s1>", f"<lact s{n}>"), DONE)]
    backend = Scripted(*turns, cost=0.3, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
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


def test_a_call_of_unknown_or_incomplete_cost_adds_nothing_to_the_cost_and_the_readers_mark_the_sum(
    tmp_path, store
):
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

    state = fresh_store().notes("agent")
    backend = Billed(DONE, DONE, DONE, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
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
    day, _, last = stats(tmp_path, noted(state)).splitlines()
    assert "0 out, $0.7000 (partial: 2 call(s) of unknown cost); 3 wake(s) with 3 mail;" in day
    assert "0 out, $0.4000 (partial: 1 call(s) of unknown cost); 0 idle poll(s) before it" in last


class _Lying(float):  # holds a negative figure and says it is zero or more
    def __ge__(self, other):
        return True


@pytest.mark.parametrize(
    "figure",
    [-0.3, float("nan"), "0.3", True, _Lying(-0.3)],
    ids=["negative", "nan", "str", "bool", "subclass"],
)
def test_a_malformed_cost_figure_adds_nothing_and_counts_as_a_call_of_unknown_cost(store, figure):
    # a figure the run would not sum (ADR-0003/S7) is no figure (ADR-0013/C5): it never lowers the
    # day's spend, so the cap still holds once the figures that serve reach it
    now = [1_000_000.0]
    khive = FakeKhive()
    envelopes = [{"total_cost_usd": 0.3}, {"total_cost_usd": figure}, {"total_cost_usd": 0.3}]

    class Billed(Scripted):
        async def __call__(self, messages):
            text = await super().__call__(messages)
            self.calls[-1] = envelopes.pop(0)
            return text

    backend = Billed(DONE, DONE, DONE, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    agent = Agent(actor, khive, backend, profile_for, budget=Budget(cost_per_day=0.5), clock=lambda: now[0])
    rows = []
    for n in range(3):
        khive.mail.append(mail("lambda:a", "go", mid=f"m{n}"))
        assert asyncio.run(agent.wake(wait_ms=0)) is not None
        rows.append({k: agent.state.get("spend").value[k] for k in ("cost", "unknown")})
    assert rows == [
        {"cost": 0.3, "unknown": 0},
        {"cost": 0.3, "unknown": 1},
        {"cost": pytest.approx(0.6), "unknown": 1},
    ]
    khive.mail.append(mail("lambda:a", "go", mid="m3"))
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert agent.held == "$0.6000 spent today (partial: 1 call(s) of unknown cost) (cap $0.5); next day"


def test_a_days_spend_past_a_floats_range_is_kept_as_the_largest_float(store):
    # two figures that each serve can sum past what a float holds: the row keeps the largest float, past
    # any cap, where an infinite sum could not be written at all
    now = [1_000_000.0]
    khive = FakeKhive()
    envelopes = [{"total_cost_usd": 1.7e308}, {"total_cost_usd": 1.7e308}]

    class Billed(Scripted):
        async def __call__(self, messages):
            text = await super().__call__(messages)
            self.calls[-1] = envelopes.pop(0)
            return text

    backend = Billed(DONE, DONE, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    budget = Budget(cost_per_day=math.inf)
    agent = Agent(actor, khive, backend, profile_for, budget=budget, clock=lambda: now[0])
    for n in range(2):
        khive.mail.append(mail("lambda:a", "go", mid=f"m{n}"))
        assert asyncio.run(agent.wake(wait_ms=0)) is not None
    assert agent.state.get("spend").value["cost"] == sys.float_info.max


def test_one_wakes_calls_summing_past_a_floats_range_still_write_its_cursor(store):
    # the wake's own sum is read for the cursor as well as the day: past a float's range it is the
    # largest float there too, where an infinite one would fail the cursor's write after the calls
    khive = FakeKhive(mail("lambda:a", "go", mid="m0"))
    backend = Scripted(REPLY, DONE, cost=1.7e308, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    budget = Budget(cost_per_day=1.75e308)
    agent = Agent(actor, khive, backend, profile_for, budget=budget, clock=lambda: 1_000_000.0)
    assert asyncio.run(agent.wake(wait_ms=0)) is not None
    assert len(backend.calls) == 2
    assert agent.state.get("cursor").value["tokens"]["cost"] == sys.float_info.max
    assert agent.state.get("spend").value["cost"] == sys.float_info.max
    assert agent.hold() is not None


ROWS = {
    "negative": {"cost": -0.6, "unknown": 0},  # an earlier version summed the envelopes as they came
    "str": {"cost": "0.6", "unknown": 0},
    "bool": {"cost": True, "unknown": 0},
    "no-cost": {"unknown": 0},
    "no-day": {"cost": 0.6, "unknown": 0, "day": None},
    "day-not-str": {"cost": 0.6, "unknown": 0, "day": 20261006},
    "not-a-row": None,
}


@pytest.mark.parametrize("kind", list(ROWS))
def test_a_day_row_the_agent_would_not_have_written_reads_as_none_spent_and_partial(tmp_path, store, kind):
    # never room against a wake that serves: none spent, and the day partial to its end, in `stats` too
    from hub.agent.agent import _day

    now = 1_000_000.0
    khive = FakeKhive(mail("lambda:a", "go", mid="m0"))
    state = store.notes("agent")
    if ROWS[kind] is None:
        state.put("spend", [_day(now), 0.6, 0], run="-", seq=0)
    else:
        row = {"day": _day(now), "calls": 2, "wakes": [], "runs": 1, **ROWS[kind]}
        state.put("spend", {k: v for k, v in row.items() if v is not None}, run="-", seq=0)
    backend = Scripted(REPLY, DONE, cost=0.3, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    agent = Agent(
        actor, khive, backend, profile_for, state=state, budget=Budget(cost_per_day=0.5), clock=lambda: now
    )
    assert agent.hold() is None  # none spent: the day's row lends no room and takes none
    uncounted = "(partial: calls of unknown cost before this count went uncounted)"
    assert f" out, $0.0000 {uncounted}; " in stats(tmp_path, noted(state)).splitlines()[0]
    assert asyncio.run(agent.wake(wait_ms=0)) is not None
    row = agent.state.get("spend").value
    assert (row["cost"], row["unknown"]) == (pytest.approx(0.6), None)
    assert agent.hold() == (
        "$0.6000 spent today (partial: calls of unknown cost before this count went uncounted)"
        " (cap $0.5); next day"
    )


@pytest.mark.parametrize("count", ["2", True, -1, 1.0], ids=["str", "bool", "negative", "float"])
def test_a_day_row_whose_count_is_no_number_of_calls_keeps_its_dollars_and_reads_as_partial(
    tmp_path, store, count
):
    from hub.agent.agent import _day

    now = 1_000_000.0
    khive = FakeKhive(mail("lambda:a", "go", mid="m0"))
    state = store.notes("agent")
    row = {"day": _day(now), "cost": 0.1, "unknown": count, "calls": 2, "wakes": [], "runs": 1}
    state.put("spend", row, run="-", seq=0)
    backend = Scripted(REPLY, DONE, cost=0.3, khive=khive)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
    agent = Agent(
        actor, khive, backend, profile_for, state=state, budget=Budget(cost_per_day=0.5), clock=lambda: now
    )
    assert agent.hold() is None
    uncounted = "(partial: calls of unknown cost before this count went uncounted)"
    assert f" out, $0.1000 {uncounted}; " in stats(tmp_path, noted(state)).splitlines()[0]
    assert asyncio.run(agent.wake(wait_ms=0)) is not None  # the booking takes the count as unknown
    assert len(backend.calls) == 2
    row = agent.state.get("spend").value
    assert (row["cost"], row["unknown"]) == (pytest.approx(0.7), None)
    assert agent.hold() == (
        "$0.7000 spent today (partial: calls of unknown cost before this count went uncounted)"
        " (cap $0.5); next day"
    )


def test_a_same_day_row_from_before_the_count_reads_as_partial_by_an_unknown_number_to_its_end(
    tmp_path, store
):
    from hub.agent.agent import _day, _line

    now = [1_000_000.0]
    khive = FakeKhive()
    envelopes = [
        {"total_cost_usd": 0.3},
        {"total_cost_usd": None},
        {"total_cost_usd": 0.1},
        {"total_cost_usd": 0.2},
    ]

    class Billed(Scripted):
        async def __call__(self, messages):
            text = await super().__call__(messages)
            self.calls[-1] = envelopes.pop(0)
            return text

    state = fresh_store().notes("agent")
    # the day's row as the version before the count wrote it: dollars, no `unknown` key
    state.put("spend", {"day": _day(now[0]), "cost": 0.5, "calls": 2, "wakes": [], "runs": 1}, run="-", seq=0)
    backend = Billed(DONE, DONE, DONE, DONE, khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
        khive,
        backend,
        profile_for,
        state=state,
        budget=Budget(cost_per_day=0.85),
        clock=lambda: now[0],
    )
    uncounted = "(partial: calls of unknown cost before this count went uncounted)"
    rows, lines = [], []
    for n in range(3):
        khive.mail.append(mail("lambda:a", "go", mid=f"m{n}"))
        assert asyncio.run(agent.wake(wait_ms=0)) is not None
        rows.append({k: agent.state.get("spend").value[k] for k in ("calls", "cost", "unknown")})
        lines.append(_line(agent.state.get("cursor").value))
    # the count stays None however many calls land after it: the ones before cannot be counted now
    assert rows == [
        {"calls": 3, "cost": 0.8, "unknown": None},
        {"calls": 4, "cost": 0.8, "unknown": None},
        {"calls": 5, "cost": pytest.approx(0.9), "unknown": None},
    ]
    assert "$0.3000; 0 idle poll(s)" in lines[0]  # the wake's own line counts its own calls
    assert "$0.0000 (partial: 1 call(s) of unknown cost); 0 idle poll(s)" in lines[1]
    assert "$0.1000; 0 idle poll(s)" in lines[2]
    khive.mail.append(mail("lambda:a", "go", mid="m3"))
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert agent.held == f"$0.9000 spent today {uncounted} (cap $0.85); next day"
    day = stats(tmp_path, noted(state)).splitlines()[0]
    assert f"$0.9000 {uncounted}; 4 wake(s)" in day
    now[0] += 86_400  # the next day starts counted
    khive.mail.append(mail("lambda:a", "go", mid="m4"))
    assert asyncio.run(agent.wake(wait_ms=0)) is not None
    assert {k: agent.state.get("spend").value[k] for k in ("calls", "cost", "unknown")} == {
        "calls": 1,
        "cost": 0.2,
        "unknown": 0,
    }
    assert "partial" not in stats(tmp_path, noted(state)).splitlines()[0]
    # a row from before the count that is already at the cap is held before any wake writes it: the
    # hold and the stats line still say it is uncounted
    state.put("spend", {"day": _day(now[0]), "cost": 0.9, "calls": 2, "wakes": [], "runs": 1}, run="-", seq=9)
    khive.mail.append(mail("lambda:a", "go", mid="m5"))
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    assert agent.held == f"$0.9000 spent today {uncounted} (cap $0.85); next day"
    assert f"$0.9000 {uncounted}; 1 wake(s)" in stats(tmp_path, noted(state)).splitlines()[0]


def test_stats_prints_the_spend_row_the_last_wake_and_the_ledgers_as_this_text(tmp_path, kkernel_stub):
    assert stats(tmp_path) == f"notes unread at {tmp_path}: chores.toml names no actor"
    (tmp_path / "chores.toml").write_text('actor = "lambda:x:stats"\n')
    assert stats(tmp_path) == (
        f"notes unread at {tmp_path}: {tmp_path} holds no .khive/config.toml; "
        "khive would resolve it as 'local'"
    )
    store = resident_store(tmp_path)
    assert stats(tmp_path) == f"no notes at {tmp_path}: the agent has not run from this directory"
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
        "turns": 2,
        "mail": ["a1b2c3d4", "b2c3d4e5"],
        "sent": 1,
        "tokens": {"calls": 2, "in": 5000, "out": 300, "cost": 0.004, "unknown": 0, "context": 2400},
        "idle_polls": 2,
    }
    store.put("spend", spend, run="-", seq=0)
    store.put("cursor", cursor, run="-", seq=0)
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
        "last wake 3 at 2026-09-22 11:09:15: Success after 2 turn(s), 2 mail, 1 sent; 2 call(s), 5000 in, "
        "300 out, $0.0040; 2 idle poll(s) before it\n"
        "ledger: 3 check(s), 2 answered, 1 not\n"
        "desk: 2 of the owner's message(s) read today, 1 answered, 1 left"
    )
    store.put("spend", {**spend, "mail": 0}, run="-", seq=1)
    assert stats(tmp_path).splitlines()[1] == "per handled message: no mail handled today"
    # a row whose day is no date reads as partial and places no desk row on that day, never a crash
    store.put("spend", {**spend, "day": 20260922}, run="-", seq=2)
    out = stats(tmp_path).splitlines()
    assert out[0].startswith("day 20260922: 5 model call(s), 12000 in, 800 out, $0.0000 (partial: ")
    assert out[-1] == "desk: not counted, the spend row's day 20260922 is not a date"
    store.put("spend", {k: v for k, v in spend.items() if k != "day"}, run="-", seq=3)  # a row with no day
    out = stats(tmp_path).splitlines()
    assert out[0].startswith("day None: 5 model call(s), 12000 in, 800 out, $0.0000 (partial: ")
    assert out[-1] == "desk: not counted, the spend row's day None is not a date"
    # a desk row with no date, or one that is not a string, is on no day, never a crash
    store.put("spend", spend, run="-", seq=4)
    undated = [*desk, {"at": 20260922, "class": "answered"}, {"class": "routed"}, {"at": None}]
    (tmp_path / "desk.jsonl").write_text("\n".join(json.dumps(r) for r in undated) + "\n")
    assert stats(tmp_path).splitlines()[-1] == (
        "desk: 2 of the owner's message(s) read today, 1 answered, 1 left, 3 row(s) with no date not counted"
    )
    # nor is a row that is no object, or an `at` that only starts with the day; a timestamp with a zone is
    junk = [[], None, "x", 7, {"at": "2026-09-22junk"}, {"at": "2026-09-220 12:00:00"}]
    zoned = {"at": "2026-09-22T12:00:00.5Z", "class": "answered"}
    basic = {"at": "20260922T120000", "class": "routed"}  # the same day, written without its dashes
    (tmp_path / "desk.jsonl").write_text(
        "\n".join(json.dumps(r) for r in (*desk, *junk, zoned, basic)) + "\n"
    )
    assert stats(tmp_path).splitlines()[-1] == (
        "desk: 4 of the owner's message(s) read today, 2 answered, 2 left, 6 row(s) with no date not counted"
    )
    (tmp_path / "ledger.jsonl").write_text('{"answered": true}\n[]\nnull\n"x"\n7\n')
    assert stats(tmp_path).splitlines()[-2] == "ledger: 5 check(s), 1 answered, 4 not"
    # counts that are no numbers are not divided: the line says so
    odds = ({"mail": "1"}, {"in": "12000"}, {"out": None}, {"mail": True}, {"mail": None}, {"mail": 0.0})
    for odd in (*odds, {"mail": -1}, {"in": 1.5}):
        store.put("spend", {**spend, **odd}, run="-", seq=5)
        assert stats(tmp_path).splitlines()[1] == (
            "per handled message: not computed, the spend row's counts are not all whole numbers"
        )
    # a whole number past a float's range still divides, and a half rounds as the line always did
    store.put("spend", {**spend, "in": 10**400, "out": 0, "mail": 1}, run="-", seq=6)
    assert stats(tmp_path).splitlines()[1] == f"per handled message: {10**400} tokens"
    store.put("spend", {**spend, "in": 5, "out": 0, "mail": 2}, run="-", seq=7)
    assert stats(tmp_path).splitlines()[1] == "per handled message: 2 tokens"
    store.put(
        "spend", {k: v for k, v in spend.items() if k != "mail"}, run="-", seq=8
    )  # absent: none handled
    assert stats(tmp_path).splitlines()[1] == "per handled message: no mail handled today"


def test_a_failed_session_cli_call_is_counted_in_the_days_spend_row_and_still_ends_the_run(
    monkeypatch, store
):
    printed = {"total_cost_usd": 0.5, "usage": {"input_tokens": 9000, "output_tokens": 700}}

    async def capped(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        raise claude_code.ClaudeCodeError("claude reported error_max_turns: cap", printed)

    monkeypatch.setattr(claude_code, "_exec", capped)
    khive = FakeKhive(mail("lambda:a", "go"))
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
        khive,
        ClaudeCodeSession(cli="/bin/claude"),
        profile_for,
    )
    with pytest.raises(claude_code.ClaudeCodeError, match="error_max_turns"):
        asyncio.run(agent.wake(wait_ms=0))
    spend = agent.state.get("spend").value
    assert spend["calls"] == 1 and spend["cost"] == pytest.approx(0.5)
    assert spend["in"] == 9000 and spend["out"] == 700
    assert agent.state.get("cursor").value["outcome"].startswith("error ClaudeCodeError: ")


def test_a_wake_killed_after_its_mark_read_is_reported_to_the_owner_and_rerun_takes_it_back(store):
    state = fresh_store().notes("agent")
    khive = FakeKhive(mail("lambda:a", "hi"))

    class Died(Scripted):
        async def __call__(self, messages):
            raise SystemExit(1)  # the process died mid-run: nothing after the mark-read ran

    actor = Actor("echo", privileges={"comm.send"}, store=store)
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
    agent.backend = Scripted(REPLY2, DONE, khive=khive)
    run = asyncio.run(agent.rerun(["a1b2c3d4-0000-0000-0000-000000000000"]))
    assert isinstance(run.outcome, Success) and khive.sent[2] == SENT and agent.wakes == 3
    assert khive.log.count("read") == 2  # rerun never touches the unread view
    with pytest.raises(AgentError, match="not in the read box"):
        asyncio.run(agent.rerun(["nope"]))


def test_a_wake_whose_run_raises_tells_the_owner_which_batch_went_unanswered(store):
    khive = FakeKhive(mail("lambda:a", "hi"))

    class Boom(Scripted):
        async def __call__(self, messages):
            raise RuntimeError("boom")

    actor = Actor("echo", privileges={"comm.send"}, store=store)
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
            asyncio.run(build(khive, state=fresh_store().notes("agent")).serve(until=lambda: True))
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)
        holder.stdout.close()
    (tmp_path / "agent.pid").write_text("999999999")  # a stale file nobody holds does not
    khive.mail.append(mail("lambda:a", "hi"))
    state = fresh_store().notes("agent")
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
    state = fresh_store().notes("agent")
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
    state = fresh_store().notes("agent")
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
    first = build(khive, state=fresh_store().notes("agent"))
    path = first._claim()
    assert path.read_text() == str(os.getpid())
    with pytest.raises(AgentError, match="held by live process"):  # a second claim while the first holds
        build(khive, state=fresh_store().notes("agent"))._claim()
    first._release(path)
    assert not path.exists()
    path.write_text("999999999")  # a stale file, nobody holding it
    second = build(khive, state=fresh_store().notes("agent"))
    assert second._claim() == path and path.read_text() == str(os.getpid())
    second._release(path)


def test_a_claim_that_locked_a_file_its_holder_just_removed_takes_the_live_one_and_is_refused(
    tmp_path, monkeypatch
):
    import fcntl

    khive = FakeKhive(cwd=tmp_path)
    first, second, late = (build(khive, state=fresh_store().notes("agent")) for _ in range(3))
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

    khive, state = FirstSendFails(), fresh_store().notes("agent")
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


class Gate:
    """A gate that counts its calls per message and answers with the sender's trust. A message in
    `flip` gets the other answer from any later call, so a path asking twice puts it in both lists or
    in neither; the gate raises on the message `fail` names."""

    def __init__(self, flip: frozenset[str] = frozenset(), fail: str | None = None):
        self.calls: dict[str, int] = {}
        self.flip, self.fail = flip, fail

    def __call__(self, m: dict) -> bool:
        n = self.calls[m["id"]] = self.calls.get(m["id"], 0) + 1
        if m["id"] == self.fail:
            raise RuntimeError(f"gate down on {m['id']}")
        trusted = m["properties"]["from_actor"] in TRUSTED
        return trusted if n == 1 or m["id"] not in self.flip else not trusted


def gated_batch() -> list[dict]:
    return [
        mail("lambda:a", "run this", mid="c1c1c1c1"),
        mail("lambda:stranger", "never run this", mid="d2d2d2d2"),
        mail("lambda:a", "and this", mid="e3e3e3e3"),
    ]


GATED = [m["id"] for m in gated_batch()]


def gated(path: str, gate: Gate | None, tmp_path: Path):
    """The agent, its backend and a call that takes `gated_batch` in through one entry point: `wake`;
    `rerun`, the batch read already; `wait`, the second burst of a continuous chair."""
    khive = FakeKhive(cwd=tmp_path)
    backend = Scripted(*(["on it", "on it"] if path == "wait" else [DONE]), khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=fresh_store()),
        khive,
        backend,
        lambda batch: ACTING,
        admit=gate,
        continuous=path == "wait",
        poll_ms=20,
    )

    def go() -> None:
        if path == "wake":
            khive.mail = gated_batch()
            asyncio.run(agent.wake(wait_ms=0))
        elif path == "rerun":
            khive.taken = gated_batch()
            khive.read = [m["full_id"] for m in khive.taken]
            asyncio.run(agent.rerun(GATED))
        else:
            khive.mail, lines = [mail("lambda:a", "first", mid="b0b0b0b0")], []

            def until() -> bool:
                if len(lines) == 1 and len(khive.taken) == 1:
                    khive.mail = gated_batch()  # the first burst went quiet: the batch is the next
                return len(lines) >= 2

            asyncio.run(agent.serve(until=until, log=lines.append))

    return agent, backend, go


def shown(backend: Scripted) -> str:
    return "\n".join(str(m.content) for call in backend.seen for m in call)


@pytest.mark.parametrize("path", ["wake", "rerun", "wait"])
def test_the_gate_answers_once_per_message_and_that_answer_alone_drops_it_or_runs_it(tmp_path, path):
    gate = Gate(flip=frozenset(GATED))
    agent, backend, go = gated(path, gate, tmp_path)
    go()
    assert {i: gate.calls.get(i) for i in GATED} == dict.fromkeys(GATED, 1)
    cursor = agent.state.get("cursor").value
    ran, dropped = cursor["mail"], [d["id"] for d in cursor["dropped"]]
    assert ran == ["c1c1c1c1", "e3e3e3e3"] and dropped == ["d2d2d2d2"]
    assert not set(ran) & set(dropped) and sorted(ran + dropped) == sorted(GATED)
    assert "run this" in shown(backend) and "never run this" not in shown(backend)


@pytest.mark.parametrize("path", ["wake", "rerun", "wait"])
def test_with_no_gate_every_message_runs_and_none_is_dropped(tmp_path, path):
    agent, backend, go = gated(path, None, tmp_path)
    go()
    cursor = agent.state.get("cursor").value
    assert cursor["mail"] == GATED and "dropped" not in cursor and "never run this" in shown(backend)


@pytest.mark.parametrize("path", ["wake", "rerun", "wait"])
def test_a_gate_that_raises_runs_none_of_the_batch_records_none_dropped_and_leaves_it_taken(tmp_path, path):
    """The gate answered the first two and raised on the third: no answer stands, nothing runs, no
    message is recorded as dropped, and mail a wake took stays under `taking` for recovery."""
    gate = Gate(fail="e3e3e3e3")
    agent, backend, go = gated(path, gate, tmp_path)
    if path == "wait":
        go()  # the run outlives the failed take; the cursor is the first burst's
        assert "RuntimeError: gate down on e3e3e3e3" in agent.errors and len(backend.calls) == 1
        cursor = agent.state.get("cursor").value
        assert cursor["mail"] == ["b0b0b0b0"] and "dropped" not in cursor
    else:
        with pytest.raises(RuntimeError, match="gate down on e3e3e3e3"):
            go()
        assert backend.calls == [] and agent.state.get("cursor") is None
    assert {i: gate.calls.get(i) for i in GATED} == dict.fromkeys(GATED, 1)
    assert "never run this" not in shown(backend) and "run this" not in shown(backend)
    taking = agent.state.get("taking")
    assert (taking and taking.value) == (None if path == "rerun" else {"mail": GATED})


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


def test_raise_then_failed_tell_survives_restart(store):
    """A wake whose run raises owes its batch to the owner (ADR-0013/C3); the telling was dropped, so
    the next start tells it, with the error and the ids."""
    khive, state = FailingSends(mail("lambda:a", "hi"), fail=1), fresh_store().notes("agent")
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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


def test_new_failure_preserves_older_owed_batch(store):
    """A batch already owed stays owed while a new failure is recorded beside it; both are told once the
    transport comes back."""
    khive, state = FailingSends(mail("lambda:a", "hi", mid="bbbbbbbb"), fail=2), fresh_store().notes("agent")
    state.put("unreported", [{"why": "an earlier wake died mid-run", "mail": ["aaaaaaaa"]}], run="-", seq=0)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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


@pytest.mark.parametrize(
    ("chair", "order"), [("", ["ticks", "rerun"]), ("\n[chair]\n", ["rerun"])], ids=["chores", "chair"]
)
def test_the_rerun_flag_takes_read_mail_through_one_wake_by_id(tmp_path, monkeypatch, chair, order):
    """--rerun ID...: the batch a killed turn left read and unanswered, back through Agent.rerun, after
    the missing ticks are made as a start makes them, and none for a chair."""
    from types import SimpleNamespace

    seen: dict = {"order": []}

    class Ticks:
        async def ensure_ticks(self):
            seen["order"].append("ticks")
            return []

    class Held:
        khive = SimpleNamespace(actor="lambda:x")

        def _claim(self):
            return None

        def _release(self, pid):
            seen["released"] = True

        async def rerun(self, ids):
            seen["order"].append("rerun")
            seen["ids"] = ids
            return SimpleNamespace(record=[], request=SimpleNamespace(history=()))

    (tmp_path / "chores.toml").write_text('model = "deepseek/deepseek-v4.1-flash"\n' + chair)
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: (Ticks(), Held()))
    a = argparse.Namespace(
        dir=str(tmp_path),
        stats=False,
        model=None,
        check=None,
        ticks=False,
        arm=False,
        once=False,
        forget=None,
        wait=0,
    )
    asyncio.run(agent_main(argparse.Namespace(**{**vars(a), "rerun": ["12345678", "0ff1ce00"]})))
    assert seen == {"ids": ["12345678", "0ff1ce00"], "released": True, "order": order}


def test_the_forget_flag_lets_go_of_the_run_and_makes_no_tick(tmp_path, monkeypatch, capsys):
    """--forget RUN: the owner's act after a refused wake, through Agent.forget under the lock; no tick is
    made and nothing is woken."""
    from types import SimpleNamespace

    seen: dict = {"order": []}

    class Ticks:
        async def ensure_ticks(self):
            seen["order"].append("ticks")
            return []

    class Held:
        khive = SimpleNamespace(actor="lambda:x")

        def _claim(self):
            seen["order"].append("claim")

        def _release(self, pid):
            seen["released"] = True

        async def forget(self, run):
            seen["order"].append("forget")
            seen["run"] = run

    (tmp_path / "chores.toml").write_text('model = "deepseek/deepseek-v4.1-flash"\n')
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: (Ticks(), Held()))
    base = dict(dir=str(tmp_path), stats=False, model=None, check=None, ticks=False, arm=False, once=False)
    asyncio.run(agent_main(argparse.Namespace(**base, rerun=None, forget="r-gone", wait=0)))
    assert seen == {"order": ["claim", "forget"], "run": "r-gone", "released": True}
    assert "forgot r-gone: the next wake starts a new chain" in capsys.readouterr().out


@pytest.mark.parametrize(
    "beside",
    [["--once"], ["--rerun", "12345678"], ["--check", "inbox"], ["--ticks"], ["--arm"], ["--stats"]],
    ids=lambda b: b[0],
)
def test_forget_beside_another_action_is_refused_before_anything_is_claimed(
    tmp_path, monkeypatch, capsys, beside
):
    """Each action exits after itself, so another one given beside --forget would run in its place: the
    parser refuses the pair, and no directory is built or claimed."""
    built: list = []
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: built.append(directory))
    with pytest.raises(SystemExit) as e:
        main(["agent", "--dir", str(tmp_path), "--forget", "r-gone", *beside])
    assert e.value.code == 2
    assert f"argument --forget: not allowed with {beside[0]}" in capsys.readouterr().err
    assert built == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("beside", [[], ["--ticks"]], ids=["alone", "--ticks"])
@pytest.mark.parametrize("value", ["", "  "], ids=["empty", "blank"])
def test_an_empty_forget_is_refused_by_the_parser_not_read_as_absent(
    tmp_path, monkeypatch, capsys, value, beside
):
    """An empty run is not a run: read as falsy it would let another action through beside it, or fall
    through alone to serving. The parser refuses it, and no directory is built or claimed."""
    built: list = []
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: built.append(directory))
    with pytest.raises(SystemExit) as e:
        main(["agent", "--dir", str(tmp_path), "--forget", value, *beside])
    assert e.value.code == 2
    assert "argument --forget: expected a run id" in capsys.readouterr().err
    assert built == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("flag", ["once", "rerun"])
def test_a_failed_agenda_read_raises_before_the_wake_once_or_rerun_asked_for(tmp_path, monkeypatch, flag):
    """--once and --rerun make the missing ticks first, as a start does: an agenda read that fails
    raises there, before any wake, and the identity is released."""
    from types import SimpleNamespace

    seen: dict = {"order": []}

    class Ticks:
        async def ensure_ticks(self):
            seen["order"].append("ticks")
            raise RuntimeError("agenda: boom")

    class Held:
        khive = SimpleNamespace(actor="lambda:x")

        def _claim(self):
            return None

        def _release(self, pid):
            seen["released"] = True

        async def rerun(self, ids):
            seen["order"].append("rerun")

        async def wake(self, wait_ms):
            seen["order"].append("wake")

    (tmp_path / "chores.toml").write_text('model = "deepseek/deepseek-v4.1-flash"\n')
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: (Ticks(), Held()))
    a = argparse.Namespace(
        dir=str(tmp_path),
        stats=False,
        model=None,
        check=None,
        ticks=False,
        arm=False,
        once=False,
        forget=None,
        wait=0,
    )
    asked = {"once": True} if flag == "once" else {"rerun": ["12345678"]}
    with pytest.raises(RuntimeError, match="agenda: boom"):
        asyncio.run(agent_main(argparse.Namespace(**{**vars(a), "rerun": [], **asked})))
    assert seen == {"order": ["ticks"], "released": True}


def test_once_refuses_served_identity(tmp_path, kkernel_stub, monkeypatch):
    """While a live process holds the identity, --check, --ticks, --arm, --once and a normal start each
    refuse before any act: no kkernel call, no landing, nothing written. --stats reads and stays
    available."""
    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:agent", d))
    (d / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n\n[chores.gtd-inbox]\n'
        'every = "every:1h"\n'
    )
    ops: list[str] = []

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
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
        forget=None,
        wait=0,
    )
    branches = {
        "check": {"check": "gtd-inbox"},
        "ticks": {"ticks": True},
        "arm": {"arm": True},
        "once": {"once": True},
        "rerun": {"rerun": ["a1b2c3d4"]},
        "forget": {"forget": "r-gone"},
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


def test_a_once_wake_reads_the_pending_ticks_so_a_digest_inside_the_first_grace_is_not_missing(
    tmp_path, kkernel_stub, monkeypatch
):
    """--once makes the missing ticks before its wake, as a start does, so a digest wake sees the first due
    of a tick already pending: a chore with no run yet is inside its first grace, not missing."""
    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:agent", d))
    (d / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\nbatch = 1\n\n'
        '[chores.gtd-inbox]\nevery = "every:1h"\n'
    )

    def at(t: float) -> str:
        return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    now = time.time()
    tick = {  # made a moment ago, first firing a minute out
        "id": "ev1",
        "content": "tick: gtd-inbox",
        "created_at": at(now),
        "properties": {
            "status": "pending",
            "created_by_actor": "lambda:x:agent",
            "trigger_at": at(now + 60),
            "repeat": "every:1h",
        },
    }
    ops: list[str] = []
    boxes = [[mail("lambda:x:agent", "tick: digest")]]

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
        ops.append(op)
        if op.startswith("schedule.agenda("):
            return [{"events": [tick]}]
        if op.startswith("comm.inbox("):
            return [{"messages": boxes.pop(0) if boxes else []}]
        if op.startswith("comm.send("):
            return [{"id": "m1", "thread_id": THREAD}]
        return [{"messages": [], "events": [], "items": []}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    monkeypatch.setattr(
        "lion_cli.agent.backend_for", lambda *a, **k: Scripted("<lact d1>digest()</lact>", DONE)
    )
    base = dict(
        dir=str(d),
        stats=False,
        model=None,
        check=None,
        ticks=False,
        arm=False,
        rerun=None,
        forget=None,
        wait=0,
    )
    asyncio.run(agent_main(argparse.Namespace(**base, once=True)))
    (sent,) = [op for op in ops if op.startswith("comm.send(")]
    assert "lambda:x gtd-inbox no run yet, first due by " in sent and "MISSING: " not in sent
    order = [op.split("(")[0] for op in ops]
    assert order.index("schedule.agenda") < order.index("comm.inbox")  # the ticks first, as a start has it


def test_a_once_wake_of_a_chair_makes_no_tick(tmp_path, kkernel_stub, monkeypatch):
    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    (d / "chores.toml").write_text(
        'actor = "lambda:x:v1"\nowner = "lambda:x"\nsteward = "lambda:owner"\nbatch = 1\n\n[chair]\n'
    )
    ops: list[str] = []
    boxes = [[mail("lambda:x", "hello")]]

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
        ops.append(op)
        if op.startswith("comm.inbox("):
            return [{"messages": boxes.pop(0) if boxes else []}]
        return [{"messages": [], "events": [], "items": []}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    monkeypatch.setattr("lion_cli.agent.backend_for", lambda *a, **k: Scripted(DONE))
    base = dict(
        dir=str(d),
        stats=False,
        model=None,
        check=None,
        ticks=False,
        arm=False,
        rerun=None,
        forget=None,
        wait=0,
    )
    asyncio.run(agent_main(argparse.Namespace(**base, once=True)))
    assert any(op.startswith("comm.inbox(") for op in ops)  # the wake ran
    assert not [op for op in ops if op.startswith("schedule.")]  # and a chair's box holds no tick


def test_a_once_wake_prints_its_own_entries_and_not_the_run_it_continues(
    tmp_path, kkernel_stub, monkeypatch, capsys
):
    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    (d / "chores.toml").write_text(
        'actor = "lambda:x:v1"\nowner = "lambda:x"\nsteward = "lambda:owner"\nbatch = 1\n\n[chair]\n'
    )
    boxes = [[mail("lambda:x", "FIRST-MAIL")], [mail("lambda:x", "SECOND-MAIL", mid="c3d4e5f6")]]
    lion = LionFake()  # one store across both processes

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
        if op.startswith("comm.inbox("):
            return [{"messages": boxes.pop(0) if boxes else []}]
        return [{"messages": [], "events": [], "items": []}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    monkeypatch.setattr("lion_cli.agent.backend_for", lambda *a, **k: Scripted(DONE))
    base = dict(
        dir=str(d),
        stats=False,
        model=None,
        check=None,
        ticks=False,
        arm=False,
        rerun=None,
        forget=None,
        wait=0,
    )
    printed = []
    for _ in range(2):
        asyncio.run(agent_main(argparse.Namespace(**base, once=True)))
        printed.append(capsys.readouterr().out)
    assert "FIRST-MAIL" in printed[0] and "SECOND-MAIL" in printed[1]
    assert "FIRST-MAIL" not in printed[1]  # the second run continues the first and prints only its own


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
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:agent", d))
    (d / "chores.toml").write_text('actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n')
    ops: list[str] = []

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
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
    base = dict(
        dir=str(d),
        stats=False,
        model=None,
        ticks=False,
        arm=False,
        once=False,
        rerun=None,
        forget=None,
        wait=0,
    )
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
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:agent", d))
    (d / "chores.toml").write_text(
        'actor = "lambda:x:agent"\nowner = "lambda:x"\nsteward = "lambda:owner"\n\n[chores.gtd-inbox]\n'
        'every = "every:1h"\n'
    )
    ops: list[str] = []

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
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
        forget=None,
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
    agent = build(khive, state=fresh_store().notes("agent"))
    path = agent._claim()
    asyncio.run(agent.serve(until=lambda: True))
    assert agent.errors == [] and not path.exists() and agent._lock_fd is None


def test_serve_refuses_a_directory_that_routes_the_lion_tables_while_nothing_folds_the_chain(
    tmp_path, monkeypatch
):
    """ADR-0026 C6 has each wake continue the last run and replay its chain whole; with no fold point, a
    served agent over routed lion tables would replay a history that only grows, so it is refused before
    it acts."""
    from tests.lionfake import actor_dir, patch_khive

    patch_khive(monkeypatch)
    d = actor_dir(tmp_path / "routed", "lambda:a:echo")
    store = Store(Khive(d, "lambda:a:echo"))
    assert store.database == d / ".khive" / "lion.db"  # routed: the lion tables have their own file
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    backend = Scripted(REPLY, DONE, khive=khive)
    agent = Agent(Actor("echo", privileges={"comm.send"}, store=store), khive, backend, profile_for)
    with pytest.raises(AgentError, match="routes the lion tables, and no fold point yet bounds the chain"):
        asyncio.run(agent.serve(until=lambda: True))
    # refused before the claim: no lock taken, no mail read, no model called
    assert not (d / "agent.pid").exists() and khive.log == [] and backend.seen == []


def test_serve_runs_a_store_that_routes_nothing(tmp_path):
    """The control: the same serve over a double, which routes no tables, claims, serves and releases."""
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    agent = build(khive, REPLY, DONE)
    assert agent.unservable() is None and agent.actor.store.database is None
    lines: list[str] = []
    asyncio.run(agent.serve(until=lambda: len(lines) >= 1, log=lines.append))
    assert khive.sent == [SENT] and not (tmp_path / "agent.pid").exists()


@pytest.mark.parametrize(("asked", "refused"), [({}, True), ({"once": True}, False)], ids=["serve", "once"])
def test_the_serve_form_is_refused_before_the_start_makes_its_ticks(tmp_path, monkeypatch, asked, refused):
    """`lion agent --dir D` asks `unservable` after the claim and before any tick; a one-shot wake does not
    serve, so it is not asked. The claim is the pid file's real lock: a refused serve lets it go, so a
    `--once` on the same directory then claims it and wakes."""
    from types import SimpleNamespace

    seen: dict = {"order": []}

    class Ticks:
        async def ensure_ticks(self):
            seen["order"].append("ticks")
            return []

    class Routed:
        _claim, _release = Agent._claim, Agent._release  # the lock an agent takes, on its directory

        def __init__(self):
            self.khive, self._lock_fd = SimpleNamespace(actor="lambda:x", cwd=tmp_path), None

        def unservable(self):
            seen["order"].append("asked")
            return "lambda:x is not served: its directory routes the lion tables"

        async def wake(self, wait_ms=None):
            seen["order"].append("wake")
            assert (tmp_path / "agent.pid").read_text() == str(os.getpid())  # woken under the lock

    (tmp_path / "chores.toml").write_text('model = "deepseek/deepseek-v4.1-flash"\n')
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: (Ticks(), Routed()))
    base = dict(dir=str(tmp_path), stats=False, model=None, check=None, ticks=False, arm=False, once=False)
    a = argparse.Namespace(**{**base, "rerun": None, "forget": None, "wait": 0, **asked})
    if refused:
        with pytest.raises(AgentError, match="routes the lion tables"):
            asyncio.run(agent_main(a))
        assert seen["order"] == ["asked"] and not (tmp_path / "agent.pid").exists()
        # the refusal released the lock it claimed: a --once on the same directory claims it and wakes
        asyncio.run(agent_main(argparse.Namespace(**{**vars(a), "once": True})))
        assert seen["order"] == ["asked", "ticks", "wake"]
    else:
        asyncio.run(agent_main(a))
        assert seen["order"] == ["ticks", "wake"]
    assert not (tmp_path / "agent.pid").exists()


def test_the_forget_flag_runs_in_a_directory_whose_serving_form_is_refused(tmp_path, monkeypatch, capsys):
    """A refused wake is likeliest where the store routes the lion tables, the directory whose serving form
    is refused: `--forget` returns before that refusal is asked, so the owner lets go of the run there."""
    from tests.lionfake import actor_dir, patch_khive

    patch_khive(monkeypatch)
    d = actor_dir(tmp_path / "routed", "lambda:a:echo")
    store = Store(Khive(d, "lambda:a:echo"))
    assert store.database == d / ".khive" / "lion.db"  # routed: the lion tables have their own file
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=d)
    backend = Scripted(REPLY, DONE, khive=khive)
    agent = Agent(Actor("echo", privileges={"comm.send"}, store=store), khive, backend, profile_for)
    agent.state.put("cursor", {"wake": 3, "run": "r-gone"}, run="-", seq=0)
    ticks: list = []

    class Ticks:
        async def ensure_ticks(self):
            ticks.append("ticks")
            return []

    (d / "chores.toml").write_text('model = "deepseek/deepseek-v4.1-flash"\n')
    monkeypatch.setattr("lion_cli.agent.build", lambda directory, backend: (Ticks(), agent))
    base = dict(dir=str(d), stats=False, model=None, check=None, ticks=False, arm=False, once=False)
    serve = argparse.Namespace(**base, rerun=None, forget=None, wait=0)
    with pytest.raises(AgentError, match="routes the lion tables"):
        asyncio.run(agent_main(serve))
    asyncio.run(agent_main(argparse.Namespace(**{**vars(serve), "forget": "r-gone"})))
    assert "forgot r-gone: the next wake starts a new chain" in capsys.readouterr().out
    (gone,) = agent.state.get("forgotten").value
    assert gone["run"] == "r-gone" and agent.state.get("cursor").value["run"] is None
    with pytest.raises(AgentError, match="routes the lion tables"):  # the forget made nothing servable
        asyncio.run(agent_main(serve))
    # no tick, no mail read, no model call, and each run let go of the lock it claimed
    assert ticks == [] and khive.log == [] and backend.seen == [] and not (d / "agent.pid").exists()


# the actor as the resident: `[chair]` in chores.toml, the claude CLI as the backend


# the chair as the resident: `[chair]` in chores.toml, the claude CLI as the backend


def test_backend_for_claude_code_is_the_cli_chat_shape_on_the_named_model(monkeypatch):
    from lion_cli.agent import backend_for

    from hub.backend import ClaudeCodeSession

    monkeypatch.setattr("hub.backend.claude_code.shutil.which", lambda name: "/opt/bin/claude")
    b = backend_for("claude_code/claude-opus-5-5")
    assert isinstance(b, ClaudeCodeSession) and (b.model, b.cli) == ("claude-opus-5-5", "/opt/bin/claude")
    assert isinstance(backend_for("claude_code"), ClaudeCodeSession)
    assert backend_for("claude_code").model is None
    p = backend_for(
        "claude_code/claude-opus-5-5",
        {"timeout": 1800, "effort": "medium", "cli": "/opt/bin/claude-named"},
        cwd=Path("/a"),
    )
    # no tools of its own: one turn per call, no MCP, no gate to hand an actor
    assert (p.tools, p.allowed, p.max_turns, p.add_dirs, p.mcp_config, p.requires, p.actor) == (
        (),
        (),
        1,
        (),
        None,
        {},
        None,
    )
    assert p.cwd == Path("/a") and p.cli == "/opt/bin/claude-named"
    assert p.timeout == 1800.0 and backend_for("claude_code").timeout == 300.0
    assert p.effort == "medium" and backend_for("claude_code").effort is None
    assert type(backend_for("deepseek/deepseek-v4.1-flash")).__name__ == "OpenRouter"


@pytest.mark.parametrize("model", ["claude_code/claude-opus-5-5", "deepseek/deepseek-v4.1-flash"])
@pytest.mark.parametrize(
    "key", ["tools", "allowed", "max_turns", "add_dirs", "mcp_config", "requires", "tool"]
)
def test_a_claude_code_key_that_is_not_timeout_effort_or_cli_is_refused_by_name_whatever_the_model(
    monkeypatch, model, key
):
    """The model gets no tools of its own, in `lion agent` as in the chat: a key that would hand it one,
    or a misspelling, refuses by name; a `--model` that is not claude_code does not make it ignored."""
    from lion_cli.agent import backend_for

    monkeypatch.setattr("hub.backend.claude_code.shutil.which", lambda name: "/opt/bin/claude")
    says = (
        rf"\[claude_code\] {key}: a claude_code model has no tools of its own \(ADR-0016/D3\); the keys are "
    )
    with pytest.raises(ValueError, match=says + "timeout, effort, cli$"):
        backend_for(model, {"timeout": 600, key: ["Bash"]})
    # `claude_code = "..."` is not the table: named, not a conversion error
    with pytest.raises(
        ValueError, match=r"\[claude_code\] is a table of timeout, effort and cli, not 'Bash'"
    ):
        backend_for(model, "Bash")


@pytest.mark.parametrize(
    "given", [{"tools": ("Bash",)}, {"allowed": ("mcp__khive",)}, {"mcp_config": "/m.json"}, {}]
)
def test_build_refuses_a_claude_code_session_handed_in_with_tools_of_its_own(tmp_path, kkernel_stub, given):
    """`build` takes a backend from anywhere: one that could run tools of its own would act outside every
    gate, so it is refused; the tool-less session `backend_for` makes builds, and keeps no actor."""
    from lion_cli.agent import build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    (d / "chores.toml").write_text('actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n')
    backend = ClaudeCodeSession(cli="/bin/claude", **given)
    if given:
        with pytest.raises(
            ValueError, match=r"a claude_code session with tools of its own: the model gets none"
        ):
            build(d, backend)
    else:
        _, agent = build(d, backend)
        assert agent.backend is backend and backend.actor is None
        # the config's own `[claude_code] tools` refuses here too, whoever built the session
        (d / "chores.toml").write_text(
            'actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n\n[claude_code]\ntools = ["Bash"]\n'
        )
        with pytest.raises(
            ValueError, match=r"\[claude_code\] tools: a claude_code model has no tools of its own"
        ):
            build(d, backend)


def test_backend_for_codex_is_the_session_on_the_named_model_on_a_measured_release_only(
    monkeypatch, tmp_path
):
    from lion_cli.agent import backend_for

    from hub.backend import CodexSession

    monkeypatch.setattr("hub.backend.codex.shutil.which", lambda name: "/opt/bin/codex")
    b = backend_for("codex/gpt-6-luna")
    assert isinstance(b, CodexSession) and (b.model, b.cli) == ("gpt-6-luna", "/opt/bin/codex")
    assert (b.timeout, b.effort, b.home) == (300.0, None, None)
    assert isinstance(backend_for("codex"), CodexSession) and backend_for("codex").model is None
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    options = {"timeout": 1800, "effort": "high", "cli": "/opt/bin/codex-named", "home": str(home)}
    p = backend_for("codex/gpt-6-sol", codex=options, cwd=Path("/a"))
    assert (p.timeout, p.effort, p.cli, p.home) == (1800.0, "high", "/opt/bin/codex-named", str(home))
    # no name and no key reaches a CLI release the tools-off claim was not measured on
    assert not b.unmeasured and not p.unmeasured
    assert type(backend_for("codexx/gpt-6-luna")).__name__ == "OpenRouter"  # a name that only starts so
    # the two tables are apart: `[claude_code]` configures nothing of a codex model
    assert backend_for("codex/gpt-6-luna", {"timeout": 1800}).timeout == 300.0


@pytest.mark.parametrize("model", ["codex/gpt-6-luna", "claude_code", "deepseek/deepseek-v4.1-flash"])
@pytest.mark.parametrize("key", ["unmeasured", "tools", "sandbox", "retries", "mcp_config"])
def test_a_codex_key_that_is_not_timeout_effort_cli_or_home_is_refused_by_name_whatever_the_model(
    monkeypatch, model, key
):
    """`unmeasured` is the one way back to a model that may act unseen: no config sets it, and a
    `--model` that is not codex does not make the table ignored."""
    from lion_cli.agent import backend_for

    monkeypatch.setattr("hub.backend.codex.shutil.which", lambda name: f"/opt/bin/{name}")
    says = rf"\[codex\] {key}: a codex model has no tools of its own \(ADR-0016/D3\); the keys are "
    with pytest.raises(ValueError, match=says + "timeout, effort, cli, home$"):
        backend_for(model, codex={"timeout": 600, key: True})
    with pytest.raises(ValueError, match=r"\[codex\] is a table of timeout, effort, cli and home, not 'x'"):
        backend_for(model, codex="x")


# values a `[claude_code]` or `[codex]` key does not take
_NOT_TAKEN = [
    ("timeout", True),  # an int to Python: one second, off a typo
    ("timeout", "fast"),
    ("timeout", 0),
    ("timeout", -5),
    ("timeout", float("inf")),
    ("timeout", float("nan")),
    ("effort", 3),
    ("effort", ""),
    ("cli", ""),
    ("cli", ["codex"]),
    ("home", ""),  # an empty home would be read as the directory the process stands in
    ("home", 7),
]


@pytest.mark.parametrize(("key", "value"), _NOT_TAKEN)
def test_a_claude_code_value_that_is_not_what_its_key_takes_is_refused_by_the_key(key, value):
    from lion_cli.agent import backend_for

    from hub.agent.agent import claude_code_options

    if key == "home":  # `[claude_code]` has no such key: refused as a key, before any value is read
        with pytest.raises(ValueError, match=r"^\[claude_code\] home: a claude_code model has no tools"):
            claude_code_options({key: value})
        return
    with pytest.raises(ValueError, match=rf"^\[claude_code\] {key} is a "):
        claude_code_options({key: value})
    with pytest.raises(ValueError, match=rf"^\[claude_code\] {key} is a "):
        backend_for("claude_code/sonnet", {key: value})
    good = {"timeout": 0.5, "effort": "low", "cli": "claude"}
    assert claude_code_options(good) == good and claude_code_options({"timeout": 1800}) == {"timeout": 1800}


@pytest.mark.parametrize(("key", "value"), _NOT_TAKEN)
def test_a_codex_value_that_is_not_what_its_key_takes_is_refused_by_the_key(monkeypatch, key, value):
    from lion_cli.agent import backend_for

    from hub.agent.agent import codex_options

    monkeypatch.setattr("hub.backend.codex.shutil.which", lambda name: f"/opt/bin/{name}")
    with pytest.raises(ValueError, match=rf"^\[codex\] {key} is a "):
        codex_options({key: value})
    with pytest.raises(ValueError, match=rf"^\[codex\] {key} is a "):
        backend_for("codex", codex={key: value})
    good = {"timeout": 0.5, "effort": "low", "cli": "codex", "home": "/h"}
    assert codex_options(good) == good and codex_options({"timeout": 1800}) == {"timeout": 1800}


def test_build_refuses_a_codex_backend_built_for_an_unmeasured_release(tmp_path, kkernel_stub):
    """`build` takes a backend from anywhere: one built to run a CLI release nobody measured may let
    the model act outside every gate, so it is refused, as is the config key that would ask for it."""
    from lion_cli.agent import build

    from hub.backend import CodexSession

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    (d / "chores.toml").write_text('actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n')
    with pytest.raises(ValueError, match="a codex backend built to run an unmeasured CLI release"):
        build(d, CodexSession(cli="/bin/codex", unmeasured=True))
    backend = CodexSession(cli="/bin/codex")
    _, agent = build(d, backend)
    assert agent.backend is backend
    (d / "chores.toml").write_text(
        'actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n\n[codex]\nunmeasured = true\n'
    )
    with pytest.raises(ValueError, match=r"\[codex\] unmeasured: a codex model has no tools of its own"):
        build(d, backend)


def test_a_chair_wakes_on_the_owners_mail_reads_its_directory_and_answers_on_the_thread(
    tmp_path, kkernel_stub, monkeypatch
):
    """`[chair]`: the wake runs under the chair profile (files, khive reads, notes, send; no chore
    handler), the guidance names the actor, the owner and the role text, and the answer goes out as
    one comm.send on the mail's thread. Mail from outside the trusted set is still dropped at the gate."""
    from lion_cli.agent import build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
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

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
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
    named = ", ".join(sorted(ALLOWED_VERBS))  # the verbs by name, not a count and a summary
    assert f"(these verbs and no other: {named}; memory.remember with an explicit salience" in system
    # the runtime's tools are all it has: nothing offers Bash or a tool of its own, and git is an ask
    assert "you have no tools of your own" in system and "what needs git, gh" in system
    # the queue is read-only: the chair says a task's condition and the owner moves the row
    assert "You cannot take a task to active, complete it or move its `at`" in system
    assert "the owner moves the row" in system
    # a task says why it is in the batch, and the queue line is there only while it holds tasks
    assert "a task in a batch says why it is there" in system and "While the queue holds tasks" in system
    # OUT{} ends the conversation: said with what the next one opens on, and the hand-run wake it ends
    assert "the next mail starts a new one without its record" in system
    assert "a wake's input asks you to finish with it" in system
    assert "Bash" not in system and "Claude Code" not in system and "khive MCP" not in system
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
        forget=None,
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
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    (d / "chores.toml").write_text('actor = "lambda:x:v1"\nowner = "lambda:x"\nchair = true\n')
    with pytest.raises(ValueError, match=r"\[chair\] is a table"):
        build(d, Scripted())


@pytest.mark.parametrize("gtd", ["", '\n[gtd]\nwork = "idle"\n'], ids=["claude_code-tools", "gtd-idle"])
def test_a_chair_config_giving_the_model_tools_or_idle_work_fails_to_load_and_without_them_loads(
    tmp_path, kkernel_stub, monkeypatch, gtd
):
    """Both refuse by name before the identity is claimed: `[claude_code] tools` (the model has no tools
    of its own) and `[gtd] work = "idle"` (the queue is read-only, so an idle pass would take the same
    task forever). The same directory without them loads and serves its `--ticks`."""
    which = claude_code.shutil.which  # the stub kkernel is found as before; only claude is named here
    monkeypatch.setattr(
        claude_code.shutil,
        "which",
        lambda name, *a, **k: "/opt/bin/claude" if name == "claude" else which(name),
    )
    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    base = 'actor = "lambda:x:v1"\nowner = "lambda:x"\nmodel = "claude_code/claude-opus-5-5"\n\n[chair]\n'
    args = argparse.Namespace(
        dir=str(d),
        stats=False,
        model=None,
        check=None,
        ticks=True,
        arm=False,
        once=False,
        rerun=None,
        forget=None,
        wait=0,
    )
    if gtd:
        (d / "chores.toml").write_text(base + gtd)
        says = r'\[gtd\] work "idle": the queue is read-only to the agent'
    else:
        (d / "chores.toml").write_text(base + '\n[claude_code]\ntimeout = 600\ntools = ["Bash", "Read"]\n')
        says = r"\[claude_code\] tools: a claude_code model has no tools of its own"
    with pytest.raises(ValueError, match=says):
        asyncio.run(agent_main(args))
    assert not (d / "agent.pid").exists()  # refused before the claim
    (d / "chores.toml").write_text(base + '\n[claude_code]\ntimeout = 600\n\n[gtd]\ndue = "now"\n')
    asyncio.run(agent_main(args))


def test_a_chair_with_a_due_task_names_it_in_the_posture_and_its_gtd_write_is_refused_on_the_record(
    tmp_path, kkernel_stub, monkeypatch
):
    """The chair's queue is read-only: the due task reaches it as a notice, a gtd.transition through its
    khive is refused at the boundary and lands on the record as a failed result, nothing moves in the
    store, and the chair says the task's condition in its posture for the owner to move the row."""
    from lion_cli.agent import build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    (d / "chores.toml").write_text(
        'actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n\n[gtd]\ndue = "now"\n'
    )
    ops: list[str] = []

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
        ops.append(op)
        if op.startswith("gtd.next("):
            return [[task("t1", "cost report", at=1.0, description="send the numbers")]]
        return [{"messages": [], "events": [], "items": []}]

    monkeypatch.setattr(Khive, "exec", fake_exec)
    turns = [
        '<lact g1>khive_ops(ops=\'gtd.transition(id="t1", status="active")\')</lact>',
        'OUT{posture: {done: "t1 cost report is due and not started", awaiting: "the owner to move t1"}}',
    ]
    _, agent = build(d, Scripted(*turns))
    run = asyncio.run(agent.wake(wait_ms=0))
    first = next(e for e in run.record if e.kind is Kind.INPUT).content
    assert "[task t1 p1 next at 1.0: due now] cost report" in first
    refused = [e.content for e in run.record if e.kind is Kind.RESULT and not e.content.ok]
    assert len(refused) == 1 and "agent boundary" in str(refused[0].error)
    assert "gtd.transition" in str(refused[0].error)
    assert not [op for op in ops if "gtd.transition" in op or "gtd.complete" in op]  # nothing moved
    posture = agent.actor.notes_for(run.profile).get("posture").value
    assert "t1 cost report is due" in posture and "the owner to move t1" in posture


def test_arm_prints_the_auth_env_the_claude_child_inherits_and_fails_on_an_api_key(
    tmp_path, kkernel_stub, monkeypatch
):
    """A resident on the subscription must never bill the API key, so the arm landing says which of the
    two the CLI child would see, and an ANTHROPIC_API_KEY fails the arm."""
    from lion_cli.agent import arm, build

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x:v1", d))
    (d / "chores.toml").write_text('actor = "lambda:x:v1"\nowner = "lambda:x"\n\n[chair]\n')

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
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
    # the queue is read-only: both task writes are refused by the agent's own boundary, before the store
    for verb in ("gtd.complete", "gtd.transition"):
        assert any(
            line.startswith(f"boundary: refused {verb}(")
            and f"['{verb}'] are not verbs the agent may call" in line
            for line in lines
        )
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
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x", d))
    (d / "chores.toml").write_text(
        'actor = "lambda:x"\nowner = "lambda:owner"\nbatch = 1\n\n[chair]\nadmit = "all"\n'
    )
    ops: list[str] = []
    boxes = [[mail("someone:else", "hello from outside")]]

    lion = LionFake()

    async def fake_exec(self, op):
        if op.lstrip("[").startswith("lion."):
            return lion.answer(op)
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
        "turns": 1,
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


class FailsOnce(KhiveNoteStore):
    """A store whose write of `key` raises once, the first time it follows a write of `after`, while
    `armed`: the disk that fills between two notes of one wake."""

    def __init__(self, key: str, after: str):
        super().__init__(LionKhive(), "agent")
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
    khive, state = FakeKhive(), fresh_store().notes("agent")
    waiting = [{**mail("lambda:a", "a verdict", mid="d1d1d1d1"), "deferred_ts": 0.0}]
    waiting.append({**mail("lambda:a", "another", mid="f3f3f3f3"), "deferred_ts": 0.0})
    state.put("deferred", {"mail": waiting}, run="-", seq=0)  # a take whose `deferred` write never landed
    state.put("taking", {"mail": ["d1d1d1d1"]}, run="-", seq=0)
    agent = build(khive, state=state, wake_rules=rules, owner="lambda:a")
    asyncio.run(agent.serve(until=lambda: True))
    assert len(khive.sent) == 1 and "d1d1d1d1" in khive.sent[0]["content"]
    assert [m["id"] for m in state.get("deferred").value["mail"]] == ["f3f3f3f3"]


class StopsAt(KhiveNoteStore):
    """A store whose `n`th write while `armed` raises: the process that stops between two notes."""

    def __init__(self, n: int):
        super().__init__(LionKhive(), "agent")
        self.n, self.armed, self.writes, self.stopped = n, False, 0, False

    def put(self, key, value, *, run, seq):
        if self.armed:
            self.writes += 1
            if self.writes == self.n:
                self.armed, self.stopped = False, True
                raise OSError("stopped")
        return super().put(key, value, run=run, seq=seq)


@pytest.mark.parametrize("left_under", ["taking", "deferred"])
def test_a_rerun_stopped_at_any_write_leaves_its_mail_told_at_the_next_start_or_still_waiting(left_under):
    """Wherever the process stops, at each of the rerun's note writes in turn, the mail it takes back is
    either told to the owner at the next start or still waiting under `deferred` for a later wake: never
    both, never neither."""
    rules = Wake(default="batch", batch_after=3600, batch_size=5)
    stops = []
    for n in itertools.count(1):
        khive, state = FakeKhive(mail("lambda:a", "kept for later", mid="d1d1d1d1")), StopsAt(n)
        agent = build(khive, REPLY, DONE, state=state, wake_rules=rules, owner="lambda:owner")
        assert asyncio.run(agent.wake(wait_ms=0)) is None  # read, waiting under `deferred`
        other = mail("lambda:a", "a dead wake's", mid="e5f6a7b8")
        khive.taken.append(other)
        khive.read.append(other["full_id"])
        if left_under == "taking":  # a wake that died after taking both; `deferred` no longer names it
            state.put("deferred", {"mail": []}, run="-", seq=0)
            state.put("taking", {"mail": ["d1d1d1d1", "e5f6a7b8"]}, run="-", seq=0)
        else:
            state.put("taking", {"mail": ["e5f6a7b8"]}, run="-", seq=0)
        state.armed = True
        try:
            asyncio.run(agent.rerun(["d1d1d1d1"]))
        except OSError as e:
            assert str(e) == "stopped" and state.writes == n
        else:
            if not state.stopped:  # the rerun wrote fewer than n notes: every stop has been tried
                break
            # the send's hop count failed: that command's error, which the run tells the model and outlives
            assert agent.state.get("cursor").value["outcome"] == "Success"
            continue
        stops.append(n)
        restarted = build(khive, state=state, wake_rules=rules, owner="lambda:owner")
        asyncio.run(restarted.serve(until=lambda: True))
        told = " ".join(s["content"] for s in khive.sent if s["to"] == "lambda:owner")
        waiting = [m["id"] for m in (state.get("deferred").value or {}).get("mail") or []]
        assert ("d1d1d1d1" in told) != ("d1d1d1d1" in waiting), (n, told, waiting)
        assert "e5f6a7b8" in told, (n, told)
    assert len(stops) >= 8, stops  # the stops before, inside and after recovery, at entry and at the end


def test_a_rerun_of_waiting_mail_takes_it_out_of_deferred_so_its_due_wake_does_not_run_it_again():
    rules = Wake(default="batch", batch_after=3600, batch_size=5)
    khive = FakeKhive(mail("lambda:a", "kept for later", mid="d1d1d1d1"))
    agent = build(khive, REPLY, DONE, REPLY2, DONE, wake_rules=rules, owner="lambda:owner")
    assert asyncio.run(agent.wake(wait_ms=0)) is None
    due = agent.state.get("deferred").value["mail"][0]["deferred_ts"] + 3600
    run = asyncio.run(agent.rerun(["d1d1d1d1"]))
    assert isinstance(run.outcome, Success) and agent.state.get("deferred").value == {"mail": []}
    agent.clock = lambda: due
    assert asyncio.run(agent.wake(wait_ms=0)) is None and len(agent.backend.calls) == 2


def test_a_batch_under_taking_and_a_running_cursor_both_is_told_once():
    """A wake that died entering its run left its batch under `taking` and on the cursor at once."""
    khive, state = FakeKhive(), fresh_store().notes("agent")
    state.put("taking", {"mail": ["c3d4e5f6"]}, run="-", seq=0)
    running = {"wake": 4, "outcome": "running", "mail": ["c3d4e5f6"], "notices": []}
    state.put("cursor", running, run="-", seq=0)
    asyncio.run(build(khive, state=state, owner="lambda:owner")._recover())
    (told,) = [s["content"] for s in khive.sent]
    assert told.count("c3d4e5f6") == 1 and "wake 4 died mid-run" in told
    assert state.get("cursor").value["outcome"] == "lost" and state.get("unreported").value == []


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
    # a key that would give the model a tool is said at the reload, not reported as reloaded
    cfg.write_text('[budget]\ncost_per_day = 3.0\n\n[claude_code]\ntools = ["Bash"]\n')
    os.utime(cfg, (time.time() + 15, time.time() + 15))
    agent._reload()
    assert agent.budget.cost_per_day == 9.0
    said = "reload failed, the values in force stay: ValueError: [claude_code] tools: a claude_code model has"
    assert lines[-1].startswith(said + " no tools of its own")
    cfg.write_text("[budget]\ncost_per_day = 3.0\n\n[codex]\nunmeasured = true\n")
    os.utime(cfg, (time.time() + 20, time.time() + 20))
    agent._reload()
    assert agent.budget.cost_per_day == 9.0
    said = "reload failed, the values in force stay: ValueError: [codex] unmeasured: a codex model has"
    assert lines[-1].startswith(said + " no tools of its own")


def test_build_reads_the_wake_rules_and_names_the_config_for_reload(tmp_path, kkernel_stub):
    from lion_cli.agent import build as build_dir

    d = tmp_path / "agent"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(routed_config("lambda:x", d))
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
        if ops.startswith("comm.read("):
            return await super().exec(ops)
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

    g = Gtd.load({"poll": "2m", "due": "now", "idle_after": "10m", "notice": 3})
    assert (g.poll, g.due, g.work, g.idle_after, g.notice, g.limit) == (120.0, "now", "off", 600.0, 3, 20)
    assert g.describe() == "poll 120s, due now, work off"
    assert Gtd(work="idle", idle_after=600).describe() == "poll 60s, due now, work idle after 600s quiet"
    assert Gtd.load(None) is None and Gtd.load({}) is None
    with pytest.raises(ValueError, match="due"):
        Gtd.load({"due": "later"})
    with pytest.raises(ValueError, match=r"work 'always': off"):
        Gtd.load({"work": "always"})
    # the agent reads its queue and cannot move a task: an idle pass would take the same task forever
    with pytest.raises(ValueError, match=r'work "idle": the queue is read-only to the agent'):
        Gtd.load({"work": "idle", "idle_after": "10m"})
    assert (
        _at({}) is None and _at({"at": ""}) is None and _at({"at": "soon"}) is None and _at({"at": 5}) == 5.0
    )
    local = time.mktime(time.strptime("2026-09-24 20:14:00", "%Y-%m-%d %H:%M:%S"))
    assert _at({"at": "2026-09-24T20:14:00"}) == local  # naive ISO is local time


def test_a_due_task_wakes_the_chair_once_per_at_as_a_notice_not_mail(tmp_path, store):
    # the runtime reads the queue and wakes on it; the owner moves the rows
    from hub.agent.agent import Gtd, _line

    now = [1_000_000.0]
    due = task("t1", "cost report", at=now[0] - 1, description="send the numbers")
    khive = QueueKhive(queue=[due, task("t2", "later", at=now[0] + 3600)], cwd=tmp_path)
    actor = Actor("echo", privileges={"comm.send"}, store=store)
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
    khive.queue[0] = {**due, "at": now[0] - 1}  # the owner moved it: a new `at` is a new wake
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run is not None and agent.state.get("cursor").value["tasks"] == ["t1"] and len(khive.ops) == 2


def test_a_quiet_chair_works_the_top_of_its_queue_after_idle_after(tmp_path, store):
    from hub.agent.agent import Gtd

    now = [2_000_000.0]
    khive = QueueKhive(queue=[task("t9", "tidy the corpus", status="active", priority="p0")], cwd=tmp_path)
    backend = Scripted(DONE, DONE, khive=khive)
    gtd = Gtd(poll=1, work="idle", idle_after=600)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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
        Actor("echo2", privileges={"comm.send"}, store=store),
        khive,
        Scripted(khive=khive),
        lambda b: ACTING,
        gtd=off,
        clock=lambda: now[0] + 10_000,
    )
    assert asyncio.run(agent.wake(wait_ms=0)) is None  # off: the queue is a line in the notice, never a wake


def test_idle_work_reads_the_queue_when_due_is_off_and_due_tasks_still_do_not_wake(tmp_path, store):
    from hub.agent.agent import Gtd

    now = [2_000_000.0]
    top = task("t9", "tidy the corpus", status="active", priority="p0", at=now[0] - 1)
    khive = QueueKhive(queue=[top], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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


def test_a_wake_now_file_with_no_mail_waiting_runs_the_queue_as_it_is_now_or_is_consumed_saying_so(
    tmp_path, store
):
    from hub.agent.agent import Gtd

    now = [2_000_000.0]
    khive = QueueKhive(queue=[], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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


def test_the_pull_reads_the_queue_with_due_off_and_keeps_when_refused_mail_used_the_poll(tmp_path, store):
    from hub.agent.agent import Gtd

    now = [3_000_000.0]
    khive = QueueKhive(mail("lambda:a", "junk"), queue=[task("t4", "answer the desk")], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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


def test_a_pull_beside_a_due_task_is_one_wake_and_no_queue_config_is_no_task(tmp_path, store):
    from hub.agent.agent import Gtd

    now = [4_000_000.0]
    due = task("t5", "cost report", at=now[0] - 1)
    khive = QueueKhive(queue=[due], cwd=tmp_path)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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


def test_a_pull_during_the_continuous_chairs_wait_is_one_wake_on_that_path(tmp_path, store):
    """The deployed path: after its first wake a chair polls in `_wait`, not `wake()`. A pull there
    with a task queued adds one wake to the same run; a pull in the poll of a due task adds none."""
    from hub.agent.agent import Gtd

    now = [5_000_000.0]
    queued = [task("t3", "read the desk"), task("t6", "cost report", at=now[0] + 1000)]
    khive = QueueKhive(mail("lambda:a", "say hi back"), queue=queued, cwd=tmp_path)
    # the mail is answered on its thread; a task notice has no thread, so those bursts end on plain words
    backend = Scripted(REPLY, "on it", "on it", "on it", khive=khive)
    agent = Agent(
        Actor("echo", privileges={"comm.send"}, store=store),
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
    inputs = [e.content for run in agent.runs for e in own(run) if e.kind is Kind.INPUT]
    assert len(inputs) == 3
    assert "[task t3 p1 next: a person asked for a wake; the top of your queue] read the desk" in inputs[1]
    assert "due now] cost report" in inputs[2] and "a person asked" not in inputs[2]
    assert "wake-now: no mail waiting; the queue's top runs" in lines
    assert "wake-now: taking 0 waiting and 0 new" in lines
